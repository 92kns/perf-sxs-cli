#!/usr/bin/env python3
"""
Async side-by-side video comparison tool for Mozilla Try pushes.

Usage:
    perf-sxs <perfcompare-url> [options]
    perf-sxs <base-revision> <new-revision> [options]

Example with perfcompare URL (recommended):
    perf-sxs \
        "https://perf.compare/compare-results?baseRev=881d2bbf...&newRev=56290454..."

Example with revisions:
    perf-sxs \
        881d2bbfaf5390c3344757213fc7199839fe3e7f \
        56290454af1890c3344757213fc7199839fe3e7f \
        --platforms linux,windows

Example with Treeherder URLs:
    perf-sxs \
        "https://treeherder.mozilla.org/jobs?repo=try&revision=abc123" \
        "https://treeherder.mozilla.org/jobs?repo=try&revision=def456" \
        --output ./videos
"""

import argparse
import asyncio
import json
import shutil
import sys
import threading
import webbrowser
from pathlib import Path
from typing import NoReturn

from .api import (
    fetch_perfcompare_data_from_treeherder,
    find_task_group_ids,
    get_tasks_for_revision,
    perfcompare_test_version,
    resolve_lando_id,
)
from .api import new_session as create_session
from .download import download_video_artifacts
from .models import MAX_CONCURRENT_DOWNLOADS, TryPush, VideoTask
from .organize import organize_single_revision, organize_videos_for_comparison
from .selection import (
    TEST_VERSIONS,
    cross_browser_test_name,
    extract_test_info,
    filter_video_tasks,
    load_high_confidence_from_file,
)
from .urls import parse_lando_url, parse_perfcompare_url, parse_try_url


def log(msg: str, *, json_mode: bool) -> None:
    """Print a progress/diagnostic message.

    Routed to stderr in --json mode so stdout carries only the final
    machine-readable summary (or, on error, the JSON error object).
    """
    print(msg, file=sys.stderr if json_mode else sys.stdout)


def build_error_json(message: str) -> dict[str, object]:
    """Build the structured error object for a --json failure path.

    Matches the ``{"error": ..., "exit_code": 1}`` shape used by the
    perftest-brain CLI elsewhere in this project.
    """
    return {"error": message, "exit_code": 1}


def fail(message: str, *, json_mode: bool, hints: tuple[str, ...] = ()) -> NoReturn:
    """Exit 1 with a JSON error object (--json) or ``Error: ...`` prose plus hints."""
    if json_mode:
        print(json.dumps(build_error_json(message)))
    else:
        print(f"Error: {message}")
        for hint in hints:
            print(f"  {hint}")
    sys.exit(1)


def parse_cross_browser(value: str | None) -> tuple[str, str] | None:
    """Parse ``--cross-browser BASE_APP,NEW_APP`` (e.g. ``firefox,chrome``)."""
    if not value:
        return None
    apps = [a.strip().lower() for a in value.split(",")]
    if len(apps) != 2 or not all(apps) or apps[0] == apps[1]:
        raise ValueError(
            f"--cross-browser expects two different apps, e.g. firefox,chrome (got {value!r})"
        )
    return apps[0], apps[1]


def apply_test_filters(tasks: list[dict], test_filters: list[str] | None) -> list[dict]:
    """Keep tasks whose name contains any of the (case-insensitive) filters."""
    if not test_filters:
        return tasks
    return [
        t
        for t in tasks
        if any(f.lower() in t["task"]["metadata"]["name"].lower() for f in test_filters)
    ]


def build_video_tasks(
    base_bt: list[dict],
    new_bt: list[dict],
    base_push: TryPush,
    new_push: TryPush | None,
    cross_browser: tuple[str, str] | None = None,
) -> list[VideoTask]:
    """Turn filtered task records into VideoTasks for the downloader.

    In cross-browser mode the base side keeps only tasks for the first app and
    the new side only tasks for the second; each test name has its app token
    replaced with ``<a>-vs-<b>`` so the two sides land in the same
    ``<platform>/<test_name>`` directory and get paired. Tests that exist for
    only one of the two apps are dropped up front rather than downloaded.
    """
    pair_label = f"{cross_browser[0]}-vs-{cross_browser[1]}" if cross_browser else ""

    def _side(tasks: list[dict], label: str, push: TryPush, app: str | None) -> list[VideoTask]:
        out: list[VideoTask] = []
        seen: set[tuple[str, str]] = set()
        for task in tasks:
            task_name = task["task"]["metadata"]["name"]
            test_name, platform = extract_test_info(task_name)
            if app is not None:
                normalized = cross_browser_test_name(test_name, app, pair_label)
                if normalized is None:
                    continue
                test_name = normalized
            if (platform, test_name) in seen:
                continue
            seen.add((platform, test_name))
            out.append(
                VideoTask(
                    task_id=task["status"]["taskId"],
                    test_name=test_name,
                    platform=platform,
                    label=label,
                    revision=push.revision,
                    task_type="perftest" if task_name.startswith("perftest-") else "browsertime",
                )
            )
        return out

    base = _side(base_bt, "base", base_push, cross_browser[0] if cross_browser else None)
    if new_push is None:
        return base
    new = _side(new_bt, "new", new_push, cross_browser[1] if cross_browser else None)
    if cross_browser:
        paired = {(v.platform, v.test_name) for v in base} & {
            (v.platform, v.test_name) for v in new
        }
        base = [v for v in base if (v.platform, v.test_name) in paired]
        new = [v for v in new if (v.platform, v.test_name) in paired]
    return base + new


def find_missing(
    video_tasks: list[VideoTask], downloaded: dict[str, list[Path]], output_dir: Path
) -> list[VideoTask]:
    """VideoTasks for which no video landed under ``<label>/<platform>/<test_name>/``.

    ``downloaded`` maps label ("base"/"new") to the downloaded video paths.
    Keyed on (platform, test_name) — the same test can exist on several
    platforms and one platform's success mustn't mask another's failure.
    """
    got: dict[str, set[tuple[str, str]]] = {}
    for label, paths in downloaded.items():
        keys = got.setdefault(label, set())
        for p in paths:
            try:
                parts = p.relative_to(output_dir).parts
            except ValueError:
                continue
            if len(parts) > 2 and parts[0] == label:
                keys.add((parts[1], parts[2]))
    return [vt for vt in video_tasks if (vt.platform, vt.test_name) not in got.get(vt.label, set())]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download and compare browsertime videos from two Try pushes",
        epilog="""
Examples:
  # Using perfcompare URL (recommended)
  %(prog)s "https://perf.compare/compare-results?baseRev=...&newRev=..."

  # Using two separate revisions
  %(prog)s 881d2bbfaf53 56290454af18

  # Using Treeherder URLs
  %(prog)s "https://treeherder.mozilla.org/...&revision=abc" "https://treeherder.mozilla.org/...&revision=def"

  # Filter on Mann-Whitney U significance instead of the t-test
  %(prog)s "https://perf.compare/compare-results?..." --test-version mann-whitney-u

  # Firefox vs Chrome from the same push (or base app vs new app across two pushes)
  %(prog)s 881d2bbfaf53 --cross-browser firefox,chrome
  %(prog)s 881d2bbfaf53 56290454af18 --cross-browser fenix,chrome-m
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "revisions", nargs="+", help="Perfcompare URL, or base and new revisions/URLs"
    )
    parser.add_argument(
        "--platforms",
        "-p",
        help="Comma-separated platform filters (e.g., linux,windows)",
        default=None,
    )
    parser.add_argument(
        "--tests",
        "-t",
        help="Comma-separated test name filters (e.g., amazon,google)",
        default=None,
    )
    parser.add_argument(
        "--output", "-o", help="Output directory for videos", default="./sxs_videos"
    )
    parser.add_argument(
        "--force",
        help="Overwrite an existing, non-empty output directory without asking",
        action="store_true",
    )
    parser.add_argument(
        "--max-downloads",
        "-m",
        help="Maximum concurrent downloads",
        type=int,
        default=MAX_CONCURRENT_DOWNLOADS,
    )
    parser.add_argument(
        "--no-serve", help="Don't start Flask server after download", action="store_true"
    )
    parser.add_argument(
        "--all-tests",
        help="Download all tests (ignore High confidence filter from perfcompare)",
        action="store_true",
    )
    parser.add_argument(
        "--all-runs",
        help="Download all runs (default: only the median run per test)",
        action="store_true",
    )
    parser.add_argument(
        "--confidence-json",
        help="Path to local perfcompare JSON file for confidence filtering",
        default=None,
    )
    parser.add_argument(
        "--test-version",
        choices=TEST_VERSIONS,
        default=None,
        help="Statistical test used for the significance filter: student-t (High "
        "confidence) or mann-whitney-u (significant p-value + non-negligible effect "
        "size). Default: the perfcompare URL's test_version, else student-t",
    )
    parser.add_argument(
        "--cross-browser",
        metavar="BASE_APP,NEW_APP",
        default=None,
        help="Compare two browsers, e.g. firefox,chrome or fenix,chrome-m. With one "
        "revision both sides come from that push; with two revisions (or a perfcompare "
        "URL) base uses BASE_APP tasks and new uses NEW_APP tasks. Disables the "
        "significance filter (perfcompare doesn't compare across browsers)",
    )
    parser.add_argument(
        "--no-compare",
        help="Single revision mode: download videos without a comparison target",
        action="store_true",
    )
    parser.add_argument(
        "--port", help="Port for Flask server (default: 3333)", type=int, default=3333
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the auto-launched viewer to (default: 127.0.0.1; "
        "use 0.0.0.0 to expose it on your LAN)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout (progress/diagnostics go to stderr instead)",
    )
    return parser


def _parse_inputs(
    args: argparse.Namespace, cross_browser: tuple[str, str] | None
) -> tuple[TryPush, TryPush | None, str | None, tuple | None]:
    """Resolve CLI positionals to (base_push, new_push, perfcompare_url, lando_ids).

    Raises ValueError with a user-facing message on bad input. Lando IDs are
    returned unresolved (network call happens later, inside the session).
    """
    revisions = args.revisions
    if len(revisions) == 2:
        return parse_try_url(revisions[0]), parse_try_url(revisions[1]), None, None
    if len(revisions) != 1:
        raise ValueError(
            f"Expected 1 perfcompare URL or 2 revisions, got {len(revisions)} arguments"
        )

    url = revisions[0]
    if args.no_compare:
        return parse_try_url(url), None, None, None

    if "compare-lando" in url:
        lando_ids = parse_lando_url(url)
        return (
            TryPush(revision="", repo=lando_ids[2]),
            TryPush(revision="", repo=lando_ids[3]),
            None,
            lando_ids,
        )
    try:
        base_push, new_push = parse_perfcompare_url(url)
        return base_push, new_push, url, None
    except ValueError:
        if cross_browser:
            # One push, two browsers: both sides come from the same revision.
            push = parse_try_url(url)
            return push, TryPush(revision=push.revision, repo=push.repo), None, None
        raise ValueError("Single argument must be a perfcompare URL") from None


SINGLE_ARG_HINTS = (
    "Expected: https://perf.compare/compare-results?baseRev=...&newRev=...",
    "Or a lando URL: https://perf.compare/compare-lando-results?baseLando=...&newLando=...",
    "Or provide two separate revisions/URLs, or use --no-compare",
    "Or pass --cross-browser A,B to compare two browsers within one push",
)


async def _amain(args: argparse.Namespace) -> None:
    jm = args.json
    log("Parsing revisions...", json_mode=jm)
    # Strip stray whitespace/newlines that can corrupt a pasted URL
    args.revisions = [r.replace("\n", "").replace("\r", "").strip() for r in args.revisions]

    try:
        cross_browser = parse_cross_browser(args.cross_browser)
        if cross_browser and args.no_compare:
            raise ValueError("--cross-browser and --no-compare can't be combined")
        base_push, new_push, perfcompare_url, lando_ids = _parse_inputs(args, cross_browser)
    except ValueError as e:
        message = str(e)
        if message == "Single argument must be a perfcompare URL":
            if jm:
                message += (
                    ". Expected https://perf.compare/compare-results?baseRev=...&newRev=..., "
                    "or a lando URL https://perf.compare/compare-lando-results?baseLando=...&newLando=..., "
                    "or provide two separate revisions/URLs, or use --no-compare, "
                    "or --cross-browser A,B for one push"
                )
            fail(message, json_mode=jm, hints=SINGLE_ARG_HINTS)
        fail(message, json_mode=jm)

    if lando_ids:
        log("  Parsed lando perfcompare URL (will resolve IDs via Lando API)", json_mode=jm)
    elif perfcompare_url:
        log("  Parsed perfcompare URL", json_mode=jm)
    if cross_browser:
        log(f"  Cross-browser: base={cross_browser[0]}, new={cross_browser[1]}", json_mode=jm)

    # Parse filters
    platforms = args.platforms.split(",") if args.platforms else None
    test_filters = args.tests.split(",") if args.tests else None

    if args.confidence_json and not Path(args.confidence_json).exists():
        fail(f"--confidence-json file not found: {args.confidence_json}", json_mode=jm)

    output_dir = Path(args.output)
    if output_dir.exists() and any(output_dir.iterdir()):
        if not args.force:
            fail(
                f"Output directory {output_dir} already exists and is not empty.",
                json_mode=jm,
                hints=("Pass --force to overwrite it, or choose a different --output path.",),
            )
        log(f"\n--force set: wiping existing output directory {output_dir}", json_mode=jm)
        shutil.rmtree(output_dir)
        log(f"  Cleared {output_dir}", json_mode=jm)
    output_dir.mkdir(parents=True, exist_ok=True)

    test_version: str | None = None
    significant_count: int | None = None

    async with create_session() as session:
        # Resolve lando IDs to revision hashes if needed
        if lando_ids:
            base_id, new_id, base_repo, new_repo, extra_params = lando_ids
            log("\nResolving Lando IDs via Lando API...", json_mode=jm)
            try:
                base_rev, new_rev = await asyncio.gather(
                    resolve_lando_id(session, base_id),
                    resolve_lando_id(session, new_id),
                )
            except Exception as e:
                fail(f"Failed to resolve Lando IDs: {e}", json_mode=jm)
            base_push = TryPush(revision=base_rev, repo=base_repo)
            new_push = TryPush(revision=new_rev, repo=new_repo)
            log(f"  Base lando {base_id} -> {base_rev[:12]}", json_mode=jm)
            log(f"  New  lando {new_id} -> {new_rev[:12]}", json_mode=jm)
            # Build synthetic perfcompare URL for confidence filtering
            extra_qs = "&".join(f"{k}={v}" for k, v in extra_params.items())
            perfcompare_url = (
                f"https://perf.compare/compare-results?"
                f"baseRev={base_rev}&baseRepo={base_repo}"
                f"&newRev={new_rev}&newRepo={new_repo}" + (f"&{extra_qs}" if extra_qs else "")
            )

        # Only meaningful once Lando IDs are resolved (unresolved pushes all have revision "").
        same_push = bool(
            new_push and base_push.revision == new_push.revision and base_push.repo == new_push.repo
        )

        log(f"\n  Base: {base_push.revision[:12]} ({base_push.repo})", json_mode=jm)
        if new_push and not same_push:
            log(f"  New:  {new_push.revision[:12]} ({new_push.repo})", json_mode=jm)

        # Find task groups, then the tasks in them
        log("\nFinding task groups...", json_mode=jm)
        try:
            base_push.task_group_ids = await find_task_group_ids(
                session, base_push.revision, base_push.repo
            )
            log(f"  Base task groups: {base_push.task_group_ids}", json_mode=jm)
            if new_push and not same_push:
                new_push.task_group_ids = await find_task_group_ids(
                    session, new_push.revision, new_push.repo
                )
                log(f"  New task groups:  {new_push.task_group_ids}", json_mode=jm)

            log("\nFetching task lists...", json_mode=jm)
            base_tasks = await get_tasks_for_revision(session, base_push.task_group_ids)
            if new_push and same_push:
                new_tasks = base_tasks
            elif new_push:
                new_tasks = await get_tasks_for_revision(session, new_push.task_group_ids or [])
            else:
                new_tasks = []
        except Exception as e:
            fail(f"Failed to fetch tasks from Taskcluster: {e}", json_mode=jm)
        log(f"  Base: {len(base_tasks)} total tasks", json_mode=jm)
        if new_push and not same_push:
            log(f"  New:  {len(new_tasks)} total tasks", json_mode=jm)

        high_conf_tests = None
        if cross_browser:
            log(
                "\n--cross-browser set: significance filter disabled "
                "(perfcompare doesn't compare across browsers)",
                json_mode=jm,
            )
        elif args.all_tests:
            log(
                "\n--all-tests flag set: downloading all tests (ignoring confidence filter)",
                json_mode=jm,
            )
        elif args.confidence_json:
            test_version = args.test_version
            log(
                f"\nLoading significant tests from local file: {args.confidence_json}", json_mode=jm
            )
            high_conf_tests = load_high_confidence_from_file(
                Path(args.confidence_json), test_version
            )
        elif perfcompare_url:
            test_version = perfcompare_test_version(perfcompare_url, args.test_version)
            log(
                f"\nFetching significant tests from Treeherder API ({test_version})...",
                json_mode=jm,
            )
            high_conf_tests = await fetch_perfcompare_data_from_treeherder(
                session, perfcompare_url, test_version
            )
            if high_conf_tests is None:
                log(
                    "  Treeherder API unreachable — no significance filter applied "
                    "(downloading all tests)",
                    json_mode=jm,
                )
        if high_conf_tests is not None:
            significant_count = len(high_conf_tests)
            if not high_conf_tests:
                fail(
                    "No significant changes found in the perfcompare results"
                    + (f" ({test_version})" if test_version else ""),
                    json_mode=jm,
                    hints=(
                        "Pass --all-tests to download every test anyway, or try "
                        "--test-version with the other statistical test.",
                    ),
                )
            log(
                f"  Found {len(high_conf_tests)} significant test/platform combinations",
                json_mode=jm,
            )
            log(f"  Unique suites: {sorted({s for s, p in high_conf_tests})}", json_mode=jm)
            log("  Will only download videos for significant changes", json_mode=jm)

        # Filter to video tasks (browsertime + perftest startup)
        log("\nFiltering video tasks...", json_mode=jm)
        base_bt = apply_test_filters(
            filter_video_tasks(base_tasks, platforms, high_conf_tests), test_filters
        )
        new_bt = (
            apply_test_filters(
                filter_video_tasks(new_tasks, platforms, high_conf_tests), test_filters
            )
            if new_push
            else []
        )
        video_tasks = build_video_tasks(base_bt, new_bt, base_push, new_push, cross_browser)
        n_base = sum(1 for v in video_tasks if v.label == "base")
        n_new = len(video_tasks) - n_base
        log(f"  Base: {n_base} video tasks", json_mode=jm)
        if new_push:
            log(f"  New:  {n_new} video tasks", json_mode=jm)

        if not n_base or (new_push and not n_new):
            hints: tuple[str, ...] = ()
            if cross_browser:
                hints = (
                    f"No test ran for both {cross_browser[0]} and {cross_browser[1]} on the "
                    "same platform. App tokens must match task names exactly "
                    "(e.g. chrome-m on Android).",
                )
            fail("No matching video tasks found", json_mode=jm, hints=hints)

        log(f"\nDownloading {len(video_tasks)} video artifacts...", json_mode=jm)
        if not args.all_runs:
            log("  (median run only — use --all-runs to download all)", json_mode=jm)
        results = await download_video_artifacts(
            session,
            video_tasks,
            output_dir,
            args.max_downloads,
            all_runs=args.all_runs,
            quiet=jm,
        )

    base_videos = results["base"]["videos"]
    new_videos = results["new"]["videos"]
    base_images = results["base"]["images"]
    new_images = results["new"]["images"]

    log("\nDownloaded:", json_mode=jm)
    log(
        f"  Base: {len(base_videos)} videos"
        + (f", {len(base_images)} images" if base_images else ""),
        json_mode=jm,
    )
    log(
        f"  New:  {len(new_videos)} videos" + (f", {len(new_images)} images" if new_images else ""),
        json_mode=jm,
    )

    missing = find_missing(video_tasks, {"base": base_videos, "new": new_videos}, output_dir)
    if missing:
        log(f"\n  Missing video artifacts ({len(missing)} tasks):", json_mode=jm)
        for vt in missing:
            log(f"    [{vt.label}] {vt.platform} / {vt.test_name}", json_mode=jm)

    # Organize videos
    if new_push:
        comparisons = organize_videos_for_comparison(output_dir)
        mode = "compare"
    else:
        comparisons = organize_single_revision(output_dir)
        mode = "single"

    cross_browser_meta = (
        {"base_app": cross_browser[0], "new_app": cross_browser[1]} if cross_browser else None
    )

    # Save metadata
    meta_path = output_dir / "comparisons.json"
    with open(meta_path, "w") as f:
        json.dump(
            {
                "mode": mode,
                "base_revision": base_push.revision,
                "new_revision": new_push.revision if new_push else None,
                "cross_browser": cross_browser_meta,
                "test_version": test_version,
                "comparisons": comparisons,
            },
            f,
            indent=2,
        )

    log(f"\nFound {len(comparisons)} comparisons", json_mode=jm)
    log(f"Metadata saved to: {meta_path}", json_mode=jm)

    serve = not args.no_serve
    viewer_info: dict[str, object]
    url = f"http://{args.host}:{args.port}"
    if serve:
        log(f"\nStarting viewer at {url}", json_mode=jm)
        viewer_info = {"serving": True, "url": url}
    else:
        log("\nTo view videos later, run:", json_mode=jm)
        log(f"  perf-sxs-viewer {output_dir}", json_mode=jm)
        viewer_info = {"serving": False, "command": f"perf-sxs-viewer {output_dir}"}

    if jm:
        summary = {
            "mode": mode,
            "base_revision": base_push.revision,
            "base_repo": base_push.repo,
            "new_revision": new_push.revision if new_push else None,
            "new_repo": new_push.repo if new_push else None,
            "cross_browser": cross_browser_meta,
            "platforms": platforms,
            "tests": test_filters,
            "test_version": test_version,
            "significant_tests": significant_count,
            "output_dir": str(output_dir),
            "video_tasks_found": len(video_tasks),
            "downloaded": {
                "base": {"videos": len(base_videos), "images": len(base_images)},
                "new": {"videos": len(new_videos), "images": len(new_images)},
            },
            "missing": [
                {"label": vt.label, "platform": vt.platform, "test_name": vt.test_name}
                for vt in missing
            ],
            "comparisons_count": len(comparisons),
            "metadata_path": str(meta_path),
            "viewer": viewer_info,
        }
        # Print the summary before app.run() blocks, so a --json caller
        # always gets it even when the viewer is left running.
        print(json.dumps(summary), flush=True)

    if serve:
        from .viewer import create_app

        app = create_app(output_dir)

        if not jm:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        app.run(host=args.host, port=args.port, debug=False)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()

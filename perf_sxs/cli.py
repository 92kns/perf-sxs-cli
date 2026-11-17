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
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from .api import (
    fetch_significant_rows,
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
    load_significant_rows_from_file,
    rows_for_step,
    rows_for_task,
    significant_pairs,
    significant_steps,
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
                    task_name=task_name,
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
        "--resume",
        help="Reuse an existing output directory from an earlier run of the same "
        "revisions: tasks already downloaded are skipped, new ones (e.g. from a "
        "wider --tests/--platforms filter) are added",
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
        "--all-steps",
        help="Multi-step tests (nav-bench): keep every step, not just the steps "
        "perfcompare flagged as significant",
        action="store_true",
    )
    parser.add_argument(
        "--include-profiled-runs",
        help="Keep perftest runs recorded under a profiler (simpleperf/geckoprofiler "
        "subdirectories). Dropped by default: profilers skew timing",
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


class CliError(Exception):
    """A user-facing failure: `_amain` turns it into `fail()` output."""

    def __init__(self, message: str, hints: tuple[str, ...] = ()):
        super().__init__(message)
        self.hints = hints


@dataclass
class RunResult:
    mode: str
    base_push: TryPush
    new_push: TryPush | None
    output_dir: Path
    video_tasks: list[VideoTask]
    downloaded: dict[str, dict[str, list[Path]]]
    missing: list[VideoTask]
    comparisons: dict
    meta_path: Path
    cross_browser: tuple[str, str] | None = None
    platforms: list[str] | None = None
    test_filters: list[str] | None = None
    test_version: str | None = None
    significant_rows: list[dict] | None = field(default=None)

    @property
    def cross_browser_meta(self) -> dict | None:
        if not self.cross_browser:
            return None
        return {"base_app": self.cross_browser[0], "new_app": self.cross_browser[1]}

    def summary(self, viewer_info: dict) -> dict:
        d = self.downloaded
        return {
            "mode": self.mode,
            "base_revision": self.base_push.revision,
            "base_repo": self.base_push.repo,
            "new_revision": self.new_push.revision if self.new_push else None,
            "new_repo": self.new_push.repo if self.new_push else None,
            "cross_browser": self.cross_browser_meta,
            "platforms": self.platforms,
            "tests": self.test_filters,
            "test_version": self.test_version,
            "significant_tests": (
                len(significant_pairs(self.significant_rows))
                if self.significant_rows is not None
                else None
            ),
            "output_dir": str(self.output_dir),
            "video_tasks_found": len(self.video_tasks),
            "downloaded": {
                side: {"videos": len(d[side]["videos"]), "images": len(d[side]["images"])}
                for side in ("base", "new")
            },
            "missing": [
                {"label": vt.label, "platform": vt.platform, "test_name": vt.test_name}
                for vt in self.missing
            ],
            "comparisons_count": len(self.comparisons),
            "metadata_path": str(self.meta_path),
            "viewer": viewer_info,
        }


def _prepare_output_dir(output_dir: Path, *, force: bool, resume: bool, log_) -> None:
    if force and resume:
        raise CliError("--force and --resume can't be combined")
    if output_dir.exists() and any(output_dir.iterdir()) and not resume:
        if not force:
            raise CliError(
                f"Output directory {output_dir} already exists and is not empty.",
                hints=(
                    "Pass --force to overwrite it, --resume to add to it, or choose a "
                    "different --output path.",
                ),
            )
        log_(f"\n--force set: wiping existing output directory {output_dir}")
        shutil.rmtree(output_dir)
        log_(f"  Cleared {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)


def _same_revision(a: str | None, b: str | None) -> bool:
    """Equal, allowing one to be an abbreviated hash of the other."""
    if a is None or b is None:
        return a == b
    return a.startswith(b) or b.startswith(a)


def _check_resume_target(output_dir: Path, base_push: TryPush, new_push: TryPush | None) -> None:
    """--resume must not mix videos from different revisions into one directory."""
    meta = _read_metadata(output_dir)
    if not meta:
        return
    want = [
        (base_push.revision, base_push.repo),
        (new_push.revision if new_push else None, new_push.repo if new_push else None),
    ]
    have = [
        (meta.get("base_revision"), meta.get("base_repo")),
        (meta.get("new_revision"), meta.get("new_repo")),
    ]
    same = all(
        _same_revision(w[0], h[0]) and (h[1] is None or w[1] == h[1])
        for w, h in zip(want, have, strict=True)
    )
    if not same:
        raise CliError(
            f"--resume: {output_dir} holds a different comparison "
            f"({str(have[0][0])[:12]} vs {str(have[1][0])[:12]})",
            hints=("Use --force to replace it, or a different --output path.",),
        )


async def _resolve_lando(session, lando_ids: tuple, log_) -> tuple[TryPush, TryPush, str]:
    base_id, new_id, base_repo, new_repo, extra_params = lando_ids
    log_("\nResolving Lando IDs via Lando API...")
    try:
        base_rev, new_rev = await asyncio.gather(
            resolve_lando_id(session, base_id),
            resolve_lando_id(session, new_id),
        )
    except Exception as e:
        raise CliError(f"Failed to resolve Lando IDs: {e}") from e
    log_(f"  Base lando {base_id} -> {base_rev[:12]}")
    log_(f"  New  lando {new_id} -> {new_rev[:12]}")
    # Build synthetic perfcompare URL for confidence filtering
    extra_qs = "&".join(f"{k}={v}" for k, v in extra_params.items())
    perfcompare_url = (
        f"https://perf.compare/compare-results?"
        f"baseRev={base_rev}&baseRepo={base_repo}"
        f"&newRev={new_rev}&newRepo={new_repo}" + (f"&{extra_qs}" if extra_qs else "")
    )
    return TryPush(base_rev, base_repo), TryPush(new_rev, new_repo), perfcompare_url


async def _fetch_tasks(
    session, base_push: TryPush, new_push: TryPush | None, same_push: bool, log_
) -> tuple[list, list]:
    log_("\nFinding task groups...")
    try:
        base_push.task_group_ids = await find_task_group_ids(
            session, base_push.revision, base_push.repo
        )
        log_(f"  Base task groups: {base_push.task_group_ids}")
        if new_push and not same_push:
            new_push.task_group_ids = await find_task_group_ids(
                session, new_push.revision, new_push.repo
            )
            log_(f"  New task groups:  {new_push.task_group_ids}")

        log_("\nFetching task lists...")
        base_tasks = await get_tasks_for_revision(session, base_push.task_group_ids)
        if new_push and same_push:
            new_tasks = base_tasks
        elif new_push:
            new_tasks = await get_tasks_for_revision(session, new_push.task_group_ids or [])
        else:
            new_tasks = []
    except Exception as e:
        raise CliError(f"Failed to fetch tasks from Taskcluster: {e}") from e
    log_(f"  Base: {len(base_tasks)} total tasks")
    if new_push and not same_push:
        log_(f"  New:  {len(new_tasks)} total tasks")
    return base_tasks, new_tasks


async def _significance(
    session,
    args: argparse.Namespace,
    perfcompare_url: str | None,
    cross_browser: tuple[str, str] | None,
    log_,
) -> tuple[list[dict] | None, str | None]:
    """Significant perfcompare rows (None = no filter) and the test version used."""
    rows: list[dict] | None = None
    test_version: str | None = None
    if cross_browser:
        log_(
            "\n--cross-browser set: significance filter disabled "
            "(perfcompare doesn't compare across browsers)"
        )
        return None, None
    if args.all_tests:
        log_("\n--all-tests flag set: downloading all tests (ignoring confidence filter)")
        return None, None
    if args.confidence_json:
        test_version = args.test_version
        log_(f"\nLoading significant tests from local file: {args.confidence_json}")
        rows = load_significant_rows_from_file(Path(args.confidence_json), test_version)
    elif perfcompare_url:
        test_version = perfcompare_test_version(perfcompare_url, args.test_version)
        log_(f"\nFetching significant tests from Treeherder API ({test_version})...")
        rows = await fetch_significant_rows(session, perfcompare_url, test_version)
        if rows is None:
            log_(
                "  Treeherder API unreachable — no significance filter applied "
                "(downloading all tests)"
            )
            return None, None
    if rows is None:
        return None, None

    pairs = significant_pairs(rows)
    if not pairs:
        raise CliError(
            "No significant changes found in the perfcompare results"
            + (f" ({test_version})" if test_version else ""),
            hints=(
                "Pass --all-tests to download every test anyway, or try "
                "--test-version with the other statistical test.",
            ),
        )
    log_(f"  Found {len(pairs)} significant test/platform combinations")
    log_(f"  Unique suites: {sorted({s for s, p in pairs})}")
    log_("  Will only download videos for significant changes")
    return rows, test_version


def attach_perfcompare(
    comparisons: dict,
    video_tasks: list[VideoTask],
    rows: list[dict] | None,
    previous: dict | None = None,
) -> None:
    """Add each comparison's significant perfcompare rows as ``comparisons[k]["perfcompare"]``.

    Comparisons that this run didn't download (left in the directory by an
    earlier run, with --resume) keep the rows recorded in ``previous``.
    """
    previous = previous or {}
    by_dir = (
        {
            (vt.platform, vt.test_name): rows_for_task(vt.task_name, rows)
            for vt in video_tasks
            if vt.label == "base"
        }
        if rows is not None
        else {}
    )
    for key, comp in comparisons.items():
        task_rows = by_dir.get((comp["platform"], comp["test_name"]))
        if task_rows is not None:
            comp["perfcompare"] = rows_for_step(task_rows, comp.get("step"))
        elif "perfcompare" in previous.get(key, {}):
            comp["perfcompare"] = previous[key]["perfcompare"]


def _read_metadata(output_dir: Path) -> dict:
    try:
        return json.loads((output_dir / "comparisons.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


async def run(args: argparse.Namespace) -> RunResult:
    """The whole download pipeline. Raises `CliError` for user-facing failures."""
    jm = args.json

    def log_(msg: str) -> None:
        log(msg, json_mode=jm)

    log_("Parsing revisions...")
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
            raise CliError(message, hints=SINGLE_ARG_HINTS) from e
        raise CliError(message) from e

    if lando_ids:
        log_("  Parsed lando perfcompare URL (will resolve IDs via Lando API)")
    elif perfcompare_url:
        log_("  Parsed perfcompare URL")
    if cross_browser:
        log_(f"  Cross-browser: base={cross_browser[0]}, new={cross_browser[1]}")

    platforms = args.platforms.split(",") if args.platforms else None
    test_filters = args.tests.split(",") if args.tests else None

    if args.confidence_json and not Path(args.confidence_json).exists():
        raise CliError(f"--confidence-json file not found: {args.confidence_json}")

    output_dir = Path(args.output)
    _prepare_output_dir(output_dir, force=args.force, resume=args.resume, log_=log_)

    async with create_session() as session:
        if lando_ids:
            base_push, new_push, perfcompare_url = await _resolve_lando(session, lando_ids, log_)
        if args.resume:
            _check_resume_target(output_dir, base_push, new_push)

        # Only meaningful once Lando IDs are resolved (unresolved pushes all have revision "").
        same_push = bool(
            new_push and base_push.revision == new_push.revision and base_push.repo == new_push.repo
        )
        log_(f"\n  Base: {base_push.revision[:12]} ({base_push.repo})")
        if new_push and not same_push:
            log_(f"  New:  {new_push.revision[:12]} ({new_push.repo})")

        base_tasks, new_tasks = await _fetch_tasks(session, base_push, new_push, same_push, log_)
        rows, test_version = await _significance(
            session, args, perfcompare_url, cross_browser, log_
        )
        pairs = significant_pairs(rows) if rows is not None else None

        log_("\nFiltering video tasks...")
        base_bt = apply_test_filters(filter_video_tasks(base_tasks, platforms, pairs), test_filters)
        new_bt = (
            apply_test_filters(filter_video_tasks(new_tasks, platforms, pairs), test_filters)
            if new_push
            else []
        )
        video_tasks = build_video_tasks(base_bt, new_bt, base_push, new_push, cross_browser)
        if rows is not None and not args.all_steps:
            for vt in video_tasks:
                vt.steps = significant_steps(vt.task_name, rows)
        n_base = sum(1 for v in video_tasks if v.label == "base")
        n_new = len(video_tasks) - n_base
        log_(f"  Base: {n_base} video tasks")
        if new_push:
            log_(f"  New:  {n_new} video tasks")

        if not n_base or (new_push and not n_new):
            hints: tuple[str, ...] = ()
            if cross_browser:
                hints = (
                    f"No test ran for both {cross_browser[0]} and {cross_browser[1]} on the "
                    "same platform. App tokens must match task names exactly "
                    "(e.g. chrome-m on Android).",
                )
            raise CliError("No matching video tasks found", hints=hints)

        log_(f"\nDownloading {len(video_tasks)} video artifacts...")
        if not args.all_runs:
            log_("  (median run only — use --all-runs to download all)")
        downloaded = await download_video_artifacts(
            session,
            video_tasks,
            output_dir,
            args.max_downloads,
            all_runs=args.all_runs,
            quiet=jm,
            resume=args.resume,
            include_profiled=args.include_profiled_runs,
        )

    log_("\nDownloaded:")
    for side, name in (("base", "Base:"), ("new", "New: ")):
        vids, imgs = downloaded[side]["videos"], downloaded[side]["images"]
        if side == "new" and not new_push:
            break
        log_(f"  {name} {len(vids)} videos" + (f", {len(imgs)} images" if imgs else ""))

    missing = find_missing(
        video_tasks,
        {"base": downloaded["base"]["videos"], "new": downloaded["new"]["videos"]},
        output_dir,
    )
    if missing:
        log_(f"\n  Missing video artifacts ({len(missing)} tasks):")
        for vt in missing:
            log_(f"    [{vt.label}] {vt.platform} / {vt.test_name}")

    if new_push:
        comparisons = organize_videos_for_comparison(output_dir)
        mode = "compare"
    else:
        comparisons = organize_single_revision(output_dir)
        mode = "single"
    previous = _read_metadata(output_dir).get("comparisons", {}) if args.resume else {}
    attach_perfcompare(comparisons, video_tasks, rows, previous)

    result = RunResult(
        mode=mode,
        base_push=base_push,
        new_push=new_push,
        output_dir=output_dir,
        video_tasks=video_tasks,
        downloaded=downloaded,
        missing=missing,
        comparisons=comparisons,
        meta_path=output_dir / "comparisons.json",
        cross_browser=cross_browser,
        platforms=platforms,
        test_filters=test_filters,
        test_version=test_version,
        significant_rows=rows,
    )
    with open(result.meta_path, "w") as f:
        json.dump(
            {
                "mode": mode,
                "base_revision": base_push.revision,
                "base_repo": base_push.repo,
                "new_revision": new_push.revision if new_push else None,
                "new_repo": new_push.repo if new_push else None,
                "cross_browser": result.cross_browser_meta,
                "test_version": test_version,
                "comparisons": comparisons,
            },
            f,
            indent=2,
        )
    log_(f"\nFound {len(comparisons)} comparisons")
    log_(f"Metadata saved to: {result.meta_path}")
    return result


async def _amain(args: argparse.Namespace) -> None:
    jm = args.json
    try:
        result = await run(args)
    except CliError as e:
        fail(str(e), json_mode=jm, hints=e.hints)

    serve = not args.no_serve
    viewer_info: dict[str, object]
    url = f"http://{args.host}:{args.port}"
    if serve:
        log(f"\nStarting viewer at {url}", json_mode=jm)
        viewer_info = {"serving": True, "url": url}
    else:
        log("\nTo view videos later, run:", json_mode=jm)
        log(f"  perf-sxs-viewer {result.output_dir}", json_mode=jm)
        viewer_info = {"serving": False, "command": f"perf-sxs-viewer {result.output_dir}"}

    if jm:
        # Print the summary before app.run() blocks, so a --json caller
        # always gets it even when the viewer is left running.
        print(json.dumps(result.summary(viewer_info)), flush=True)

    if serve:
        from .viewer import create_app

        app = create_app(result.output_dir)

        if not jm:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        app.run(host=args.host, port=args.port, debug=False)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()

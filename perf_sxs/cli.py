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

from .api import (
    fetch_perfcompare_data_from_treeherder,
    find_task_group_ids,
    get_tasks_for_revision,
    resolve_lando_id,
)
from .api import new_session as create_session
from .download import download_video_artifacts
from .models import MAX_CONCURRENT_DOWNLOADS, TryPush, VideoTask
from .organize import organize_single_revision, organize_videos_for_comparison
from .selection import extract_test_info, filter_video_tasks, load_high_confidence_from_file
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


async def _amain(args: argparse.Namespace) -> None:
    log("Parsing revisions...", json_mode=args.json)
    perfcompare_url = None
    lando_ids: tuple[str, str, str, str, dict] | None = None
    new_push = None
    try:
        # Strip stray whitespace/newlines that can corrupt a pasted URL
        args.revisions = [r.replace("\n", "").replace("\r", "").strip() for r in args.revisions]

        if len(args.revisions) == 1:
            if args.no_compare:
                base_push = parse_try_url(args.revisions[0])
            else:
                url = args.revisions[0]
                # Try lando URL first, then regular perfcompare URL
                try:
                    lando_ids = parse_lando_url(url)
                    if "compare-lando" not in url:
                        raise ValueError("not a lando URL")
                    base_push = TryPush(revision="", repo=lando_ids[2])
                    new_push = TryPush(revision="", repo=lando_ids[3])
                    log(
                        "  Parsed lando perfcompare URL (will resolve IDs via Lando API)",
                        json_mode=args.json,
                    )
                except ValueError:
                    try:
                        base_push, new_push = parse_perfcompare_url(url)
                        perfcompare_url = url
                        log("  Parsed perfcompare URL", json_mode=args.json)
                    except ValueError:
                        if args.json:
                            print(
                                json.dumps(
                                    build_error_json(
                                        "Single argument must be a perfcompare URL. Expected "
                                        "https://perf.compare/compare-results?baseRev=...&newRev=..., "
                                        "or a lando URL "
                                        "https://perf.compare/compare-lando-results?baseLando=...&newLando=..., "
                                        "or provide two separate revisions/URLs, or use --no-compare"
                                    )
                                )
                            )
                        else:
                            print("Error: Single argument must be a perfcompare URL")
                            print(
                                "  Expected: https://perf.compare/compare-results?baseRev=...&newRev=..."
                            )
                            print(
                                "  Or a lando URL: https://perf.compare/compare-lando-results?baseLando=...&newLando=..."
                            )
                            print("  Or provide two separate revisions/URLs, or use --no-compare")
                        sys.exit(1)
        elif len(args.revisions) == 2:
            base_push = parse_try_url(args.revisions[0])
            new_push = parse_try_url(args.revisions[1])
        else:
            message = (
                f"Expected 1 perfcompare URL or 2 revisions, got {len(args.revisions)} arguments"
            )
            if args.json:
                print(json.dumps(build_error_json(message)))
            else:
                print(f"Error: {message}")
            sys.exit(1)
    except ValueError as e:
        if args.json:
            print(json.dumps(build_error_json(str(e))))
        else:
            print(f"Error: {e}")
        sys.exit(1)

    # Parse filters
    platforms = args.platforms.split(",") if args.platforms else None
    test_filters = args.tests.split(",") if args.tests else None

    output_dir = Path(args.output)
    if output_dir.exists() and any(output_dir.iterdir()):
        if not args.force:
            message = f"Output directory {output_dir} already exists and is not empty."
            if args.json:
                print(json.dumps(build_error_json(message)))
            else:
                print(f"Error: {message}")
                print("  Pass --force to overwrite it, or choose a different --output path.")
            sys.exit(1)
        log(f"\n--force set: wiping existing output directory {output_dir}", json_mode=args.json)
        shutil.rmtree(output_dir)
        log(f"  Cleared {output_dir}", json_mode=args.json)
    output_dir.mkdir(parents=True, exist_ok=True)

    max_concurrent = args.max_downloads

    async with create_session() as session:
        # Resolve lando IDs to revision hashes if needed
        if lando_ids:
            base_id, new_id, base_repo, new_repo, extra_params = lando_ids
            log("\nResolving Lando IDs via Lando API...", json_mode=args.json)
            try:
                base_rev, new_rev = await asyncio.gather(
                    resolve_lando_id(session, base_id),
                    resolve_lando_id(session, new_id),
                )
            except Exception as e:
                if args.json:
                    print(json.dumps(build_error_json(f"Failed to resolve Lando IDs: {e}")))
                else:
                    print(f"  Error: {e}")
                sys.exit(1)
            base_push = TryPush(revision=base_rev, repo=base_repo)
            new_push = TryPush(revision=new_rev, repo=new_repo)
            log(f"  Base lando {base_id} -> {base_rev[:12]}", json_mode=args.json)
            log(f"  New  lando {new_id} -> {new_rev[:12]}", json_mode=args.json)
            # Build synthetic perfcompare URL for confidence filtering
            extra_qs = "&".join(f"{k}={v}" for k, v in extra_params.items())
            perfcompare_url = (
                f"https://perf.compare/compare-results?"
                f"baseRev={base_rev}&baseRepo={base_repo}"
                f"&newRev={new_rev}&newRepo={new_repo}" + (f"&{extra_qs}" if extra_qs else "")
            )

        log(f"\n  Base: {base_push.revision[:12]} ({base_push.repo})", json_mode=args.json)
        if new_push:
            log(f"  New:  {new_push.revision[:12]} ({new_push.repo})", json_mode=args.json)

        # Find task group IDs
        log("\nFinding task groups...", json_mode=args.json)
        base_group_ids = await find_task_group_ids(session, base_push.revision, base_push.repo)
        base_push.task_group_ids = base_group_ids
        log(f"  Base task groups: {base_group_ids}", json_mode=args.json)
        new_group_ids: list[str] = []
        if new_push:
            new_group_ids = await find_task_group_ids(session, new_push.revision, new_push.repo)
            new_push.task_group_ids = new_group_ids
            log(f"  New task groups:  {new_group_ids}", json_mode=args.json)

        # Get tasks in groups
        log("\nFetching task lists...", json_mode=args.json)
        if new_push:
            base_tasks, new_tasks = await asyncio.gather(
                get_tasks_for_revision(session, base_group_ids),
                get_tasks_for_revision(session, new_group_ids),
            )
            log(f"  Base: {len(base_tasks)} total tasks", json_mode=args.json)
            log(f"  New:  {len(new_tasks)} total tasks", json_mode=args.json)
        else:
            base_tasks = await get_tasks_for_revision(session, base_group_ids)
            new_tasks = []
            log(f"  Base: {len(base_tasks)} total tasks", json_mode=args.json)

        high_conf_tests = None
        if args.confidence_json and not args.all_tests:
            log(
                f"\nLoading high confidence tests from local file: {args.confidence_json}",
                json_mode=args.json,
            )
            json_path = Path(args.confidence_json)
            if json_path.exists():
                high_conf_tests = load_high_confidence_from_file(json_path)
                log(
                    f"  Found {len(high_conf_tests)} high confidence test/platform combinations",
                    json_mode=args.json,
                )
                log(
                    f"  Unique suites: {sorted({s for s, p in high_conf_tests})}",
                    json_mode=args.json,
                )
                log("  Will only download videos for high confidence changes", json_mode=args.json)
            else:
                log(f"  Error: File not found: {args.confidence_json}", json_mode=args.json)
        elif perfcompare_url and not args.all_tests:
            log("\nFetching high confidence tests from Treeherder API...", json_mode=args.json)
            high_conf_tests = await fetch_perfcompare_data_from_treeherder(session, perfcompare_url)
            if high_conf_tests:
                log(
                    f"  Found {len(high_conf_tests)} high confidence test/platform combinations",
                    json_mode=args.json,
                )
                log(
                    f"  Unique suites: {sorted({s for s, p in high_conf_tests})}",
                    json_mode=args.json,
                )
                log("  Will only download videos for high confidence changes", json_mode=args.json)
            else:
                log(
                    "  No high confidence filter applied (API fetch may have failed)",
                    json_mode=args.json,
                )
        elif args.all_tests:
            log(
                "\n--all-tests flag set: downloading all tests (ignoring confidence filter)",
                json_mode=args.json,
            )

        # Filter to video tasks (browsertime + perftest startup)
        log("\nFiltering video tasks...", json_mode=args.json)
        base_bt = filter_video_tasks(base_tasks, platforms, high_conf_tests)
        new_bt = filter_video_tasks(new_tasks, platforms, high_conf_tests) if new_push else []

        # Apply test name filters
        if test_filters:
            base_bt = [
                t
                for t in base_bt
                if any(f.lower() in t["task"]["metadata"]["name"].lower() for f in test_filters)
            ]
            new_bt = [
                t
                for t in new_bt
                if any(f.lower() in t["task"]["metadata"]["name"].lower() for f in test_filters)
            ]

        log(f"  Base: {len(base_bt)} video tasks", json_mode=args.json)
        if new_push:
            log(f"  New:  {len(new_bt)} video tasks", json_mode=args.json)

        if not base_bt or (new_push and not new_bt):
            if args.json:
                print(json.dumps(build_error_json("No matching video tasks found")))
            else:
                print("\nNo matching video tasks found!")
            sys.exit(1)

        # Build list of video tasks to download
        video_tasks = []

        for task in base_bt:
            task_name = task["task"]["metadata"]["name"]
            test_name, platform = extract_test_info(task_name)
            task_type = "perftest" if task_name.startswith("perftest-") else "browsertime"
            video_tasks.append(
                VideoTask(
                    task_id=task["status"]["taskId"],
                    test_name=test_name,
                    platform=platform,
                    label="base",
                    revision=base_push.revision,
                    task_type=task_type,
                )
            )

        if new_push:
            for task in new_bt:
                task_name = task["task"]["metadata"]["name"]
                test_name, platform = extract_test_info(task_name)
                task_type = "perftest" if task_name.startswith("perftest-") else "browsertime"
                video_tasks.append(
                    VideoTask(
                        task_id=task["status"]["taskId"],
                        test_name=test_name,
                        platform=platform,
                        label="new",
                        revision=new_push.revision,
                        task_type=task_type,
                    )
                )

        log(f"\nDownloading {len(video_tasks)} video artifacts...", json_mode=args.json)
        if not args.all_runs:
            log("  (median run only — use --all-runs to download all)", json_mode=args.json)
        results = await download_video_artifacts(
            session,
            video_tasks,
            output_dir,
            max_concurrent,
            all_runs=args.all_runs,
            quiet=args.json,
        )

        base_videos = results["base"]["videos"]
        new_videos = results["new"]["videos"]
        base_images = results["base"]["images"]
        new_images = results["new"]["images"]

        log("\nDownloaded:", json_mode=args.json)
        log(
            f"  Base: {len(base_videos)} videos"
            + (f", {len(base_images)} images" if base_images else ""),
            json_mode=args.json,
        )
        log(
            f"  New:  {len(new_videos)} videos"
            + (f", {len(new_images)} images" if new_images else ""),
            json_mode=args.json,
        )

        # Report tasks where no video was downloaded
        downloaded_base_ids = {
            p.parts[p.parts.index("base") + 2] for p in base_videos if "base" in p.parts
        }
        downloaded_new_ids = {
            p.parts[p.parts.index("new") + 2] for p in new_videos if "new" in p.parts
        }
        missing = []
        for vt in video_tasks:
            downloaded = downloaded_base_ids if vt.label == "base" else downloaded_new_ids
            if vt.test_name not in downloaded:
                missing.append(vt)
        if missing:
            log(f"\n  Missing video artifacts ({len(missing)} tasks):", json_mode=args.json)
            for vt in missing:
                log(f"    [{vt.label}] {vt.platform} / {vt.test_name}", json_mode=args.json)

    # Organize videos
    if new_push:
        comparisons = organize_videos_for_comparison(output_dir)
        mode = "compare"
    else:
        comparisons = organize_single_revision(output_dir)
        mode = "single"

    # Save metadata
    meta_path = output_dir / "comparisons.json"
    with open(meta_path, "w") as f:
        json.dump(
            {
                "mode": mode,
                "base_revision": base_push.revision,
                "new_revision": new_push.revision if new_push else None,
                "comparisons": comparisons,
            },
            f,
            indent=2,
        )

    log(f"\nFound {len(comparisons)} test/platform combinations", json_mode=args.json)
    log(f"Metadata saved to: {meta_path}", json_mode=args.json)

    serve = not args.no_serve
    viewer_info: dict[str, object]
    if serve:
        url = f"http://{args.host}:{args.port}"
        log(f"\nStarting viewer at {url}", json_mode=args.json)
        viewer_info = {"serving": True, "url": url}
    else:
        log("\nTo view videos later, run:", json_mode=args.json)
        log(f"  perf-sxs-viewer {output_dir}", json_mode=args.json)
        viewer_info = {"serving": False, "command": f"perf-sxs-viewer {output_dir}"}

    if args.json:
        summary = {
            "mode": mode,
            "base_revision": base_push.revision,
            "base_repo": base_push.repo,
            "new_revision": new_push.revision if new_push else None,
            "new_repo": new_push.repo if new_push else None,
            "platforms": platforms,
            "tests": test_filters,
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
        print(json.dumps(summary))

    if serve:
        from .viewer import create_app

        app = create_app(output_dir)

        if not args.json:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        app.run(host=args.host, port=args.port, debug=False)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()

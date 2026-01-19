#!/usr/bin/env python3
"""
Async side-by-side video comparison tool for Mozilla Try pushes.

Usage:
    python perf_sxs.py <perfcompare-url> [options]
    python perf_sxs.py <base-revision> <new-revision> [options]

Example with perfcompare URL (recommended):
    python perf_sxs.py \
        "https://perf.compare/compare-results?baseRev=881d2bbf...&newRev=56290454..." \
        --serve

Example with revisions:
    python perf_sxs.py \
        881d2bbfaf5390c3344757213fc7199839fe3e7f \
        56290454af1890c3344757213fc7199839fe3e7f \
        --platforms linux,windows

Example with Treeherder URLs:
    python perf_sxs.py \
        "https://treeherder.mozilla.org/jobs?repo=try&revision=abc123" \
        "https://treeherder.mozilla.org/jobs?repo=try&revision=def456" \
        --output ./videos
"""

import argparse
import asyncio
import aiohttp
import json
import os
import re
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

TASKCLUSTER_ROOT = "https://firefox-ci-tc.services.mozilla.com/api"
TASKCLUSTER_QUEUE = f"{TASKCLUSTER_ROOT}/queue/v1"
TASKCLUSTER_INDEX = f"{TASKCLUSTER_ROOT}/index/v1"

MAX_CONCURRENT_DOWNLOADS = 10


@dataclass
class TryPush:
    revision: str
    repo: str = "try"
    task_group_id: Optional[str] = None


@dataclass
class VideoTask:
    task_id: str
    test_name: str
    platform: str
    label: str  # "base" or "new"
    revision: str


def parse_perfcompare_url(url: str) -> tuple[TryPush, TryPush]:
    """Extract base and new revisions from a perfcompare URL."""
    parsed = urlparse(url)

    if "perf.compare" in parsed.netloc or "perfcompare" in parsed.netloc:
        params = parse_qs(parsed.query)
        base_rev = params.get("baseRev", [None])[0]
        new_rev = params.get("newRev", [None])[0]
        base_repo = params.get("baseRepo", ["try"])[0]
        new_repo = params.get("newRepo", ["try"])[0]

        if base_rev and new_rev:
            return (
                TryPush(revision=base_rev, repo=base_repo),
                TryPush(revision=new_rev, repo=new_repo)
            )

    raise ValueError(f"Could not parse perfcompare URL: {url}")


def parse_try_url(url: str) -> TryPush:
    """Extract revision and repo from a Treeherder URL or plain revision string."""
    parsed = urlparse(url)

    if "treeherder" in parsed.netloc:
        params = parse_qs(parsed.query)
        revision = params.get("revision", [None])[0]
        repo = params.get("repo", ["try"])[0]
        if revision:
            return TryPush(revision=revision, repo=repo)

    if re.fullmatch(r"[a-f0-9]{12,40}", url.strip()):
        return TryPush(revision=url.strip(), repo="try")

    rev_match = re.search(r"([a-f0-9]{12,40})", url)
    if rev_match:
        return TryPush(revision=rev_match.group(1), repo="try")

    raise ValueError(f"Could not parse Try URL or revision: {url}")


async def fetch_json(session: aiohttp.ClientSession, url: str) -> dict:
    """Fetch JSON from URL with error handling."""
    async with session.get(url) as resp:
        if resp.status != 200:
            raise Exception(f"HTTP {resp.status} for {url}")
        return await resp.json()


async def find_task_group_id(session: aiohttp.ClientSession, revision: str, repo: str) -> str:
    """Find the task group ID for a revision."""
    index_url = f"{TASKCLUSTER_INDEX}/tasks/gecko.v2.{repo}.revision.{revision}.taskgraph"
    print(f"  Fetching task index for {revision[:12]}...")

    data = await fetch_json(session, index_url)
    if not data.get("tasks"):
        raise Exception(f"No tasks found for revision {revision}")

    task_id = data["tasks"][0]["taskId"]
    task_url = f"{TASKCLUSTER_QUEUE}/task/{task_id}"
    task_data = await fetch_json(session, task_url)

    return task_data["taskGroupId"]


async def get_tasks_in_group(session: aiohttp.ClientSession, task_group_id: str) -> list:
    """Get all tasks in a task group."""
    tasks = []
    continuation_token = None

    while True:
        url = f"{TASKCLUSTER_QUEUE}/task-group/{task_group_id}/list"
        if continuation_token:
            url += f"?continuationToken={continuation_token}"

        data = await fetch_json(session, url)
        tasks.extend(data.get("tasks", []))

        continuation_token = data.get("continuationToken")
        if not continuation_token:
            break

    return tasks


def extract_test_info(task_name: str) -> tuple[str, str]:
    """Extract test name and platform from task name."""
    # Task names look like: test-linux1804-64-shippable-qr/opt-browsertime-tp6-firefox-amazon-e10s
    parts = task_name.split("-browsertime-")
    if len(parts) == 2:
        # Replace / with _ to avoid nested directories
        platform = parts[0].replace("/", "_")
        test_name = "browsertime-" + parts[1]
        return test_name, platform
    return task_name, "unknown"


def filter_browsertime_video_tasks(tasks: list, platforms: list[str] | None = None) -> list[dict]:
    """Filter tasks to only browsertime tests with video artifacts. Deduplicates by test/platform."""
    filtered = []
    seen = set()  # Track test_name/platform combos to avoid duplicates

    for task in tasks:
        task_name = task.get("task", {}).get("metadata", {}).get("name", "")

        # Must be a browsertime test
        if "browsertime" not in task_name:
            continue

        # Skip profiling tasks
        if "profiling" in task_name:
            continue

        # Must be completed successfully
        status = task.get("status", {}).get("state")
        if status != "completed":
            continue

        # Check platform filter
        if platforms:
            platform_match = False
            for p in platforms:
                if p.lower() in task_name.lower():
                    platform_match = True
                    break
            if not platform_match:
                continue

        # Deduplicate: only keep first task per test/platform combo
        test_name, platform = extract_test_info(task_name)
        key = f"{platform}:{test_name}"
        if key in seen:
            continue
        seen.add(key)

        filtered.append(task)

    return filtered


async def download_artifact(
    session: aiohttp.ClientSession,
    task_id: str,
    artifact_name: str,
    output_path: Path,
    semaphore: asyncio.Semaphore,
    progress_callback=None
) -> bool:
    """Download a single artifact."""
    async with semaphore:
        url = f"{TASKCLUSTER_QUEUE}/task/{task_id}/artifacts/{artifact_name}"

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return False

                output_path.parent.mkdir(parents=True, exist_ok=True)

                with open(output_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        f.write(chunk)

                if progress_callback:
                    progress_callback()

                return True
        except Exception as e:
            print(f"    Error downloading {artifact_name}: {e}")
            return False


async def download_video_artifacts(
    session: aiohttp.ClientSession,
    video_tasks: list[VideoTask],
    output_dir: Path,
    max_concurrent: int = 10
) -> dict[str, list[Path]]:
    """Download video artifacts for all tasks."""
    semaphore = asyncio.Semaphore(max_concurrent)
    results = {"base": [], "new": []}

    total = len(video_tasks)
    completed = 0

    def progress():
        nonlocal completed
        completed += 1
        print(f"\r  Downloaded {completed}/{total} artifacts...", end="", flush=True)

    async def download_task_videos(vt: VideoTask) -> list[Path]:
        """Download videos for a single task."""
        downloaded = []

        # Try different artifact names (note: .tgz extension)
        artifact_names = [
            "public/test_info/browsertime-videos-original.tgz",
            "public/test_info/browsertime-videos-annotated.tgz",
            "public/test_info/browsertime-videos.tgz",
        ]

        task_dir = output_dir / vt.label / vt.platform / vt.test_name

        for artifact_name in artifact_names:
            tar_path = task_dir / f"{vt.task_id}.tar.gz"

            success = await download_artifact(
                session, vt.task_id, artifact_name, tar_path, semaphore, progress
            )

            if success:
                # Extract the tar.gz
                try:
                    extract_dir = task_dir / vt.task_id
                    extract_dir.mkdir(parents=True, exist_ok=True)

                    with tarfile.open(tar_path, "r:gz") as tar:
                        tar.extractall(extract_dir)

                    # Find MP4 files
                    for mp4 in extract_dir.rglob("*.mp4"):
                        downloaded.append(mp4)

                    # Clean up tar file
                    tar_path.unlink()

                except Exception as e:
                    print(f"    Error extracting {tar_path}: {e}")

                break

        return downloaded

    # Download all in parallel
    tasks_to_run = [download_task_videos(vt) for vt in video_tasks]
    all_results = await asyncio.gather(*tasks_to_run, return_exceptions=True)

    print()  # Newline after progress

    for vt, result in zip(video_tasks, all_results):
        if isinstance(result, Exception):
            print(f"    Failed: {vt.test_name} - {result}")
        elif result:
            results[vt.label].extend(result)

    return results


def organize_videos_for_comparison(output_dir: Path) -> dict:
    """Organize downloaded videos into a structure for the viewer."""
    comparisons = {}

    base_dir = output_dir / "base"
    new_dir = output_dir / "new"

    if not base_dir.exists() or not new_dir.exists():
        return comparisons

    # Find matching test/platform combinations
    for platform_dir in base_dir.iterdir():
        if not platform_dir.is_dir():
            continue
        platform = platform_dir.name

        for test_dir in platform_dir.iterdir():
            if not test_dir.is_dir():
                continue
            test_name = test_dir.name

            # Check if new also has this test/platform
            new_test_dir = new_dir / platform / test_name
            if not new_test_dir.exists():
                continue

            # Find MP4 files in both
            base_videos = list(test_dir.rglob("*.mp4"))
            new_videos = list(new_test_dir.rglob("*.mp4"))

            if base_videos and new_videos:
                key = f"{platform}/{test_name}"
                comparisons[key] = {
                    "platform": platform,
                    "test_name": test_name,
                    "base_videos": [str(v) for v in sorted(base_videos)],
                    "new_videos": [str(v) for v in sorted(new_videos)],
                }

    return comparisons


async def main():
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
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "revisions",
        nargs="+",
        help="Perfcompare URL, or base and new revisions/URLs"
    )
    parser.add_argument(
        "--platforms", "-p",
        help="Comma-separated platform filters (e.g., linux,windows)",
        default=None
    )
    parser.add_argument(
        "--tests", "-t",
        help="Comma-separated test name filters (e.g., amazon,google)",
        default=None
    )
    parser.add_argument(
        "--output", "-o",
        help="Output directory for videos",
        default="./sxs_videos"
    )
    parser.add_argument(
        "--max-downloads", "-m",
        help="Maximum concurrent downloads",
        type=int,
        default=MAX_CONCURRENT_DOWNLOADS
    )
    parser.add_argument(
        "--serve",
        help="Start Flask server after download",
        action="store_true"
    )
    parser.add_argument(
        "--port",
        help="Port for Flask server",
        type=int,
        default=5000
    )

    args = parser.parse_args()

    print("Parsing revisions...")
    try:
        if len(args.revisions) == 1:
            try:
                base_push, new_push = parse_perfcompare_url(args.revisions[0])
                print(f"  Parsed perfcompare URL")
            except ValueError:
                print(f"Error: Single argument must be a perfcompare URL")
                print(f"  Expected: https://perf.compare/compare-results?baseRev=...&newRev=...")
                print(f"  Or provide two separate revisions/URLs")
                sys.exit(1)
        elif len(args.revisions) == 2:
            base_push = parse_try_url(args.revisions[0])
            new_push = parse_try_url(args.revisions[1])
        else:
            print(f"Error: Expected 1 perfcompare URL or 2 revisions, got {len(args.revisions)} arguments")
            sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print(f"  Base: {base_push.revision[:12]} ({base_push.repo})")
    print(f"  New:  {new_push.revision[:12]} ({new_push.repo})")

    # Parse filters
    platforms = args.platforms.split(",") if args.platforms else None
    test_filters = args.tests.split(",") if args.tests else None

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    max_concurrent = args.max_downloads

    async with aiohttp.ClientSession() as session:
        # Find task group IDs
        print("\nFinding task groups...")
        base_push.task_group_id = await find_task_group_id(
            session, base_push.revision, base_push.repo
        )
        new_push.task_group_id = await find_task_group_id(
            session, new_push.revision, new_push.repo
        )
        print(f"  Base task group: {base_push.task_group_id}")
        print(f"  New task group:  {new_push.task_group_id}")

        # Get tasks in both groups
        print("\nFetching task lists...")
        base_tasks, new_tasks = await asyncio.gather(
            get_tasks_in_group(session, base_push.task_group_id),
            get_tasks_in_group(session, new_push.task_group_id)
        )
        print(f"  Base: {len(base_tasks)} total tasks")
        print(f"  New:  {len(new_tasks)} total tasks")

        # Filter to browsertime video tasks
        print("\nFiltering browsertime tasks...")
        base_bt = filter_browsertime_video_tasks(base_tasks, platforms)
        new_bt = filter_browsertime_video_tasks(new_tasks, platforms)

        # Apply test name filters
        if test_filters:
            base_bt = [t for t in base_bt if any(
                f.lower() in t["task"]["metadata"]["name"].lower()
                for f in test_filters
            )]
            new_bt = [t for t in new_bt if any(
                f.lower() in t["task"]["metadata"]["name"].lower()
                for f in test_filters
            )]

        print(f"  Base: {len(base_bt)} browsertime tasks")
        print(f"  New:  {len(new_bt)} browsertime tasks")

        if not base_bt or not new_bt:
            print("\nNo matching browsertime tasks found!")
            sys.exit(1)

        # Build list of video tasks to download
        video_tasks = []

        for task in base_bt:
            task_name = task["task"]["metadata"]["name"]
            test_name, platform = extract_test_info(task_name)
            video_tasks.append(VideoTask(
                task_id=task["status"]["taskId"],
                test_name=test_name,
                platform=platform,
                label="base",
                revision=base_push.revision
            ))

        for task in new_bt:
            task_name = task["task"]["metadata"]["name"]
            test_name, platform = extract_test_info(task_name)
            video_tasks.append(VideoTask(
                task_id=task["status"]["taskId"],
                test_name=test_name,
                platform=platform,
                label="new",
                revision=new_push.revision
            ))

        print(f"\nDownloading {len(video_tasks)} video artifacts...")
        results = await download_video_artifacts(session, video_tasks, output_dir, max_concurrent)

        print(f"\nDownloaded:")
        print(f"  Base: {len(results['base'])} videos")
        print(f"  New:  {len(results['new'])} videos")

    # Organize for comparison
    comparisons = organize_videos_for_comparison(output_dir)

    # Save comparison metadata
    meta_path = output_dir / "comparisons.json"
    with open(meta_path, "w") as f:
        json.dump({
            "base_revision": base_push.revision,
            "new_revision": new_push.revision,
            "comparisons": comparisons
        }, f, indent=2)

    print(f"\nFound {len(comparisons)} test/platform combinations for comparison")
    print(f"Metadata saved to: {meta_path}")

    if args.serve:
        print(f"\nStarting viewer at http://localhost:{args.port}")
        from viewer import create_app
        app = create_app(output_dir)
        app.run(host="0.0.0.0", port=args.port, debug=False)
    else:
        print(f"\nTo view videos, run:")
        print(f"  python viewer.py {output_dir}")


if __name__ == "__main__":
    asyncio.run(main())

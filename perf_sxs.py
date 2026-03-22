#!/usr/bin/env python3
"""
Async side-by-side video comparison tool for Mozilla Try pushes.

Usage:
    python perf_sxs.py <perfcompare-url> [options]
    python perf_sxs.py <base-revision> <new-revision> [options]

Example with perfcompare URL (recommended):
    python perf_sxs.py \
        "https://perf.compare/compare-results?baseRev=881d2bbf...&newRev=56290454..."

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
import json
import re
import sys
import tarfile
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp

TASKCLUSTER_ROOT = "https://firefox-ci-tc.services.mozilla.com/api"
TASKCLUSTER_QUEUE = f"{TASKCLUSTER_ROOT}/queue/v1"
TASKCLUSTER_INDEX = f"{TASKCLUSTER_ROOT}/index/v1"

MAX_CONCURRENT_DOWNLOADS = 10


@dataclass
class TryPush:
    revision: str
    repo: str = "try"
    task_group_id: str | None = None


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
                TryPush(revision=new_rev, repo=new_repo),
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


def load_high_confidence_from_file(json_path: Path) -> set[tuple[str, str]]:
    """
    Load perfcompare JSON from local file and extract (suite, platform) pairs with High confidence.
    """
    with open(json_path) as f:
        data = json.load(f)

    high_conf_tests = set()

    for item in data:
        for _test_name, test_data in item.items():
            for result in test_data:
                if result.get("confidence_text") == "High":
                    suite = result.get("suite")
                    platform = result.get("platform")
                    if suite and platform:
                        high_conf_tests.add((suite, platform))

    return high_conf_tests


async def fetch_perfcompare_data_from_treeherder(
    session: aiohttp.ClientSession, perfcompare_url: str
) -> set[tuple[str, str]]:
    """
    Fetch performance comparison data from Treeherder API and extract high confidence tests.

    Uses the Treeherder API endpoint that PerfCompare itself uses:
    https://treeherder.mozilla.org/api/perfcompare/results/
    """
    parsed = urlparse(perfcompare_url)
    params = parse_qs(parsed.query)

    base_rev = params.get("baseRev", [""])[0]
    base_repo = params.get("baseRepo", ["mozilla-central"])[0]
    new_rev = params.get("newRev", [""])[0]
    new_repo = params.get("newRepo", ["mozilla-central"])[0]
    framework = params.get("framework", ["1"])[0]
    test_version = params.get("test_version", ["student-t"])[0]
    replicates = params.get("replicates", ["false"])[0]

    api_url = "https://treeherder.mozilla.org/api/perfcompare/results/"
    api_params = {
        "base_repository": base_repo,
        "base_revision": base_rev,
        "new_repository": new_repo,
        "new_revision": new_rev,
        "framework": framework,
        "no_subtests": "true",
        "replicates": replicates,
        "test_version": test_version,
    }

    query_string = "&".join(f"{k}={v}" for k, v in api_params.items())
    full_url = f"{api_url}?{query_string}"

    try:
        print("  Calling Treeherder API...")
        async with session.get(full_url) as resp:
            if resp.status != 200:
                print(f"  Error: Treeherder API returned status {resp.status}")
                return set()

            results = await resp.json()

            if not isinstance(results, list):
                print("  Error: Unexpected API response format")
                return set()

            high_conf_tests = set()

            for result in results:
                if result.get("confidence_text") == "High":
                    suite = result.get("suite")
                    platform = result.get("platform")
                    if suite and platform:
                        high_conf_tests.add((suite, platform))

            return high_conf_tests

    except Exception as e:
        print(f"  Error fetching from Treeherder API: {e}")
        print("  Fallback: Use --confidence-json with manually downloaded JSON")
        return set()


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


def extract_suite_and_platform(task_name: str) -> tuple[str, str]:
    """
    Extract suite name and platform for perfcompare matching.

    Task name: test-linux1804-64-shippable-qr/opt-browsertime-tp6-firefox-amazon-e10s
    Returns: ("amazon", "linux1804-64-shippable-qr")
    """
    parts = task_name.split("-browsertime-")
    if len(parts) != 2:
        return "", ""

    platform_part = parts[0].replace("test-", "")
    platform = platform_part.split("/")[0]

    test_part = parts[1]
    suite_parts = test_part.split("-firefox-")
    if len(suite_parts) == 2:
        after_firefox = suite_parts[1]

        known_suffixes = [
            "-e10s",
            "-fission",
            "-live",
            "-cold",
            "-warm",
            "-webrender",
            "-bytecode-cached",
            "-nofis",
        ]

        suite = after_firefox
        for suffix in known_suffixes:
            if suffix in suite:
                suite = suite.split(suffix)[0]

        return suite, platform

    return "", ""


def filter_browsertime_video_tasks(
    tasks: list,
    platforms: list[str] | None = None,
    high_conf_tests: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """
    Filter tasks to only browsertime tests with video artifacts. Deduplicates by test/platform.

    Args:
        tasks: List of TaskCluster tasks
        platforms: Optional list of platform filters (e.g., ["linux", "windows"])
        high_conf_tests: Optional set of (suite, platform) tuples from perfcompare with High confidence
    """
    filtered = []
    seen = set()

    for task in tasks:
        task_name = task.get("task", {}).get("metadata", {}).get("name", "")

        if "browsertime" not in task_name:
            continue

        if "profiling" in task_name:
            continue

        status = task.get("status", {}).get("state")
        if status != "completed":
            continue

        if platforms:
            platform_match = False
            for p in platforms:
                if p.lower() in task_name.lower():
                    platform_match = True
                    break
            if not platform_match:
                continue

        if high_conf_tests:
            suite, platform = extract_suite_and_platform(task_name)
            if not suite or not platform:
                continue
            if (suite, platform) not in high_conf_tests:
                continue

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
    progress_callback=None,
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


async def fetch_perfherder_data(
    session: aiohttp.ClientSession,
    task_id: str,
    semaphore: asyncio.Semaphore,
) -> dict | None:
    """Download perfherder-data.json artifact for a task."""
    url = f"{TASKCLUSTER_QUEUE}/task/{task_id}/artifacts/public/perfherder-data.json"
    async with semaphore:
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
        except Exception:
            return None


def find_median_run_index(data: dict) -> int:
    """Find the run index whose replicate value is closest to the median (subtest value)."""
    try:
        subtests = data["suites"][0]["subtests"]
        if not subtests:
            return 0
        replicates = subtests[0]["replicates"]
        median_val = subtests[0]["value"]
        if len(replicates) <= 1:
            return 0
        return min(range(len(replicates)), key=lambda i: abs(replicates[i] - median_val))
    except (KeyError, IndexError, TypeError):
        return 0


async def download_video_artifacts(
    session: aiohttp.ClientSession,
    video_tasks: list[VideoTask],
    output_dir: Path,
    max_concurrent: int = 10,
    all_runs: bool = False,
) -> dict[str, list[Path]]:
    """Download video artifacts for all tasks."""
    semaphore = asyncio.Semaphore(max_concurrent)
    results: dict[str, list[Path]] = {"base": [], "new": []}

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
            "public/test_info/browsertime-videos-annotated.tgz",
            "public/test_info/browsertime-videos-original.tgz",
            "public/test_info/browsertime-videos.tgz",
        ]

        task_dir = output_dir / vt.label / vt.platform / vt.test_name

        for artifact_name in artifact_names:
            tar_path = task_dir / f"{vt.task_id}.tar.gz"

            success = await download_artifact(
                session, vt.task_id, artifact_name, tar_path, semaphore, progress
            )

            if success:
                try:
                    extract_dir = task_dir / vt.task_id
                    extract_dir.mkdir(parents=True, exist_ok=True)

                    with tarfile.open(tar_path, "r:gz") as tar:
                        tar.extractall(extract_dir)

                    mp4s = sorted(extract_dir.rglob("*.mp4"))

                    ph_data = await fetch_perfherder_data(session, vt.task_id, semaphore)
                    median_idx = find_median_run_index(ph_data) if ph_data else 0
                    median_idx = min(median_idx, len(mp4s) - 1) if mp4s else 0

                    if all_runs:
                        downloaded = list(mp4s)
                        # Write sidecar so viewer can label the median run
                        (extract_dir / "median_idx.txt").write_text(str(median_idx))
                    else:
                        # Keep only the median video
                        for i, mp4 in enumerate(mp4s):
                            if i != median_idx:
                                mp4.unlink()
                        if mp4s:
                            downloaded = [mp4s[median_idx]]

                    tar_path.unlink()

                except Exception as e:
                    print(f"    Error extracting {tar_path}: {e}")

                break

        return downloaded

    # Download all in parallel
    tasks_to_run = [download_task_videos(vt) for vt in video_tasks]
    all_results = await asyncio.gather(*tasks_to_run, return_exceptions=True)

    print()  # Newline after progress

    for vt, result in zip(video_tasks, all_results, strict=False):
        if isinstance(result, Exception):
            print(f"    Failed: {vt.test_name} - {result}")
        elif isinstance(result, list):
            results[vt.label].extend(result)

    return results


def read_median_idx(test_dir: Path) -> int | None:
    """Read median_idx.txt sidecar written during --all-runs download."""
    for task_dir in test_dir.iterdir():
        if task_dir.is_dir():
            idx_file = task_dir / "median_idx.txt"
            if idx_file.exists():
                try:
                    return int(idx_file.read_text().strip())
                except ValueError:
                    return None
    return None


def organize_videos_for_comparison(output_dir: Path) -> dict:
    """Organize downloaded videos into a structure for the viewer."""
    comparisons: dict[str, dict] = {}

    base_dir = output_dir / "base"
    new_dir = output_dir / "new"

    if not base_dir.exists() or not new_dir.exists():
        return comparisons

    for platform_dir in base_dir.iterdir():
        if not platform_dir.is_dir():
            continue
        platform = platform_dir.name

        for test_dir in platform_dir.iterdir():
            if not test_dir.is_dir():
                continue
            test_name = test_dir.name

            new_test_dir = new_dir / platform / test_name
            if not new_test_dir.exists():
                continue

            base_videos = sorted(test_dir.rglob("*.mp4"))
            new_videos = sorted(new_test_dir.rglob("*.mp4"))

            if base_videos and new_videos:
                key = f"{platform}/{test_name}"
                comparisons[key] = {
                    "platform": platform,
                    "test_name": test_name,
                    "base_videos": [str(v.relative_to(output_dir)) for v in base_videos],
                    "new_videos": [str(v.relative_to(output_dir)) for v in new_videos],
                    "base_median_idx": read_median_idx(test_dir),
                    "new_median_idx": read_median_idx(new_test_dir),
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
        "--port", help="Port for Flask server (default: 3333)", type=int, default=3333
    )

    args = parser.parse_args()

    print("Parsing revisions...")
    perfcompare_url = None
    try:
        if len(args.revisions) == 1:
            try:
                base_push, new_push = parse_perfcompare_url(args.revisions[0])
                perfcompare_url = args.revisions[0]
                print("  Parsed perfcompare URL")
            except ValueError:
                print("Error: Single argument must be a perfcompare URL")
                print("  Expected: https://perf.compare/compare-results?baseRev=...&newRev=...")
                print("  Or provide two separate revisions/URLs")
                sys.exit(1)
        elif len(args.revisions) == 2:
            base_push = parse_try_url(args.revisions[0])
            new_push = parse_try_url(args.revisions[1])
        else:
            print(
                f"Error: Expected 1 perfcompare URL or 2 revisions, got {len(args.revisions)} arguments"
            )
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
        new_push.task_group_id = await find_task_group_id(session, new_push.revision, new_push.repo)
        print(f"  Base task group: {base_push.task_group_id}")
        print(f"  New task group:  {new_push.task_group_id}")

        # Get tasks in both groups
        print("\nFetching task lists...")
        base_tasks, new_tasks = await asyncio.gather(
            get_tasks_in_group(session, base_push.task_group_id),
            get_tasks_in_group(session, new_push.task_group_id),
        )
        print(f"  Base: {len(base_tasks)} total tasks")
        print(f"  New:  {len(new_tasks)} total tasks")

        high_conf_tests = None
        if args.confidence_json and not args.all_tests:
            print(f"\nLoading high confidence tests from local file: {args.confidence_json}")
            json_path = Path(args.confidence_json)
            if json_path.exists():
                high_conf_tests = load_high_confidence_from_file(json_path)
                print(f"  Found {len(high_conf_tests)} high confidence test/platform combinations")
                print(f"  Unique suites: {sorted({s for s, p in high_conf_tests})}")
                print("  Will only download videos for high confidence changes")
            else:
                print(f"  Error: File not found: {args.confidence_json}")
        elif perfcompare_url and not args.all_tests:
            print("\nFetching high confidence tests from Treeherder API...")
            high_conf_tests = await fetch_perfcompare_data_from_treeherder(session, perfcompare_url)
            if high_conf_tests:
                print(f"  Found {len(high_conf_tests)} high confidence test/platform combinations")
                print(f"  Unique suites: {sorted({s for s, p in high_conf_tests})}")
                print("  Will only download videos for high confidence changes")
            else:
                print("  No high confidence filter applied (API fetch may have failed)")
        elif args.all_tests:
            print("\n--all-tests flag set: downloading all tests (ignoring confidence filter)")

        # Filter to browsertime video tasks
        print("\nFiltering browsertime tasks...")
        base_bt = filter_browsertime_video_tasks(base_tasks, platforms, high_conf_tests)
        new_bt = filter_browsertime_video_tasks(new_tasks, platforms, high_conf_tests)

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
            video_tasks.append(
                VideoTask(
                    task_id=task["status"]["taskId"],
                    test_name=test_name,
                    platform=platform,
                    label="base",
                    revision=base_push.revision,
                )
            )

        for task in new_bt:
            task_name = task["task"]["metadata"]["name"]
            test_name, platform = extract_test_info(task_name)
            video_tasks.append(
                VideoTask(
                    task_id=task["status"]["taskId"],
                    test_name=test_name,
                    platform=platform,
                    label="new",
                    revision=new_push.revision,
                )
            )

        print(f"\nDownloading {len(video_tasks)} video artifacts...")
        if not args.all_runs:
            print("  (median run only — use --all-runs to download all)")
        results = await download_video_artifacts(
            session, video_tasks, output_dir, max_concurrent, all_runs=args.all_runs
        )

        print("\nDownloaded:")
        print(f"  Base: {len(results['base'])} videos")
        print(f"  New:  {len(results['new'])} videos")

    # Organize for comparison
    comparisons = organize_videos_for_comparison(output_dir)

    # Save comparison metadata
    meta_path = output_dir / "comparisons.json"
    with open(meta_path, "w") as f:
        json.dump(
            {
                "base_revision": base_push.revision,
                "new_revision": new_push.revision,
                "comparisons": comparisons,
            },
            f,
            indent=2,
        )

    print(f"\nFound {len(comparisons)} test/platform combinations for comparison")
    print(f"Metadata saved to: {meta_path}")

    if not args.no_serve:
        url = f"http://localhost:{args.port}"
        print(f"\nStarting viewer at {url}")
        from viewer import create_app

        app = create_app(output_dir)

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        app.run(host="0.0.0.0", port=args.port, debug=False)
    else:
        print("\nTo view videos later, run:")
        print(f"  python viewer.py {output_dir}")


if __name__ == "__main__":
    asyncio.run(main())

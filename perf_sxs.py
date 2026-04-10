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
import shutil
import sys
import tarfile
import threading
import time
import webbrowser
import zipfile
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
    task_group_ids: list[str] | None = None


@dataclass
class VideoTask:
    task_id: str
    test_name: str
    platform: str
    label: str  # "base" or "new"
    revision: str
    task_type: str = "browsertime"  # "browsertime" or "perftest"


LANDO_API = "https://api.lando.services.mozilla.com"


def parse_lando_url(url: str) -> tuple[str, str, str, str, dict]:
    """Extract baseLando, newLando IDs, repos, and extra params from a perfcompare lando URL."""
    parsed = urlparse(url)
    if "perf.compare" not in parsed.netloc and "perfcompare" not in parsed.netloc:
        raise ValueError(f"Not a perfcompare URL: {url}")

    params = parse_qs(parsed.query)
    base_id = params.get("baseLando", [None])[0]
    new_id = params.get("newLando", [None])[0]

    if not base_id or not new_id:
        raise ValueError(f"Could not parse lando IDs from URL: {url}")

    base_repo = params.get("baseRepo", ["try"])[0]
    new_repo = params.get("newRepo", ["try"])[0]
    extra = {
        k: v[0]
        for k, v in params.items()
        if k not in ("baseLando", "newLando", "baseRepo", "newRepo")
    }
    return base_id, new_id, base_repo, new_repo, extra


async def resolve_lando_id(session: aiohttp.ClientSession, lando_id: str) -> str:
    """Resolve a Lando landing job ID to a revision hash via the Lando API."""
    url = f"{LANDO_API}/landing_jobs/{lando_id}"
    async with session.get(url) as resp:
        if resp.status != 200:
            raise Exception(f"Lando API returned HTTP {resp.status} for job {lando_id}")
        data = await resp.json(content_type=None)
        commit_id = data.get("commit_id")
        if not commit_id or not isinstance(commit_id, str):
            raise Exception(
                f"No commit_id in Lando response for job {lando_id} (job may still be pending)"
            )
        return commit_id


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


async def find_task_group_ids(
    session: aiohttp.ClientSession, revision: str, repo: str
) -> list[str]:
    """Find all task group IDs for a revision.

    A revision can have multiple task groups (e.g. main CI + perf push on mozilla-central).
    We page through all indexed tasks and collect every unique group ID.
    """
    index_url = f"{TASKCLUSTER_INDEX}/tasks/gecko.v2.{repo}.revision.{revision}.taskgraph"
    print(f"  Fetching task index for {revision[:12]}...")

    data = await fetch_json(session, index_url)
    indexed_tasks = data.get("tasks", [])
    if not indexed_tasks:
        raise Exception(f"No tasks found for revision {revision}")

    seen: set[str] = set()
    group_ids: list[str] = []
    for entry in indexed_tasks:
        task_id = entry["taskId"]
        task_data = await fetch_json(session, f"{TASKCLUSTER_QUEUE}/task/{task_id}")
        gid = task_data["taskGroupId"]
        if gid not in seen:
            seen.add(gid)
            group_ids.append(gid)

    return group_ids


async def _get_tasks_in_group(session: aiohttp.ClientSession, task_group_id: str) -> list:
    """Get all tasks in a single task group."""
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


async def get_tasks_for_revision(session: aiohttp.ClientSession, group_ids: list[str]) -> list:
    """Aggregate tasks from all task groups, deduplicating by task ID."""
    results = await asyncio.gather(*[_get_tasks_in_group(session, gid) for gid in group_ids])
    seen: set[str] = set()
    all_tasks: list = []
    for task_list in results:
        for task in task_list:
            task_id = task.get("status", {}).get("taskId", "")
            if task_id and task_id not in seen:
                seen.add(task_id)
                all_tasks.append(task)
    return all_tasks


def extract_test_info(task_name: str) -> tuple[str, str]:
    """Extract test name and platform from task name."""
    if task_name.startswith("perftest-"):
        return _extract_test_info_perftest(task_name)
    # Task names look like: test-linux1804-64-shippable-qr/opt-browsertime-tp6-firefox-amazon-e10s
    parts = task_name.split("-browsertime-")
    if len(parts) == 2:
        # Replace / with _ to avoid nested directories
        platform = parts[0].replace("/", "_")
        test_name = "browsertime-" + parts[1]
        return test_name, platform
    return task_name, "unknown"


def _extract_test_info_perftest(task_name: str) -> tuple[str, str]:
    """Extract test name and platform from a perftest task name.

    Handles:
      perftest-android-hw-a55-aarch64-shippable/opt-startup-fenix-homeview-startup
      perftest-android-hw-a55-aarch64-shippable-startup-fenix-homeview-startup
    """
    if "/" in task_name:
        platform_raw, rest = task_name.split("/", 1)
        platform = platform_raw.replace("/", "_")
        test_name = re.sub(r"^(opt|debug|shippable)[_-]", "", rest)
        return test_name, platform

    # Flat format: split on known platform-terminating suffix
    name = task_name
    for suffix in ("-shippable-", "-opt-", "-debug-"):
        idx = name.rfind(suffix)
        if idx != -1:
            platform = name[: idx + len(suffix) - 1]
            test_name = name[idx + len(suffix) :]
            return test_name, platform

    return task_name, "unknown"


def extract_suite_and_platform(task_name: str) -> tuple[str, str]:
    """
    Extract suite name and platform for perfcompare matching.

    Task name: test-linux1804-64-shippable-qr/opt-browsertime-tp6-firefox-amazon-e10s
    Returns: ("amazon", "linux1804-64-shippable-qr")
    """
    if task_name.startswith("perftest-"):
        test_name, platform = _extract_test_info_perftest(task_name)
        # Strip perftest- prefix from platform for Treeherder matching
        platform_clean = re.sub(r"^perftest-", "", platform)
        return test_name, platform_clean

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


def _is_video_task(task_name: str) -> bool:
    """Return True if this task produces video artifacts we can download."""
    return ("browsertime" in task_name and "profiling" not in task_name) or (
        task_name.startswith("perftest-") and "-startup-" in task_name
    )


def _matches_high_conf(task_name: str, high_conf_tests: set[tuple[str, str]]) -> bool:
    """Check if a task matches the high-confidence filter set."""
    suite, platform = extract_suite_and_platform(task_name)
    if suite and platform:
        if (suite, platform) in high_conf_tests:
            return True
        # For perftest tasks, also try platform-substring matching since TC names
        # and Treeherder names may differ slightly.
        if task_name.startswith("perftest-"):
            return any(
                (platform.lower() in p.lower() or p.lower() in platform.lower())
                and (suite.lower() in s.lower() or s.lower() in suite.lower())
                for s, p in high_conf_tests
            )
        return False
    # Can't determine suite/platform — include perftest tasks, skip others
    return task_name.startswith("perftest-")


def filter_video_tasks(
    tasks: list,
    platforms: list[str] | None = None,
    high_conf_tests: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """
    Filter tasks to browsertime or perftest-startup tasks with video artifacts.
    Deduplicates by test/platform.
    """
    filtered = []
    seen = set()

    for task in tasks:
        task_name = task.get("task", {}).get("metadata", {}).get("name", "")

        if not _is_video_task(task_name):
            continue

        status = task.get("status", {}).get("state")
        if status != "completed":
            continue

        if platforms:
            platform_match = any(p.lower() in task_name.lower() for p in platforms)
            if not platform_match:
                continue

        if high_conf_tests and not _matches_high_conf(task_name, high_conf_tests):
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


async def list_task_artifacts(
    session: aiohttp.ClientSession,
    task_id: str,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    """Return artifact list for a task: [{name, contentType, ...}]."""
    url = f"{TASKCLUSTER_QUEUE}/task/{task_id}/artifacts"
    async with semaphore:
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
                return data.get("artifacts", [])
        except Exception:
            return []


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
) -> dict[str, dict[str, list[Path]]]:
    """Download video (and image) artifacts for all tasks.

    Returns {"base": {"videos": [...], "images": [...]}, "new": {...}}.
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    results: dict[str, dict[str, list[Path]]] = {
        "base": {"videos": [], "images": []},
        "new": {"videos": [], "images": []},
    }

    total = len(video_tasks)
    completed = 0

    def progress():
        nonlocal completed
        completed += 1
        print(f"\r  Downloaded {completed}/{total} artifacts...", end="", flush=True)

    async def _extract_tgz_media(
        tar_path: Path, extract_dir: Path, vt: VideoTask
    ) -> dict[str, list[Path]]:
        """Extract a tgz, apply median selection for videos, return {videos, images}."""
        extract_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(extract_dir)

        mp4s = sorted(extract_dir.rglob("*.mp4"))
        pngs = sorted(extract_dir.rglob("*.png"))

        ph_data = await fetch_perfherder_data(session, vt.task_id, semaphore)
        median_idx = find_median_run_index(ph_data) if ph_data else 0
        median_idx = min(median_idx, len(mp4s) - 1) if mp4s else 0

        image_groups = _group_images_by_video(mp4s, pngs)

        if all_runs:
            videos = list(mp4s)
            if mp4s:
                (extract_dir / "median_idx.txt").write_text(str(median_idx))
        else:
            keep_images = set(image_groups[median_idx]) if median_idx < len(image_groups) else set()
            for i, mp4 in enumerate(mp4s):
                if i != median_idx:
                    mp4.unlink()
            for png in pngs:
                if png not in keep_images:
                    png.unlink()
            videos = [mp4s[median_idx]] if mp4s else []
            pngs = sorted(keep_images)

        tar_path.unlink()
        return {"videos": videos, "images": pngs}

    async def download_task_videos(vt: VideoTask) -> dict[str, list[Path]]:
        task_dir = output_dir / vt.label / vt.platform / vt.test_name

        # Try the standard tgz artifact paths (used by both browsertime and perftest)
        tgz_candidates = [
            "public/test_info/browsertime-videos-annotated.tgz",
            "public/test_info/browsertime-videos-original.tgz",
            "public/test_info/browsertime-videos.tgz",
        ]

        for artifact_name in tgz_candidates:
            tar_path = task_dir / f"{vt.task_id}.tar.gz"
            success = await download_artifact(
                session,
                vt.task_id,
                artifact_name,
                tar_path,
                semaphore,
                progress if vt.task_type == "browsertime" else None,
            )
            if success:
                try:
                    result = await _extract_tgz_media(tar_path, task_dir / vt.task_id, vt)
                    if vt.task_type == "perftest":
                        progress()
                    return result
                except Exception as e:
                    print(f"    Error extracting {tar_path}: {e}")
                break

        if vt.task_type != "perftest":
            return {"videos": [], "images": []}

        # Perftest fallback: discover artifacts via the TC artifacts list API
        artifacts = await list_task_artifacts(session, vt.task_id, semaphore)
        artifact_names_list = [a["name"] for a in artifacts]

        # Exclude known build binary filenames and log/toolchain archives
        excluded_names = {"target.tar.bz2", "target.zip", "target.apk", "build.tar.gz"}
        excluded_words = {"log", "mozharness", "sdk", "crashreporter"}
        archives = [
            n
            for n in artifact_names_list
            if n.startswith("public/")
            and (n.endswith(".tgz") or n.endswith(".zip"))
            and Path(n).name not in excluded_names
            and not any(x in n.lower() for x in excluded_words)
        ]
        direct_mp4 = [n for n in artifact_names_list if n.endswith(".mp4")]
        direct_png = [n for n in artifact_names_list if n.endswith(".png")]

        if not archives and not direct_mp4 and not direct_png:
            progress()
            return {"videos": [], "images": []}

        extract_dir = task_dir / vt.task_id
        extract_dir.mkdir(parents=True, exist_ok=True)

        if archives:
            archive = archives[0]
            ext = ".zip" if archive.endswith(".zip") else ".tgz"
            archive_path = task_dir / f"{vt.task_id}{ext}"
            success = await download_artifact(session, vt.task_id, archive, archive_path, semaphore)
            if success:
                try:
                    if ext == ".tgz":
                        with tarfile.open(archive_path, "r:gz") as tar:
                            tar.extractall(extract_dir)
                    else:
                        with zipfile.ZipFile(archive_path) as zf:
                            zf.extractall(extract_dir)
                    archive_path.unlink()
                    progress()
                    return {
                        "videos": sorted(extract_dir.rglob("*.mp4")),
                        "images": sorted(extract_dir.rglob("*.png")),
                    }
                except Exception as e:
                    print(f"    Error extracting {archive_path}: {e}")

        if direct_mp4 or direct_png:
            videos: list[Path] = []
            images: list[Path] = []
            for remote in direct_mp4:
                local = extract_dir / Path(remote).name
                if await download_artifact(session, vt.task_id, remote, local, semaphore):
                    videos.append(local)
            for remote in direct_png:
                local = extract_dir / Path(remote).name
                if await download_artifact(session, vt.task_id, remote, local, semaphore):
                    images.append(local)
            progress()
            return {"videos": sorted(videos), "images": sorted(images)}

        progress()
        return {"videos": [], "images": []}

    # Download all in parallel
    tasks_to_run = [download_task_videos(vt) for vt in video_tasks]
    all_results = await asyncio.gather(*tasks_to_run, return_exceptions=True)

    print()  # Newline after progress

    for vt, result in zip(video_tasks, all_results, strict=False):
        if isinstance(result, Exception):
            print(f"    Failed: {vt.test_name} - {result}")
        elif isinstance(result, dict):
            results[vt.label]["videos"].extend(result.get("videos", []))
            results[vt.label]["images"].extend(result.get("images", []))

    return results


def _group_images_by_video(mp4s: list[Path], pngs: list[Path]) -> list[list[Path]]:
    """Group PNG images with their corresponding video.

    Uses per-subdirectory grouping when each video is in a distinct directory.
    Falls back to even index-based distribution for flat (same-dir) layouts.
    Returns one sublist per video, possibly empty.
    """
    if not pngs or not mp4s:
        return [[] for _ in mp4s]

    n = len(mp4s)
    mp4_dirs = [mp4.parent for mp4 in mp4s]

    # Per-subdir grouping only works when each mp4 is in its own unique directory
    if len(set(mp4_dirs)) == n:
        grouped = [[p for p in pngs if p.parent == d] for d in mp4_dirs]
        if any(grp for grp in grouped):
            return grouped

    # Flat layout: distribute evenly by index
    per_run = len(pngs) // n
    if per_run:
        return [pngs[i * per_run : (i + 1) * per_run] for i in range(n)]

    return [[pngs[i]] if i < len(pngs) else [] for i in range(n)]


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

            base_images = sorted(test_dir.rglob("*.png"))
            new_images = sorted(new_test_dir.rglob("*.png"))

            if base_videos and new_videos:
                key = f"{platform}/{test_name}"
                base_task_ids = {d.name for d in test_dir.iterdir() if d.is_dir()}
                new_task_ids = {d.name for d in new_test_dir.iterdir() if d.is_dir()}
                same_task = bool(base_task_ids & new_task_ids)
                base_img_groups = _group_images_by_video(base_videos, base_images)
                new_img_groups = _group_images_by_video(new_videos, new_images)
                comparisons[key] = {
                    "platform": platform,
                    "test_name": test_name,
                    "base_videos": [str(v.relative_to(output_dir)) for v in base_videos],
                    "new_videos": [str(v.relative_to(output_dir)) for v in new_videos],
                    "base_median_idx": read_median_idx(test_dir),
                    "new_median_idx": read_median_idx(new_test_dir),
                    "same_task_warning": same_task,
                    "base_images": [
                        [str(p.relative_to(output_dir)) for p in grp] for grp in base_img_groups
                    ],
                    "new_images": [
                        [str(p.relative_to(output_dir)) for p in grp] for grp in new_img_groups
                    ],
                }

    return comparisons


def organize_single_revision(output_dir: Path) -> dict:
    """Organize downloaded videos for single-revision (no-compare) mode."""
    comparisons: dict[str, dict] = {}

    base_dir = output_dir / "base"
    if not base_dir.exists():
        return comparisons

    for platform_dir in base_dir.iterdir():
        if not platform_dir.is_dir():
            continue
        platform = platform_dir.name

        for test_dir in platform_dir.iterdir():
            if not test_dir.is_dir():
                continue
            test_name = test_dir.name

            videos = sorted(test_dir.rglob("*.mp4"))
            images = sorted(test_dir.rglob("*.png"))
            if videos:
                key = f"{platform}/{test_name}"
                img_groups = _group_images_by_video(videos, images)
                comparisons[key] = {
                    "platform": platform,
                    "test_name": test_name,
                    "base_videos": [str(v.relative_to(output_dir)) for v in videos],
                    "base_median_idx": read_median_idx(test_dir),
                    "base_images": [
                        [str(p.relative_to(output_dir)) for p in grp] for grp in img_groups
                    ],
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
        "--no-compare",
        help="Single revision mode: download videos without a comparison target",
        action="store_true",
    )
    parser.add_argument(
        "--port", help="Port for Flask server (default: 3333)", type=int, default=3333
    )

    args = parser.parse_args()

    print("Parsing revisions...")
    perfcompare_url = None
    lando_ids: tuple[str, str, str, str] | None = None
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
                    print("  Parsed lando perfcompare URL (will resolve IDs via Lando API)")
                except ValueError:
                    try:
                        base_push, new_push = parse_perfcompare_url(url)
                        perfcompare_url = url
                        print("  Parsed perfcompare URL")
                    except ValueError:
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
            print(
                f"Error: Expected 1 perfcompare URL or 2 revisions, got {len(args.revisions)} arguments"
            )
            sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Parse filters
    platforms = args.platforms.split(",") if args.platforms else None
    test_filters = args.tests.split(",") if args.tests else None

    output_dir = Path(args.output)
    if output_dir.exists() and any(output_dir.iterdir()):
        print(f"\nOutput directory {output_dir} already exists and will be wiped.")
        print("Press Ctrl-C to cancel, or wait 3 seconds to continue...")
        time.sleep(3)
        shutil.rmtree(output_dir)
        print(f"  Cleared {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    max_concurrent = args.max_downloads

    async with aiohttp.ClientSession() as session:
        # Resolve lando IDs to revision hashes if needed
        if lando_ids:
            base_id, new_id, base_repo, new_repo, extra_params = lando_ids
            print("\nResolving Lando IDs via Lando API...")
            try:
                base_rev, new_rev = await asyncio.gather(
                    resolve_lando_id(session, base_id),
                    resolve_lando_id(session, new_id),
                )
            except Exception as e:
                print(f"  Error: {e}")
                sys.exit(1)
            base_push = TryPush(revision=base_rev, repo=base_repo)
            new_push = TryPush(revision=new_rev, repo=new_repo)
            print(f"  Base lando {base_id} -> {base_rev[:12]}")
            print(f"  New  lando {new_id} -> {new_rev[:12]}")
            # Build synthetic perfcompare URL for confidence filtering
            extra_qs = "&".join(f"{k}={v}" for k, v in extra_params.items())
            perfcompare_url = (
                f"https://perf.compare/compare-results?"
                f"baseRev={base_rev}&baseRepo={base_repo}"
                f"&newRev={new_rev}&newRepo={new_repo}" + (f"&{extra_qs}" if extra_qs else "")
            )

        print(f"\n  Base: {base_push.revision[:12]} ({base_push.repo})")
        if new_push:
            print(f"  New:  {new_push.revision[:12]} ({new_push.repo})")

        # Find task group IDs
        print("\nFinding task groups...")
        base_push.task_group_ids = await find_task_group_ids(
            session, base_push.revision, base_push.repo
        )
        print(f"  Base task groups: {base_push.task_group_ids}")
        if new_push:
            new_push.task_group_ids = await find_task_group_ids(
                session, new_push.revision, new_push.repo
            )
            print(f"  New task groups:  {new_push.task_group_ids}")

        # Get tasks in groups
        print("\nFetching task lists...")
        if new_push:
            base_tasks, new_tasks = await asyncio.gather(
                get_tasks_for_revision(session, base_push.task_group_ids),
                get_tasks_for_revision(session, new_push.task_group_ids),
            )
            print(f"  Base: {len(base_tasks)} total tasks")
            print(f"  New:  {len(new_tasks)} total tasks")
        else:
            base_tasks = await get_tasks_for_revision(session, base_push.task_group_ids)
            new_tasks = []
            print(f"  Base: {len(base_tasks)} total tasks")

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

        # Filter to video tasks (browsertime + perftest startup)
        print("\nFiltering video tasks...")
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

        print(f"  Base: {len(base_bt)} video tasks")
        if new_push:
            print(f"  New:  {len(new_bt)} video tasks")

        if not base_bt or (new_push and not new_bt):
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

        print(f"\nDownloading {len(video_tasks)} video artifacts...")
        if not args.all_runs:
            print("  (median run only — use --all-runs to download all)")
        results = await download_video_artifacts(
            session, video_tasks, output_dir, max_concurrent, all_runs=args.all_runs
        )

        base_videos = results["base"]["videos"]
        new_videos = results["new"]["videos"]
        base_images = results["base"]["images"]
        new_images = results["new"]["images"]

        print("\nDownloaded:")
        print(
            f"  Base: {len(base_videos)} videos"
            + (f", {len(base_images)} images" if base_images else "")
        )
        print(
            f"  New:  {len(new_videos)} videos"
            + (f", {len(new_images)} images" if new_images else "")
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
            print(f"\n  Missing video artifacts ({len(missing)} tasks):")
            for vt in missing:
                print(f"    [{vt.label}] {vt.platform} / {vt.test_name}")

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

    print(f"\nFound {len(comparisons)} test/platform combinations")
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

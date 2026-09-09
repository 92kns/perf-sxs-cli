"""Candidate selection: which tasks/runs to download.

Covers task-name parsing, high-confidence filtering, and median-run selection.
"""

import json
import re
from pathlib import Path


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

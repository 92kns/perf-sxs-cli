"""Candidate selection: which tasks/runs to download.

Covers task-name parsing, high-confidence filtering, and median-run selection.
"""

import json
import re
import statistics
from pathlib import Path

# perfcompare/Treeherder statistical test versions (the `test_version` query param).
STUDENT_T = "student-t"
MANN_WHITNEY_U = "mann-whitney-u"
TEST_VERSIONS = (STUDENT_T, MANN_WHITNEY_U)

# Browser/app tokens as they appear in task names — mirrors the app lists in
# mozilla-central's testing/raptor/raptor/cmdline.py. Multi-token names must be
# listed so e.g. `--cross-browser fenix,chrome` doesn't match `chrome-m` tasks
# as "chrome" followed by a stray "-m".
KNOWN_APPS = (
    "firefox",
    "chrome",
    "safari",
    "safari-tp",
    "custom-car",
    "geckoview",
    "refbrow",
    "fenix",
    "chrome-m",
    "cstm-car-m",
)


def natural_sort_key(path: Path) -> list:
    """Sort key that orders `2.mp4` before `10.mp4`.

    Browsertime names run videos `1.mp4 … N.mp4`; a plain lexical sort puts
    `10.mp4` second, which misaligns the video list with perfherder's
    replicate order (and therefore the median index) whenever a task has 10+
    runs.
    """
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(path))]


def is_significant_result(result: dict, test_version: str | None = None) -> bool:
    """Whether one perfcompare result row counts as a meaningful change.

    - student-t rows carry ``confidence_text`` ("Low"/"Medium"/"High").
    - mann-whitney-u rows carry no confidence_text; Treeherder instead sets
      ``direction_of_change`` to "regression"/"improvement" only when the MWU
      p-value is significant *and* Cliff's delta is non-negligible
      (otherwise "no change").

    With ``test_version=None`` either signal is accepted (used for local JSON
    files, whose test version isn't known up front).
    """
    high_t = result.get("confidence_text") == "High"
    mwu = result.get("direction_of_change") in ("regression", "improvement")
    if test_version == STUDENT_T:
        return high_t
    if test_version == MANN_WHITNEY_U:
        return mwu
    return high_t or mwu


def find_app_span(test_name: str, app: str) -> tuple[int, int] | None:
    """Locate `app` as a whole dash-delimited token run inside `test_name`.

    Returns (start_token, end_token) or None. A match is rejected when it's
    really the prefix of a longer known app (`chrome` inside `chrome-m`).
    """
    tokens = test_name.split("-")
    app_tokens = app.split("-")
    n = len(app_tokens)
    longer = [k.split("-") for k in KNOWN_APPS if k.startswith(app + "-")]
    for i in range(len(tokens) - n + 1):
        if tokens[i : i + n] != app_tokens:
            continue
        if any(tokens[i : i + len(k)] == k for k in longer):
            continue
        return i, i + n
    return None


def cross_browser_test_name(test_name: str, app: str, pair_label: str) -> str | None:
    """Replace the `app` token in `test_name` with `pair_label`.

    `browsertime-benchmark-firefox-speedometer3` + app `firefox` +
    label `firefox-vs-chrome` -> `browsertime-benchmark-firefox-vs-chrome-speedometer3`.
    The chrome task normalizes to the same string, which is what lets the
    organizer pair the two. Returns None when the task isn't for `app`.
    """
    span = find_app_span(test_name, app)
    if span is None:
        return None
    tokens = test_name.split("-")
    return "-".join(tokens[: span[0]] + [pair_label] + tokens[span[1] :])


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


# Perftest profiling variants record extra runs under these subdirectories.
# Profilers slow the app down, so those runs aren't comparable to normal ones.
PROFILER_RUN_DIRS = frozenset({"simpleperf", "geckoprofiler", "simpleperf-geckoprofiler"})


def is_profiled_run(path: Path) -> bool:
    return path.parent.name in PROFILER_RUN_DIRS


def _is_video_task(task_name: str) -> bool:
    """Return True if this task produces video artifacts we can download."""
    return ("-browsertime-" in task_name and "profiling" not in task_name) or (
        task_name.startswith("perftest-") and "-startup-" in task_name
    )


def _pair_matches_task(suite: str, platform: str, s: str, p: str, perftest: bool) -> bool:
    """Whether a perfcompare (suite ``s``, platform ``p``) belongs to a task's (suite, platform)."""
    if (s, p) == (suite, platform):
        return True
    # Multi-step tests report one suite per step as `<suite>.<step>`
    # (e.g. nav-bench.amazon-nav-load) — the task is significant if any step is.
    if s.startswith(suite + ".") and p == platform:
        return True
    # Perftest TC names and Treeherder names may differ slightly.
    return perftest and (
        (platform.lower() in p.lower() or p.lower() in platform.lower())
        and (suite.lower() in s.lower() or s.lower() in suite.lower())
    )


def _matches_high_conf(task_name: str, high_conf_tests: set[tuple[str, str]]) -> bool:
    """Check if a task matches the high-confidence filter set."""
    suite, platform = extract_suite_and_platform(task_name)
    if suite and platform:
        perftest = task_name.startswith("perftest-")
        return any(_pair_matches_task(suite, platform, s, p, perftest) for s, p in high_conf_tests)
    # Can't determine suite/platform — include perftest tasks, skip others
    return task_name.startswith("perftest-")


def rows_for_task(task_name: str, rows: list[dict]) -> list[dict]:
    """The significant perfcompare rows (see `summarize_row`) that belong to a task."""
    suite, platform = extract_suite_and_platform(task_name)
    if not (suite and platform):
        return []
    perftest = task_name.startswith("perftest-")
    return [
        r for r in rows if _pair_matches_task(suite, platform, r["suite"], r["platform"], perftest)
    ]


def significant_steps(task_name: str, rows: list[dict]) -> set[str] | None:
    """Steps of a multi-step task that perfcompare flagged, or None for "keep every step".

    nav-bench reports each step as its own suite `nav-bench.<step>`. When every
    matching row is such a step suite, only those steps are worth downloading.
    Any whole-task row (suite == the task's suite) means keep everything.
    """
    suite, _ = extract_suite_and_platform(task_name)
    matched = rows_for_task(task_name, rows)
    if not suite or not matched:
        return None
    steps = set()
    for r in matched:
        if r["suite"].startswith(suite + "."):
            steps.add(r["suite"][len(suite) + 1 :])
            continue
        kind = _load_kind(r)
        if kind is None:
            return None
        steps.add(kind)
    return steps


def _load_kind(row: dict) -> str | None:
    """ "cold"/"warm" from a row's extra_options (tp6 page loads), else None."""
    tokens = set(str(row.get("extra_options") or "").split())
    kinds = [k for k in ("cold", "warm") if k in tokens]
    return kinds[0] if len(kinds) == 1 else None


def rows_for_step(rows: list[dict], step: str | None) -> list[dict]:
    """Narrow a task's rows to one step (rows for the whole task always apply)."""
    if not step:
        return rows
    if step in ("cold", "warm"):
        return [r for r in rows if _load_kind(r) in (step, None)]
    return [r for r in rows if "." not in r["suite"] or r["suite"].endswith("." + step)]


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


def summarize_row(result: dict) -> dict:
    """Compact, JSON-friendly view of one Treeherder perfcompare row.

    student-t rows carry ``base_avg_value``/``new_avg_value``/``delta_percentage``;
    mann-whitney-u rows only carry ``*_standard_stats``, so fall back to the
    medians and compute the delta.
    """

    def _avg(side: str) -> float | None:
        v = result.get(f"{side}_avg_value")
        if isinstance(v, int | float):
            return float(v)
        stats = result.get(f"{side}_standard_stats") or {}
        v = stats.get("median", stats.get("mean"))
        return float(v) if isinstance(v, int | float) else None

    base, new = _avg("base"), _avg("new")
    delta = result.get("delta_percentage")
    if not isinstance(delta, int | float):
        delta = round((new - base) / base * 100, 2) if base and new is not None else None
    direction = result.get("direction_of_change")
    if direction not in ("regression", "improvement"):
        if result.get("is_regression"):
            direction = "regression"
        elif result.get("is_improvement"):
            direction = "improvement"
        else:
            direction = None
    return {
        "suite": result.get("suite", ""),
        "test": result.get("test", ""),
        "platform": result.get("platform", ""),
        "unit": result.get("new_measurement_unit") or result.get("base_measurement_unit"),
        "lower_is_better": result.get("lower_is_better"),
        "base": base,
        "new": new,
        "delta_pct": delta,
        "direction": direction,
        "confidence": result.get("confidence_text"),
        "extra_options": result.get("extra_options") or "",
    }


def significant_rows(results: list, test_version: str | None = None) -> list[dict]:
    """Summaries of the rows that count as significant under ``test_version``."""
    return [
        summarize_row(r)
        for r in results
        if isinstance(r, dict)
        and r.get("suite")
        and r.get("platform")
        and is_significant_result(r, test_version)
    ]


def significant_pairs(rows: list[dict]) -> set[tuple[str, str]]:
    return {(r["suite"], r["platform"]) for r in rows}


def load_significant_rows_from_file(json_path: Path, test_version: str | None = None) -> list[dict]:
    """
    Load perfcompare JSON from a local file and return its significant rows.

    Accepts both the perfcompare UI export shape (``[{test_name: [rows]}]``) and
    the raw Treeherder API shape (``[row, ...]``). See `is_significant_result`
    for what counts as significant under each ``test_version``.
    """
    with open(json_path) as f:
        data = json.load(f)

    rows: list[dict] = []
    for item in data:
        if isinstance(item, dict) and "suite" in item:
            rows.append(item)
        elif isinstance(item, dict):
            for test_data in item.values():
                if isinstance(test_data, list):
                    rows.extend(r for r in test_data if isinstance(r, dict))
    return significant_rows(rows, test_version)


def load_high_confidence_from_file(
    json_path: Path, test_version: str | None = None
) -> set[tuple[str, str]]:
    """Significant (suite, platform) pairs from a local perfcompare JSON file."""
    return significant_pairs(load_significant_rows_from_file(json_path, test_version))


def _closest_to_value(subtest: dict, *, true_median: bool = False) -> int:
    replicates = subtest["replicates"]
    median_val = statistics.median(replicates) if true_median else subtest["value"]
    if len(replicates) <= 1:
        return 0
    return min(range(len(replicates)), key=lambda i: abs(replicates[i] - median_val))


def find_median_run_index(data: dict, step: str | None = None) -> int:
    """Find the run index whose replicate value is closest to the median (subtest value).

    By default uses ``suites[0].subtests[0]``. For multi-step tasks (e.g. the
    nav-bench test, where one task records several navigation steps with
    their own runs) pass ``step``: the first subtest across all suites whose
    name is ``step`` or starts with ``step-`` is used instead, falling back to
    the default when none matches. Step subtests are matched against the
    real median of their replicates, not ``value`` — nav-bench reports e.g.
    ``<step>-min-si`` whose ``value`` is the minimum.
    """
    try:
        if step:
            for suite in data.get("suites", []):
                for subtest in suite.get("subtests", []):
                    name = subtest.get("name", "")
                    if (name == step or name.startswith(step + "-")) and subtest.get("replicates"):
                        return _closest_to_value(subtest, true_median=True)
            # cold/warm page loads: one suite per kind, told apart by extraOptions.
            for suite in data.get("suites", []):
                if step in (suite.get("extraOptions") or []) and suite.get("subtests"):
                    return _closest_to_value(suite["subtests"][0])
        subtests = data["suites"][0]["subtests"]
        if not subtests:
            return 0
        return _closest_to_value(subtests[0])
    except (KeyError, IndexError, TypeError, AttributeError):
        return 0

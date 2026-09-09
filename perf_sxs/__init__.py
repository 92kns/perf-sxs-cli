"""perf-sxs-cli: download and compare browsertime/perftest videos from Mozilla CI.

This package used to be a single flat `perf_sxs.py` module. The public names it
exposed are re-exported here so existing imports (`from perf_sxs import ...`)
keep working; new code should prefer importing from the specific submodule
(`perf_sxs.api`, `perf_sxs.selection`, `perf_sxs.download`, `perf_sxs.organize`,
`perf_sxs.urls`, `perf_sxs.cli`).
"""

from .api import (
    download_artifact,
    fetch_json,
    fetch_perfcompare_data_from_treeherder,
    fetch_perfherder_data,
    find_task_group_ids,
    get_tasks_for_revision,
    list_task_artifacts,
    resolve_lando_id,
)
from .cli import main
from .download import download_video_artifacts
from .models import TASKCLUSTER_INDEX, TASKCLUSTER_QUEUE, TASKCLUSTER_ROOT, TryPush, VideoTask
from .organize import organize_single_revision, organize_videos_for_comparison, read_median_idx
from .selection import (
    extract_suite_and_platform,
    extract_test_info,
    filter_video_tasks,
    find_median_run_index,
    load_high_confidence_from_file,
)
from .urls import parse_lando_url, parse_perfcompare_url, parse_try_url

__version__ = "0.1.0"

__all__ = [
    "TASKCLUSTER_INDEX",
    "TASKCLUSTER_QUEUE",
    "TASKCLUSTER_ROOT",
    "TryPush",
    "VideoTask",
    "download_artifact",
    "download_video_artifacts",
    "extract_suite_and_platform",
    "extract_test_info",
    "fetch_json",
    "fetch_perfcompare_data_from_treeherder",
    "fetch_perfherder_data",
    "filter_video_tasks",
    "find_median_run_index",
    "find_task_group_ids",
    "get_tasks_for_revision",
    "list_task_artifacts",
    "load_high_confidence_from_file",
    "main",
    "organize_single_revision",
    "organize_videos_for_comparison",
    "parse_lando_url",
    "parse_perfcompare_url",
    "parse_try_url",
    "read_median_idx",
    "resolve_lando_id",
]

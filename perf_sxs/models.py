"""Shared constants and data models for perf-sxs-cli."""

from dataclasses import dataclass

TASKCLUSTER_ROOT = "https://firefox-ci-tc.services.mozilla.com/api"
TASKCLUSTER_QUEUE = f"{TASKCLUSTER_ROOT}/queue/v1"
TASKCLUSTER_INDEX = f"{TASKCLUSTER_ROOT}/index/v1"

LANDO_API = "https://api.lando.services.mozilla.com"

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

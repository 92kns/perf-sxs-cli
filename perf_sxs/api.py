"""Network calls: Taskcluster, Treeherder, and Lando API clients.

All requests go through `request_with_retry`, which applies the module-wide
default timeout and retries transient failures (connection errors, timeouts,
429/5xx) with exponential backoff. No request in this tool should be able to
hang forever.
"""

import asyncio
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from .models import LANDO_API, TASKCLUSTER_INDEX, TASKCLUSTER_QUEUE
from .selection import STUDENT_T, is_significant_result

TREEHERDER_PERFCOMPARE_API = "https://treeherder.mozilla.org/api/perfcompare/results/"

# Browsertime writes perfherder-data.json under test_info/; the bare path is
# kept as a fallback for older tasks.
PERFHERDER_DATA_ARTIFACTS = (
    "public/test_info/perfherder-data.json",
    "public/perfherder-data.json",
)

# Concurrency for per-task metadata lookups (index -> task -> taskGroupId).
METADATA_CONCURRENCY = 16

# (total, connect, sock_read) — generous enough for slow TC artifact listings
# and large video downloads, but bounded so nothing hangs indefinitely.
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=120, connect=15, sock_connect=15, sock_read=60)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 0.5


def new_session() -> aiohttp.ClientSession:
    """Create the shared aiohttp session with the tool's default timeout."""
    return aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)


async def request_with_retry(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
    **kwargs,
) -> aiohttp.ClientResponse:
    """Issue a request, retrying connection errors/timeouts/429/5xx with backoff.

    Returns the response (caller is responsible for reading/closing it, e.g. via
    `async with resp:`). Raises the last exception if all retries are exhausted.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = await session.request(method, url, **kwargs)
        except (TimeoutError, aiohttp.ClientError):
            if attempt >= retries:
                raise
            await asyncio.sleep(backoff * (2 ** (attempt - 1)))
            continue

        if resp.status in RETRYABLE_STATUS and attempt < retries:
            resp.release()
            await asyncio.sleep(backoff * (2 ** (attempt - 1)))
            continue
        return resp


async def fetch_json(session: aiohttp.ClientSession, url: str) -> dict:
    """Fetch JSON from URL with error handling."""
    resp = await request_with_retry(session, "GET", url)
    async with resp:
        if resp.status != 200:
            raise Exception(f"HTTP {resp.status} for {url}")
        return await resp.json()


async def resolve_lando_id(session: aiohttp.ClientSession, lando_id: str) -> str:
    """Resolve a Lando landing job ID to a revision hash via the Lando API."""
    url = f"{LANDO_API}/landing_jobs/{lando_id}"
    resp = await request_with_retry(session, "GET", url)
    async with resp:
        if resp.status != 200:
            raise Exception(f"Lando API returned HTTP {resp.status} for job {lando_id}")
        data = await resp.json(content_type=None)
        commit_id = data.get("commit_id")
        if not commit_id or not isinstance(commit_id, str):
            raise Exception(
                f"No commit_id in Lando response for job {lando_id} (job may still be pending)"
            )
        return commit_id


def perfcompare_test_version(perfcompare_url: str, override: str | None = None) -> str:
    """The statistical test to filter by: explicit override, else the URL's, else student-t."""
    if override:
        return override
    params = parse_qs(urlparse(perfcompare_url).query)
    return params.get("test_version", [STUDENT_T])[0] or STUDENT_T


async def fetch_perfcompare_data_from_treeherder(
    session: aiohttp.ClientSession,
    perfcompare_url: str,
    test_version: str | None = None,
) -> set[tuple[str, str]] | None:
    """
    Fetch significant (suite, platform) pairs from Treeherder for all video-producing frameworks.

    Always queries frameworks 13 (browsertime/raptor) and 15 (mozperftest) in
    parallel so mixed perfcompare links work without --all-tests.
    ``test_version`` overrides the URL's ``test_version`` (student-t or
    mann-whitney-u); see `selection.is_significant_result` for what each
    counts as significant.

    Returns None if *every* framework request failed (so the caller can tell
    "couldn't reach Treeherder" apart from "no significant changes"), else
    the (possibly empty) set.
    """
    params = parse_qs(urlparse(perfcompare_url).query)
    version = perfcompare_test_version(perfcompare_url, test_version)

    base_params = {
        "base_repository": params.get("baseRepo", ["mozilla-central"])[0],
        "base_revision": params.get("baseRev", [""])[0],
        "new_repository": params.get("newRepo", ["mozilla-central"])[0],
        "new_revision": params.get("newRev", [""])[0],
        "no_subtests": "true",
        "replicates": params.get("replicates", ["false"])[0],
        "test_version": version,
    }

    # 13 = browsertime/raptor, 15 = mozperftest (Android startup) — the only two that produce videos
    frameworks = ["13", "15"]

    async def _fetch_framework(fw: str) -> set[tuple[str, str]] | None:
        url = f"{TREEHERDER_PERFCOMPARE_API}?{urlencode({**base_params, 'framework': fw})}"
        try:
            resp = await request_with_retry(session, "GET", url)
            async with resp:
                if resp.status != 200:
                    return None
                results = await resp.json()
                if not isinstance(results, list):
                    return None
                return {
                    (r["suite"], r["platform"])
                    for r in results
                    if isinstance(r, dict)
                    and r.get("suite")
                    and r.get("platform")
                    and is_significant_result(r, version)
                }
        except Exception:
            return None

    results = await asyncio.gather(*[_fetch_framework(fw) for fw in frameworks])
    ok = [r for r in results if r is not None]
    if not ok:
        return None
    return set().union(*ok)


async def find_task_group_ids(
    session: aiohttp.ClientSession, revision: str, repo: str
) -> list[str]:
    """Find all task group IDs for a revision.

    A revision can have multiple task groups (e.g. main CI + perf push on mozilla-central).
    We page through all indexed tasks and collect every unique group ID.
    """
    index_url = f"{TASKCLUSTER_INDEX}/tasks/gecko.v2.{repo}.revision.{revision}.taskgraph"

    indexed_tasks: list[dict] = []
    continuation_token = None
    while True:
        url = index_url + (f"?continuationToken={continuation_token}" if continuation_token else "")
        data = await fetch_json(session, url)
        indexed_tasks.extend(data.get("tasks", []))
        continuation_token = data.get("continuationToken")
        if not continuation_token:
            break
    if not indexed_tasks:
        raise Exception(f"No tasks found for revision {revision} in repo {repo}")

    semaphore = asyncio.Semaphore(METADATA_CONCURRENCY)

    async def _group_of(task_id: str) -> str:
        async with semaphore:
            task_data = await fetch_json(session, f"{TASKCLUSTER_QUEUE}/task/{task_id}")
            return task_data["taskGroupId"]

    gids = await asyncio.gather(*[_group_of(entry["taskId"]) for entry in indexed_tasks])
    # dict preserves first-seen order while deduplicating
    return list(dict.fromkeys(gids))


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


async def download_artifact(
    session: aiohttp.ClientSession,
    task_id: str,
    artifact_name: str,
    output_path,
    semaphore: asyncio.Semaphore,
    progress_callback=None,
) -> bool:
    """Download a single artifact."""
    async with semaphore:
        url = f"{TASKCLUSTER_QUEUE}/task/{task_id}/artifacts/{artifact_name}"

        try:
            resp = await request_with_retry(session, "GET", url)
            async with resp:
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
            print(f"    Error downloading {artifact_name}: {e}", file=sys.stderr)
            return False


async def fetch_perfherder_data(
    session: aiohttp.ClientSession,
    task_id: str,
    semaphore: asyncio.Semaphore,
) -> dict | None:
    """Download a task's perfherder-data.json (None if unavailable)."""
    async with semaphore:
        for artifact in PERFHERDER_DATA_ARTIFACTS:
            url = f"{TASKCLUSTER_QUEUE}/task/{task_id}/artifacts/{artifact}"
            try:
                resp = await request_with_retry(session, "GET", url)
                async with resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict):
                        return data
            except Exception:
                continue
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
            resp = await request_with_retry(session, "GET", url)
            async with resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
                return data.get("artifacts", [])
        except Exception:
            return []

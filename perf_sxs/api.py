"""Network calls: Taskcluster, Treeherder, and Lando API clients.

All requests go through `request_with_retry`, which applies the module-wide
default timeout and retries transient failures (connection errors, timeouts,
429/5xx) with exponential backoff. No request in this tool should be able to
hang forever.
"""

import asyncio
from urllib.parse import parse_qs, urlparse

import aiohttp

from .models import LANDO_API, TASKCLUSTER_INDEX, TASKCLUSTER_QUEUE

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


async def fetch_perfcompare_data_from_treeherder(
    session: aiohttp.ClientSession, perfcompare_url: str
) -> set[tuple[str, str]]:
    """
    Fetch high-confidence tests from Treeherder for all video-producing frameworks.

    Always queries frameworks 13 (browsertime/raptor) and 15 (mozperftest) in
    parallel, plus whatever framework is in the URL, so mixed perfcompare links
    work without --all-tests.
    """
    parsed = urlparse(perfcompare_url)
    params = parse_qs(parsed.query)

    base_rev = params.get("baseRev", [""])[0]
    base_repo = params.get("baseRepo", ["mozilla-central"])[0]
    new_rev = params.get("newRev", [""])[0]
    new_repo = params.get("newRepo", ["mozilla-central"])[0]
    test_version = params.get("test_version", ["student-t"])[0]
    replicates = params.get("replicates", ["false"])[0]

    # 13 = browsertime/raptor, 15 = mozperftest (Android startup) — the only two that produce videos
    frameworks = ["13", "15"]

    api_url = "https://treeherder.mozilla.org/api/perfcompare/results/"
    base_params = {
        "base_repository": base_repo,
        "base_revision": base_rev,
        "new_repository": new_repo,
        "new_revision": new_rev,
        "no_subtests": "true",
        "replicates": replicates,
        "test_version": test_version,
    }

    async def _fetch_framework(fw: str) -> set[tuple[str, str]]:
        qs = "&".join(f"{k}={v}" for k, v in {**base_params, "framework": fw}.items())
        try:
            resp = await request_with_retry(session, "GET", f"{api_url}?{qs}")
            async with resp:
                if resp.status != 200:
                    return set()
                results = await resp.json()
                if not isinstance(results, list):
                    return set()
                return {
                    (r["suite"], r["platform"])
                    for r in results
                    if r.get("confidence_text") == "High" and r.get("suite") and r.get("platform")
                }
        except Exception:
            return set()

    print(f"  Calling Treeherder API (frameworks: {', '.join(frameworks)})...")
    results = await asyncio.gather(*[_fetch_framework(fw) for fw in frameworks])
    high_conf_tests: set[tuple[str, str]] = set().union(*results)
    return high_conf_tests


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
            resp = await request_with_retry(session, "GET", url)
            async with resp:
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
            resp = await request_with_retry(session, "GET", url)
            async with resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
                return data.get("artifacts", [])
        except Exception:
            return []

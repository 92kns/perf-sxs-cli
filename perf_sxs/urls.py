"""Pure parsing helpers for perfcompare/lando/Treeherder URLs and revisions.

No network I/O lives here — see `perf_sxs.api` for the Lando ID resolution call.
"""

import re
from urllib.parse import parse_qs, urlparse

from .models import TryPush


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

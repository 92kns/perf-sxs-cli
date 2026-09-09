"""Download orchestration: fetch video/image artifacts for a set of tasks."""

import asyncio
import sys
import tarfile
import zipfile
from pathlib import Path

import aiohttp

from .api import download_artifact, fetch_perfherder_data, list_task_artifacts
from .models import VideoTask
from .organize import _group_images_by_video
from .selection import find_median_run_index


class UnsafeArchiveMemberError(Exception):
    """Raised when an archive member would extract outside the target directory."""


def _resolved_member_target(extract_dir: Path, member_name: str) -> Path:
    """Resolve `member_name` against `extract_dir`, refusing traversal/absolute paths.

    Archives (even from a trusted-seeming source like CI) are untrusted input —
    a member name like `../../etc/passwd` or an absolute path must never be
    allowed to resolve outside `extract_dir` ("zip slip"/"tar slip"). Raises
    `UnsafeArchiveMemberError` rather than silently skipping, so a malicious or
    corrupted archive fails the whole extraction loudly instead of partially
    succeeding.
    """
    if Path(member_name).is_absolute():
        raise UnsafeArchiveMemberError(f"archive member has an absolute path: {member_name!r}")
    target = (extract_dir / member_name).resolve()
    if not target.is_relative_to(extract_dir.resolve()):
        raise UnsafeArchiveMemberError(
            f"archive member {member_name!r} would extract outside {extract_dir}"
        )
    return target


def _safe_extract_tar(tar: tarfile.TarFile, extract_dir: Path) -> None:
    """Extract every regular file/directory member of `tar` into `extract_dir`.

    Rejects any member that would traverse outside `extract_dir`, and rejects
    symlinks/hardlinks/device-or-fifo-special members entirely — this tool
    only ever expects plain media files (mp4/png) and directories from CI
    artifacts, so there's no legitimate reason for a symlink whose target
    could point outside the extraction root.
    """
    for member in tar.getmembers():
        if member.issym() or member.islnk() or member.isdev():
            raise UnsafeArchiveMemberError(
                f"archive member {member.name!r} is a symlink/hardlink/device file, refusing"
            )
        _resolved_member_target(extract_dir, member.name)
    tar.extractall(extract_dir)


def _safe_extract_zip(zf: zipfile.ZipFile, extract_dir: Path) -> None:
    """Extract every member of `zf` into `extract_dir`, rejecting traversal.

    `zipfile` doesn't support symlinks the way tar does, but a member name
    can still traverse via `../` or be absolute, so the same containment
    check applies.
    """
    for name in zf.namelist():
        _resolved_member_target(extract_dir, name)
    zf.extractall(extract_dir)


async def download_video_artifacts(
    session: aiohttp.ClientSession,
    video_tasks: list[VideoTask],
    output_dir: Path,
    max_concurrent: int = 10,
    all_runs: bool = False,
    quiet: bool = False,
) -> dict[str, dict[str, list[Path]]]:
    """Download video (and image) artifacts for all tasks.

    Returns {"base": {"videos": [...], "images": [...]}, "new": {...}}.

    With `quiet=True` (used by `perf-sxs --json`), progress/error output is
    written to stderr instead of stdout, so stdout stays reserved for the
    final JSON summary.
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
        print(
            f"\r  Downloaded {completed}/{total} artifacts...",
            end="",
            flush=True,
            file=sys.stderr if quiet else sys.stdout,
        )

    async def _extract_tgz_media(
        tar_path: Path, extract_dir: Path, vt: VideoTask
    ) -> dict[str, list[Path]]:
        """Extract a tgz, apply median selection for videos, return {videos, images}."""
        extract_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tar_path, "r:gz") as tar:
            _safe_extract_tar(tar, extract_dir)

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
                    print(
                        f"    Error extracting {tar_path}: {e}",
                        file=sys.stderr if quiet else sys.stdout,
                    )
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
                            _safe_extract_tar(tar, extract_dir)
                    else:
                        with zipfile.ZipFile(archive_path) as zf:
                            _safe_extract_zip(zf, extract_dir)
                    archive_path.unlink()
                    progress()
                    return {
                        "videos": sorted(extract_dir.rglob("*.mp4")),
                        "images": sorted(extract_dir.rglob("*.png")),
                    }
                except Exception as e:
                    print(
                        f"    Error extracting {archive_path}: {e}",
                        file=sys.stderr if quiet else sys.stdout,
                    )

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

    print(file=sys.stderr if quiet else sys.stdout)  # Newline after progress

    for vt, result in zip(video_tasks, all_results, strict=False):
        if isinstance(result, Exception):
            print(
                f"    Failed: {vt.test_name} - {result}", file=sys.stderr if quiet else sys.stdout
            )
        elif isinstance(result, dict):
            results[vt.label]["videos"].extend(result.get("videos", []))
            results[vt.label]["images"].extend(result.get("images", []))

    return results

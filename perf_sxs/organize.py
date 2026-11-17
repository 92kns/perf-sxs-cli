"""File organization: arrange downloaded artifacts into the viewer's layout.

Comparison keys are ``{platform}/{test_name}``. A task that records several
steps (e.g. nav-bench: one task navigates through several sites, each with its
own set of runs under ``pages/<site>/<step>/data/video/``) is split into one
comparison per step, keyed ``{platform}/{test_name}::{step}``.
"""

import json
from os.path import commonpath
from pathlib import Path

from .selection import natural_sort_key

MEDIAN_IDX_FILE = "median_idx.txt"  # single-step tasks
MEDIAN_IDX_STEPS_FILE = "median_idx.json"  # multi-step tasks: {step_label: idx}


def _sorted(paths) -> list[Path]:
    return sorted(paths, key=natural_sort_key)


def _is_browsertime_run_video(mp4: Path) -> bool:
    return mp4.parent.name == "video" and mp4.parent.parent.name == "data"


def _step_dir(mp4: Path) -> Path:
    """Directory that identifies the step a run video belongs to.

    Browsertime lays videos out as ``<step>/data/video/N.mp4``; anything else
    (perftest archives, flat layouts) uses the video's own directory.
    """
    if _is_browsertime_run_video(mp4):
        return mp4.parent.parent.parent
    return mp4.parent


def group_videos_by_step(mp4s: list[Path]) -> dict[str, list[Path]]:
    """Group run videos by step. Returns ``{label: [mp4, ...]}`` (naturally sorted).

    Only browsertime's ``<step>/data/video/`` layout is split into steps;
    other layouts (perftest archives — including profiling variants that put
    runs in per-profiler subdirectories) stay a single group. Single-step tasks return one group with label
    ``""``. For multi-step tasks the label is the step directory name (e.g.
    ``duckduckgo-nav-load``), or — if those collide — its path relative to the
    steps' common parent, with ``/`` flattened to ``_``. Labels are stable
    across base/new pushes, which is what pairs steps between the two sides.
    """
    mp4s = _sorted(mp4s)
    if not any(_is_browsertime_run_video(m) for m in mp4s):
        return {"": mp4s} if mp4s else {}

    groups: dict[Path, list[Path]] = {}
    for mp4 in mp4s:
        groups.setdefault(_step_dir(mp4), []).append(mp4)
    if len(groups) <= 1:
        return {"": next(iter(groups.values()))} if groups else {}

    dirs = list(groups)
    names = [d.name for d in dirs]
    if len(set(names)) == len(names):
        labels = names
    else:
        root = Path(commonpath([str(d) for d in dirs]))
        labels = [str(d.relative_to(root)).replace("/", "_") for d in dirs]
    return {label: groups[d] for label, d in sorted(zip(labels, dirs, strict=True))}


def _images_for_step(label: str, step_videos: list[Path], pngs: list[Path]) -> list[Path]:
    """PNGs that belong to one step (everything, for single-step tasks)."""
    if not label:
        return pngs
    step_dir = _step_dir(step_videos[0])
    return [p for p in pngs if p.is_relative_to(step_dir)]


def _group_images_by_video(mp4s: list[Path], pngs: list[Path]) -> list[list[Path]]:
    """Group PNG images with their corresponding video.

    Uses per-subdirectory grouping when each video is in a distinct directory.
    Falls back to even index-based distribution for flat (same-dir) layouts.
    Returns one sublist per video, possibly empty.
    """
    if not pngs or not mp4s:
        return [[] for _ in mp4s]

    pngs = _sorted(pngs)
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


def read_median_idx(test_dir: Path, step: str = "") -> int | None:
    """Read the median sidecar written during an --all-runs download."""
    for task_dir in test_dir.iterdir():
        if not task_dir.is_dir():
            continue
        try:
            if step:
                steps_file = task_dir / MEDIAN_IDX_STEPS_FILE
                if steps_file.exists():
                    value = json.loads(steps_file.read_text()).get(step)
                    return value if isinstance(value, int) else None
            else:
                idx_file = task_dir / MEDIAN_IDX_FILE
                if idx_file.exists():
                    return int(idx_file.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def _side(test_dir: Path, output_dir: Path) -> dict[str, dict]:
    """Per-step videos/images/median for one side (base or new) of a test."""
    videos = list(test_dir.rglob("*.mp4"))
    pngs = _sorted(test_dir.rglob("*.png"))
    out = {}
    for label, step_videos in group_videos_by_step(videos).items():
        img_groups = _group_images_by_video(step_videos, _images_for_step(label, step_videos, pngs))
        out[label] = {
            "videos": [str(v.relative_to(output_dir)) for v in step_videos],
            "median_idx": read_median_idx(test_dir, label),
            "images": [[str(p.relative_to(output_dir)) for p in grp] for grp in img_groups],
        }
    return out


def _key(platform: str, test_name: str, step: str) -> str:
    return f"{platform}/{test_name}" + (f"::{step}" if step else "")


def _test_dirs(side_dir: Path):
    if not side_dir.exists():
        return
    for platform_dir in sorted(side_dir.iterdir()):
        if not platform_dir.is_dir():
            continue
        for test_dir in sorted(platform_dir.iterdir()):
            if test_dir.is_dir():
                yield platform_dir.name, test_dir


def organize_videos_for_comparison(output_dir: Path) -> dict:
    """Organize downloaded videos into a structure for the viewer."""
    comparisons: dict[str, dict] = {}
    new_dir = output_dir / "new"

    for platform, test_dir in _test_dirs(output_dir / "base"):
        test_name = test_dir.name
        new_test_dir = new_dir / platform / test_name
        if not new_test_dir.exists():
            continue

        base_steps = _side(test_dir, output_dir)
        new_steps = _side(new_test_dir, output_dir)
        base_task_ids = {d.name for d in test_dir.iterdir() if d.is_dir()}
        new_task_ids = {d.name for d in new_test_dir.iterdir() if d.is_dir()}
        same_task = bool(base_task_ids & new_task_ids)

        for step, base in base_steps.items():
            new = new_steps.get(step)
            if not new or not base["videos"] or not new["videos"]:
                continue
            comparisons[_key(platform, test_name, step)] = {
                "platform": platform,
                "test_name": test_name,
                "step": step or None,
                "base_videos": base["videos"],
                "new_videos": new["videos"],
                "base_median_idx": base["median_idx"],
                "new_median_idx": new["median_idx"],
                "same_task_warning": same_task,
                "base_images": base["images"],
                "new_images": new["images"],
            }

    return comparisons


def organize_single_revision(output_dir: Path) -> dict:
    """Organize downloaded videos for single-revision (no-compare) mode."""
    comparisons: dict[str, dict] = {}

    for platform, test_dir in _test_dirs(output_dir / "base"):
        for step, base in _side(test_dir, output_dir).items():
            if not base["videos"]:
                continue
            comparisons[_key(platform, test_dir.name, step)] = {
                "platform": platform,
                "test_name": test_dir.name,
                "step": step or None,
                "base_videos": base["videos"],
                "base_median_idx": base["median_idx"],
                "base_images": base["images"],
            }

    return comparisons

"""File organization: arrange downloaded artifacts into the viewer's layout."""

from pathlib import Path


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

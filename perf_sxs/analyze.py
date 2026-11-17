"""perf-sxs-analyze: measure and explain base-vs-new video differences.

Three steps, usable separately:

1. ``prepare`` (no API key needed): for every comparison, decode the videos at
   low resolution, compute a visual-progress curve and approximate visual
   metrics (first change, speed index, visually complete), pick the moments
   where each side reached the same progress, and render one contact sheet
   (BASE row over NEW row) per comparison. Writes ``analysis/manifest.json``.
2. judge: decide what is visibly different. Either ``run`` (calls Claude
   through the Anthropic API) or an agent (the analyze-perf-videos skill)
   reads the sheets and writes a judgments file.
3. ``report``: merge the manifest and judgments into ``analysis.json`` (read by
   the viewer) and a self-contained ``analysis_report.html``.

The numbers come from the frames, not from the model: the model's job is to
explain *what* is different, and the numbers say *how much*.
"""

import argparse
import asyncio
import base64
import hashlib
import html
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5-5"
WORK_DIR = "analysis"
MANIFEST = "manifest.json"
INSTRUCTIONS = "instructions.md"
ANALYSIS_JSON = "analysis.json"
REPORT_HTML = "analysis_report.html"

# Low-res grayscale decode used for the progress curve.
PROBE_W, PROBE_H = 64, 48
# Leave out of the diff: the browser toolbar at the top (tab spinner and URL
# change before the page paints) and the running timer browsertime's
# annotated videos burn into the bottom (it changes every frame).
MASK_TOP = 0.10
MASK_BOTTOM = 0.15
# A pixel "changed" when its gray value moves by more than this.
PIXEL_THRESHOLD = 16
# ...and a frame changed when more than this fraction of pixels did.
CHANGE_FRACTION = 0.005
VISUALLY_COMPLETE = 0.98

SHEET_W, SHEET_H = 392, 310
MILESTONES = (
    ("first change", None),
    ("50%", 0.50),
    ("85%", 0.85),
    ("visually complete", VISUALLY_COMPLETE),
)

VERDICTS = ("regression", "improvement", "no_visible_difference", "inconclusive")
SEVERITIES = ("none", "low", "medium", "high")


# --------------------------------------------------------------------------
# Visual progress (pure functions, unit-tested)
# --------------------------------------------------------------------------


def changed_fraction(a: bytes, b: bytes) -> float:
    """Fraction of pixels whose gray value differs by more than PIXEL_THRESHOLD."""
    if not a:
        return 0.0
    return sum(1 for x, y in zip(a, b, strict=True) if abs(x - y) > PIXEL_THRESHOLD) / len(a)


def _distance(a: bytes, b: bytes) -> int:
    return sum(abs(x - y) for x, y in zip(a, b, strict=True))


def progress_curve(frames: list[bytes]) -> list[float]:
    """Speedline-style visual progress: 0 = first frame, 1 = last frame."""
    if not frames:
        return []
    last = frames[-1]
    total = _distance(frames[0], last)
    if total == 0:
        return [1.0] * len(frames)
    return [max(0.0, min(1.0, 1 - _distance(f, last) / total)) for f in frames]


def visual_metrics(times_ms: list[float], frames: list[bytes]) -> dict:
    """Approximate visual metrics from a decoded video.

    - ``first_change_ms``: first frame that differs visibly from frame 0.
    - ``speed_index``: area above the progress curve (ms), like browsertime's.
    - ``visual_complete_85_ms`` / ``visual_complete_ms``: first time progress
      reaches 85% / and from which it stays at or above 98%.
    """
    if not frames:
        return {}
    progress = progress_curve(frames)
    first_change = next(
        (
            t
            for t, f in zip(times_ms, frames, strict=True)
            if changed_fraction(frames[0], f) > CHANGE_FRACTION
        ),
        None,
    )
    speed_index = 0.0
    for i in range(1, len(times_ms)):
        speed_index += (1 - progress[i - 1]) * (times_ms[i] - times_ms[i - 1])
    vc85 = next((t for t, p in zip(times_ms, progress, strict=True) if p >= 0.85), None)
    last_incomplete = max((i for i, p in enumerate(progress) if p < VISUALLY_COMPLETE), default=-1)
    vc = times_ms[min(last_incomplete + 1, len(times_ms) - 1)]
    return {
        "first_change_ms": round(first_change) if first_change is not None else None,
        "speed_index": round(speed_index),
        "visual_complete_85_ms": round(vc85) if vc85 is not None else None,
        "visual_complete_ms": round(vc),
        "duration_ms": round(times_ms[-1]),
    }


def milestone_times(times_ms: list[float], frames: list[bytes]) -> list[tuple[str, float]]:
    """The moments shown in the contact sheet: first change, 50%, 85%, complete."""
    if not frames:
        return []
    progress = progress_curve(frames)
    metrics = visual_metrics(times_ms, frames)
    out: list[tuple[str, float]] = []
    for label, threshold in MILESTONES:
        if threshold is None:
            t = metrics["first_change_ms"]
            t = times_ms[0] if t is None else t
        elif threshold == VISUALLY_COMPLETE:
            t = metrics["visual_complete_ms"]
        else:
            t = next(
                (t for t, p in zip(times_ms, progress, strict=True) if p >= threshold),
                times_ms[-1],
            )
        out.append((label, float(t)))
    return out


def summarize_runs(per_run: list[dict]) -> dict:
    """Median of each metric across runs, plus min/max when there are several."""
    out: dict = {"runs": len(per_run)}
    for name in ("first_change_ms", "speed_index", "visual_complete_85_ms", "visual_complete_ms"):
        values = [m[name] for m in per_run if m.get(name) is not None]
        if not values:
            out[name] = None
            continue
        out[name] = round(statistics.median(values))
        if len(values) > 1:
            out[f"{name}_range"] = [min(values), max(values)]
    return out


def pct_delta(base: float | None, new: float | None) -> float | None:
    if not base or new is None:
        return None
    return round((new - base) / base * 100, 1)


# --------------------------------------------------------------------------
# ffmpeg
# --------------------------------------------------------------------------


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise SystemExit("Error: ffmpeg not found in PATH. Install it and try again.")


def decode_video(path: Path) -> tuple[list[float], list[bytes]]:
    """Decode to PROBE_W x PROBE_H grayscale frames with their timestamps (ms)."""
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-v", "info", "-i", str(path), "-an",
            "-fps_mode", "passthrough",
            "-vf", f"showinfo,scale={PROBE_W}:{PROBE_H}:flags=area,format=gray",
            "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip
    # One showinfo line per frame; keep them aligned with the frames even when
    # pts is negative or missing (N/A: reuse the previous timestamp).
    times: list[float] = []
    for m in re.findall(rb"pts_time:\s*(-?[0-9.]+|N/A)", proc.stderr):
        times.append(float(m) * 1000 if m != b"N/A" else (times[-1] if times else 0.0))
    size = PROBE_W * PROBE_H
    top = int(PROBE_H * MASK_TOP) * PROBE_W
    bottom = int(PROBE_H * (1 - MASK_BOTTOM)) * PROBE_W
    raw = proc.stdout
    frames = [raw[i : i + size][top:bottom] for i in range(0, len(raw) - size + 1, size)]
    n = min(len(times), len(frames))
    if n and times[0] != 0:
        times = [t - times[0] for t in times]
    return times[:n], frames[:n]


def _seek(t_ms: float) -> str:
    """-ss value that lands on the frame at ``t_ms``.

    Rounding up past the frame's pts makes ffmpeg skip to the next frame (or
    output nothing for the last frame), so always round down a little.
    """
    return f"{max(0.0, math.floor(t_ms) / 1000 - 0.0005):.4f}"


def extract_frame(video: Path, t_ms: float, out: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-ss", _seek(t_ms), "-i", str(video),
            "-frames:v", "1",
            "-vf", f"scale={SHEET_W}:{SHEET_H}:force_original_aspect_ratio=decrease,"
                   f"pad={SHEET_W}:{SHEET_H}:(ow-iw)/2:(oh-ih)/2:color=gray",
            "-q:v", "3", str(out),
        ],
        check=True,
    )  # fmt: skip
    return out


def build_sheet(rows: list[list[Path]], out: Path) -> Path:
    """Tile frames into a grid: one row per side, one column per milestone."""
    inputs: list[str] = []
    for row in rows:
        for f in row:
            inputs += ["-i", str(f)]
    parts, idx, labels = [], 0, []
    for r, row in enumerate(rows):
        refs = "".join(f"[{idx + i}]" for i in range(len(row)))
        idx += len(row)
        parts.append(
            f"{refs}hstack=inputs={len(row)}[r{r}]" if len(row) > 1 else f"{refs}copy[r{r}]"
        )
        labels.append(f"[r{r}]")
    graph = ";".join(parts)
    graph += f";{''.join(labels)}vstack=inputs={len(rows)}[out]" if len(rows) > 1 else ""
    final = "[out]" if len(rows) > 1 else "[r0]"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", final,
         "-q:v", "3", str(out)],
        check=True,
    )  # fmt: skip
    return out


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------


# Browsertime subtests worth showing, as reported in perfherder-data.json.
BROWSERTIME_METRICS = (
    "FirstVisualChange",
    "SpeedIndex",
    "ContentfulSpeedIndex",
    "LastVisualChange",
    "fcp",
    "loadtime",
)
PERFHERDER_DATA_FILE = "perfherder-data.json"


def run_index(video: str) -> int | None:
    """Browsertime run videos are 1.mp4 … N.mp4: replicate index = N - 1."""
    stem = Path(video).stem
    return int(stem) - 1 if stem.isdigit() else None


def browsertime_metrics(video_dir: Path, video: str, step: str | None) -> dict | None:
    """Per-run metrics browsertime itself recorded, from the task's perfherder data.

    `perf-sxs` saves perfherder-data.json in each task dir
    (``<side>/<platform>/<test>/<task_id>/``). Returns, per metric, the value for
    this video's run and the median over all runs, or None when unavailable
    (older downloads, perftest tasks, nav-bench steps). For a cold/warm step
    only that kind's suite is used.
    """
    parts = Path(video).parts
    if len(parts) < 5:
        return None
    path = video_dir.joinpath(*parts[:4]) / PERFHERDER_DATA_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    idx = run_index(video)
    out: dict = {}
    suites = data.get("suites", [])
    if step:
        # cold/warm loads have one suite each; other steps (nav-bench) name
        # their subtests per step instead and don't have these metrics.
        suites = [x for x in suites if step in (x.get("extraOptions") or [])]
    for suite in suites:
        for sub in suite.get("subtests", []):
            name, reps = sub.get("name"), sub.get("replicates") or []
            if name in BROWSERTIME_METRICS and name not in out and reps:
                out[name] = {
                    "run": reps[idx] if idx is not None and idx < len(reps) else None,
                    "median": statistics.median(reps),
                    "runs": len(reps),
                }
    return out or None


def safe_name(key: str) -> str:
    """Directory name for a comparison key (hash suffix keeps it unique)."""
    digest = hashlib.sha1(key.encode()).hexdigest()[:8]
    return f"{re.sub(r'[^A-Za-z0-9._-]+', '_', key)}-{digest}"


def _median_video(videos: list[str], idx: int | None) -> str:
    return videos[min(idx or 0, len(videos) - 1)]


def prepare_one(video_dir: Path, key: str, comp: dict, mode: str) -> dict:
    """Measure one comparison and render its contact sheet."""
    out_dir = video_dir / WORK_DIR / safe_name(key)
    out_dir.mkdir(parents=True, exist_ok=True)
    sides = ["base"] + (["new"] if mode != "single" and comp.get("new_videos") else [])

    entry: dict = {
        "key": key,
        "platform": comp.get("platform"),
        "test_name": comp.get("test_name"),
        "step": comp.get("step"),
        "perfcompare": comp.get("perfcompare", []),
        "sides": {},
    }
    sheet_rows: list[list[Path]] = []
    last_frames: dict[str, bytes] = {}
    for side in sides:
        videos = comp.get(f"{side}_videos") or []
        if not videos:
            continue
        per_run = []
        median_rel = _median_video(videos, comp.get(f"{side}_median_idx"))
        median_decoded = None
        for rel in videos:
            decoded = decode_video(video_dir / rel)
            per_run.append(visual_metrics(*decoded))
            if rel == median_rel:
                median_decoded = decoded
        assert median_decoded is not None
        times, frames = median_decoded
        if not frames:
            continue
        last_frames[side] = frames[-1]
        moments = milestone_times(times, frames)
        row = [
            extract_frame(video_dir / median_rel, t, out_dir / f"{side}_{i}.jpg")
            for i, (_, t) in enumerate(moments)
        ]
        sheet_rows.append(row)
        entry["sides"][side] = {
            "video": median_rel,
            "median_run": visual_metrics(times, frames),
            "all_runs": summarize_runs(per_run),
            "sheet_times_ms": [round(t) for _, t in moments],
            "browsertime": browsertime_metrics(video_dir, median_rel, comp.get("step")),
        }

    if sheet_rows:
        sheet = build_sheet(sheet_rows, out_dir / "sheet.jpg")
        entry["sheet"] = str(sheet.relative_to(video_dir))
        for f in (f for row in sheet_rows for f in row):
            f.unlink()
    if "base" in last_frames and "new" in last_frames:
        entry["final_frame_diff_pct"] = round(
            changed_fraction(last_frames["base"], last_frames["new"]) * 100, 1
        )
        entry["deltas_pct"] = deltas(entry["sides"]["base"], entry["sides"]["new"])
    return entry


def deltas(base: dict, new: dict) -> dict:
    """NEW vs BASE in percent. Browsertime's medians over all runs when both
    sides have them, else the medians computed from the downloaded videos."""
    bt_b, bt_n = base.get("browsertime") or {}, new.get("browsertime") or {}
    if bt_b and bt_n:
        return {
            "source": "browsertime",
            **{
                k: pct_delta(bt_b[k]["median"], bt_n[k]["median"])
                for k in BROWSERTIME_METRICS
                if k in bt_b and k in bt_n
            },
        }
    b, n = base["all_runs"], new["all_runs"]
    return {
        "source": "video",
        **{
            name: pct_delta(b.get(name), n.get(name))
            for name in ("first_change_ms", "speed_index", "visual_complete_ms")
        },
    }


def _ranking(entry: dict) -> float:
    d = entry.get("deltas_pct") or {}
    d = d.get("SpeedIndex", d.get("speed_index"))
    pc = max((abs(r.get("delta_pct") or 0) for r in entry.get("perfcompare", [])), default=0)
    return max(abs(d or 0), pc)


def prepare(video_dir: Path, filters: list[str] | None = None, jobs: int = 4) -> dict:
    """Run `prepare_one` for every (filtered) comparison; write and return the manifest."""
    require_ffmpeg()
    meta = json.loads((video_dir / "comparisons.json").read_text())
    mode = meta.get("mode", "compare")
    comparisons = meta.get("comparisons", {})
    if filters:
        comparisons = {
            k: v for k, v in comparisons.items() if any(f.lower() in k.lower() for f in filters)
        }

    def _one(item: tuple[str, dict]) -> dict:
        key, comp = item
        try:
            return prepare_one(video_dir, key, comp, mode)
        except (subprocess.CalledProcessError, OSError, ValueError) as e:
            return {"key": key, "error": f"{type(e).__name__}: {e}", "perfcompare": []}

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        entries = list(pool.map(_one, comparisons.items()))
    entries.sort(key=_ranking, reverse=True)

    cb = meta.get("cross_browser") or {}
    manifest = {
        "mode": mode,
        "base_revision": meta.get("base_revision"),
        "new_revision": meta.get("new_revision"),
        "base_label": cb.get("base_app", "base"),
        "new_label": cb.get("new_app", "new"),
        "cross_browser": bool(cb),
        "milestones": [label for label, _ in MILESTONES],
        "comparisons": {e["key"]: e for e in entries},
    }
    for entry in manifest["comparisons"].values():
        if entry.get("sheet"):
            entry["context"] = describe(entry, manifest)
    work = video_dir / WORK_DIR
    work.mkdir(exist_ok=True)
    (work / MANIFEST).write_text(json.dumps(manifest, indent=2))
    (work / INSTRUCTIONS).write_text(
        SYSTEM_PROMPT
        + "\n\nAnswer for each comparison with a JSON object: "
        + json.dumps(JUDGMENT_SCHEMA["properties"])
        + "\n"
    )
    return manifest


# --------------------------------------------------------------------------
# Judging with the API
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You review screen recordings from Firefox's performance CI \
(browsertime page loads, or app-startup recordings on Android).

For each comparison you get one contact sheet image and some numbers.
- Top row: the BASE recording. Bottom row: the NEW recording (in single-revision \
mode there is only one row).
- Columns, left to right: the moment that recording first changed visibly, \
reached 50% and 85% of its final appearance, and became visually complete. \
Columns line up by progress, not by time, so compare what each side shows at \
the same stage; the timestamps tell you when it got there.
- Frames show the video's timer at the bottom. Later frames in browsertime \
videos also show metric labels (e.g. "SpeedIndex 897") overlaid in the \
middle. Those labels are readouts, not page content.

The numbers:
- "perfcompare" rows, when present, are statistically significant changes over \
many runs. They're the most reliable signal that something changed.
- "browsertime metrics", when present, are what browsertime measured: for the \
run shown and the median over all runs. Prefer them. Otherwise the video \
metrics were computed from the frames and are approximate; from one run per \
side they're noisy, and differences under about 10% or 100 ms are usually \
noise.

Your job is to explain what is visibly different and whether it plausibly \
accounts for the measured change: what renders first, missing or extra content \
(images, fonts, ads, banners, popups, error pages), layout differences, blank or \
stalled frames. If the two sides show different page content (a live site \
changed, a consent dialog, an A/B variant), say so: that points to noise or a \
test-setup problem rather than a browser regression. Don't comment on video or \
encoding quality.

verdict: "regression" if NEW is visibly slower or renders worse than BASE, \
"improvement" if visibly better, "no_visible_difference", or "inconclusive" \
when the frames can't tell you. severity is the user-visible impact: none, low, \
medium, high. observations: short, concrete, with timestamps where useful."""

JUDGMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "observations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "verdict", "severity", "observations"],
    "additionalProperties": False,
}


def describe(entry: dict, manifest: dict) -> str:
    """The text that goes next to a contact sheet: what was measured."""
    lines = [f"Comparison: {entry['key']}"]
    if manifest.get("cross_browser"):
        lines.append(
            f"Cross-browser: BASE row is {manifest['base_label']}, NEW row is "
            f"{manifest['new_label']}. 'regression' means {manifest['new_label']} looks worse."
        )
    for side, info in entry.get("sides", {}).items():
        runs = info["all_runs"]
        label = side.upper()
        lines.append(
            f"{label} ({runs['runs']} run{'s' if runs['runs'] != 1 else ''}, medians): "
            + ", ".join(
                f"{name}={runs.get(name)}"
                + (f" (range {runs[name + '_range']})" if name + "_range" in runs else "")
                for name in ("first_change_ms", "speed_index", "visual_complete_ms")
            )
        )
        lines.append(f"{label} sheet column times (ms): {info['sheet_times_ms']}")
        if info.get("browsertime"):
            lines.append(
                f"{label} browsertime metrics (this run / median of all runs): "
                + ", ".join(
                    f"{k}={v['run']}/{round(v['median'])}" for k, v in info["browsertime"].items()
                )
            )
    if entry.get("deltas_pct"):
        lines.append(f"NEW vs BASE (%): {json.dumps(entry['deltas_pct'])}")
    if "final_frame_diff_pct" in entry:
        lines.append(
            f"Final frames differ in {entry['final_frame_diff_pct']}% of (low-res) pixels."
        )
    for r in entry.get("perfcompare", []):
        lines.append(
            f"perfcompare: {r.get('suite')} {r.get('test') or ''} {r.get('direction') or ''} "
            f"{r.get('delta_pct')}% ({r.get('base')} -> {r.get('new')} {r.get('unit') or ''})"
        )
    return "\n".join(lines)


async def _judge_one(client, model: str, entry: dict, manifest: dict, video_dir: Path) -> dict:
    image = base64.standard_b64encode((video_dir / entry["sheet"]).read_bytes()).decode()
    response = await client.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": JUDGMENT_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}},
                    {"type": "text", "text": entry.get("context") or describe(entry, manifest)},
                ],
            }
        ],
        # Retry on another model if this one declines (server-side fallback, beta).
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
        extra_body={"fallbacks": "default"},
    )  # fmt: skip
    if response.stop_reason == "refusal":
        return {"summary": "The model declined to analyze this comparison.", "verdict": "inconclusive", "severity": "none", "observations": []}  # fmt: skip
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text)


async def judge_with_api(
    video_dir: Path, manifest: dict, model: str, concurrency: int
) -> dict[str, dict]:
    try:
        import anthropic
    except ImportError as e:
        raise SystemExit(
            "perf-sxs-analyze run needs the anthropic package: "
            "uv sync --extra analyze (or use the analyze-perf-videos skill, which doesn't)"
        ) from e

    client = anthropic.AsyncAnthropic()
    sem = asyncio.Semaphore(concurrency)
    entries = [e for e in manifest["comparisons"].values() if e.get("sheet")]
    judgments: dict[str, dict] = {}
    done = 0

    async def _one(entry: dict) -> None:
        nonlocal done
        async with sem:
            try:
                judgments[entry["key"]] = await _judge_one(
                    client, model, entry, manifest, video_dir
                )
            except (anthropic.APIError, json.JSONDecodeError) as e:
                judgments[entry["key"]] = {
                    "summary": f"Analysis failed: {e}",
                    "verdict": "inconclusive",
                    "severity": "none",
                    "observations": [],
                }
            done += 1
            j = judgments[entry["key"]]
            print(f"  [{done}/{len(entries)}] {j['verdict']} [{j['severity']}] {entry['key']}")

    await asyncio.gather(*(_one(e) for e in entries))
    return judgments


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def merge(manifest: dict, judgments: dict[str, dict], model: str | None) -> dict:
    """Build analysis.json: viewer fields (summary/regression/severity/observations) + metrics."""
    out: dict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": model,
        "base_label": manifest.get("base_label"),
        "new_label": manifest.get("new_label"),
        "comparisons": {},
    }
    for key, entry in manifest["comparisons"].items():
        j = judgments.get(key, {})
        verdict = j.get("verdict") if j.get("verdict") in VERDICTS else None
        if entry.get("error"):
            summary = f"Could not process videos: {entry['error']}"
        else:
            summary = j.get("summary", "Not analyzed.")
        out["comparisons"][key] = {
            "summary": summary,
            "verdict": verdict or "inconclusive",
            "regression": {"regression": True, "improvement": False, "no_visible_difference": False}.get(verdict or ""),
            "severity": j.get("severity") if j.get("severity") in SEVERITIES else "none",
            "observations": [str(o) for o in j.get("observations", [])],
            "metrics": {s: i["all_runs"] for s, i in entry.get("sides", {}).items()},
            "browsertime": {
                s: i["browsertime"] for s, i in entry.get("sides", {}).items() if i.get("browsertime")
            },
            "deltas_pct": entry.get("deltas_pct"),
            "final_frame_diff_pct": entry.get("final_frame_diff_pct"),
            "perfcompare": entry.get("perfcompare", []),
            "sheet": entry.get("sheet"),
        }  # fmt: skip
    return out


REPORT_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #1a1a2e; color: #eee; padding: 2rem; }
h1 { font-size: 1.5rem; font-weight: 500; margin-bottom: .5rem; }
h2 { font-size: 1rem; font-weight: 500; margin: 2rem 0 1rem; }
.meta { color: #888; font-size: .85rem; margin-bottom: 1.5rem; }
.stats { display: flex; gap: 1rem; }
.stat { background: #16213e; padding: 1rem 1.5rem; border-radius: 8px; text-align: center; }
.stat b { display: block; font-size: 1.8rem; }
table { width: 100%; border-collapse: collapse; background: #16213e; border-radius: 8px; }
th, td { padding: .5rem 1rem; border-bottom: 1px solid #0f3460; text-align: left;
         font-size: .85rem; vertical-align: top; }
th { color: #888; text-transform: uppercase; font-size: .75rem; }
.card { background: #16213e; border-radius: 8px; margin-bottom: 1.5rem; padding: 1rem;
        border-left: 3px solid #555; }
.card.regression { border-color: #e94560; } .card.improvement { border-color: #4ecca3; }
.card img { width: 100%; border-radius: 4px; margin: .75rem 0; }
.badge { padding: 2px 8px; border-radius: 3px; font-size: .75rem; color: #000; background: #888; }
.badge.regression { background: #e94560; } .badge.improvement { background: #4ecca3; }
.badge.no_visible_difference { background: #9ab; }
ul { padding-left: 1.25rem; color: #bbb; font-size: .85rem; }
.num { color: #aaa; font-size: .8rem; }
"""


def _fmt_delta(v: float | None) -> str:
    return "–" if v is None else f"{v:+.1f}%"


def render_report(analysis: dict, manifest: dict, video_dir: Path) -> str:
    e = html.escape
    comps = analysis["comparisons"]
    counts = {v: sum(1 for c in comps.values() if c["verdict"] == v) for v in VERDICTS}
    rows, cards = [], []
    for key, c in comps.items():
        d = c.get("deltas_pct") or {}
        pcs = "; ".join(
            f"{r.get('suite')} {_fmt_delta(r.get('delta_pct'))} {r.get('direction') or ''}"
            for r in c.get("perfcompare", [])
        )
        rows.append(
            f"<tr><td>{e(key)}</td><td><span class='badge {e(c['verdict'])}'>"
            f"{e(c['verdict'].replace('_', ' '))} · {e(c['severity'])}</span></td>"
            f"<td class='num'>{_fmt_delta(d.get('SpeedIndex', d.get('speed_index')))}"
            f"{' (video)' if d.get('source') == 'video' else ''}</td>"
            f"<td class='num'>{e(pcs) or '–'}</td><td>{e(c['summary'])}</td></tr>"
        )
        img = ""
        if c.get("sheet") and (video_dir / c["sheet"]).exists():
            data = base64.standard_b64encode((video_dir / c["sheet"]).read_bytes()).decode()
            img = f"<img alt='contact sheet' src='data:image/jpeg;base64,{data}'>"
        metrics = " · ".join(
            f"{e(side)}: SI {m.get('speed_index')}, first change {m.get('first_change_ms')} ms, "
            f"complete {m.get('visual_complete_ms')} ms ({m.get('runs')} run(s))"
            for side, m in c.get("metrics", {}).items()
        )
        obs = "".join(f"<li>{e(o)}</li>" for o in c["observations"])
        cards.append(
            f"<div class='card {e(c['verdict'])}'><b>{e(key)}</b> "
            f"<span class='badge {e(c['verdict'])}'>{e(c['verdict'].replace('_', ' '))}</span>"
            f"<div class='num'>{metrics}</div>{img}<p>{e(c['summary'])}</p>"
            f"{'<ul>' + obs + '</ul>' if obs else ''}</div>"
        )
    base = e(str(manifest.get("base_revision") or "")[:12])
    new = e(str(manifest.get("new_revision") or "")[:12])
    stats = "".join(
        f"<div class='stat'><b>{counts[v]}</b>{e(v.replace('_', ' '))}</div>" for v in VERDICTS
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Perf video analysis</title>
<style>{REPORT_CSS}</style></head><body>
<h1>Perf video analysis</h1>
<div class="meta">Generated {e(analysis["generated_at"])} · Model: {e(str(analysis.get("model")))}
 · Base {base}{" · New " + new if new else ""}. Sheets: top row {e(str(manifest.get("base_label")))},
 bottom row {e(str(manifest.get("new_label")))}; columns = first change, 50%, 85%, visually complete.
 Video metrics are computed from the frames and approximate.</div>
<div class="stats">{stats}</div>
<h2>Summary</h2>
<table><thead><tr><th>Test</th><th>Verdict</th><th>SpeedIndex Δ</th><th>perfcompare</th>
<th>Finding</th></tr></thead><tbody>{"".join(rows)}</tbody></table>
<h2>Details</h2>{"".join(cards)}
</body></html>"""


def write_outputs(video_dir: Path, manifest: dict, judgments: dict, model: str | None) -> dict:
    analysis = merge(manifest, judgments, model)
    (video_dir / ANALYSIS_JSON).write_text(json.dumps(analysis, indent=2))
    (video_dir / REPORT_HTML).write_text(render_report(analysis, manifest, video_dir))
    return analysis


def _print_done(video_dir: Path, analysis: dict) -> None:
    comps = analysis["comparisons"].values()
    counts = {v: sum(1 for c in comps if c["verdict"] == v) for v in VERDICTS}
    print(f"\nDone. {len(analysis['comparisons'])} comparison(s): {counts}")
    print(f"JSON:   {video_dir / ANALYSIS_JSON}")
    print(f"Report: {video_dir / REPORT_HTML}")
    print("Refresh the viewer to see the analysis panel.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perf-sxs-analyze", description="Measure and explain perf-sxs video differences"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("video_dir", nargs="?", default="./sxs_videos")

    p_prep = sub.add_parser("prepare", help="Measure videos and render contact sheets (no API)")
    common(p_prep)
    p_prep.add_argument("--tests", "-t", help="Comma-separated substrings of comparison keys")
    p_prep.add_argument("--jobs", "-j", type=int, default=4, help="Parallel ffmpeg jobs")

    p_run = sub.add_parser("run", help="prepare, judge each comparison with Claude, report")
    common(p_run)
    p_run.add_argument("--tests", "-t", help="Comma-separated substrings of comparison keys")
    p_run.add_argument("--jobs", "-j", type=int, default=4, help="Parallel ffmpeg jobs")
    p_run.add_argument("--model", default=DEFAULT_MODEL, help=f"Default: {DEFAULT_MODEL}")
    p_run.add_argument("--concurrency", "-c", type=int, default=5, help="Concurrent API calls")

    p_rep = sub.add_parser("report", help="Write analysis.json + HTML from a judgments file")
    common(p_rep)
    p_rep.add_argument(
        "--judgments",
        required=True,
        help='JSON file: {"<comparison key>": {"summary", "verdict", "severity", "observations"}}',
    )
    p_rep.add_argument("--model", default=None, help="Recorded in analysis.json")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    video_dir = Path(args.video_dir)
    if not (video_dir / "comparisons.json").exists():
        raise SystemExit(f"Error: {video_dir / 'comparisons.json'} not found. Run perf-sxs first.")

    if args.command in ("prepare", "run"):
        filters = [t.strip() for t in args.tests.split(",")] if args.tests else None
        manifest = prepare(video_dir, filters, jobs=args.jobs)
        n = len(manifest["comparisons"])
        failed = [k for k, v in manifest["comparisons"].items() if v.get("error")]
        print(f"Prepared {n} comparison(s) -> {video_dir / WORK_DIR / MANIFEST}", file=sys.stderr)
        for k in failed:
            print(f"  failed: {k}: {manifest['comparisons'][k]['error']}", file=sys.stderr)
        if args.command == "prepare":
            return
        print(f"Judging with {args.model}...")
        judgments = asyncio.run(judge_with_api(video_dir, manifest, args.model, args.concurrency))
        _print_done(video_dir, write_outputs(video_dir, manifest, judgments, args.model))
        return

    manifest_path = video_dir / WORK_DIR / MANIFEST
    if not manifest_path.exists():
        raise SystemExit(f"Error: {manifest_path} not found. Run perf-sxs-analyze prepare first.")
    manifest = json.loads(manifest_path.read_text())
    judgments = json.loads(Path(args.judgments).read_text())
    _print_done(video_dir, write_outputs(video_dir, manifest, judgments, args.model))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Standalone video analysis script for perf-sxs-cli.

Extracts frames from base/new video pairs, computes PSNR/SSIM on final frames,
analyzes loading behaviour with Claude vision, and writes analysis.json and
analysis_report.html to the video directory.

Requires: ANTHROPIC_API_KEY environment variable, ffmpeg in PATH.

Usage:
    python analyze.py [video_dir] [--tests amazon,cnn] [--model claude-opus-4-6]
"""

import argparse
import asyncio
import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import anthropic

ANALYSIS_PROMPT = """You are analyzing browsertime performance test video recordings.
You will be shown frames extracted from two recordings of the same web page:
- BASE: the baseline/reference build
- NEW: the new/modified build being evaluated

Focus ONLY on web page behaviour visible in the recording. Do NOT comment on video
quality, compression artefacts, or encoding differences.

Look for:
1. When does content first appear? Is new faster or slower than base?
2. Blank/white frames — does new have more than base at equivalent points?
3. Layout shifts — visible reflow or jumps during loading?
4. Scroll smoothness — if scrolling is shown, is it jittery vs smooth?

Respond with a JSON object (no markdown, no code fences):
{
  "summary": "One sentence describing the key finding.",
  "regression": true | false | null,
  "severity": "none" | "low" | "medium" | "high",
  "observations": ["concrete observation 1", "concrete observation 2", ...]
}

regression=null means you cannot determine from the frames alone.
Keep observations concrete. Include approximate timestamps where visible."""

# PSNR/SSIM interpretation thresholds
PSNR_IDENTICAL = 50.0  # dB — effectively identical
PSNR_GOOD = 35.0  # dB — minor differences
SSIM_IDENTICAL = 0.99
SSIM_GOOD = 0.95


def check_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        print("Error: ffmpeg not found in PATH. Install it and try again.")
        sys.exit(1)


def extract_frames(video_path: Path, out_dir: Path, label: str, n_frames: int = 5) -> list[Path]:
    """Extract n_frames evenly spaced frames from video_path into out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / f"{label}_%02d.jpg"
    subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(video_path),
            "-vf",
            "fps=1/2",
            "-frames:v",
            str(n_frames),
            "-q:v",
            "2",
            str(pattern),
            "-y",
            "-loglevel",
            "error",
        ],
        check=True,
    )
    return sorted(out_dir.glob(f"{label}_*.jpg"))


def extract_last_frame(video_path: Path, out_path: Path) -> Path:
    """Extract the last frame of a video (fully loaded state)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-sseof",
            "-1",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(out_path),
            "-y",
            "-loglevel",
            "error",
        ],
        check=True,
    )
    return out_path


def compute_psnr_ssim(base_frame: Path, new_frame: Path) -> tuple[float | None, float | None]:
    """Compute PSNR and SSIM between two images using ffmpeg filters."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(base_frame),
            "-i",
            str(new_frame),
            "-filter_complex",
            "[0:v]scale=iw:ih[ref];[1:v]scale=iw:ih[dist];[ref][dist]psnr;[ref][dist]ssim",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    stderr = result.stderr

    psnr: float | None = None
    ssim: float | None = None

    psnr_match = re.search(r"PSNR.*?average:(\S+)", stderr)
    if psnr_match:
        val = psnr_match.group(1)
        psnr = None if val == "inf" else float(val)

    ssim_match = re.search(r"SSIM.*?All:(\S+)", stderr)
    if ssim_match:
        val = ssim_match.group(1).split(" ")[0]
        ssim = 1.0 if val == "inf" else float(val)

    return psnr, ssim


def encode_image(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("utf-8")


async def analyze_pair(
    client: anthropic.AsyncAnthropic,
    base_frames: list[Path],
    new_frames: list[Path],
    model: str,
) -> dict:
    """Send frame pairs to Claude vision and return structured analysis."""
    content: list[dict] = [{"type": "text", "text": "BASE frames (baseline build):"}]
    for f in base_frames:
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": encode_image(f)},
            }
        )

    if new_frames:
        content.append({"type": "text", "text": "NEW frames (modified build):"})
        for f in new_frames:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": encode_image(f),
                    },
                }
            )

    response = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=ANALYSIS_PROMPT,
        messages=[{"role": "user", "content": content}],
    )

    try:
        return json.loads(response.content[0].text)
    except (json.JSONDecodeError, IndexError, KeyError):
        return {
            "summary": "Could not parse analysis response.",
            "regression": None,
            "severity": "none",
            "observations": [response.content[0].text if response.content else "No response."],
        }


def psnr_label(psnr: float | None) -> str:
    if psnr is None:
        return "identical"
    if psnr >= PSNR_IDENTICAL:
        return f"{psnr:.1f} dB (identical)"
    if psnr >= PSNR_GOOD:
        return f"{psnr:.1f} dB (minor diff)"
    return f"{psnr:.1f} dB (significant diff)"


def ssim_label(ssim: float | None) -> str:
    if ssim is None:
        return "N/A"
    if ssim >= SSIM_IDENTICAL:
        return f"{ssim:.4f} (identical)"
    if ssim >= SSIM_GOOD:
        return f"{ssim:.4f} (very similar)"
    return f"{ssim:.4f} (differs)"


def severity_color(severity: str) -> str:
    return {"high": "#e94560", "medium": "#f0a500", "low": "#4ecca3", "none": "#4ecca3"}.get(
        severity, "#888"
    )


def generate_html_report(
    results: dict[str, dict],
    metadata: dict,
    generated_at: str,
    model: str,
    base_last_frames: dict[str, Path],
    new_last_frames: dict[str, Path],
) -> str:
    base_rev = metadata.get("base_revision", "N/A")
    new_rev = metadata.get("new_revision", "N/A")
    mode = metadata.get("mode", "compare")

    regressions = [v for v in results.values() if v.get("regression") is True]
    high = sum(1 for v in regressions if v.get("severity") == "high")
    medium = sum(1 for v in regressions if v.get("severity") == "medium")
    low = sum(1 for v in regressions if v.get("severity") == "low")

    def badge(regression, severity):
        if regression is True:
            color = severity_color(severity)
            return f'<span style="background:{color};color:#000;padding:2px 8px;border-radius:3px;font-size:0.8rem">regression · {severity}</span>'
        if regression is False:
            return '<span style="background:#4ecca3;color:#000;padding:2px 8px;border-radius:3px;font-size:0.8rem">no regression</span>'
        return '<span style="background:#555;color:#eee;padding:2px 8px;border-radius:3px;font-size:0.8rem">?</span>'

    def img_tag(path: Path | None) -> str:
        if path is None or not path.exists():
            return '<div style="background:#0f3460;height:120px;display:flex;align-items:center;justify-content:center;color:#555;font-size:0.8rem">no frame</div>'
        data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
        return f'<img src="data:image/jpeg;base64,{data}" style="width:100%;border-radius:4px">'

    rows = ""
    for key, result in results.items():
        sev = result.get("severity", "none")
        reg = result.get("regression")
        rows += f"""
        <tr>
            <td style="padding:0.5rem 1rem;border-bottom:1px solid #0f3460;font-size:0.85rem">{key}</td>
            <td style="padding:0.5rem 1rem;border-bottom:1px solid #0f3460">{badge(reg, sev)}</td>
            <td style="padding:0.5rem 1rem;border-bottom:1px solid #0f3460;font-size:0.8rem;color:#aaa">{psnr_label(result.get("psnr"))}</td>
            <td style="padding:0.5rem 1rem;border-bottom:1px solid #0f3460;font-size:0.8rem;color:#aaa">{ssim_label(result.get("ssim"))}</td>
            <td style="padding:0.5rem 1rem;border-bottom:1px solid #0f3460;font-size:0.85rem;color:#ccc">{result.get("summary", "")}</td>
        </tr>"""

    cards = ""
    for key, result in results.items():
        reg = result.get("regression")
        sev = result.get("severity", "none")
        border_color = severity_color(sev) if reg is True else "#4ecca3" if reg is False else "#555"
        obs_html = "".join(
            f"<li style='margin-bottom:0.25rem;color:#aaa'>{o}</li>"
            for o in result.get("observations", [])
        )
        base_img = img_tag(base_last_frames.get(key))
        new_img = img_tag(new_last_frames.get(key)) if mode != "single" else ""
        frame_cols = "1fr 1fr" if mode != "single" else "1fr"

        cards += f"""
        <div style="background:#16213e;border-radius:8px;border-left:3px solid {border_color};margin-bottom:1.5rem;overflow:hidden">
            <div style="padding:1rem;display:flex;justify-content:space-between;align-items:center">
                <span style="font-weight:500">{key}</span>
                {badge(reg, sev)}
            </div>
            <div style="padding:0 1rem 1rem;display:grid;grid-template-columns:{frame_cols};gap:1rem">
                <div>
                    <div style="font-size:0.75rem;color:#666;margin-bottom:0.4rem">BASE final frame · {base_rev[:12] if base_rev else "N/A"}</div>
                    {base_img}
                </div>
                {"<div><div style='font-size:0.75rem;color:#666;margin-bottom:0.4rem'>NEW final frame · " + (new_rev[:12] if new_rev else "N/A") + "</div>" + new_img + "</div>" if mode != "single" else ""}
            </div>
            <div style="padding:0 1rem 0.5rem;display:flex;gap:2rem;font-size:0.8rem;color:#888">
                <span>PSNR: {psnr_label(result.get("psnr"))}</span>
                <span>SSIM: {ssim_label(result.get("ssim"))}</span>
            </div>
            <div style="padding:0.75rem 1rem;border-top:1px solid #0f3460;font-size:0.85rem">
                <strong>{result.get("summary", "")}</strong>
                {"<ul style='margin-top:0.5rem;padding-left:1.25rem'>" + obs_html + "</ul>" if obs_html else ""}
            </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Perf Video Analysis Report</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #1a1a2e; color: #eee; padding: 2rem; }}
h1 {{ font-size: 1.5rem; font-weight: 500; margin-bottom: 0.5rem; }}
table {{ width: 100%; border-collapse: collapse; background: #16213e; border-radius: 8px; overflow: hidden; }}
th {{ padding: 0.75rem 1rem; text-align: left; font-size: 0.8rem; text-transform: uppercase; color: #666; border-bottom: 2px solid #0f3460; }}
a {{ color: #4ecca3; }}
</style>
</head>
<body>
<h1>Perf Video Analysis Report</h1>
<div style="color:#888;font-size:0.85rem;margin-bottom:2rem">
    Generated {generated_at} · Model: {model} ·
    Base: <code style="color:#e94560">{base_rev[:12] if base_rev else "N/A"}</code>
    {f'· New: <code style="color:#4ecca3">{new_rev[:12] if new_rev else "N/A"}</code>' if mode != "single" else ""}
</div>

<div style="display:flex;gap:1.5rem;margin-bottom:2rem">
    <div style="background:#16213e;padding:1rem 1.5rem;border-radius:8px;text-align:center">
        <div style="font-size:2rem;font-weight:600">{len(results)}</div>
        <div style="color:#888;font-size:0.8rem">analyzed</div>
    </div>
    <div style="background:#16213e;padding:1rem 1.5rem;border-radius:8px;text-align:center;border-left:3px solid #e94560">
        <div style="font-size:2rem;font-weight:600;color:#e94560">{high}</div>
        <div style="color:#888;font-size:0.8rem">high</div>
    </div>
    <div style="background:#16213e;padding:1rem 1.5rem;border-radius:8px;text-align:center;border-left:3px solid #f0a500">
        <div style="font-size:2rem;font-weight:600;color:#f0a500">{medium}</div>
        <div style="color:#888;font-size:0.8rem">medium</div>
    </div>
    <div style="background:#16213e;padding:1rem 1.5rem;border-radius:8px;text-align:center;border-left:3px solid #4ecca3">
        <div style="font-size:2rem;font-weight:600;color:#4ecca3">{low}</div>
        <div style="color:#888;font-size:0.8rem">low</div>
    </div>
</div>

<h2 style="font-size:1rem;font-weight:500;margin-bottom:1rem">Summary</h2>
<table style="margin-bottom:2rem">
    <thead><tr>
        <th>Test</th><th>Status</th><th>PSNR</th><th>SSIM</th><th>Finding</th>
    </tr></thead>
    <tbody>{rows}</tbody>
</table>

<h2 style="font-size:1rem;font-weight:500;margin-bottom:1rem">Details</h2>
{cards}
</body>
</html>"""


async def _run(args: argparse.Namespace) -> None:
    check_ffmpeg()

    video_dir = Path(args.video_dir)
    meta_path = video_dir / "comparisons.json"
    if not meta_path.exists():
        print(f"Error: {meta_path} not found. Run perf_sxs.py first.")
        sys.exit(1)

    with open(meta_path) as f:
        metadata = json.load(f)

    comparisons = metadata.get("comparisons", {})
    mode = metadata.get("mode", "compare")

    filters = [t.strip().lower() for t in args.tests.split(",")] if args.tests else None
    if filters:
        comparisons = {k: v for k, v in comparisons.items() if any(f in k.lower() for f in filters)}

    if not comparisons:
        print("No comparisons to analyze.")
        sys.exit(0)

    total = len(comparisons)
    print(f"Analyzing {total} comparison(s) in {video_dir} (concurrency={args.concurrency})")
    print(f"Model: {args.model}\n")

    client = anthropic.AsyncAnthropic()
    semaphore = asyncio.Semaphore(args.concurrency)
    results: dict[str, dict] = {}
    base_last_frames: dict[str, Path] = {}
    new_last_frames: dict[str, Path] = {}
    completed = 0

    async def analyze_one(key: str, comp: dict, frame_dir: Path) -> None:
        nonlocal completed
        async with semaphore:
            try:
                base_idx = comp.get("base_median_idx") or 0
                base_video = video_dir / comp["base_videos"][base_idx]
                base_frames = extract_frames(base_video, frame_dir, "base")
                base_last = extract_last_frame(base_video, frame_dir / "base_last.jpg")
                base_last_frames[key] = base_last

                new_frames: list[Path] = []
                new_last: Path | None = None
                if mode != "single" and comp.get("new_videos"):
                    new_idx = comp.get("new_median_idx") or 0
                    new_video = video_dir / comp["new_videos"][new_idx]
                    new_frames = extract_frames(new_video, frame_dir, "new")
                    new_last = extract_last_frame(new_video, frame_dir / "new_last.jpg")
                    new_last_frames[key] = new_last

                psnr, ssim = None, None
                if new_last and base_last.exists() and new_last.exists():
                    psnr, ssim = compute_psnr_ssim(base_last, new_last)

                result = await analyze_pair(client, base_frames, new_frames, args.model)
                result["psnr"] = psnr
                result["ssim"] = ssim
                results[key] = result

                completed += 1
                regression_str = {True: "REGRESSION", False: "ok", None: "?"}
                psnr_str = f"PSNR={psnr:.1f}dB" if psnr is not None else "PSNR=identical"
                print(
                    f"  [{completed}/{total}] {regression_str.get(result.get('regression'), '?')} "
                    f"[{result.get('severity', '?')}] {psnr_str} — {key.split('/')[-1]}"
                )

            except Exception as e:
                completed += 1
                print(f"  [{completed}/{total}] Error: {key} — {e}")
                results[key] = {
                    "summary": f"Analysis failed: {e}",
                    "regression": None,
                    "severity": "none",
                    "observations": [],
                    "psnr": None,
                    "ssim": None,
                }

    with tempfile.TemporaryDirectory(prefix="perf_analysis_") as tmp:
        tmp_path = Path(tmp)
        await asyncio.gather(
            *[
                analyze_one(key, comp, tmp_path / key.replace("/", "_"))
                for key, comp in comparisons.items()
            ]
        )

    generated_at = datetime.now(UTC).isoformat()
    json_path = video_dir / "analysis.json"
    html_path = video_dir / "analysis_report.html"

    analysis = {
        "generated_at": generated_at,
        "model": args.model,
        "comparisons": results,
    }
    with open(json_path, "w") as f:
        json.dump(analysis, f, indent=2)

    html = generate_html_report(
        results, metadata, generated_at, args.model, base_last_frames, new_last_frames
    )
    with open(html_path, "w") as f:
        f.write(html)

    regressions = [v for v in results.values() if v.get("regression") is True]
    high = sum(1 for v in regressions if v.get("severity") == "high")
    medium = sum(1 for v in regressions if v.get("severity") == "medium")
    low = sum(1 for v in regressions if v.get("severity") == "low")

    print(
        f"\nDone. {len(results)} analyzed, {len(regressions)} regression(s) "
        f"[high={high} medium={medium} low={low}]"
    )
    print(f"JSON:   {json_path}")
    print(f"Report: {html_path}")
    print("Refresh the viewer to see the analysis panel.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze perf-sxs video comparisons")
    parser.add_argument("video_dir", nargs="?", default="./sxs_videos")
    parser.add_argument("--tests", "-t", help="Comma-separated test name filters", default=None)
    parser.add_argument("--model", default="claude-opus-4-6", help="Claude model to use")
    parser.add_argument(
        "--concurrency",
        "-c",
        type=int,
        default=5,
        help="Max concurrent API calls (default: 5)",
    )
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

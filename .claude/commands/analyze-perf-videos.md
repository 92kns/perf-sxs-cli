Analyze browsertime video comparisons for visual regressions and write findings to `analysis.json` and `analysis_report.html`.

## Arguments

`$ARGUMENTS` may contain:
- A path to the video directory (default: `./sxs_videos`)
- Optional test filters as comma-separated substrings (e.g. `amazon,cnn`)

Parse `$ARGUMENTS`: if it looks like a path use it as the directory, otherwise treat as filters against `./sxs_videos`.

## Steps

### 1. Load comparisons

Read `{video_dir}/comparisons.json`. If it doesn't exist, tell the user and stop.

Extract the list of comparisons. If test filters were provided, only include comparisons whose key contains at least one filter substring.

### 2. Check ffmpeg

Run `which ffmpeg` — if not found, tell the user to install ffmpeg and stop.

### 3. Extract frames and analyze each comparison

For each comparison (up to 20; if more exist, tell the user and suggest using filters):

**a. Find the videos**

- Base video: `{video_dir}/{comp.base_videos[comp.base_median_idx or 0]}`
- New video: `{video_dir}/{comp.new_videos[comp.new_median_idx or 0]}` (skip if single-revision mode)

**b. Extract 5 frames + last frame per video** to a temp directory:

```bash
ffmpeg -i {video} -vf "fps=1/2" -frames:v 5 -q:v 2 /tmp/perf_analysis/{safe_key}/{label}_%02d.jpg -y -loglevel error
ffmpeg -sseof -1 -i {video} -frames:v 1 -q:v 2 /tmp/perf_analysis/{safe_key}/{label}_last.jpg -y -loglevel error
```

Where `safe_key` is the comparison key with `/` replaced by `_`.

**b2. Compute PSNR/SSIM on final frames** (base_last vs new_last only — avoids timing-offset false positives):

```bash
ffmpeg -i base_last.jpg -i new_last.jpg \
  -filter_complex "[0:v]scale=iw:ih[ref];[1:v]scale=iw:ih[dist];[ref][dist]psnr;[ref][dist]ssim" \
  -f null -
```

Parse `PSNR.*average:(\S+)` and `SSIM.*All:(\S+)` from stderr. Store as `psnr` and `ssim` in results. PSNR=inf or SSIM=inf means identical frames.

**c. Read all extracted frames** using the Read tool — read base frames first, then new frames.

**d. Analyze** what you see, focused on what matters for web performance regression detection:
- **When does content first appear?** Compare base vs new — is new slower or faster to show anything?
- **Blank/white frames** — does new have more blank frames than base at equivalent timestamps?
- **Layout shifts** — any visible jumps or reflow during loading?
- **Scroll smoothness** — if scrolling is visible, does it look jittery vs smooth?
- **Overall visual impression** — does new feel faster, slower, or the same?

Do NOT comment on video quality, compression, or encoding artefacts — focus purely on the web page behaviour visible in the recording.

**e. Clean up temp frames:**

```bash
rm -rf /tmp/perf_analysis/{safe_key}
```

### 4. Write analysis.json

Write `{video_dir}/analysis.json` with this structure:

```json
{
  "generated_at": "<ISO timestamp>",
  "model": "<model you are>",
  "comparisons": {
    "platform/test-name": {
      "summary": "One sentence summary of the key finding.",
      "regression": true | false | null,
      "severity": "none" | "low" | "medium" | "high",
      "psnr": 38.5,
      "ssim": 0.973,
      "observations": [
        "Base: content appears at ~1.2s. New: content appears at ~1.8s — ~0.6s slower.",
        "New has 2 blank frames early in the recording not present in base.",
        "Scroll behaviour looks equivalent in both."
      ]
    }
  }
}
```

Also write a self-contained `{video_dir}/analysis_report.html` containing:
- Summary stats (counts by severity)
- A table of all tests with status, PSNR, SSIM, and one-line finding
- Per-test cards with base64-inlined final frame thumbnails side-by-side and full observations
- Dark theme matching the viewer

Inline all images as base64 `data:image/jpeg;base64,...` so the file is fully self-contained and shareable (e.g. attach to a Bugzilla comment).

- `regression: null` means you could not determine (e.g. single-revision mode or frames unclear)
- Keep `observations` concrete and timestamped where possible
- Keep `summary` to one sentence

### 5. Report to user

After writing the files, print a brief summary:
- How many comparisons were analyzed
- How many regressions found (severity breakdown)
- Paths to analysis.json and analysis_report.html
- Remind them to refresh the viewer to see the analysis panel

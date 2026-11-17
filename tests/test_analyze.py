"""Tests for perf-sxs-analyze: visual metrics, prepare (real ffmpeg), judging, report."""

import asyncio
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from perf_sxs import analyze

BLANK = bytes([255] * 100)
HALF = bytes([0] * 50 + [255] * 50)
FULL = bytes([0] * 100)


class TestVisualMetrics:
    def test_progress_curve(self):
        assert analyze.progress_curve([BLANK, HALF, FULL]) == [0.0, 0.5, 1.0]

    def test_static_video_is_complete(self):
        assert analyze.progress_curve([FULL, FULL]) == [1.0, 1.0]

    def test_metrics(self):
        times = [0.0, 100.0, 200.0, 300.0, 400.0]
        frames = [BLANK, BLANK, HALF, FULL, FULL]
        m = analyze.visual_metrics(times, frames)
        assert m["first_change_ms"] == 200
        assert m["visual_complete_ms"] == 300
        # 100ms at 0% + 100ms at 0% + 100ms at 50% -> 250
        assert m["speed_index"] == 250

    def test_regressing_progress_delays_visual_complete(self):
        times = [0.0, 100.0, 200.0, 300.0]
        frames = [BLANK, FULL, HALF, FULL]  # content flashes in, disappears, returns
        assert analyze.visual_metrics(times, frames)["visual_complete_ms"] == 300

    def test_milestones_follow_progress(self):
        times = [0.0, 100.0, 200.0, 300.0]
        frames = [BLANK, HALF, FULL, FULL]
        labels = [label for label, _ in analyze.milestone_times(times, frames)]
        assert labels == ["first change", "50%", "85%", "visually complete"]
        assert [t for _, t in analyze.milestone_times(times, frames)] == [100, 100, 200, 200]

    def test_summarize_runs(self):
        runs = [{"speed_index": v, "first_change_ms": None} for v in (300, 100, 200)]
        s = analyze.summarize_runs(runs)
        assert s["runs"] == 3 and s["speed_index"] == 200
        assert s["speed_index_range"] == [100, 300]
        assert s["first_change_ms"] is None


class TestBrowsertimeMetrics:
    def test_reads_saved_perfherder_data(self, tmp_path):
        task = tmp_path / "base/plat/test/TASK"
        task.mkdir(parents=True)
        (task / "perfherder-data.json").write_text(
            json.dumps(
                {"suites": [{"subtests": [{"name": "SpeedIndex", "replicates": [10, 30, 20]}]}]}
            )
        )
        video = "base/plat/test/TASK/bt/pages/a/data/video/2.mp4"
        m = analyze.browsertime_metrics(tmp_path, video, None)
        assert m == {"SpeedIndex": {"run": 30, "median": 20, "runs": 3}}

    def test_missing_file_or_step(self, tmp_path):
        assert analyze.browsertime_metrics(tmp_path, "base/p/t/T/x/1.mp4", None) is None
        assert analyze.browsertime_metrics(tmp_path, "base/p/t/T/x/1.mp4", "step") is None

    def test_deltas_prefer_browsertime(self):
        bt = {"browsertime": {"SpeedIndex": {"median": 100}}, "all_runs": {}}
        bt_new = {"browsertime": {"SpeedIndex": {"median": 110}}, "all_runs": {}}
        assert analyze.deltas(bt, bt_new) == {"source": "browsertime", "SpeedIndex": 10.0}
        video = {"all_runs": {"speed_index": 100}}
        video_new = {"all_runs": {"speed_index": 50}}
        d = analyze.deltas(video, video_new)
        assert d["source"] == "video" and d["speed_index"] == -50.0


class TestMergeAndReport:
    MANIFEST = {
        "base_revision": "a" * 40,
        "new_revision": "b" * 40,
        "base_label": "base",
        "new_label": "new",
        "comparisons": {
            "p/t1": {"key": "p/t1", "sides": {}, "perfcompare": []},
            "p/t2": {"key": "p/t2", "error": "boom", "perfcompare": []},
        },
    }

    def test_verdict_maps_to_viewer_regression_flag(self):
        j = {
            "p/t1": {
                "summary": "s",
                "verdict": "regression",
                "severity": "high",
                "observations": [],
            }
        }
        out = analyze.merge(self.MANIFEST, j, "m")["comparisons"]
        assert out["p/t1"]["regression"] is True and out["p/t1"]["severity"] == "high"
        assert out["p/t2"]["regression"] is None
        assert "boom" in out["p/t2"]["summary"]

    def test_bad_values_are_normalized(self):
        j = {"p/t1": {"verdict": "maybe", "severity": "extreme"}}
        c = analyze.merge(self.MANIFEST, j, None)["comparisons"]["p/t1"]
        assert (c["verdict"], c["severity"], c["regression"]) == ("inconclusive", "none", None)

    def test_report_escapes_model_text(self, tmp_path):
        j = {"p/t1": {"summary": "<script>x</script>", "verdict": "improvement",
                      "severity": "low", "observations": ["<b>o</b>"]}}  # fmt: skip
        page = analyze.render_report(analyze.merge(self.MANIFEST, j, "m"), self.MANIFEST, tmp_path)
        assert "<script>x" not in page and "&lt;script&gt;" in page
        assert "<b>o</b>" not in page


def _video(path: Path, colors: list[str]) -> None:
    """A tiny CFR video showing each color for 0.2 s."""
    path.parent.mkdir(parents=True, exist_ok=True)
    inputs = []
    for c in colors:
        inputs += ["-f", "lavfi", "-i", f"color=c={c}:s=160x120:r=25:d=0.2"]
    n = len(colors)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex",
         "".join(f"[{i}]" for i in range(n)) + f"concat=n={n}:v=1[v]", "-map", "[v]",
         "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )  # fmt: skip


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
class TestPrepare:
    def _dir(self, tmp_path: Path) -> Path:
        base = "base/p/t/B/bt/pages/a/data/video/1.mp4"
        new = "new/p/t/N/bt/pages/a/data/video/1.mp4"
        _video(tmp_path / base, ["white", "black", "black"])
        _video(tmp_path / new, ["white", "white", "black"])
        (tmp_path / "comparisons.json").write_text(
            json.dumps(
                {
                    "mode": "compare",
                    "base_revision": "a",
                    "new_revision": "b",
                    "comparisons": {
                        "p/t": {
                            "platform": "p", "test_name": "t", "step": None,
                            "base_videos": [base], "new_videos": [new],
                            "base_median_idx": None, "new_median_idx": None,
                            "perfcompare": [{"suite": "t", "delta_pct": 12.0}],
                        }
                    },
                }
            )
        )  # fmt: skip
        return tmp_path

    def test_prepare_measures_and_renders_sheet(self, tmp_path):
        d = self._dir(tmp_path)
        manifest = analyze.prepare(d)
        entry = manifest["comparisons"]["p/t"]
        base = entry["sides"]["base"]["median_run"]
        new = entry["sides"]["new"]["median_run"]
        assert base["first_change_ms"] == 200
        assert new["first_change_ms"] == 400
        assert entry["deltas_pct"]["first_change_ms"] == 100.0
        assert (d / entry["sheet"]).stat().st_size > 0
        assert "perfcompare: t" in entry["context"]
        assert (d / "analysis/instructions.md").read_text().startswith("You review")
        # The per-frame JPEGs are folded into the sheet and removed.
        assert sorted(p.name for p in (d / entry["sheet"]).parent.iterdir()) == ["sheet.jpg"]

    def test_cli_prepare_then_report(self, tmp_path):
        d = self._dir(tmp_path)
        analyze.main(["prepare", str(d), "-t", "p/t"])
        j = tmp_path / "j.json"
        j.write_text(json.dumps({"p/t": {"summary": "slower", "verdict": "regression",
                                          "severity": "medium", "observations": ["x"]}}))  # fmt: skip
        analyze.main(["report", str(d), "--judgments", str(j), "--model", "agent"])
        out = json.loads((d / "analysis.json").read_text())
        assert out["model"] == "agent"
        assert out["comparisons"]["p/t"]["regression"] is True
        assert (d / "analysis_report.html").exists()

    def test_run_with_fake_api(self, tmp_path, monkeypatch):
        d = self._dir(tmp_path)
        requests = []

        class FakeMessages:
            async def create(self, **kwargs):
                requests.append(kwargs)
                text = json.dumps({"summary": "s", "verdict": "regression",
                                   "severity": "low", "observations": []})  # fmt: skip
                return types.SimpleNamespace(
                    stop_reason="end_turn",
                    content=[types.SimpleNamespace(type="text", text=text)],
                )

        class FakeClient:
            messages = FakeMessages()

        fake = types.ModuleType("anthropic")
        fake.AsyncAnthropic = FakeClient  # type: ignore[attr-defined]
        fake.APIError = RuntimeError  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "anthropic", fake)
        analyze.main(["run", str(d)])

        (req,) = requests
        assert req["model"] == analyze.DEFAULT_MODEL
        assert req["output_config"]["format"]["schema"] == analyze.JUDGMENT_SCHEMA
        image, text = req["messages"][0]["content"]
        assert image["type"] == "image" and "Comparison: p/t" in text["text"]
        out = json.loads((d / "analysis.json").read_text())
        assert out["comparisons"]["p/t"]["verdict"] == "regression"

    def test_refusal_is_inconclusive(self, tmp_path):
        d = self._dir(tmp_path)
        manifest = analyze.prepare(d)

        class Client:
            class messages:  # noqa: N801
                @staticmethod
                async def create(**kwargs):
                    return types.SimpleNamespace(stop_reason="refusal", content=[])

        entry = manifest["comparisons"]["p/t"]
        j = asyncio.run(analyze._judge_one(Client, "m", entry, manifest, d))
        assert j["verdict"] == "inconclusive"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
class TestFrameExtraction:
    def test_exact_frame_including_last(self, tmp_path):
        # 60 fps: frame times like 16.667 ms round up past the pts at 1 ms precision.
        v = tmp_path / "v.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=64x48:r=60:d=1",
             "-pix_fmt", "yuv420p", str(v)],
            check=True,
        )  # fmt: skip
        times, frames = analyze.decode_video(v)
        for i in (1, len(times) - 1):
            out = analyze.extract_frame(v, times[i], tmp_path / f"{i}.jpg")
            assert out.stat().st_size > 0


def test_safe_name_is_unique():
    assert analyze.safe_name("linux/x::y") != analyze.safe_name("linux/x_y")

"""Tests for perfcompare rows, step pruning, --resume, profiled runs and the CLI pipeline."""

import asyncio
import io
import json
import tarfile
from pathlib import Path

import pytest

import perf_sxs.cli as cli
import perf_sxs.download as download_mod
from perf_sxs.cli import CliError, attach_perfcompare, build_parser
from perf_sxs.models import VideoTask
from perf_sxs.selection import (
    rows_for_step,
    significant_rows,
    significant_steps,
    summarize_row,
)

NAV = "test-linux2404-64-shippable/opt-browsertime-nav-bench-firefox-nav-bench"
AMAZON = "test-linux2404-64-shippable/opt-browsertime-tp6-firefox-amazon"
PLATFORM = "linux2404-64-shippable"


def _row(suite: str, platform: str = PLATFORM, **extra) -> dict:
    return {"suite": suite, "test": "", "platform": platform, **extra}


def _task(name: str, task_id: str) -> dict:
    return {
        "task": {"metadata": {"name": name}},
        "status": {"taskId": task_id, "state": "completed"},
    }


def _make_tgz(path: Path, members: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        for name in members:
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))


class TestSummarizeRow:
    def test_student_t_row(self):
        r = summarize_row(
            {
                "suite": "amazon", "test": "", "platform": PLATFORM,
                "base_avg_value": 100.0, "new_avg_value": 110.0, "delta_percentage": 10.0,
                "is_regression": True, "confidence_text": "High",
                "new_measurement_unit": "ms", "lower_is_better": True,
            }
        )  # fmt: skip
        assert r["direction"] == "regression"
        assert (r["base"], r["new"], r["delta_pct"], r["unit"]) == (100.0, 110.0, 10.0, "ms")

    def test_mwu_row_uses_medians(self):
        # Real MWU rows (Treeherder, Nov 2026) have no avg/delta fields.
        r = summarize_row(
            {
                "suite": "nav-bench.amazon-nav-load", "platform": PLATFORM,
                "direction_of_change": "improvement",
                "base_standard_stats": {"median": 771.0}, "new_standard_stats": {"median": 750.5},
            }
        )  # fmt: skip
        assert r["direction"] == "improvement"
        assert r["delta_pct"] == pytest.approx(-2.66, abs=0.01)

    def test_significant_rows_filters(self):
        rows = significant_rows(
            [_row("a", confidence_text="High"), _row("b", confidence_text="Low"), "junk"],
            "student-t",
        )
        assert [r["suite"] for r in rows] == ["a"]


class TestSignificantSteps:
    def test_only_flagged_steps(self):
        rows = [_row("nav-bench.amazon-nav-load"), _row("nav-bench.bbc-nav-load")]
        assert significant_steps(NAV, rows) == {"amazon-nav-load", "bbc-nav-load"}

    def test_other_platform_ignored(self):
        rows = [_row("nav-bench.amazon-nav-load"), _row("nav-bench.cnn-nav-load", "macosx")]
        assert significant_steps(NAV, rows) == {"amazon-nav-load"}

    def test_single_page_test_keeps_everything(self):
        assert significant_steps(AMAZON, [_row("amazon")]) is None

    def test_rows_for_step(self):
        rows = [_row("nav-bench.a"), _row("nav-bench.b"), _row("nav-bench")]
        assert [r["suite"] for r in rows_for_step(rows, "a")] == ["nav-bench.a", "nav-bench"]
        assert rows_for_step(rows, None) == rows


class TestDownloadPruning:
    """Through download_video_artifacts with the network stubbed out."""

    def _run(self, monkeypatch, tmp_path, members, *, steps=None, ph=None, **kwargs):
        calls = []

        async def fake_download(session, task_id, artifact_name, output_path, semaphore, cb=None):
            if not artifact_name.endswith("browsertime-videos-annotated.tgz"):
                return False
            calls.append(task_id)
            _make_tgz(output_path, members)
            return True

        async def fake_ph(session, task_id, semaphore):
            return ph

        monkeypatch.setattr(download_mod, "download_artifact", fake_download)
        monkeypatch.setattr(download_mod, "fetch_perfherder_data", fake_ph)
        vt = VideoTask("T1", "t", "p", "base", "r", steps=steps)
        res = asyncio.run(
            download_mod.download_video_artifacts(None, [vt], tmp_path, quiet=True, **kwargs)
        )
        return res, calls

    NAV_MEMBERS = [
        f"bt/nav/pages/{s}/{s}-nav-load/data/video/{i}.mp4" for s in ("a", "b", "c") for i in (1, 2)
    ]

    def test_keeps_only_significant_steps(self, monkeypatch, tmp_path):
        res, _ = self._run(monkeypatch, tmp_path, self.NAV_MEMBERS, steps={"b-nav-load"})
        assert {p.parent.parent.parent.name for p in res["base"]["videos"]} == {"b-nav-load"}
        assert {p.parent.parent.parent.name for p in tmp_path.rglob("*.mp4")} == {"b-nav-load"}

    def test_all_runs_with_steps(self, monkeypatch, tmp_path):
        from perf_sxs.organize import organize_single_revision

        ph = {
            "suites": [{"subtests": [{"name": "a-nav-load-x", "value": 0, "replicates": [5, 1]}]}]
        }
        res, _ = self._run(
            monkeypatch, tmp_path, self.NAV_MEMBERS, steps={"a-nav-load"}, all_runs=True, ph=ph
        )
        assert len(res["base"]["videos"]) == 2
        assert len(list(tmp_path.rglob("*.mp4"))) == 2
        # One step left: organize still labels it, with its median.
        (comp,) = organize_single_revision(tmp_path).values()
        assert comp["step"] == "a-nav-load"
        assert comp["base_median_idx"] == 0

    def test_unknown_steps_keep_everything(self, monkeypatch, tmp_path):
        res, _ = self._run(monkeypatch, tmp_path, self.NAV_MEMBERS, steps={"nope"})
        assert len(res["base"]["videos"]) == 3  # one median per step

    def test_profiled_runs_dropped_by_default(self, monkeypatch, tmp_path):
        members = [f"app/vid{i}_fenix.mp4" for i in range(2)] + [
            f"app/simpleperf/vid{i}_fenix.mp4" for i in range(2)
        ]
        res, _ = self._run(monkeypatch, tmp_path, members, all_runs=True)
        assert all("simpleperf" not in str(p) for p in res["base"]["videos"])
        assert not list(tmp_path.rglob("simpleperf/*.mp4"))

    def test_profiled_runs_kept_on_request(self, monkeypatch, tmp_path):
        members = ["app/vid0_fenix.mp4", "app/simpleperf/vid0_fenix.mp4"]
        res, _ = self._run(monkeypatch, tmp_path, members, all_runs=True, include_profiled=True)
        assert len(res["base"]["videos"]) == 2

    def test_saves_perfherder_data(self, monkeypatch, tmp_path):
        ph = {"suites": [{"subtests": [{"value": 1, "replicates": [1, 2, 3]}]}]}
        members = [f"bt/x/pages/a/data/video/{i}.mp4" for i in (1, 2, 3)]
        self._run(monkeypatch, tmp_path, members, ph=ph)
        saved = tmp_path / "base/p/t/T1" / download_mod.PERFHERDER_DATA_FILE
        assert json.loads(saved.read_text()) == ph

    def test_resume_skips_finished_task(self, monkeypatch, tmp_path):
        members = [f"bt/x/pages/a/data/video/{i}.mp4" for i in (1, 2)]
        _, first = self._run(monkeypatch, tmp_path, members)
        res, second = self._run(monkeypatch, tmp_path, members, resume=True)
        assert first == ["T1"] and second == []
        assert len(res["base"]["videos"]) == 1

    def test_resume_redoes_unfinished_task(self, monkeypatch, tmp_path):
        (tmp_path / "base/p/t/T1").mkdir(parents=True)  # dir but no marker
        _, calls = self._run(monkeypatch, tmp_path, ["bt/x/pages/a/data/video/1.mp4"], resume=True)
        assert calls == ["T1"]


class TestAttachPerfcompare:
    def test_rows_per_step(self):
        rows = [_row("nav-bench.amazon-nav-load"), _row("nav-bench.bbc-nav-load")]
        vts = [
            VideoTask(
                "N", "browsertime-nav-bench-firefox-nav-bench", "P", "base", "r", task_name=NAV
            )
        ]
        comps = {
            "a": {"platform": "P", "test_name": vts[0].test_name, "step": "amazon-nav-load"},
            "b": {"platform": "P", "test_name": "other", "step": None},
        }
        attach_perfcompare(comps, vts, rows)
        assert [r["suite"] for r in comps["a"]["perfcompare"]] == ["nav-bench.amazon-nav-load"]
        assert "perfcompare" not in comps["b"]  # not downloaded in this run

    def test_resume_keeps_rows_of_earlier_downloads(self):
        comps = {"old": {"platform": "P", "test_name": "google"}}
        previous = {"old": {"perfcompare": [{"suite": "google"}]}}
        attach_perfcompare(comps, [], [_row("amazon")], previous)
        assert comps["old"]["perfcompare"] == [{"suite": "google"}]

    def test_no_rows_no_field(self):
        comps = {"a": {"platform": "P", "test_name": "t"}}
        attach_perfcompare(comps, [], None)
        assert "perfcompare" not in comps["a"]


class TestRunPipeline:
    """`cli.run` end to end with Taskcluster/Treeherder/downloads stubbed."""

    URL = "https://perf.compare/compare-results?baseRev=" + "a" * 40 + "&newRev=" + "b" * 40

    def _stub(self, monkeypatch, rows):
        seen: dict = {}

        async def groups(session, rev, repo):
            return [f"G{rev[0]}"]

        async def tasks(session, gids):
            ch = gids[0][-1]
            return [_task(NAV, f"N{ch}"), _task(AMAZON, f"A{ch}")]

        async def sig(session, url, version):
            return rows

        async def download(session, vts, out, *a, **k):
            seen["vts"] = vts
            seen["kwargs"] = k
            result: dict = {
                "base": {"videos": [], "images": []},
                "new": {"videos": [], "images": []},
            }
            for vt in vts:
                if vt.steps:
                    d = out / vt.label / vt.platform / vt.test_name / vt.task_id
                    d.mkdir(parents=True, exist_ok=True)
                    (d / "kept_steps.json").write_text(json.dumps(sorted(vt.steps)))
                for step in sorted(vt.steps or {"x"}):
                    p = (
                        out
                        / vt.label
                        / vt.platform
                        / vt.test_name
                        / vt.task_id
                        / "bt/pages/s"
                        / step
                        / "data/video/1.mp4"
                    )
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"x")
                    result[vt.label]["videos"].append(p)
            return result

        monkeypatch.setattr(cli, "find_task_group_ids", groups)
        monkeypatch.setattr(cli, "get_tasks_for_revision", tasks)
        monkeypatch.setattr(cli, "fetch_significant_rows", sig)
        monkeypatch.setattr(cli, "download_video_artifacts", download)
        return seen

    def _args(self, tmp_path, *extra):
        return build_parser().parse_args(
            [self.URL, "--no-serve", "--json", "-o", str(tmp_path / "o"), *extra]
        )

    def test_steps_and_perfcompare_rows(self, monkeypatch, tmp_path):
        seen = self._stub(
            monkeypatch,
            [_row("nav-bench.amazon-nav-load", direction="regression", delta_pct=5.0)],
        )
        result = asyncio.run(cli.run(self._args(tmp_path)))
        assert [vt.task_id for vt in seen["vts"]] == ["Na", "Nb"]
        assert all(vt.steps == {"amazon-nav-load"} for vt in seen["vts"])
        (comp,) = result.comparisons.values()
        assert comp["step"] == "amazon-nav-load"
        assert comp["perfcompare"][0]["delta_pct"] == 5.0
        assert result.summary({})["significant_tests"] == 1
        saved = json.loads(result.meta_path.read_text())
        assert saved["comparisons"][next(iter(saved["comparisons"]))]["perfcompare"]

    def test_all_steps_flag(self, monkeypatch, tmp_path):
        seen = self._stub(monkeypatch, [_row("nav-bench.amazon-nav-load")])
        asyncio.run(cli.run(self._args(tmp_path, "--all-steps")))
        assert all(vt.steps is None for vt in seen["vts"])

    def test_no_significant_rows_is_an_error(self, monkeypatch, tmp_path):
        self._stub(monkeypatch, [])
        with pytest.raises(CliError, match="No significant changes"):
            asyncio.run(cli.run(self._args(tmp_path)))

    def test_resume_passes_through_and_checks_revisions(self, monkeypatch, tmp_path):
        seen = self._stub(monkeypatch, [_row("amazon")])
        asyncio.run(cli.run(self._args(tmp_path)))
        asyncio.run(cli.run(self._args(tmp_path, "--resume")))
        assert seen["kwargs"]["resume"] is True

        other = "https://perf.compare/compare-results?baseRev=" + "c" * 40 + "&newRev=" + "b" * 40
        args = build_parser().parse_args(
            [other, "--no-serve", "--json", "-o", str(tmp_path / "o"), "--resume"]
        )
        with pytest.raises(CliError, match="different comparison"):
            asyncio.run(cli.run(args))

    def test_force_and_resume_conflict(self, tmp_path):
        with pytest.raises(CliError, match="can't be combined"):
            asyncio.run(cli.run(self._args(tmp_path, "--force", "--resume")))


class TestColdWarmLoads:
    """tp6 essential/bytecode tasks: one cold and one warm load per task.

    Layout and perfherder shape from autoland tp6-essential-firefox-amazon (Nov 2026).
    """

    BASE = "bt/amazon/pages/www_amazon_com/s"

    def test_steps_are_labelled_cold_and_warm(self, tmp_path):
        from perf_sxs.organize import group_videos_by_step

        mp4s = []
        for kind, h in (("cold", "query-2f96b7ee"), ("warm", "query-79238515")):
            for i in (1, 2):
                p = tmp_path / self.BASE / kind / h / f"data/video/{i}.mp4"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"x")
                mp4s.append(p)
        assert list(group_videos_by_step(mp4s)) == ["cold", "warm"]

    def test_median_uses_the_steps_own_suite(self):
        from perf_sxs.selection import find_median_run_index

        data = {
            "suites": [
                {
                    "extraOptions": ["fission", "cold"],
                    "subtests": [{"value": 5, "replicates": [9, 5, 1]}],
                },
                {
                    "extraOptions": ["fission", "warm"],
                    "subtests": [{"value": 1, "replicates": [9, 5, 1]}],
                },
            ]
        }
        assert find_median_run_index(data, step="cold") == 1
        assert find_median_run_index(data, step="warm") == 2

    def test_rows_split_by_extra_options(self):
        rows = [
            _row("amazon", extra_options="cold fission webrender"),
            _row("amazon", extra_options="fission webrender warm"),
        ]
        assert significant_steps(AMAZON, rows[:1]) == {"cold"}
        assert significant_steps(AMAZON, rows) == {"cold", "warm"}
        assert rows_for_step(rows, "warm") == rows[1:]
        # A row without a load kind applies to the whole task.
        assert significant_steps(AMAZON, [_row("amazon", extra_options="fission")]) is None

    def test_browsertime_metrics_per_kind(self, tmp_path):
        from perf_sxs.analyze import browsertime_metrics

        task = tmp_path / "base/p/t/T"
        task.mkdir(parents=True)
        (task / "perfherder-data.json").write_text(
            json.dumps(
                {
                    "suites": [
                        {
                            "extraOptions": ["cold"],
                            "subtests": [{"name": "SpeedIndex", "replicates": [100]}],
                        },
                        {
                            "extraOptions": ["warm"],
                            "subtests": [{"name": "SpeedIndex", "replicates": [50]}],
                        },
                    ]
                }
            )
        )
        video = "base/p/t/T/bt/warm/q/data/video/1.mp4"
        assert browsertime_metrics(tmp_path, video, "warm")["SpeedIndex"]["run"] == 50


class TestResumeOptions:
    def test_resume_redoes_task_pruned_differently(self, monkeypatch, tmp_path):
        members = [f"bt/x/pages/a/data/video/{i}.mp4" for i in (1, 2)]
        run = TestDownloadPruning()._run
        _, first = run(monkeypatch, tmp_path, members)
        _, second = run(monkeypatch, tmp_path, members, resume=True, all_runs=True)
        assert first == ["T1"] and second == ["T1"]

    def test_partial_direct_download_not_marked_done(self, monkeypatch, tmp_path):
        async def fake_download(session, task_id, name, output_path, semaphore, cb=None):
            if name.endswith(".tgz"):
                return False
            if name.endswith("vid1.mp4"):
                return False
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"x")
            return True

        async def fake_list(session, task_id, semaphore):
            return [{"name": "public/build/vid0.mp4"}, {"name": "public/build/vid1.mp4"}]

        monkeypatch.setattr(download_mod, "download_artifact", fake_download)
        monkeypatch.setattr(download_mod, "list_task_artifacts", fake_list)
        vt = VideoTask("T1", "t", "p", "base", "r", task_type="perftest")
        res = asyncio.run(download_mod.download_video_artifacts(None, [vt], tmp_path, quiet=True))
        assert len(res["base"]["videos"]) == 1
        assert not (tmp_path / "base/p/t/T1" / download_mod.DONE_MARKER).exists()


class TestResumeTarget:
    def test_abbreviated_hash_is_the_same_push(self, tmp_path):
        from perf_sxs.models import TryPush

        (tmp_path / "comparisons.json").write_text(
            json.dumps({"base_revision": "a" * 40, "base_repo": "try", "new_revision": "b" * 40})
        )
        cli._check_resume_target(tmp_path, TryPush("a" * 12), TryPush("b" * 40))
        with pytest.raises(CliError):
            cli._check_resume_target(tmp_path, TryPush("a" * 40, "autoland"), TryPush("b" * 40))

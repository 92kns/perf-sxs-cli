"""Tests for run ordering, multi-step tasks, MWU filtering and cross-browser mode.

Layouts and names mirror real CI artifacts:
- browsertime: ``browsertime-videos-annotated/<test>/pages/<site>/<step>/data/video/N.mp4``
- nav-bench: one task, several ``<step>`` dirs, perfherder subtests ``<step>-score``/``<step>-min-si``
- perftest applink: flat ``<test>/vidN_<app>.mp4`` (+ ``iter_N_*.png``), optionally
  split into per-profiler subdirs on profiling variants.
"""

import asyncio
import io
import json
import tarfile
from pathlib import Path

import pytest

import perf_sxs.download as download_mod
from perf_sxs.cli import (
    _parse_inputs,
    build_parser,
    build_video_tasks,
    find_missing,
    parse_cross_browser,
)
from perf_sxs.models import TryPush, VideoTask
from perf_sxs.organize import (
    group_videos_by_step,
    organize_single_revision,
    organize_videos_for_comparison,
)
from perf_sxs.selection import (
    _is_video_task,
    cross_browser_test_name,
    find_app_span,
    find_median_run_index,
    is_significant_result,
    load_high_confidence_from_file,
    natural_sort_key,
)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def _task(name: str, task_id: str = "T1") -> dict:
    return {
        "task": {"metadata": {"name": name}},
        "status": {"taskId": task_id, "state": "completed"},
    }


class TestNaturalSort:
    def test_orders_run_numbers_numerically(self):
        paths = [Path(f"data/video/{i}.mp4") for i in (1, 10, 11, 2, 3, 9)]
        assert [p.stem for p in sorted(paths, key=natural_sort_key)] == [
            "1", "2", "3", "9", "10", "11",
        ]  # fmt: skip

    def test_perftest_names(self):
        paths = [Path(f"vid{i}_fenix.mp4") for i in (0, 10, 1, 2)]
        assert [p.name for p in sorted(paths, key=natural_sort_key)][:3] == [
            "vid0_fenix.mp4", "vid1_fenix.mp4", "vid2_fenix.mp4",
        ]  # fmt: skip


class TestSignificance:
    def test_student_t_uses_confidence_text(self):
        assert is_significant_result({"confidence_text": "High"}, "student-t")
        assert not is_significant_result({"confidence_text": "Medium"}, "student-t")
        assert not is_significant_result({"direction_of_change": "regression"}, "student-t")

    def test_mwu_uses_direction_of_change(self):
        # Treeherder's MWU serializer has no confidence_text at all
        assert is_significant_result({"direction_of_change": "regression"}, "mann-whitney-u")
        assert is_significant_result({"direction_of_change": "improvement"}, "mann-whitney-u")
        assert not is_significant_result({"direction_of_change": "no change"}, "mann-whitney-u")
        assert not is_significant_result({"confidence_text": "High"}, "mann-whitney-u")

    def test_auto_accepts_either(self):
        assert is_significant_result({"confidence_text": "High"})
        assert is_significant_result({"direction_of_change": "improvement"})
        assert not is_significant_result({"confidence_text": "Low"})

    def test_load_file_mwu_raw_api_shape(self, tmp_path):
        rows = [
            {"suite": "amazon", "platform": "linux", "direction_of_change": "regression"},
            {"suite": "cnn", "platform": "linux", "direction_of_change": "no change"},
        ]
        path = tmp_path / "mwu.json"
        path.write_text(json.dumps(rows))
        assert load_high_confidence_from_file(path) == {("amazon", "linux")}
        assert load_high_confidence_from_file(path, "student-t") == set()


class TestVideoTaskDetection:
    def test_toolchain_task_is_not_a_video_task(self):
        assert not _is_video_task("toolchain-browsertime")

    def test_browsertime_and_startup_tasks(self):
        assert _is_video_task(
            "test-linux2404-64-shippable/opt-browsertime-nav-bench-firefox-nav-bench"
        )
        assert _is_video_task(
            "perftest-android-hw-a55-aarch64-shippable-startup-fenix-homeview-startup"
        )
        assert not _is_video_task(
            "test-linux2404-64-shippable/opt-browsertime-benchmark-firefox-speedometer3-native-profiling"
        )


class TestStepSuiteSignificance:
    def test_nav_bench_step_suite_matches_task(self):
        from perf_sxs.selection import filter_video_tasks

        tasks = [
            _task("test-linux2404-64-shippable/opt-browsertime-nav-bench-firefox-nav-bench", "N"),
            _task("test-linux2404-64-shippable/opt-browsertime-tp6-firefox-amazon", "A"),
        ]
        high = {("nav-bench.amazon-nav-load", "linux2404-64-shippable")}
        kept = filter_video_tasks(tasks, high_conf_tests=high)
        assert [t["status"]["taskId"] for t in kept] == ["N"]


class TestMedianForSteps:
    PH = {
        "suites": [
            {
                "name": "nav-bench-overall",
                "subtests": [
                    {"name": "amazon-nav-load-score", "value": 30, "replicates": [10, 30, 20]},
                    {"name": "bbc-nav-load-score", "value": 99, "replicates": [5, 1, 3, 4, 2]},
                ],
            },
            {
                "name": "nav-bench.bbc-nav-load",
                # value is the *minimum* — must not be used as "the median"
                "subtests": [{"name": "bbc-nav-load-min-si", "value": 1, "replicates": [5, 1, 3]}],
            },
        ]
    }

    def test_matches_step_subtest_and_uses_true_median(self):
        assert find_median_run_index(self.PH, step="amazon-nav-load") == 2  # median 20
        assert find_median_run_index(self.PH, step="bbc-nav-load") == 2  # median 3

    def test_unknown_step_falls_back_to_first_subtest(self):
        assert find_median_run_index(self.PH, step="nope") == find_median_run_index(self.PH)


class TestStepGrouping:
    def _nav_task(self, root: Path, runs: int = 3) -> list[Path]:
        base = root / "browsertime-videos-annotated/nav-bench/pages"
        out = []
        for site, step in [
            ("duckduckgo_com", "duckduckgo-nav-load"),
            ("www_bbc_com", "bbc-nav-load"),
        ]:
            for i in range(1, runs + 1):
                out.append(_touch(base / site / step / "data/video" / f"{i}.mp4"))
        return out

    def test_nav_bench_splits_by_step(self, tmp_path):
        groups = group_videos_by_step(self._nav_task(tmp_path, runs=11))
        assert list(groups) == ["bbc-nav-load", "duckduckgo-nav-load"]
        assert [p.stem for p in groups["bbc-nav-load"]][:3] == ["1", "2", "3"]

    def test_single_page_task_is_one_unlabelled_group(self, tmp_path):
        vids = [
            _touch(tmp_path / f"amazon/pages/www_amazon_com/cold/q/data/video/{i}.mp4")
            for i in (1, 2, 10)
        ]
        groups = group_videos_by_step(vids)
        assert list(groups) == [""]
        assert [p.stem for p in groups[""]] == ["1", "2", "10"]

    def test_perftest_profiler_subdirs_stay_one_group(self, tmp_path):
        vids = [
            _touch(tmp_path / f"newssite-applink-startup/{d}/vid{i}_fenix.mp4")
            for d in ("simpleperf", "geckoprofiler")
            for i in range(2)
        ]
        assert list(group_videos_by_step(vids)) == [""]

    def test_organize_emits_one_comparison_per_step(self, tmp_path):
        for side, tid in (("base", "A"), ("new", "B")):
            self._nav_task(
                tmp_path / side / "linux_opt" / "browsertime-nav-bench-firefox-nav-bench" / tid
            )
        comps = organize_videos_for_comparison(tmp_path)
        assert sorted(comps) == [
            "linux_opt/browsertime-nav-bench-firefox-nav-bench::bbc-nav-load",
            "linux_opt/browsertime-nav-bench-firefox-nav-bench::duckduckgo-nav-load",
        ]
        c = comps["linux_opt/browsertime-nav-bench-firefox-nav-bench::bbc-nav-load"]
        assert c["step"] == "bbc-nav-load"
        assert all("bbc-nav-load" in v for v in c["base_videos"] + c["new_videos"])

    def test_single_revision_steps(self, tmp_path):
        self._nav_task(tmp_path / "base" / "linux_opt" / "nav" / "A")
        assert len(organize_single_revision(tmp_path)) == 2


def _make_tgz(path: Path, members: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        for name in members:
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))


class TestDownloadMedianSelection:
    """End-to-end through download_video_artifacts with the network stubbed out."""

    def _run(self, monkeypatch, tmp_path, members, ph_data, all_runs=False):
        async def fake_download(session, task_id, artifact_name, output_path, semaphore, cb=None):
            if not artifact_name.endswith("browsertime-videos-annotated.tgz"):
                return False
            _make_tgz(output_path, members)
            if cb:
                cb()
            return True

        async def fake_ph(session, task_id, semaphore):
            return ph_data

        monkeypatch.setattr(download_mod, "download_artifact", fake_download)
        monkeypatch.setattr(download_mod, "fetch_perfherder_data", fake_ph)
        vt = VideoTask(task_id="T1", test_name="t", platform="p", label="base", revision="r")
        return asyncio.run(
            download_mod.download_video_artifacts(
                None, [vt], tmp_path, all_runs=all_runs, quiet=True
            )
        )

    def test_median_uses_numeric_run_order(self, monkeypatch, tmp_path):
        members = [f"bt/amazon/pages/a/data/video/{i}.mp4" for i in range(1, 12)]
        # replicate index 1 (= run 2) is the median; lexical sort would pick 10.mp4
        ph = {
            "suites": [
                {"subtests": [{"value": 50, "replicates": [1, 50, 2, 3, 4, 5, 6, 7, 8, 9, 99]}]}
            ]
        }
        res = self._run(monkeypatch, tmp_path, members, ph)
        assert [p.name for p in res["base"]["videos"]] == ["2.mp4"]
        assert sorted(p.name for p in tmp_path.rglob("*.mp4")) == ["2.mp4"]

    def test_keeps_one_median_per_step(self, monkeypatch, tmp_path):
        members = [
            f"bt/nav/pages/{site}/{step}/data/video/{i}.mp4"
            for site, step in (("ddg", "ddg-nav-load"), ("bbc", "bbc-nav-load"))
            for i in (1, 2, 3)
        ]
        ph = {
            "suites": [
                {
                    "subtests": [
                        {"name": "bbc-nav-load-score", "value": 0, "replicates": [9, 1, 5]},
                        {"name": "ddg-nav-load-score", "value": 0, "replicates": [5, 9, 1]},
                    ]
                }
            ]
        }
        res = self._run(monkeypatch, tmp_path, members, ph)
        kept = sorted(f"{p.parent.parent.parent.name}/{p.name}" for p in res["base"]["videos"])
        assert kept == ["bbc-nav-load/3.mp4", "ddg-nav-load/1.mp4"]

    def test_all_runs_writes_per_step_sidecar(self, monkeypatch, tmp_path):
        members = [
            f"bt/nav/pages/{s}/{s}-nav-load/data/video/{i}.mp4"
            for s in ("a", "b")
            for i in (1, 2, 3)
        ]
        ph = {
            "suites": [
                {"subtests": [{"name": "a-nav-load-score", "value": 0, "replicates": [1, 2, 3]}]}
            ]
        }
        self._run(monkeypatch, tmp_path, members, ph, all_runs=True)
        sidecar = next(tmp_path.rglob("median_idx.json"))
        assert json.loads(sidecar.read_text())["a-nav-load"] == 1
        comps = organize_single_revision(tmp_path)
        assert comps["p/t::a-nav-load"]["base_median_idx"] == 1
        assert len(comps["p/t::a-nav-load"]["base_videos"]) == 3


class TestCrossBrowserNames:
    def test_desktop_benchmark(self):
        assert (
            cross_browser_test_name(
                "browsertime-benchmark-chrome-speedometer3", "chrome", "firefox-vs-chrome"
            )
            == "browsertime-benchmark-firefox-vs-chrome-speedometer3"
        )

    def test_chrome_does_not_match_chrome_m(self):
        assert find_app_span("startup-chrome-m-newssite-applink-startup", "chrome") is None
        assert find_app_span("startup-chrome-m-newssite-applink-startup", "chrome-m") == (1, 3)

    def test_safari_does_not_match_safari_tp(self):
        assert find_app_span("browsertime-benchmark-safari-tp-speedometer3", "safari") is None

    def test_app_at_end_of_name(self):
        assert (
            cross_browser_test_name(
                "browsertime-benchmark-speedometer3-mobile-chrome-m", "chrome-m", "X"
            )
            == "browsertime-benchmark-speedometer3-mobile-X"
        )

    def test_substring_is_not_a_match(self):
        assert find_app_span("browsertime-tp6-firefoxy-amazon", "firefox") is None


class TestCrossBrowserCli:
    def test_parse_cross_browser(self):
        assert parse_cross_browser("Firefox, chrome") == ("firefox", "chrome")
        assert parse_cross_browser(None) is None
        for bad in ("firefox", "firefox,firefox", "a,b,c", "firefox,"):
            with pytest.raises(ValueError):
                parse_cross_browser(bad)

    def test_single_revision_becomes_same_push_pair(self):
        args = build_parser().parse_args(["881d2bbfaf53", "--cross-browser", "firefox,chrome"])
        base, new, url, lando = _parse_inputs(args, ("firefox", "chrome"))
        assert new is not None and base.revision == new.revision == "881d2bbfaf53"
        assert url is None and lando is None

    def test_single_revision_without_cross_browser_is_rejected(self):
        args = build_parser().parse_args(["881d2bbfaf53"])
        with pytest.raises(ValueError, match="perfcompare URL"):
            _parse_inputs(args, None)

    def test_build_video_tasks_pairs_and_drops_unpaired(self):
        tasks = [
            _task(
                "test-linux2404-64-shippable/opt-browsertime-benchmark-firefox-speedometer3", "F1"
            ),
            _task(
                "test-linux2404-64-shippable/opt-browsertime-benchmark-chrome-speedometer3", "C1"
            ),
            _task(
                "test-linux2404-64-shippable/opt-browsertime-benchmark-firefox-unity-webgl", "F2"
            ),
            _task(
                "test-linux2404-64-shippable/opt-browsertime-benchmark-safari-speedometer3", "S1"
            ),
        ]
        push = TryPush(revision="abc", repo="mozilla-central")
        vts = build_video_tasks(tasks, tasks, push, push, ("firefox", "chrome"))
        assert {(v.label, v.task_id) for v in vts} == {("base", "F1"), ("new", "C1")}
        assert {v.test_name for v in vts} == {
            "browsertime-benchmark-firefox-vs-chrome-speedometer3"
        }


class TestFindMissing:
    def test_keyed_by_platform_and_test(self, tmp_path):
        vts = [
            VideoTask("1", "amazon", "linux", "base", "r"),
            VideoTask("2", "amazon", "windows", "base", "r"),
        ]
        downloaded = {"base": [tmp_path / "base/linux/amazon/1/x.mp4"], "new": []}
        assert [v.platform for v in find_missing(vts, downloaded, tmp_path)] == ["windows"]

    def test_output_dir_named_like_a_label(self, tmp_path):
        out = tmp_path / "new" / "base"
        vts = [VideoTask("1", "tp6", "linux", "new", "r")]
        downloaded = {"base": [], "new": [out / "new/linux/tp6/1/x.mp4"]}
        assert find_missing(vts, downloaded, out) == []


class TestLandoSamePush:
    """Lando pushes start with revision "" -- must not be treated as one push."""

    def test_new_push_tasks_fetched_separately(self, monkeypatch, tmp_path, capsys):
        import perf_sxs.cli as cli

        calls = []

        async def lando(session, lando_id):
            return {"1": "a" * 40, "2": "b" * 40}[lando_id]

        async def groups(session, rev, repo):
            calls.append(rev)
            return [f"G-{rev[0]}"]

        async def tasks(session, gids):
            ch = gids[0][-1]
            return [
                _task("test-linux2404-64-shippable/opt-browsertime-tp6-firefox-amazon", f"T{ch}")
            ]

        async def download(session, vts, out, *a, **k):
            return {"base": {"videos": [], "images": []}, "new": {"videos": [], "images": []}}

        monkeypatch.setattr(cli, "resolve_lando_id", lando)
        monkeypatch.setattr(cli, "find_task_group_ids", groups)
        monkeypatch.setattr(cli, "get_tasks_for_revision", tasks)
        monkeypatch.setattr(cli, "download_video_artifacts", download)
        args = build_parser().parse_args(
            [
                "https://perf.compare/compare-lando-results?baseLando=1&newLando=2",
                "--all-tests", "--no-serve", "--json", "-o", str(tmp_path / "o"),
            ]
        )  # fmt: skip
        asyncio.run(cli._amain(args))
        assert calls == ["a" * 40, "b" * 40]
        out = json.loads(capsys.readouterr().out)
        assert out["video_tasks_found"] == 2

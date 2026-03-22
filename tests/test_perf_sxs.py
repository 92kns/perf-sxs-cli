"""
Tests for perf-sxs-cli core functionality.

Run with: pytest test_perf_sxs.py
Or with uv: uv run pytest test_perf_sxs.py
"""

import json

# Import functions to test
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from perf_sxs import (
    extract_suite_and_platform,
    extract_test_info,
    find_median_run_index,
    load_high_confidence_from_file,
    organize_single_revision,
    parse_perfcompare_url,
    parse_try_url,
    read_median_idx,
)


class TestURLParsing:
    """Test URL and revision parsing."""

    def test_parse_perfcompare_url(self):
        """Test parsing perfcompare URLs."""
        url = "https://perf.compare/compare-results?baseRev=cbd1514fc57c&baseRepo=try&newRev=f4b183534e62&newRepo=try&framework=13"
        base_push, new_push = parse_perfcompare_url(url)

        assert base_push.revision == "cbd1514fc57c"
        assert base_push.repo == "try"
        assert new_push.revision == "f4b183534e62"
        assert new_push.repo == "try"

    def test_parse_perfcompare_url_default_repo(self):
        """Test perfcompare URL with default repository."""
        url = "https://perf.compare/compare-results?baseRev=abc123&newRev=def456"
        base_push, new_push = parse_perfcompare_url(url)

        assert base_push.repo == "try"
        assert new_push.repo == "try"

    def test_parse_try_url_treeherder(self):
        """Test parsing Treeherder Try URLs."""
        url = "https://treeherder.mozilla.org/jobs?repo=try&revision=881d2bbfaf536748b4ebdbadeaaa2c9c269f91e8"
        try_push = parse_try_url(url)

        assert try_push.revision == "881d2bbfaf536748b4ebdbadeaaa2c9c269f91e8"
        assert try_push.repo == "try"

    def test_parse_try_url_plain_revision_full(self):
        """Test parsing plain full revision hash."""
        revision = "881d2bbfaf536748b4ebdbadeaaa2c9c269f91e8"
        try_push = parse_try_url(revision)

        assert try_push.revision == revision
        assert try_push.repo == "try"

    def test_parse_try_url_plain_revision_short(self):
        """Test parsing plain short revision hash."""
        revision = "881d2bbfaf53"
        try_push = parse_try_url(revision)

        assert try_push.revision == revision
        assert try_push.repo == "try"


class TestSuiteExtraction:
    """Test suite and platform extraction from task names."""

    def test_extract_simple_suite(self):
        """Test extracting simple suite name."""
        task_name = "test-linux1804-64-shippable-qr/opt-browsertime-tp6-firefox-amazon-e10s"
        suite, platform = extract_suite_and_platform(task_name)

        assert suite == "amazon"
        assert platform == "linux1804-64-shippable-qr"

    def test_extract_multi_hyphen_suite(self):
        """Test extracting multi-hyphen suite names."""
        test_cases = [
            (
                "test-windows11-64-24h2-nightlyasrelease/opt-browsertime-tp6-firefox-bing-search-e10s",
                "bing-search",
                "windows11-64-24h2-nightlyasrelease",
            ),
            (
                "test-macosx1470-64-shippable/opt-browsertime-tp6-firefox-google-slides-cold",
                "google-slides",
                "macosx1470-64-shippable",
            ),
            (
                "test-linux1804-64-qr/opt-browsertime-tp6-firefox-yahoo-mail-fission",
                "yahoo-mail",
                "linux1804-64-qr",
            ),
        ]

        for task_name, expected_suite, expected_platform in test_cases:
            suite, platform = extract_suite_and_platform(task_name)
            assert suite == expected_suite, f"Failed for {task_name}"
            assert platform == expected_platform, f"Failed for {task_name}"

    def test_extract_suite_with_multiple_suffixes(self):
        """Test extracting suite with multiple known suffixes."""
        task_name = "test-linux1804-64-qr/opt-browsertime-tp6-firefox-cnn-cold-fission-webrender"
        suite, platform = extract_suite_and_platform(task_name)

        assert suite == "cnn"
        assert platform == "linux1804-64-qr"

    def test_extract_suite_no_suffix(self):
        """Test extracting suite without suffix."""
        task_name = "test-macosx1470-64/opt-browsertime-tp6-firefox-fandom"
        suite, platform = extract_suite_and_platform(task_name)

        assert suite == "fandom"
        assert platform == "macosx1470-64"

    def test_extract_test_info(self):
        """Test extracting test name and platform."""
        task_name = "test-linux1804-64-shippable-qr/opt-browsertime-tp6-firefox-amazon-e10s"
        test_name, platform = extract_test_info(task_name)

        assert test_name == "browsertime-tp6-firefox-amazon-e10s"
        assert platform == "test-linux1804-64-shippable-qr_opt"  # Note: / replaced with _


class TestHighConfidenceFiltering:
    """Test high confidence filtering logic."""

    def test_load_high_confidence_from_file(self):
        """Test loading high confidence tests from JSON file."""
        # Create temporary JSON file with sample data
        sample_data = [
            {
                "test1": [
                    {
                        "suite": "amazon",
                        "platform": "linux1804-64-shippable-qr",
                        "confidence_text": "High",
                        "confidence": 8.5,
                    },
                    {
                        "suite": "google",
                        "platform": "linux1804-64-shippable-qr",
                        "confidence_text": "Low",
                        "confidence": 0.5,
                    },
                ]
            },
            {
                "test2": [
                    {
                        "suite": "cnn",
                        "platform": "macosx1470-64-nightlyasrelease",
                        "confidence_text": "High",
                        "confidence": 12.0,
                    }
                ]
            },
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(sample_data, f)
            temp_path = Path(f.name)

        try:
            high_conf = load_high_confidence_from_file(temp_path)

            # Should only include High confidence tests
            assert len(high_conf) == 2
            assert ("amazon", "linux1804-64-shippable-qr") in high_conf
            assert ("cnn", "macosx1470-64-nightlyasrelease") in high_conf
            assert ("google", "linux1804-64-shippable-qr") not in high_conf  # Low confidence

        finally:
            temp_path.unlink()

    def test_load_high_confidence_filters_low_and_medium(self):
        """Test that only High confidence tests are included."""
        sample_data = [
            {
                "test": [
                    {"suite": "test1", "platform": "plat1", "confidence_text": "High"},
                    {"suite": "test2", "platform": "plat2", "confidence_text": "Medium"},
                    {"suite": "test3", "platform": "plat3", "confidence_text": "Low"},
                    {"suite": "test4", "platform": "plat4", "confidence_text": "high"},  # lowercase
                ]
            }
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(sample_data, f)
            temp_path = Path(f.name)

        try:
            high_conf = load_high_confidence_from_file(temp_path)

            # Only "High" (case-sensitive)
            assert len(high_conf) == 1
            assert ("test1", "plat1") in high_conf

        finally:
            temp_path.unlink()


class TestSuiteNameEdgeCases:
    """Test edge cases in suite name extraction."""

    def test_suite_with_numbers(self):
        """Test suite names containing numbers."""
        task_name = "test-linux1804-64-qr/opt-browsertime-tp6m-firefox-facebook-e10s"
        suite, platform = extract_suite_and_platform(task_name)

        assert suite == "facebook"

    def test_chrome_browser(self):
        """Test extraction for Chrome tests (not Firefox)."""
        task_name = "test-linux1804-64-qr/opt-browsertime-tp6-chrome-amazon-e10s"
        suite, platform = extract_suite_and_platform(task_name)

        # Should return empty since it's not firefox
        assert suite == ""
        assert platform == ""

    def test_invalid_task_name(self):
        """Test handling of invalid task names."""
        task_name = "invalid-task-name-without-browsertime"
        suite, platform = extract_suite_and_platform(task_name)

        assert suite == ""
        assert platform == ""


class TestMedianRunIndex:
    """Test median run index detection from perfherder-data.json."""

    def _make_data(self, replicates, value=None):
        if value is None:
            sorted_reps = sorted(replicates)
            mid = len(sorted_reps) // 2
            value = sorted_reps[mid]
        return {"suites": [{"subtests": [{"replicates": replicates, "value": value}]}]}

    def test_picks_closest_to_median(self):
        data = self._make_data([100, 200, 150, 300, 250], value=200)
        assert find_median_run_index(data) == 1  # replicates[1] == 200

    def test_single_replicate_returns_zero(self):
        data = self._make_data([500], value=500)
        assert find_median_run_index(data) == 0

    def test_empty_suites_returns_zero(self):
        assert find_median_run_index({"suites": []}) == 0

    def test_missing_subtests_returns_zero(self):
        assert find_median_run_index({"suites": [{"subtests": []}]}) == 0

    def test_malformed_data_returns_zero(self):
        assert find_median_run_index({}) == 0
        assert find_median_run_index(None) == 0  # type: ignore[arg-type]

    def test_picks_nearest_when_no_exact_match(self):
        # value=175, closest replicate is 200 (index 1) vs 100 (index 0)
        data = self._make_data([100, 200, 300], value=175)
        assert find_median_run_index(data) == 1


class TestReadMedianIdx:
    """Test reading median_idx.txt sidecar files."""

    def test_reads_sidecar(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir)
            task_dir = test_dir / "task123"
            task_dir.mkdir()
            (task_dir / "median_idx.txt").write_text("3")
            assert read_median_idx(test_dir) == 3

    def test_returns_none_without_sidecar(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir)
            (test_dir / "task123").mkdir()
            assert read_median_idx(test_dir) is None

    def test_returns_none_for_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assert read_median_idx(Path(tmpdir)) is None


class TestOrganizeSingleRevision:
    """Test single-revision video organization."""

    def _make_video_tree(self, root: Path):
        video_path = (
            root / "base" / "test-linux_opt" / "browsertime-tp6-amazon" / "task123"
        )
        video_path.mkdir(parents=True)
        (video_path / "video0.mp4").write_bytes(b"")
        return video_path

    def test_organizes_base_videos(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._make_video_tree(root)
            comparisons = organize_single_revision(root)

            assert len(comparisons) == 1
            key = "test-linux_opt/browsertime-tp6-amazon"
            assert key in comparisons
            assert len(comparisons[key]["base_videos"]) == 1
            assert "new_videos" not in comparisons[key]

    def test_no_base_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assert organize_single_revision(Path(tmpdir)) == {}

    def test_includes_median_idx_when_sidecar_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video_path = self._make_video_tree(root)
            (video_path / "median_idx.txt").write_text("0")
            comparisons = organize_single_revision(root)
            key = "test-linux_opt/browsertime-tp6-amazon"
            assert comparisons[key]["base_median_idx"] == 0


# Pytest markers for different test categories
@pytest.mark.unit
class TestURLParsingMarked(TestURLParsing):
    """URL parsing tests (marked as unit tests)."""

    pass


@pytest.mark.unit
class TestSuiteExtractionMarked(TestSuiteExtraction):
    """Suite extraction tests (marked as unit tests)."""

    pass


@pytest.mark.integration
class TestHighConfidenceFilteringMarked(TestHighConfidenceFiltering):
    """High confidence filtering tests (marked as integration tests)."""

    pass


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "--tb=short"])

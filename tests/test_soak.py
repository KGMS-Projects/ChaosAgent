"""
Unit tests for the Sentinel soak test logging and summary generation.
These tests exercise the SoakTestSummary data structure and duration parsing
without requiring a live Prometheus connection.
"""
import tempfile
import json
from pathlib import Path

import pytest

from evaluation.sentinel_soak_test import SentinelSoakTest, SoakTestSummary, _parse_duration, SoakLogEntry


def test_parse_duration_hours():
    assert _parse_duration("24h") == 86400.0
    assert _parse_duration("1h") == 3600.0
    assert _parse_duration("0.5h") == 1800.0


def test_parse_duration_minutes():
    assert _parse_duration("30m") == 1800.0
    assert _parse_duration("10m") == 600.0


def test_parse_duration_seconds():
    assert _parse_duration("60s") == 60.0
    assert _parse_duration("10s") == 10.0


def test_parse_duration_plain_number():
    assert _parse_duration("3600") == 3600.0


def test_soak_log_entry_dataclass():
    entry = SoakLogEntry(
        timestamp="2026-01-01T00:00:00Z",
        elapsed_seconds=15.0,
        health_status="steady",
        health_score=100.0,
        error_rate=0.0,
        p99_latency_ms=50.0,
        cpu_utilization=0.2,
        memory_utilization=0.3,
        pod_restarts=0,
    )
    assert entry.health_status == "steady"
    assert entry.state_transition is None


def test_soak_log_entry_with_transition():
    entry = SoakLogEntry(
        timestamp="2026-01-01T00:00:00Z",
        elapsed_seconds=30.0,
        health_status="degraded",
        health_score=65.0,
        error_rate=0.05,
        p99_latency_ms=400.0,
        cpu_utilization=0.5,
        memory_utilization=0.6,
        pod_restarts=1,
        state_transition="steady -> degraded",
    )
    assert entry.state_transition == "steady -> degraded"


def test_soak_test_summary_dataclass():
    summary = SoakTestSummary(
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-02T00:00:00Z",
        duration_seconds=86400.0,
        total_observations=5760,
        state_transitions=2,
        time_in_steady_seconds=86100.0,
        time_in_degraded_seconds=200.0,
        time_in_critical_seconds=0.0,
        time_in_recovering_seconds=100.0,
        uptime_percent=99.65,
        longest_steady_streak_seconds=43200.0,
        false_positive_transitions=2,
        max_error_rate_observed=0.01,
        max_p99_latency_observed=350.0,
        max_cpu_utilization_observed=0.75,
        total_pod_restarts_observed=3,
    )
    assert summary.uptime_percent == 99.65
    assert summary.total_observations == 5760


def test_soak_test_creates_results_directory():
    with tempfile.TemporaryDirectory() as tmpdir:
        results_path = Path(tmpdir) / "soak_output"
        soak = SentinelSoakTest(
            duration_seconds=0.0,  # don't actually run
            poll_interval=1.0,
            results_dir=str(results_path),
        )
        assert results_path.exists()


def test_soak_test_export_creates_files():
    """Test that _export creates the expected output files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        soak = SentinelSoakTest(
            duration_seconds=0.0,
            poll_interval=1.0,
            results_dir=tmpdir,
        )
        soak.log_entries = [
            SoakLogEntry(
                timestamp="2026-01-01T00:00:00Z",
                elapsed_seconds=0.0,
                health_status="steady",
                health_score=100.0,
                error_rate=0.0,
                p99_latency_ms=50.0,
                cpu_utilization=0.2,
                memory_utilization=0.3,
                pod_restarts=0,
            ),
        ]
        summary = SoakTestSummary(
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-01-01T00:01:00Z",
            duration_seconds=60.0,
            total_observations=4,
            state_transitions=0,
            time_in_steady_seconds=60.0,
            time_in_degraded_seconds=0.0,
            time_in_critical_seconds=0.0,
            time_in_recovering_seconds=0.0,
            uptime_percent=100.0,
            longest_steady_streak_seconds=60.0,
            false_positive_transitions=0,
            max_error_rate_observed=0.0,
            max_p99_latency_observed=50.0,
            max_cpu_utilization_observed=0.2,
            total_pod_restarts_observed=0,
        )
        soak._export(summary)

        assert (Path(tmpdir) / "sentinel_soak_log.json").exists()
        assert (Path(tmpdir) / "sentinel_soak_summary.json").exists()
        assert (Path(tmpdir) / "sentinel_soak_summary.csv").exists()

        # Verify JSON is valid
        with open(Path(tmpdir) / "sentinel_soak_summary.json") as f:
            data = json.load(f)
        assert data["uptime_percent"] == 100.0
        assert data["total_observations"] == 4

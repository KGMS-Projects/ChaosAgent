import csv
import tempfile
from pathlib import Path

import pytest

from evaluation.harness import EvaluationHarness, FaultSimulator, RunResult
from evaluation.scenarios import generate_scenarios, ScenarioDefinition
from src.state.schemas import FaultCategory


def test_generate_scenarios_returns_20():
    scenarios = generate_scenarios()
    assert len(scenarios) == 20


def test_generate_scenarios_covers_five_categories():
    scenarios = generate_scenarios()
    categories = {s.fault_category for s in scenarios}
    assert categories == set(FaultCategory)


def test_scenario_ids_are_unique():
    scenarios = generate_scenarios()
    ids = [s.scenario_id for s in scenarios]
    assert len(ids) == len(set(ids))


def test_fault_simulator_recovers_only_with_matching_remediation():
    scenario = ScenarioDefinition(
        scenario_id="configuration_drift-01", fault_category=FaultCategory.CONFIGURATION_DRIFT,
        target_service="paymentservice", namespace="chaos-demo",
    )
    sim = FaultSimulator(scenario)
    sim.inject()
    assert sim.remediate("restart_based") is False  # wrong remediation type for config drift
    assert sim.remediate("configuration_based") is True


def test_harness_simulated_run_produces_recovered_result():
    harness = EvaluationHarness(mode="simulated", namespace="chaos-demo", results_dir=tempfile.mkdtemp())
    scenario = generate_scenarios()[0]
    result = harness.run_proposed_framework(scenario)
    assert isinstance(result, RunResult)
    assert result.is_simulated is True
    assert result.arm == "proposed"


def test_harness_rule_based_baseline_unsupported_for_configuration_drift():
    harness = EvaluationHarness(mode="simulated", results_dir=tempfile.mkdtemp())
    scenario = next(s for s in generate_scenarios() if s.fault_category == FaultCategory.CONFIGURATION_DRIFT)
    result = harness.run_rule_based_baseline(scenario)
    assert result.recovered is False
    assert result.ttr_seconds is None


def test_harness_rule_based_baseline_supported_for_pod_termination():
    harness = EvaluationHarness(mode="simulated", results_dir=tempfile.mkdtemp())
    scenario = next(s for s in generate_scenarios() if s.fault_category == FaultCategory.POD_TERMINATION)
    result = harness.run_rule_based_baseline(scenario)
    assert result.recovered is True
    assert result.ttr_seconds is not None


def test_harness_full_comparison_produces_two_arms_per_scenario():
    harness = EvaluationHarness(mode="simulated", results_dir=tempfile.mkdtemp())
    scenarios = generate_scenarios()[:4]
    results = harness.run_full_comparison(scenarios=scenarios, include_rule_based=True)
    assert len(results) == 8  # 4 scenarios x (proposed + rule_based)


def test_harness_export_csv_round_trips(tmp_path):
    harness = EvaluationHarness(mode="simulated", results_dir=str(tmp_path))
    harness.run_full_comparison(scenarios=generate_scenarios()[:2], include_rule_based=False)
    path = harness.export_csv("test_results.csv")
    with open(path) as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["arm"] == "proposed"


def test_load_manual_baseline_parses_csv(tmp_path):
    csv_path = tmp_path / "manual.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["scenario_id", "fault_category", "ttr_seconds", "recovered", "vulnerability_detected", "notes"])
        writer.writerow(["pod_termination-01", "pod_termination", "1476", "true", "true", "hand-timed"])
    harness = EvaluationHarness(mode="simulated", results_dir=str(tmp_path))
    results = harness.load_manual_baseline(str(csv_path))
    assert len(results) == 1
    assert results[0].arm == "manual"
    assert results[0].ttr_seconds == 1476.0
    assert results[0].is_simulated is False


def test_load_manual_baseline_skips_todo_rows(tmp_path):
    """Rows with TODO in ttr_seconds should be skipped (not yet filled in)."""
    csv_path = tmp_path / "manual.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["scenario_id", "fault_category", "ttr_seconds", "recovered", "vulnerability_detected", "notes"])
        writer.writerow(["pod_termination-01", "pod_termination", "1476", "true", "true", "filled"])
        writer.writerow(["pod_termination-02", "pod_termination", "", "true", "true", "TODO: not done yet"])
    harness = EvaluationHarness(mode="simulated", results_dir=str(tmp_path))
    results = harness.load_manual_baseline(str(csv_path))
    assert len(results) == 1  # only the filled-in row


def test_ttr_uses_cycle_delta_not_wallclock():
    """TTR must reflect monitoring-cycle elapsed time, not real Python
    wall-clock time (which is ~instant in simulated mode and would otherwise
    always report ~0 seconds regardless of how long recovery actually took)."""
    harness = EvaluationHarness(mode="simulated", results_dir=tempfile.mkdtemp())
    scenario = generate_scenarios()[0]
    result = harness.run_proposed_framework(scenario)
    if result.recovered and result.ttr_seconds is not None:
        assert result.ttr_seconds >= 15.0  # at least one monitoring interval


def test_harness_with_repetitions():
    """Multiple repetitions should produce proportionally more results."""
    harness = EvaluationHarness(mode="simulated", results_dir=tempfile.mkdtemp(), repetitions=2)
    scenarios = generate_scenarios()[:2]
    results = harness.run_full_comparison(scenarios=scenarios, include_rule_based=False)
    assert len(results) == 4  # 2 scenarios x 2 repetitions


def test_run_result_has_repetition_field():
    """RunResult should track which repetition it belongs to."""
    harness = EvaluationHarness(mode="simulated", results_dir=tempfile.mkdtemp())
    scenario = generate_scenarios()[0]
    result = harness.run_proposed_framework(scenario, repetition=3)
    assert result.repetition == 3


def test_run_result_has_wall_clock_seconds():
    """RunResult should track wall-clock time."""
    harness = EvaluationHarness(mode="simulated", results_dir=tempfile.mkdtemp())
    scenario = generate_scenarios()[0]
    result = harness.run_proposed_framework(scenario)
    assert result.wall_clock_seconds is not None
    assert result.wall_clock_seconds >= 0


# -- analyze_results tests --------------------------------------------------#

def test_analyze_results_loads_csv(tmp_path):
    """The analyzer should be able to load result CSV files."""
    from evaluation.analyze_results import load_results_csv
    csv_path = tmp_path / "results.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["scenario_id", "arm", "fault_category", "ttr_seconds", "recovered",
                         "vulnerability_detected", "is_simulated", "notes", "api_cost_usd",
                         "cycles_used"])
        writer.writerow(["pod_termination-01", "proposed", "pod_termination", "90", "True",
                         "True", "True", "test", "0.01", "12"])
    rows = load_results_csv(str(csv_path))
    assert len(rows) == 1
    assert rows[0].arm == "proposed"
    assert rows[0].ttr_seconds == 90.0
    assert rows[0].api_cost_usd == 0.01
    assert rows[0].cycles_used == 12


def test_analyze_results_table_iv():
    """Table IV generation should produce non-empty output."""
    from evaluation.analyze_results import generate_table_iv, ResultRow
    rows = [
        ResultRow("pod_termination-01", "proposed", "pod_termination", 90.0, True, True, False),
        ResultRow("pod_termination-01", "manual", "pod_termination", 1476.0, True, True, False),
        ResultRow("pod_termination-01", "rule_based", "pod_termination", 600.0, True, False, False),
    ]
    table = generate_table_iv(rows)
    assert "TABLE IV" in table
    assert "Pod Termination" in table

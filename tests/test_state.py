import pytest
from pydantic import ValidationError

from src.state.schemas import (
    GraphState, FaultReport, FaultCategory, HealthStatus, MetricsSnapshot,
    ExperimentAuthorization, AuthorizationDecision,
)


def test_graph_state_defaults():
    gs = GraphState(scenario_id="s1")
    assert gs.cycle_count == 0
    assert gs.health_status == HealthStatus.STEADY
    assert gs.fault_reports == []
    assert gs.blast_radius_budget_remaining == 1.0


def test_fault_report_blast_radius_bounds():
    with pytest.raises(ValidationError):
        FaultReport(
            message_id="m1", fault_category=FaultCategory.POD_TERMINATION,
            target_service="cartservice", target_namespace="chaos-demo",
            expected_observable_impact="x", blast_radius_fraction=1.5,
        )


def test_fault_report_valid():
    fr = FaultReport(
        message_id="m1", fault_category=FaultCategory.NETWORK_LATENCY,
        target_service="paymentservice", target_namespace="chaos-demo",
        expected_observable_impact="slow checkout", blast_radius_fraction=0.2,
    )
    assert fr.fault_category == FaultCategory.NETWORK_LATENCY
    assert 0.0 <= fr.blast_radius_fraction <= 1.0


def test_metrics_snapshot_health_score_steady():
    m = MetricsSnapshot(error_rate=0.0, p99_latency_ms=50, pod_restarts=0,
                         cpu_utilization=0.2, memory_utilization=0.2)
    assert m.health_score() == 100.0


def test_metrics_snapshot_health_score_degrades_with_errors():
    healthy = MetricsSnapshot(error_rate=0.0)
    unhealthy = MetricsSnapshot(error_rate=0.3)
    assert unhealthy.health_score() < healthy.health_score()


def test_metrics_snapshot_health_score_bounded():
    m = MetricsSnapshot(error_rate=1.0, p99_latency_ms=10000, pod_restarts=50,
                         cpu_utilization=1.0, memory_utilization=1.0)
    assert 0.0 <= m.health_score() <= 100.0


def test_experiment_authorization_decision_enum():
    auth = ExperimentAuthorization(
        message_id="a1", related_fault_report_id="m1",
        decision=AuthorizationDecision.DENIED, max_allowed_blast_radius=0.1,
    )
    assert auth.decision == AuthorizationDecision.DENIED


def test_graph_state_log_appends_entry():
    gs = GraphState(scenario_id="s1")
    gs.log("test_phase", foo="bar")
    assert len(gs.experiment_log) == 1
    assert gs.experiment_log[0].phase == "test_phase"
    assert gs.experiment_log[0].detail == {"foo": "bar"}

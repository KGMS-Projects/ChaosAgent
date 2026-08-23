import pytest

from src.safety.guardrails import (
    enforce_fault_whitelist, enforce_remediation_whitelist, SafetyViolation,
    BlastRadiusGuard, HumanApprovalGate, RollbackController, ManifestValidator,
)
from src.state.schemas import FaultReport, FaultCategory, MetricsSnapshot, HealthStatus, AuthorizationDecision


# -- operation whitelisting -------------------------------------------------#

def test_fault_whitelist_accepts_known_category():
    enforce_fault_whitelist("pod_termination")  # should not raise


def test_fault_whitelist_rejects_unknown_category():
    with pytest.raises(SafetyViolation):
        enforce_fault_whitelist("delete_all_namespaces")


def test_remediation_whitelist_rejects_unknown_category():
    with pytest.raises(SafetyViolation):
        enforce_remediation_whitelist("nuke_cluster")


# -- blast radius ------------------------------------------------------------#

def test_blast_radius_guard_rejects_over_ceiling():
    guard = BlastRadiusGuard(max_fraction=0.3)
    assert guard.check(proposed_fraction=0.5, budget_remaining=1.0) is False


def test_blast_radius_guard_rejects_over_budget():
    guard = BlastRadiusGuard(max_fraction=0.3)
    assert guard.check(proposed_fraction=0.2, budget_remaining=0.1) is False


def test_blast_radius_guard_accepts_within_limits():
    guard = BlastRadiusGuard(max_fraction=0.3)
    assert guard.check(proposed_fraction=0.2, budget_remaining=0.5) is True


def test_blast_radius_guard_consume():
    guard = BlastRadiusGuard()
    assert guard.consume(1.0, 0.3) == pytest.approx(0.7)
    assert guard.consume(0.1, 0.3) == 0.0  # never goes negative


# -- human-in-the-loop gating -------------------------------------------------#

def _sample_fault(category=FaultCategory.POD_TERMINATION, blast=0.1):
    return FaultReport(
        message_id="m1", fault_category=category, target_service="cartservice",
        target_namespace="chaos-demo", expected_observable_impact="x", blast_radius_fraction=blast,
    )


def test_human_gate_denies_when_over_budget():
    gate = HumanApprovalGate()
    auth = gate.decide(_sample_fault(blast=0.5), budget_remaining=0.1)
    assert auth.decision == AuthorizationDecision.DENIED


def test_human_gate_requires_review_for_high_risk_category():
    gate = HumanApprovalGate()
    auth = gate.decide(_sample_fault(category=FaultCategory.CONFIGURATION_DRIFT, blast=0.1), budget_remaining=1.0)
    assert auth.decision == AuthorizationDecision.PENDING_HUMAN_REVIEW
    assert auth.requires_human_ack is True


def test_human_gate_auto_approves_high_risk_with_callback():
    gate = HumanApprovalGate(auto_approve_callback=lambda f: True)
    auth = gate.decide(_sample_fault(category=FaultCategory.CONFIGURATION_DRIFT, blast=0.1), budget_remaining=1.0)
    assert auth.decision == AuthorizationDecision.APPROVED
    assert auth.requires_human_ack is True


def test_human_gate_approves_low_risk_within_budget():
    gate = HumanApprovalGate()
    auth = gate.decide(_sample_fault(blast=0.1), budget_remaining=1.0)
    assert auth.decision == AuthorizationDecision.APPROVED
    assert auth.requires_human_ack is False


# -- rollback controller ------------------------------------------------------#

def test_rollback_controller_steady():
    rc = RollbackController()
    assert rc.evaluate(MetricsSnapshot(error_rate=0.0)) == HealthStatus.STEADY


def test_rollback_controller_critical_triggers_force_rollback():
    rc = RollbackController()
    critical_metrics = MetricsSnapshot(error_rate=0.5, p99_latency_ms=5000, pod_restarts=10)
    assert rc.evaluate(critical_metrics) == HealthStatus.CRITICAL
    assert rc.should_force_rollback(critical_metrics) is True


def test_rollback_controller_degraded_does_not_force_rollback():
    rc = RollbackController()
    degraded_metrics = MetricsSnapshot(error_rate=0.05, p99_latency_ms=400)
    status = rc.evaluate(degraded_metrics)
    assert status in (HealthStatus.DEGRADED, HealthStatus.STEADY)
    if status == HealthStatus.DEGRADED:
        assert rc.should_force_rollback(degraded_metrics) is False


# -- manifest / command validation (hallucination mitigation) -----------------#

def test_manifest_validator_blocks_namespace_delete():
    v = ManifestValidator()
    ok, reason = v.validate_command("kubectl delete namespace chaos-demo")
    assert ok is False


def test_manifest_validator_blocks_rm_rf():
    v = ManifestValidator()
    ok, _ = v.validate_command("rm -rf /")
    assert ok is False


def test_manifest_validator_allows_safe_command():
    v = ManifestValidator()
    ok, _ = v.validate_command("kubectl delete pod -l app=cartservice")
    assert ok is True


def test_manifest_validator_rejects_missing_namespace():
    v = ManifestValidator()
    ok, reason = v.validate_manifest_shape({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "x"}})
    assert ok is False
    assert "namespace" in reason

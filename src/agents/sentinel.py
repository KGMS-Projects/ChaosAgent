"""
Sentinel Agent (paper Section IV.B / V.D).

Acts as the coordinating/governance agent:
  - Runs the monitoring loop (in production: every 15s via Prometheus).
  - Maintains the FSM: Steady / Degraded / Critical / Recovering.
  - Authorizes (or denies/escalates) Adversary's proposed fault injections
    against blast-radius budget and the human-in-loop gate.
  - Triggers automatic rollback when health drops below the critical threshold.
  - Emits DiagnosticRequests to Remediation when in Degraded/Critical state.
"""
from __future__ import annotations
import uuid
from typing import Optional

from src.config import SAFETY
from src.safety.guardrails import HumanApprovalGate, RollbackController, BlastRadiusGuard
from src.state.schemas import (
    FaultReport, ExperimentAuthorization, DiagnosticRequest, HealthStatus, MetricsSnapshot,
)


class SentinelAgent:
    """
    Note: Sentinel's *decisions* (FSM transition, authorization logic) are
    deterministic safety logic, not LLM calls -- this is intentional. An LLM
    is used only for the optional narrative summary (`summarize_state`),
    keeping the safety-critical control path free of hallucination risk.
    """

    def __init__(self, monitor_client=None, auto_approve_callback=None):
        self.monitor_client = monitor_client  # optional, for human-readable summaries only
        self._approval_gate = HumanApprovalGate(auto_approve_callback=auto_approve_callback)
        self._rollback_controller = RollbackController()
        self._blast_radius_guard = BlastRadiusGuard()
        self.current_status: HealthStatus = HealthStatus.STEADY

    def observe(self, metrics: MetricsSnapshot) -> HealthStatus:
        """Update the FSM based on the latest metrics snapshot."""
        new_status = self._rollback_controller.evaluate(metrics)

        if self.current_status in (HealthStatus.DEGRADED, HealthStatus.CRITICAL) and new_status == HealthStatus.STEADY:
            # Was unhealthy, metrics just recovered -- pass through Recovering
            # for one tick so downstream nodes know to keep Adversary paused.
            self.current_status = HealthStatus.RECOVERING
        else:
            self.current_status = new_status
        return self.current_status

    def confirm_recovered(self) -> None:
        """Called once a post-remediation health check confirms stability,
        moving Recovering -> Steady and re-enabling the Adversary."""
        if self.current_status == HealthStatus.RECOVERING:
            self.current_status = HealthStatus.STEADY

    def should_trigger_rollback(self, metrics: MetricsSnapshot) -> bool:
        return self._rollback_controller.should_force_rollback(metrics)

    def authorize_experiment(self, fault: FaultReport, budget_remaining: float) -> ExperimentAuthorization:
        return self._approval_gate.decide(fault, budget_remaining)

    def consume_budget(self, budget_remaining: float, fault: FaultReport) -> float:
        return self._blast_radius_guard.consume(budget_remaining, fault.blast_radius_fraction)

    def build_diagnostic_request(
        self,
        metrics: MetricsSnapshot,
        affected_services: list[str],
        related_fault_message_id: Optional[str] = None,
    ) -> DiagnosticRequest:
        anomalous = {}
        if metrics.error_rate > 0.01:
            anomalous["error_rate"] = metrics.error_rate
        if metrics.p99_latency_ms > 300:
            anomalous["p99_latency_ms"] = metrics.p99_latency_ms
        if metrics.pod_restarts > 0:
            anomalous["pod_restarts"] = float(metrics.pod_restarts)
        if metrics.cpu_utilization > 0.8:
            anomalous["cpu_utilization"] = metrics.cpu_utilization
        if metrics.memory_utilization > 0.8:
            anomalous["memory_utilization"] = metrics.memory_utilization

        return DiagnosticRequest(
            message_id=f"diag-{uuid.uuid4().hex[:8]}",
            related_fault_message_id=related_fault_message_id,
            anomalous_metrics=anomalous,
            affected_services=affected_services,
            severity=self.current_status,
        )

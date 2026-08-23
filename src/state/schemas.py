"""
Pydantic state schemas for the Multi-Agent Chaos Engineering Framework.

Implements:
  - The four typed inter-agent message types from the paper's Communication
    Protocol (Section IV.C): FaultReport, DiagnosticRequest, HealingConfirmation,
    ExperimentAuthorization.
  - The Sentinel FSM health states (Steady / Degraded / Critical / Recovering).
  - The overall LangGraph GraphState that flows through the StateGraph.
"""
from __future__ import annotations
from enum import Enum
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator, ConfigDict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #

class HealthStatus(str, Enum):
    STEADY = "steady"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    RECOVERING = "recovering"


class FaultCategory(str, Enum):
    POD_TERMINATION = "pod_termination"
    NETWORK_LATENCY = "network_latency"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    PACKET_LOSS = "packet_loss"
    CONFIGURATION_DRIFT = "configuration_drift"


class RemediationCategory(str, Enum):
    RESTART_BASED = "restart_based"
    CONFIGURATION_BASED = "configuration_based"
    HORIZONTAL_SCALING = "horizontal_scaling"


class AuthorizationDecision(str, Enum):
    APPROVED = "approved"
    DENIED = "denied"
    PENDING_HUMAN_REVIEW = "pending_human_review"


# --------------------------------------------------------------------------- #
# Typed inter-agent messages (paper Section IV.C)
# --------------------------------------------------------------------------- #

class FaultReport(BaseModel):
    """Adversary -> Sentinel: a fault that was (or is proposed to be) injected."""
    message_id: str
    timestamp: str = Field(default_factory=_now)
    fault_category: FaultCategory
    target_service: str
    target_namespace: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    predicted_propagation: List[str] = Field(default_factory=list)
    expected_observable_impact: str
    reasoning_trace: str = ""
    blast_radius_fraction: float = Field(ge=0.0, le=1.0)

    @field_validator("blast_radius_fraction")
    @classmethod
    def _validate_fraction(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("blast_radius_fraction must be within [0, 1]")
        return v


class DiagnosticRequest(BaseModel):
    """Sentinel -> Remediation: metric anomaly that needs root-cause analysis."""
    message_id: str
    timestamp: str = Field(default_factory=_now)
    related_fault_message_id: Optional[str] = None
    anomalous_metrics: Dict[str, float] = Field(default_factory=dict)
    affected_services: List[str] = Field(default_factory=list)
    lookback_window_minutes: int = 5
    severity: HealthStatus = HealthStatus.DEGRADED


class HealingConfirmation(BaseModel):
    """Remediation -> Sentinel: an action that was taken and its outcome."""
    message_id: str
    timestamp: str = Field(default_factory=_now)
    related_diagnostic_request_id: str
    remediation_category: RemediationCategory
    action_taken: str
    dry_run_validated: bool
    success: bool
    root_cause_summary: str = ""
    reasoning_trace: str = ""
    secondary_effects_observed: List[str] = Field(default_factory=list)


class ExperimentAuthorization(BaseModel):
    """Sentinel -> Adversary: approval/denial for a proposed fault injection."""
    message_id: str
    timestamp: str = Field(default_factory=_now)
    related_fault_report_id: str
    decision: AuthorizationDecision
    max_allowed_blast_radius: float = Field(ge=0.0, le=1.0)
    reason: str = ""
    requires_human_ack: bool = False


# --------------------------------------------------------------------------- #
# Aggregate LangGraph state
# --------------------------------------------------------------------------- #

class MetricsSnapshot(BaseModel):
    timestamp: str = Field(default_factory=_now)
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    success_rate: float = 1.0
    error_rate: float = 0.0
    cpu_utilization: float = 0.0
    memory_utilization: float = 0.0
    pod_restarts: int = 0

    def health_score(self) -> float:
        """0-100 composite health score used by Sentinel's FSM transitions."""
        score = 100.0
        score -= max(0.0, (self.error_rate - 0.01)) * 500
        score -= max(0.0, (self.p99_latency_ms - 300)) * 0.05
        score -= min(30.0, self.pod_restarts * 5)
        score -= max(0.0, (self.cpu_utilization - 0.8)) * 100
        score -= max(0.0, (self.memory_utilization - 0.8)) * 100
        return max(0.0, min(100.0, score))


class ExperimentLogEntry(BaseModel):
    cycle: int
    phase: str
    timestamp: str = Field(default_factory=_now)
    detail: Dict[str, Any] = Field(default_factory=dict)


class GraphState(BaseModel):
    """
    The full state object threaded through every LangGraph node. LangGraph
    treats this as the shared blackboard; each node reads relevant fields and
    returns a partial update dict.
    """
    scenario_id: str
    cycle_count: int = 0
    max_cycles: int = 12
    health_status: HealthStatus = HealthStatus.STEADY
    current_metrics: MetricsSnapshot = Field(default_factory=MetricsSnapshot)

    fault_reports: List[FaultReport] = Field(default_factory=list)
    diagnostic_requests: List[DiagnosticRequest] = Field(default_factory=list)
    healing_confirmations: List[HealingConfirmation] = Field(default_factory=list)
    experiment_authorizations: List[ExperimentAuthorization] = Field(default_factory=list)

    blast_radius_budget_remaining: float = 1.0
    experiment_log: List[ExperimentLogEntry] = Field(default_factory=list)

    scenario_start_time: str = Field(default_factory=_now)
    scenario_end_time: Optional[str] = None
    recovered: bool = False
    terminated_reason: Optional[str] = None

    model_config = ConfigDict(use_enum_values=False)

    def log(self, phase: str, **detail: Any) -> None:
        self.experiment_log.append(
            ExperimentLogEntry(cycle=self.cycle_count, phase=phase, detail=detail)
        )

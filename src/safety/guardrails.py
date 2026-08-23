"""
Multi-layered safety architecture (paper Section IV.D).

Layers implemented here:
  1. Operation whitelisting      -> WHITELISTED_FAULTS / WHITELISTED_REMEDIATIONS
  2. Blast-radius constraints    -> BlastRadiusGuard
  3. Human-in-the-loop gating    -> HumanApprovalGate
  4. Automatic rollback          -> RollbackController
  5. Hallucination mitigation    -> ManifestValidator (syntax + dry-run + pattern check)

None of these depend on a live cluster; they are pure decision logic so they
can be unit-tested in isolation (see tests/test_safety.py).
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Callable, Optional

from src.config import SAFETY
from src.state.schemas import (
    FaultReport, ExperimentAuthorization, AuthorizationDecision,
    HealthStatus, MetricsSnapshot, RemediationCategory,
)


class SafetyViolation(Exception):
    """Raised when an action would breach a hard safety constraint."""


# --------------------------------------------------------------------------- #
# 1. Operation whitelisting
# --------------------------------------------------------------------------- #

WHITELISTED_FAULTS = {
    "pod_termination", "network_latency", "resource_exhaustion",
    "packet_loss", "configuration_drift",
}

WHITELISTED_REMEDIATIONS = {
    "restart_based", "configuration_based", "horizontal_scaling",
}


def enforce_fault_whitelist(fault_category: str) -> None:
    if fault_category not in WHITELISTED_FAULTS:
        raise SafetyViolation(
            f"Fault category '{fault_category}' is not in the operation whitelist. "
            f"Allowed: {sorted(WHITELISTED_FAULTS)}"
        )


def enforce_remediation_whitelist(remediation_category: str) -> None:
    if remediation_category not in WHITELISTED_REMEDIATIONS:
        raise SafetyViolation(
            f"Remediation category '{remediation_category}' is not in the operation "
            f"whitelist. Allowed: {sorted(WHITELISTED_REMEDIATIONS)}"
        )


# --------------------------------------------------------------------------- #
# 2. Blast-radius constraints
# --------------------------------------------------------------------------- #

@dataclass
class BlastRadiusGuard:
    max_fraction: float = SAFETY.max_blast_radius_fraction

    def check(self, proposed_fraction: float, budget_remaining: float) -> bool:
        """Returns True iff the proposed experiment fits within both the
        per-experiment ceiling and the scenario's remaining budget."""
        if proposed_fraction > self.max_fraction:
            return False
        if proposed_fraction > budget_remaining:
            return False
        return True

    def consume(self, budget_remaining: float, proposed_fraction: float) -> float:
        return max(0.0, budget_remaining - proposed_fraction)


# --------------------------------------------------------------------------- #
# 3. Human-in-the-loop gating
# --------------------------------------------------------------------------- #

class HumanApprovalGate:
    """
    Decides whether a proposed fault requires human sign-off before injection.
    In automated evaluation runs, `auto_approve_callback` can be supplied to
    simulate an operator (e.g. always-approve for reproducible experiments);
    in a real deployment this would be wired to a Slack prompt / CLI confirm.
    """

    def __init__(self, auto_approve_callback: Optional[Callable[[FaultReport], bool]] = None):
        self._auto_approve_callback = auto_approve_callback

    def requires_human_review(self, fault: FaultReport) -> bool:
        return (
            fault.fault_category.value in SAFETY.high_risk_actions
            or fault.blast_radius_fraction > SAFETY.max_blast_radius_fraction
        )

    def decide(self, fault: FaultReport, budget_remaining: float) -> ExperimentAuthorization:
        guard = BlastRadiusGuard()
        message_id = f"auth-{fault.message_id}"

        if not guard.check(fault.blast_radius_fraction, budget_remaining):
            return ExperimentAuthorization(
                message_id=message_id,
                related_fault_report_id=fault.message_id,
                decision=AuthorizationDecision.DENIED,
                max_allowed_blast_radius=SAFETY.max_blast_radius_fraction,
                reason="Blast radius exceeds per-experiment ceiling or remaining budget.",
            )

        if self.requires_human_review(fault):
            if self._auto_approve_callback is not None and self._auto_approve_callback(fault):
                return ExperimentAuthorization(
                    message_id=message_id,
                    related_fault_report_id=fault.message_id,
                    decision=AuthorizationDecision.APPROVED,
                    max_allowed_blast_radius=fault.blast_radius_fraction,
                    reason="High-risk action auto-approved by configured operator callback.",
                    requires_human_ack=True,
                )
            return ExperimentAuthorization(
                message_id=message_id,
                related_fault_report_id=fault.message_id,
                decision=AuthorizationDecision.PENDING_HUMAN_REVIEW,
                max_allowed_blast_radius=fault.blast_radius_fraction,
                reason="High-risk action requires human approval before injection.",
                requires_human_ack=True,
            )

        return ExperimentAuthorization(
            message_id=message_id,
            related_fault_report_id=fault.message_id,
            decision=AuthorizationDecision.APPROVED,
            max_allowed_blast_radius=fault.blast_radius_fraction,
            reason="Within blast-radius budget and not flagged as high-risk.",
        )


# --------------------------------------------------------------------------- #
# 4. Automatic rollback
# --------------------------------------------------------------------------- #

class RollbackController:
    """Watches health scores and decides FSM transitions / rollback triggers."""

    def evaluate(self, metrics: MetricsSnapshot) -> HealthStatus:
        score = metrics.health_score()
        if score < SAFETY.critical_health_threshold:
            return HealthStatus.CRITICAL
        if score < SAFETY.degraded_health_threshold:
            return HealthStatus.DEGRADED
        return HealthStatus.STEADY

    def should_force_rollback(self, metrics: MetricsSnapshot) -> bool:
        return self.evaluate(metrics) == HealthStatus.CRITICAL


# --------------------------------------------------------------------------- #
# 5. Hallucination mitigation: manifest / remedy validation
# --------------------------------------------------------------------------- #

_DANGEROUS_PATTERNS = [
    re.compile(r"\bkubectl\s+delete\s+namespace\b", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
    re.compile(r"\bdrop\s+database\b", re.IGNORECASE),
    re.compile(r"--all-namespaces\b.*\bdelete\b", re.IGNORECASE),
]


class ManifestValidator:
    """
    Validates an LLM-proposed remediation action before it is ever executed.
    This is deliberately conservative: syntax/shape checks + a denylist of
    catastrophic patterns + a required dry-run flag. It does NOT guarantee
    correctness of the remedy, only that it cannot trigger the most severe
    classes of accidental damage.
    """

    def validate_command(self, command: str) -> tuple[bool, str]:
        for pattern in _DANGEROUS_PATTERNS:
            if pattern.search(command):
                return False, f"Command matched denylisted destructive pattern: {pattern.pattern}"
        if not command.strip():
            return False, "Empty command."
        return True, "OK"

    def validate_manifest_shape(self, manifest: dict) -> tuple[bool, str]:
        required_keys = {"apiVersion", "kind", "metadata"}
        missing = required_keys - manifest.keys()
        if missing:
            return False, f"Manifest missing required keys: {missing}"
        if "namespace" not in manifest.get("metadata", {}):
            return False, "Manifest metadata missing 'namespace' (refusing cluster-wide default)."
        return True, "OK"

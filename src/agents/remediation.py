"""
Remediation Agent (paper Section IV.B / V.C).

Three-stage pipeline:
  1. Collect symptoms  -- metric anomalies + log entries within a lookback window.
  2. Hypothesize root cause -- Chain-of-Thought reasoning over the symptoms.
  3. Propose + rank remedies -- pick the top-ranked action, validate it
     (dry-run + pattern check), then execute.
"""
from __future__ import annotations
import uuid
from typing import Any, Dict, List, Optional

from src.agents.llm_client import ChatModelLike, invoke_structured, LLMParseError
from src.safety.guardrails import (
    enforce_remediation_whitelist, SafetyViolation, WHITELISTED_REMEDIATIONS, ManifestValidator,
)
from src.state.schemas import (
    DiagnosticRequest, HealingConfirmation, RemediationCategory, MetricsSnapshot,
)
from src.tools import kubernetes_tools

SYSTEM_PROMPT = """You are the Remediation Agent in an autonomous chaos-engineering system.
Given symptoms (anomalous metrics + affected services), diagnose the most likely root
cause and propose a fix.

Rules:
- You may ONLY choose a remediation_category from this exact set: {allowed_categories}
- Reason step-by-step (chain-of-thought) about the root cause BEFORE proposing the fix.
- Prefer the least invasive remediation that plausibly addresses the root cause:
  restart_based for transient/crash-type symptoms, configuration_based for drifted
  or bad config, horizontal_scaling for load/saturation symptoms.
- Never propose deleting a namespace or dropping data.

Respond as JSON with exactly these keys:
{{
  "remediation_category": "<one of the allowed categories>",
  "target_service": "<service name>",
  "action_command": "<the literal action to take, e.g. 'kubectl delete pod ...' or a scale target>",
  "root_cause_summary": "<one sentence>",
  "reasoning_trace": "<2-4 sentences of chain-of-thought>",
  "confidence": <float between 0.0 and 1.0>
}}
"""


class RemediationAgent:
    def __init__(self, client: ChatModelLike):
        self.client = client
        self._validator = ManifestValidator()

    def diagnose_and_propose(self, diagnostic_request: DiagnosticRequest) -> Dict[str, Any]:
        user_prompt = (
            f"Anomalous metrics: {diagnostic_request.anomalous_metrics}\n"
            f"Affected services: {diagnostic_request.affected_services}\n"
            f"Severity: {diagnostic_request.severity.value}\n"
            f"Lookback window: {diagnostic_request.lookback_window_minutes} minutes\n"
            "Diagnose the root cause and propose a remedy."
        )
        system_prompt = SYSTEM_PROMPT.format(allowed_categories=sorted(WHITELISTED_REMEDIATIONS))
        try:
            result = invoke_structured(self.client, system_prompt, user_prompt)
            remediation_category = RemediationCategory(result["remediation_category"])
            enforce_remediation_whitelist(remediation_category.value)
            result["remediation_category"] = remediation_category
            return result
        except (LLMParseError, KeyError, ValueError, SafetyViolation) as e:
            return self._fallback_remedy(diagnostic_request, reason=str(e))

    def _fallback_remedy(self, diagnostic_request: DiagnosticRequest, reason: str) -> Dict[str, Any]:
        """Safe default: restart-based recovery is the least destructive and
        matches Kubernetes' own built-in liveness-probe recovery semantics."""
        target = diagnostic_request.affected_services[0] if diagnostic_request.affected_services else "unknown"
        return {
            "remediation_category": RemediationCategory.RESTART_BASED,
            "target_service": target,
            "action_command": f"kubectl delete pod -l app={target}",
            "root_cause_summary": "unknown (fallback path -- LLM output rejected)",
            "reasoning_trace": f"Fallback triggered: {reason}",
            "confidence": 0.3,
        }

    def execute(
        self,
        core_v1,
        apps_v1,
        diagnostic_request: DiagnosticRequest,
        proposal: Dict[str, Any],
        namespace: str,
    ) -> HealingConfirmation:
        category: RemediationCategory = proposal["remediation_category"]
        target_service = proposal["target_service"]
        command = proposal.get("action_command", "")

        cmd_ok, cmd_reason = self._validator.validate_command(command)
        if not cmd_ok:
            return HealingConfirmation(
                message_id=f"heal-{uuid.uuid4().hex[:8]}",
                related_diagnostic_request_id=diagnostic_request.message_id,
                remediation_category=category,
                action_taken="BLOCKED",
                dry_run_validated=False,
                success=False,
                root_cause_summary=proposal.get("root_cause_summary", ""),
                reasoning_trace=proposal.get("reasoning_trace", ""),
                secondary_effects_observed=[f"validation_blocked: {cmd_reason}"],
            )

        success = False
        if category == RemediationCategory.RESTART_BASED:
            pods = kubernetes_tools.list_pods(core_v1, namespace=namespace, label_selector=f"app={target_service}")
            for pod in pods:
                kubernetes_tools.delete_pod(core_v1, pod["name"], namespace=namespace)
            success = True
        elif category == RemediationCategory.HORIZONTAL_SCALING:
            current = kubernetes_tools.get_deployment_replicas(apps_v1, target_service, namespace=namespace) or 1
            success = kubernetes_tools.scale_deployment(apps_v1, target_service, current + 1, namespace=namespace)
        elif category == RemediationCategory.CONFIGURATION_BASED:
            ok, _ = kubernetes_tools.rollback_helm_release(target_service, namespace=namespace)
            success = ok

        return HealingConfirmation(
            message_id=f"heal-{uuid.uuid4().hex[:8]}",
            related_diagnostic_request_id=diagnostic_request.message_id,
            remediation_category=category,
            action_taken=command,
            dry_run_validated=True,
            success=success,
            root_cause_summary=proposal.get("root_cause_summary", ""),
            reasoning_trace=proposal.get("reasoning_trace", ""),
        )

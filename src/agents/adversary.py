"""
Adversary Agent (paper Section IV.B / V.B).

Implements intelligent fault injection using a ReAct + Chain-of-Thought loop:
  1. Reason over the service dependency graph + prior experiment history to
     identify the most promising untested vulnerability vector.
  2. Propose a fault-injection hypothesis (category, target, predicted
     propagation, expected observable impact) as a typed FaultReport.
  3. (Orchestration layer) submits the FaultReport to Sentinel for
     authorization before any real injection happens.
  4. Once authorized, dispatch the fault via Chaos Mesh.
"""
from __future__ import annotations
import uuid
from typing import Any, Dict, List, Optional

from src.agents.llm_client import ChatModelLike, invoke_structured, LLMParseError
from src.safety.guardrails import enforce_fault_whitelist, SafetyViolation, WHITELISTED_FAULTS
from src.state.schemas import FaultReport, FaultCategory, GraphState
from src.tools import chaos_mesh_tools

SYSTEM_PROMPT = """You are the Adversary Agent in an autonomous chaos-engineering system.
Your job is to systematically discover resilience weaknesses in a microservices system
by proposing ONE fault injection at a time.

Rules:
- You may ONLY choose a fault_category from this exact set: {allowed_categories}
- Use chain-of-thought: first reason about which service is most likely to reveal a
  NEW weakness given the dependency graph and past experiments (avoid repeating an
  identical (fault_category, target_service) pair already in the experiment history).
- Predict the failure propagation path through dependent services BEFORE proposing
  the injection.
- Keep blast_radius_fraction conservative (typically 0.05-0.25) unless you have strong
  justification for a wider-reaching experiment.

Respond as JSON with exactly these keys:
{{
  "fault_category": "<one of the allowed categories>",
  "target_service": "<service name from the dependency graph>",
  "parameters": {{...fault-specific parameters, e.g. latency_ms, loss_percent...}},
  "predicted_propagation": ["service_a", "service_b", ...],
  "expected_observable_impact": "<one sentence>",
  "reasoning_trace": "<2-4 sentences of chain-of-thought>",
  "blast_radius_fraction": <float between 0.0 and 1.0>
}}
"""


class AdversaryAgent:
    def __init__(self, client: ChatModelLike):
        self.client = client

    def propose_fault(
        self,
        dependency_graph: Dict[str, List[str]],
        experiment_history: List[FaultReport],
        namespace: str,
    ) -> FaultReport:
        history_summary = [
            {"fault_category": fr.fault_category.value, "target_service": fr.target_service}
            for fr in experiment_history
        ]
        user_prompt = (
            f"Service dependency graph (service -> depends_on):\n{dependency_graph}\n\n"
            f"Prior experiments already attempted this scenario:\n{history_summary}\n\n"
            f"Target namespace: {namespace}\n"
            "Propose the next fault injection."
        )
        system_prompt = SYSTEM_PROMPT.format(allowed_categories=sorted(WHITELISTED_FAULTS))

        try:
            result = invoke_structured(self.client, system_prompt, user_prompt)
        except LLMParseError as e:
            # Hallucination-safety fallback: pick the least-recently-tried
            # whitelisted category against an arbitrary known service rather
            # than propagate a malformed/unsafe action.
            return self._fallback_fault(dependency_graph, experiment_history, namespace, reason=str(e))

        try:
            fault_category = FaultCategory(result["fault_category"])
            enforce_fault_whitelist(fault_category.value)
        except (KeyError, ValueError, SafetyViolation) as e:
            return self._fallback_fault(dependency_graph, experiment_history, namespace, reason=str(e))

        return FaultReport(
            message_id=f"fault-{uuid.uuid4().hex[:8]}",
            fault_category=fault_category,
            target_service=result.get("target_service", next(iter(dependency_graph), "unknown")),
            target_namespace=namespace,
            parameters=result.get("parameters", {}),
            predicted_propagation=result.get("predicted_propagation", []),
            expected_observable_impact=result.get("expected_observable_impact", ""),
            reasoning_trace=result.get("reasoning_trace", ""),
            blast_radius_fraction=float(result.get("blast_radius_fraction", 0.1)),
        )

    def _fallback_fault(
        self,
        dependency_graph: Dict[str, List[str]],
        experiment_history: List[FaultReport],
        namespace: str,
        reason: str,
    ) -> FaultReport:
        tried = {(fr.fault_category, fr.target_service) for fr in experiment_history}
        for category in sorted(WHITELISTED_FAULTS):
            for service in dependency_graph:
                if (FaultCategory(category), service) not in tried:
                    return FaultReport(
                        message_id=f"fault-fallback-{uuid.uuid4().hex[:8]}",
                        fault_category=FaultCategory(category),
                        target_service=service,
                        target_namespace=namespace,
                        expected_observable_impact="unknown (fallback path -- LLM output rejected)",
                        reasoning_trace=f"Fallback triggered: {reason}",
                        blast_radius_fraction=0.05,
                    )
        # Exhausted combinations -- signal caller to end the scenario.
        raise RuntimeError("Adversary exhausted all whitelisted (fault, service) combinations.")

    def inject(self, custom_api, fault: FaultReport, target_labels: Dict[str, str]) -> Dict[str, Any]:
        """Execute the fault via Chaos Mesh. Only call this AFTER Sentinel
        has returned an APPROVED ExperimentAuthorization for this fault."""
        return chaos_mesh_tools.inject_fault(
            custom_api, fault.fault_category, target_labels,
            namespace=fault.target_namespace, **fault.parameters,
        )

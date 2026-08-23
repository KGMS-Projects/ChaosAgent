"""
LangGraph orchestration for the Attack-Monitor-Heal cycle (paper Section IV.A,
Fig. 1 / Fig. 2).

Node graph:

    START -> sentinel_monitor --(steady)--> adversary_propose -> sentinel_authorize
                  ^   |    ..(degraded/critical)--> remediation ---------+
                  |   ..(recovering)--> confirm_recovered -----------------+
                  |                                                       |
                  +---------------------------(loop back)-----------------+
                  |
                  +--(max_cycles reached)--> END

    sentinel_authorize --(approved)--> adversary_inject -> sentinel_monitor
    sentinel_authorize --(denied / pending_human_review)--> sentinel_monitor

A hard `max_cycles` check runs at the TOP of every pass through
`sentinel_monitor` -- the single node every path loops back through -- which
is what guarantees termination and is the direct fix for the infinite-loop
routing bug found during earlier integration testing (every branch of the
graph re-enters this one node, so one guard is sufficient and cannot be
bypassed by any routing path).
"""
from __future__ import annotations
from typing import Callable, Dict, List, Optional

from langgraph.graph import StateGraph, END, START

from src.agents.adversary import AdversaryAgent
from src.agents.remediation import RemediationAgent
from src.agents.sentinel import SentinelAgent
from src.state.schemas import (
    GraphState, HealthStatus, MetricsSnapshot, AuthorizationDecision, FaultReport,
)

MetricsProvider = Callable[[], MetricsSnapshot]
FaultInjector = Callable[[FaultReport], None]
ExperimentStopper = Callable[[], None]
LabelResolver = Callable[[str], Dict[str, str]]


def build_chaos_graph(
    adversary: AdversaryAgent,
    remediation: RemediationAgent,
    sentinel: SentinelAgent,
    dependency_graph: Dict[str, List[str]],
    namespace: str,
    metrics_provider: MetricsProvider,
    fault_injector: FaultInjector,
    label_resolver: LabelResolver,
    core_v1=None,
    apps_v1=None,
    experiment_stopper: Optional[ExperimentStopper] = None,
):
    """Factory that closes over all injected dependencies (agents + I/O
    callables) and returns a compiled LangGraph. Injecting metrics_provider /
    fault_injector / experiment_stopper as plain callables (rather than
    hardcoding Prometheus/Chaos Mesh calls into the nodes) is what lets the
    evaluation harness swap in simulated baselines without touching this
    module."""

    # ------------------------------------------------------------------ #
    # Nodes
    # ------------------------------------------------------------------ #

    def sentinel_monitor(state: GraphState):
        cycle_count = state.cycle_count + 1
        metrics = metrics_provider()
        # Reconstruct a lightweight sentinel view so FSM state carries across
        # invocations (LangGraph state itself is the source of truth; we
        # re-sync the agent's internal FSM pointer each call).
        sentinel.current_status = state.health_status
        new_status = sentinel.observe(metrics)

        log_entry = state.experiment_log + [
            _log(cycle_count, "sentinel_monitor", health_status=new_status.value,
                 health_score=metrics.health_score())
        ]
        return {
            "cycle_count": cycle_count,
            "current_metrics": metrics,
            "health_status": new_status,
            "experiment_log": log_entry,
        }

    def confirm_recovered(state: GraphState):
        sentinel.current_status = state.health_status
        sentinel.confirm_recovered()
        log_entry = state.experiment_log + [
            _log(state.cycle_count, "confirm_recovered", new_status=sentinel.current_status.value)
        ]
        return {"health_status": sentinel.current_status, "experiment_log": log_entry, "recovered": True}

    def adversary_propose(state: GraphState):
        fault = adversary.propose_fault(dependency_graph, state.fault_reports, namespace)
        log_entry = state.experiment_log + [
            _log(state.cycle_count, "adversary_propose",
                 fault_category=fault.fault_category.value, target=fault.target_service)
        ]
        return {"fault_reports": state.fault_reports + [fault], "experiment_log": log_entry}

    def sentinel_authorize(state: GraphState):
        fault = state.fault_reports[-1]
        auth = sentinel.authorize_experiment(fault, state.blast_radius_budget_remaining)
        budget = state.blast_radius_budget_remaining
        if auth.decision == AuthorizationDecision.APPROVED:
            budget = sentinel.consume_budget(budget, fault)
        log_entry = state.experiment_log + [
            _log(state.cycle_count, "sentinel_authorize", decision=auth.decision.value, reason=auth.reason)
        ]
        return {
            "experiment_authorizations": state.experiment_authorizations + [auth],
            "blast_radius_budget_remaining": budget,
            "experiment_log": log_entry,
        }

    def adversary_inject(state: GraphState):
        fault = state.fault_reports[-1]
        fault_injector(fault)
        log_entry = state.experiment_log + [
            _log(state.cycle_count, "adversary_inject", fault_id=fault.message_id)
        ]
        return {"experiment_log": log_entry}

    def remediation_node(state: GraphState):
        fault = state.fault_reports[-1] if state.fault_reports else None
        affected = ([fault.target_service] + fault.predicted_propagation) if fault else []
        diag = sentinel.build_diagnostic_request(
            state.current_metrics, affected_services=affected,
            related_fault_message_id=fault.message_id if fault else None,
        )
        proposal = remediation.diagnose_and_propose(diag)
        confirmation = remediation.execute(core_v1, apps_v1, diag, proposal, namespace)
        log_entry = state.experiment_log + [
            _log(state.cycle_count, "remediation",
                 category=confirmation.remediation_category.value, success=confirmation.success)
        ]
        return {
            "diagnostic_requests": state.diagnostic_requests + [diag],
            "healing_confirmations": state.healing_confirmations + [confirmation],
            "experiment_log": log_entry,
        }

    def rollback_node(state: GraphState):
        if experiment_stopper is not None:
            experiment_stopper()
        log_entry = state.experiment_log + [
            _log(state.cycle_count, "forced_rollback", human_notified=True)
        ]
        return {"experiment_log": log_entry}

    # ------------------------------------------------------------------ #
    # Routing
    # ------------------------------------------------------------------ #

    def route_after_monitor(state: GraphState) -> str:
        if state.cycle_count >= state.max_cycles:
            return "terminate"
        if state.health_status == HealthStatus.CRITICAL:
            return "rollback"
        if state.health_status == HealthStatus.DEGRADED:
            return "remediate"
        if state.health_status == HealthStatus.RECOVERING:
            return "confirm_recovered"
        return "propose"

    def route_after_authorize(state: GraphState) -> str:
        decision = state.experiment_authorizations[-1].decision
        if decision == AuthorizationDecision.APPROVED:
            return "inject"
        return "skip"

    def terminate_node(state: GraphState):
        return {"terminated_reason": "max_cycles_reached", "scenario_end_time": _iso_now()}

    # ------------------------------------------------------------------ #
    # Graph assembly
    # ------------------------------------------------------------------ #

    graph = StateGraph(GraphState)
    graph.add_node("sentinel_monitor", sentinel_monitor)
    graph.add_node("confirm_recovered", confirm_recovered)
    graph.add_node("adversary_propose", adversary_propose)
    graph.add_node("sentinel_authorize", sentinel_authorize)
    graph.add_node("adversary_inject", adversary_inject)
    graph.add_node("remediation", remediation_node)
    graph.add_node("rollback", rollback_node)
    graph.add_node("terminate", terminate_node)

    graph.add_edge(START, "sentinel_monitor")
    graph.add_conditional_edges("sentinel_monitor", route_after_monitor, {
        "propose": "adversary_propose",
        "remediate": "remediation",
        "rollback": "rollback",
        "confirm_recovered": "confirm_recovered",
        "terminate": "terminate",
    })
    graph.add_edge("confirm_recovered", "sentinel_monitor")
    graph.add_edge("adversary_propose", "sentinel_authorize")
    graph.add_conditional_edges("sentinel_authorize", route_after_authorize, {
        "inject": "adversary_inject",
        "skip": "sentinel_monitor",
    })
    graph.add_edge("adversary_inject", "sentinel_monitor")
    graph.add_edge("remediation", "sentinel_monitor")
    graph.add_edge("rollback", "sentinel_monitor")
    graph.add_edge("terminate", END)

    return graph.compile()


def _log(cycle: int, phase: str, **detail):
    from src.state.schemas import ExperimentLogEntry
    return ExperimentLogEntry(cycle=cycle, phase=phase, detail=detail)


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

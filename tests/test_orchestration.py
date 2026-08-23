from unittest.mock import MagicMock

import pytest

from src.agents.adversary import AdversaryAgent
from src.agents.remediation import RemediationAgent
from src.agents.sentinel import SentinelAgent
from src.state.schemas import (
    GraphState, MetricsSnapshot, FaultReport, FaultCategory, HealthStatus,
    RemediationCategory, HealingConfirmation,
)
from src.orchestration.graph import build_chaos_graph

DEP_GRAPH = {"cartservice": [], "frontend": ["cartservice"]}


def _stub_agents(fault_category=FaultCategory.POD_TERMINATION, remediation_succeeds=True):
    adversary = AdversaryAgent(client=MagicMock())
    adversary.propose_fault = MagicMock(return_value=FaultReport(
        message_id="f1", fault_category=fault_category, target_service="cartservice",
        target_namespace="chaos-demo", expected_observable_impact="x", blast_radius_fraction=0.1,
    ))
    remediation = RemediationAgent(client=MagicMock())
    remediation.diagnose_and_propose = MagicMock(return_value={
        "remediation_category": RemediationCategory.RESTART_BASED, "target_service": "cartservice",
        "action_command": "kubectl delete pod -l app=cartservice",
        "root_cause_summary": "x", "reasoning_trace": "x", "confidence": 0.9,
    })
    remediation.execute = MagicMock(return_value=HealingConfirmation(
        message_id="h1", related_diagnostic_request_id="d1",
        remediation_category=RemediationCategory.RESTART_BASED, action_taken="restart",
        dry_run_validated=True, success=remediation_succeeds,
    ))
    sentinel = SentinelAgent(auto_approve_callback=lambda f: True)
    return adversary, remediation, sentinel


def _build_graph(metrics_provider, adversary=None, remediation=None, sentinel=None, max_cycles=10):
    adversary = adversary or _stub_agents()[0]
    remediation = remediation or _stub_agents()[1]
    sentinel = sentinel or _stub_agents()[2]
    injected = []
    return build_chaos_graph(
        adversary=adversary, remediation=remediation, sentinel=sentinel,
        dependency_graph=DEP_GRAPH, namespace="chaos-demo",
        metrics_provider=metrics_provider,
        fault_injector=lambda f: injected.append(f.message_id),
        label_resolver=lambda s: {"app": s},
    ), injected


# -- termination guarantees (infinite-loop regression) ------------------------#

def test_graph_terminates_when_always_steady():
    adversary, remediation, sentinel = _stub_agents()
    graph, _ = _build_graph(lambda: MetricsSnapshot(error_rate=0.0), adversary, remediation, sentinel)
    result = graph.invoke(GraphState(scenario_id="t1", max_cycles=6), config={"recursion_limit": 100})
    assert result["cycle_count"] == 6
    assert result["terminated_reason"] == "max_cycles_reached"


def test_graph_terminates_when_always_critical():
    """Regression test: an earlier version of the orchestration graph could
    loop indefinitely if health never recovered (Critical -> rollback ->
    monitor -> Critical -> ...). The cycle cap at sentinel_monitor must bound
    this regardless of how many times rollback fires."""
    adversary, remediation, sentinel = _stub_agents()
    graph, _ = _build_graph(lambda: MetricsSnapshot(error_rate=0.9, p99_latency_ms=9000, pod_restarts=20),
                             adversary, remediation, sentinel)
    result = graph.invoke(GraphState(scenario_id="t2", max_cycles=6), config={"recursion_limit": 200})
    assert result["cycle_count"] == 6
    assert result["terminated_reason"] == "max_cycles_reached"


def test_graph_terminates_when_oscillating_steady_critical():
    """Regression test: oscillation between STEADY and CRITICAL (a real
    failure mode caught during integration testing) must still terminate."""
    calls = {"n": 0}
    def oscillating_metrics():
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            return MetricsSnapshot(error_rate=0.8, p99_latency_ms=8000, pod_restarts=15)
        return MetricsSnapshot(error_rate=0.0)
    adversary, remediation, sentinel = _stub_agents()
    graph, _ = _build_graph(oscillating_metrics, adversary, remediation, sentinel)
    result = graph.invoke(GraphState(scenario_id="t3", max_cycles=8), config={"recursion_limit": 200})
    assert result["cycle_count"] == 8
    assert result["terminated_reason"] == "max_cycles_reached"


# -- routing correctness -------------------------------------------------------#

def test_graph_injects_fault_when_steady_and_approved():
    adversary, remediation, sentinel = _stub_agents()
    graph, injected = _build_graph(lambda: MetricsSnapshot(error_rate=0.0), adversary, remediation, sentinel, max_cycles=3)
    graph.invoke(GraphState(scenario_id="t4", max_cycles=3), config={"recursion_limit": 100})
    assert len(injected) >= 1


def test_graph_invokes_remediation_on_degraded():
    adversary, remediation, sentinel = _stub_agents()
    graph, _ = _build_graph(lambda: MetricsSnapshot(error_rate=0.10, p99_latency_ms=350),
                             adversary, remediation, sentinel, max_cycles=3)
    result = graph.invoke(GraphState(scenario_id="t5", max_cycles=3), config={"recursion_limit": 100})
    phases = [e.phase for e in result["experiment_log"]]
    assert "remediation" in phases


def test_graph_skips_injection_when_denied():
    adversary, remediation, sentinel = _stub_agents()
    sentinel._approval_gate._auto_approve_callback = lambda f: False  # force denial on high-risk-only path
    # Use a fault category considered high risk to force PENDING/DENIED path is not
    # directly testable without high-risk category; instead force budget to 0.
    graph, injected = _build_graph(lambda: MetricsSnapshot(error_rate=0.0), adversary, remediation, sentinel, max_cycles=3)
    state = GraphState(scenario_id="t6", max_cycles=3, blast_radius_budget_remaining=0.0)
    graph.invoke(state, config={"recursion_limit": 100})
    assert injected == []  # budget exhausted -> every proposal denied -> never injected

import json
from unittest.mock import MagicMock

import pytest

from src.agents.adversary import AdversaryAgent
from src.agents.remediation import RemediationAgent
from src.agents.sentinel import SentinelAgent
from src.agents.llm_client import invoke_structured, LLMParseError
from src.state.schemas import (
    FaultCategory, RemediationCategory, DiagnosticRequest, MetricsSnapshot,
    HealthStatus, AuthorizationDecision, FaultReport,
)


def _fake_client(response_text: str):
    client = MagicMock()
    response = MagicMock()
    response.content = response_text
    client.invoke.return_value = response
    return client


DEP_GRAPH = {"cartservice": [], "frontend": ["cartservice"], "checkoutservice": ["cartservice"]}


# -- llm_client ---------------------------------------------------------------#

def test_invoke_structured_parses_clean_json():
    client = _fake_client('{"a": 1, "b": "x"}')
    result = invoke_structured(client, "system", "user")
    assert result == {"a": 1, "b": "x"}


def test_invoke_structured_strips_markdown_fences():
    client = _fake_client('```json\n{"a": 1}\n```')
    result = invoke_structured(client, "system", "user")
    assert result == {"a": 1}


def test_invoke_structured_raises_on_garbage():
    client = _fake_client("I cannot help with that.")
    with pytest.raises(LLMParseError):
        invoke_structured(client, "system", "user")


# -- AdversaryAgent -------------------------------------------------------------#

def test_adversary_proposes_valid_fault_from_clean_llm_response():
    payload = {
        "fault_category": "network_latency", "target_service": "cartservice",
        "parameters": {"latency_ms": 300}, "predicted_propagation": ["frontend"],
        "expected_observable_impact": "slow cart", "reasoning_trace": "cot",
        "blast_radius_fraction": 0.15,
    }
    agent = AdversaryAgent(client=_fake_client(json.dumps(payload)))
    fault = agent.propose_fault(DEP_GRAPH, [], namespace="chaos-demo")
    assert fault.fault_category == FaultCategory.NETWORK_LATENCY
    assert fault.target_service == "cartservice"
    assert fault.blast_radius_fraction == 0.15


def test_adversary_falls_back_safely_on_malformed_llm_response():
    agent = AdversaryAgent(client=_fake_client("not json at all"))
    fault = agent.propose_fault(DEP_GRAPH, [], namespace="chaos-demo")
    assert fault.fault_category.value in {"pod_termination", "network_latency", "resource_exhaustion",
                                           "packet_loss", "configuration_drift"}
    assert "fallback" in fault.message_id or "Fallback" in fault.reasoning_trace


def test_adversary_falls_back_on_unwhitelisted_category():
    payload = {"fault_category": "delete_everything", "target_service": "cartservice",
               "expected_observable_impact": "x", "blast_radius_fraction": 0.1}
    agent = AdversaryAgent(client=_fake_client(json.dumps(payload)))
    fault = agent.propose_fault(DEP_GRAPH, [], namespace="chaos-demo")
    # Must not propagate the unwhitelisted category -- fallback kicks in.
    assert fault.fault_category.value != "delete_everything"


def test_adversary_avoids_repeating_exact_prior_combination_in_fallback():
    prior = FaultReport(
        message_id="m1", fault_category=FaultCategory.POD_TERMINATION, target_service="cartservice",
        target_namespace="chaos-demo", expected_observable_impact="x", blast_radius_fraction=0.1,
    )
    agent = AdversaryAgent(client=_fake_client("garbage"))
    fault = agent.propose_fault(DEP_GRAPH, [prior], namespace="chaos-demo")
    assert (fault.fault_category, fault.target_service) != (FaultCategory.POD_TERMINATION, "cartservice")


# -- RemediationAgent -----------------------------------------------------------#

def test_remediation_proposes_valid_remedy_from_clean_llm_response():
    payload = {
        "remediation_category": "restart_based", "target_service": "cartservice",
        "action_command": "kubectl delete pod -l app=cartservice",
        "root_cause_summary": "pod crash loop", "reasoning_trace": "cot", "confidence": 0.9,
    }
    agent = RemediationAgent(client=_fake_client(json.dumps(payload)))
    diag = DiagnosticRequest(message_id="d1", affected_services=["cartservice"], severity=HealthStatus.DEGRADED)
    proposal = agent.diagnose_and_propose(diag)
    assert proposal["remediation_category"] == RemediationCategory.RESTART_BASED


def test_remediation_falls_back_to_restart_on_malformed_response():
    agent = RemediationAgent(client=_fake_client("nonsense"))
    diag = DiagnosticRequest(message_id="d1", affected_services=["cartservice"], severity=HealthStatus.DEGRADED)
    proposal = agent.diagnose_and_propose(diag)
    assert proposal["remediation_category"] == RemediationCategory.RESTART_BASED


def test_remediation_execute_blocks_dangerous_command():
    agent = RemediationAgent(client=_fake_client("{}"))
    diag = DiagnosticRequest(message_id="d1", affected_services=["cartservice"], severity=HealthStatus.CRITICAL)
    proposal = {"remediation_category": RemediationCategory.RESTART_BASED, "target_service": "cartservice",
                "action_command": "kubectl delete namespace chaos-demo",
                "root_cause_summary": "x", "reasoning_trace": "x"}
    confirmation = agent.execute(MagicMock(), MagicMock(), diag, proposal, namespace="chaos-demo")
    assert confirmation.success is False
    assert confirmation.action_taken == "BLOCKED"


def test_remediation_execute_restart_based_calls_delete_pod(monkeypatch):
    agent = RemediationAgent(client=_fake_client("{}"))
    diag = DiagnosticRequest(message_id="d1", affected_services=["cartservice"], severity=HealthStatus.DEGRADED)
    proposal = {"remediation_category": RemediationCategory.RESTART_BASED, "target_service": "cartservice",
                "action_command": "kubectl delete pod -l app=cartservice",
                "root_cause_summary": "x", "reasoning_trace": "x"}
    core_v1 = MagicMock()
    core_v1.list_namespaced_pod.return_value.items = []
    confirmation = agent.execute(core_v1, MagicMock(), diag, proposal, namespace="chaos-demo")
    assert confirmation.success is True
    assert confirmation.remediation_category == RemediationCategory.RESTART_BASED


# -- SentinelAgent ----------------------------------------------------------------#

def test_sentinel_transitions_steady_to_critical():
    sentinel = SentinelAgent()
    status = sentinel.observe(MetricsSnapshot(error_rate=0.5, p99_latency_ms=5000, pod_restarts=10))
    assert status == HealthStatus.CRITICAL


def test_sentinel_recovering_before_steady():
    sentinel = SentinelAgent()
    sentinel.observe(MetricsSnapshot(error_rate=0.5))  # -> critical
    status = sentinel.observe(MetricsSnapshot(error_rate=0.0))  # metrics recovered
    assert status == HealthStatus.RECOVERING
    sentinel.confirm_recovered()
    assert sentinel.current_status == HealthStatus.STEADY


def test_sentinel_authorizes_low_risk_fault():
    sentinel = SentinelAgent()
    fault = FaultReport(message_id="f1", fault_category=FaultCategory.POD_TERMINATION, target_service="cartservice",
                         target_namespace="chaos-demo", expected_observable_impact="x", blast_radius_fraction=0.1)
    auth = sentinel.authorize_experiment(fault, budget_remaining=1.0)
    assert auth.decision == AuthorizationDecision.APPROVED


def test_sentinel_builds_diagnostic_request_from_anomalous_metrics():
    sentinel = SentinelAgent()
    metrics = MetricsSnapshot(error_rate=0.2, p99_latency_ms=500, pod_restarts=3)
    diag = sentinel.build_diagnostic_request(metrics, affected_services=["cartservice"])
    assert "error_rate" in diag.anomalous_metrics
    assert "pod_restarts" in diag.anomalous_metrics
    assert diag.affected_services == ["cartservice"]

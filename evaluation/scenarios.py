"""
Scenario definitions for the comparative evaluation (paper Section III.4 / RO7,
target: >=20 experimental scenarios spanning >=5 distinct failure categories).

Design note: the safety-whitelisted fault categories are pod_termination,
network_latency, resource_exhaustion, packet_loss, configuration_drift (5
categories -- see src/safety/guardrails.py::WHITELISTED_FAULTS). We generate
4 target-service variations per category = 20 base scenarios. "Cascading
failure" scenarios (mentioned in the paper's results discussion) are modeled
as a composite: two base scenarios chained on dependent services, available
via `generate_cascading_scenarios()` as an optional 21st+ extension rather
than folded into the core 20, so the primary evaluation set stays a clean,
even mapping onto the whitelist used everywhere else in the codebase.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.state.schemas import FaultCategory

# Google Online Boutique services (the paper's target demo app, 10 microservices).
ONLINE_BOUTIQUE_DEPENDENCY_GRAPH: Dict[str, List[str]] = {
    "frontend": ["cartservice", "productcatalogservice", "currencyservice", "recommendationservice", "checkoutservice", "shippingservice", "adservice"],
    "cartservice": ["redis-cart"],
    "checkoutservice": ["cartservice", "paymentservice", "shippingservice", "emailservice", "currencyservice", "productcatalogservice"],
    "recommendationservice": ["productcatalogservice"],
    "paymentservice": [],
    "shippingservice": [],
    "emailservice": [],
    "currencyservice": [],
    "productcatalogservice": [],
    "adservice": [],
    "redis-cart": [],
}

_VARIATION_TARGETS: Dict[FaultCategory, List[str]] = {
    FaultCategory.POD_TERMINATION: ["cartservice", "checkoutservice", "productcatalogservice", "frontend"],
    FaultCategory.NETWORK_LATENCY: ["paymentservice", "shippingservice", "recommendationservice", "currencyservice"],
    FaultCategory.RESOURCE_EXHAUSTION: ["redis-cart", "productcatalogservice", "cartservice", "checkoutservice"],
    FaultCategory.PACKET_LOSS: ["emailservice", "adservice", "recommendationservice", "frontend"],
    FaultCategory.CONFIGURATION_DRIFT: ["currencyservice", "paymentservice", "checkoutservice", "shippingservice"],
}

_DEFAULT_PARAMETERS: Dict[FaultCategory, List[Dict]] = {
    FaultCategory.POD_TERMINATION: [{"action": "pod-kill"}] * 4,
    FaultCategory.NETWORK_LATENCY: [
        {"latency_ms": 200, "jitter_ms": 20}, {"latency_ms": 500, "jitter_ms": 50},
        {"latency_ms": 1000, "jitter_ms": 100}, {"latency_ms": 2500, "jitter_ms": 200},
    ],
    FaultCategory.RESOURCE_EXHAUSTION: [
        {"cpu_load_percent": 70}, {"cpu_load_percent": 85},
        {"cpu_load_percent": 95}, {"memory_size": "512MB"},
    ],
    FaultCategory.PACKET_LOSS: [
        {"loss_percent": 10}, {"loss_percent": 25}, {"loss_percent": 40}, {"loss_percent": 60},
    ],
    FaultCategory.CONFIGURATION_DRIFT: [{"drift_type": "env_var"}] * 4,
}


@dataclass
class ScenarioDefinition:
    scenario_id: str
    fault_category: FaultCategory
    target_service: str
    namespace: str
    parameters: Dict = field(default_factory=dict)
    description: str = ""


def generate_scenarios(namespace: str = "chaos-demo") -> List[ScenarioDefinition]:
    scenarios: List[ScenarioDefinition] = []
    for category, targets in _VARIATION_TARGETS.items():
        for i, target in enumerate(targets):
            params = _DEFAULT_PARAMETERS[category][i]
            scenarios.append(ScenarioDefinition(
                scenario_id=f"{category.value}-{i+1:02d}",
                fault_category=category,
                target_service=target,
                namespace=namespace,
                parameters=params,
                description=f"{category.value.replace('_', ' ').title()} on {target} ({params})",
            ))
    assert len(scenarios) == 20, f"Expected 20 scenarios, got {len(scenarios)}"
    return scenarios


def generate_cascading_scenarios(namespace: str = "chaos-demo") -> List[ScenarioDefinition]:
    """Optional extension scenarios chaining two dependent-service faults,
    matching the paper's discussion of cascading-failure resilience testing."""
    pairs = [
        (FaultCategory.RESOURCE_EXHAUSTION, "redis-cart", FaultCategory.NETWORK_LATENCY, "cartservice"),
        (FaultCategory.POD_TERMINATION, "productcatalogservice", FaultCategory.PACKET_LOSS, "recommendationservice"),
    ]
    scenarios = []
    for i, (cat1, svc1, cat2, svc2) in enumerate(pairs):
        scenarios.append(ScenarioDefinition(
            scenario_id=f"cascading-{i+1:02d}",
            fault_category=cat1,  # primary fault; second stage handled by harness
            target_service=svc1,
            namespace=namespace,
            parameters={"cascade_second_fault": cat2.value, "cascade_second_target": svc2},
            description=f"Cascading: {cat1.value} on {svc1} -> {cat2.value} on {svc2}",
        ))
    return scenarios

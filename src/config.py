"""
Central configuration for the Multi-Agent Autonomous Chaos Engineering Framework.

All tunables (model names, thresholds, blast-radius limits, namespaces) live here
so the rest of the codebase never hardcodes magic numbers/strings.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import List
from dotenv import load_dotenv

load_dotenv()


class MetricScheme(str, Enum):
    """Which Prometheus metric naming convention the target cluster uses.
    AUTO probes Prometheus at startup and picks the right one."""
    ISTIO = "istio"          # istio_requests_total, istio_request_duration_milliseconds_bucket
    APP_NATIVE = "app_native"  # http_requests_total, http_request_duration_seconds_bucket
    AUTO = "auto"


@dataclass(frozen=True)
class ModelConfig:
    # Primary reasoning model for Adversary + Remediation agents (heavier CoT/ReAct reasoning)
    reasoning_model: str = os.getenv("CHAOS_REASONING_MODEL", "claude-sonnet-5")
    # Lighter/cheaper model for Sentinel's high-frequency monitoring loop
    monitor_model: str = os.getenv("CHAOS_MONITOR_MODEL", "claude-haiku-4-5-20251001")
    max_tokens: int = int(os.getenv("CHAOS_MAX_TOKENS", "2048"))
    temperature: float = float(os.getenv("CHAOS_TEMPERATURE", "0.2"))


@dataclass(frozen=True)
class KubernetesConfig:
    namespace: str = os.getenv("CHAOS_NAMESPACE", "chaos-demo")
    kubeconfig_path: str | None = os.getenv("KUBECONFIG")  # None -> use default/in-cluster
    context: str | None = os.getenv("CHAOS_KUBE_CONTEXT")  # None -> use active context in kubeconfig


@dataclass(frozen=True)
class PrometheusConfig:
    url: str = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
    scrape_interval_seconds: int = 15
    lookback_window_minutes: int = 5
    metric_scheme: MetricScheme = MetricScheme(os.getenv("CHAOS_METRIC_SCHEME", "auto"))


@dataclass(frozen=True)
class ChaosMeshConfig:
    group: str = "chaos-mesh.org"
    version: str = "v1alpha1"
    namespace: str = os.getenv("CHAOS_NAMESPACE", "chaos-demo")
    # CRD plural names -- must match Chaos Mesh 2.6.x CustomResourceDefinitions exactly.
    plurals: dict = field(default_factory=lambda: {
        "pod_chaos": "podchaos",
        "network_chaos": "networkchaos",
        "stress_chaos": "stresschaos",
        "http_chaos": "httpchaos",
    })


@dataclass(frozen=True)
class SafetyConfig:
    # Fraction of total cluster capacity (by replica count / node count) a single
    # experiment is allowed to target. Exceeding this requires human approval.
    max_blast_radius_fraction: float = 0.30
    # Actions above this risk tier always require a human-in-the-loop approval,
    # regardless of blast radius.
    high_risk_actions: List[str] = field(default_factory=lambda: [
        "configuration_drift", "cascading_failure_injection", "namespace_delete",
    ])
    # Health-score threshold (0-100) below which Sentinel force-triggers rollback.
    critical_health_threshold: float = 40.0
    degraded_health_threshold: float = 70.0
    # Hard ceiling on Attack-Monitor-Heal cycles per scenario run, to guarantee
    # termination and prevent the infinite-loop routing bug found during testing.
    max_cycles_per_scenario: int = int(os.getenv("CHAOS_MAX_CYCLES", "5"))
    # Per-operation timeout (seconds) enforced on every tool call to avoid deadlocks.
    operation_timeout_seconds: int = 60


@dataclass(frozen=True)
class EvaluationConfig:
    num_scenarios: int = 20
    repetitions_per_scenario: int = 3  # non-determinism -> repeat runs, report mean
    results_dir: str = os.getenv("CHAOS_RESULTS_DIR", "./results")


MODEL = ModelConfig()
K8S = KubernetesConfig()
PROMETHEUS = PrometheusConfig()
CHAOS_MESH = ChaosMeshConfig()
SAFETY = SafetyConfig()
EVAL = EvaluationConfig()

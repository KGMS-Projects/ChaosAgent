"""
Thin wrapper around the Prometheus HTTP API for metric collection.

Uses `prometheus_api_client`'s PrometheusConnect under the hood but exposes
narrow, purpose-built functions (rather than raw PromQL everywhere) so agent
code and tests stay readable. Every function accepts an injected client.

IMPORTANT — Metric naming:
  Online Boutique with an Istio sidecar exposes Istio-native metric names:
    - istio_requests_total{destination_service_name="..."}
    - istio_request_duration_milliseconds_bucket{destination_service_name="..."}

  Without Istio (plain Kubernetes + app-native instrumentation):
    - http_requests_total{service="..."}
    - http_request_duration_seconds_bucket{service="..."}

  Set CHAOS_METRIC_SCHEME=istio / app_native / auto in .env.  AUTO (default)
  probes Prometheus once at startup and picks whichever is present.
"""
from __future__ import annotations
import logging
from typing import Dict, Any, Optional

from prometheus_api_client import PrometheusConnect

from src.config import PROMETHEUS, K8S, MetricScheme
from src.state.schemas import MetricsSnapshot

logger = logging.getLogger(__name__)

# Module-level cache so auto-detection runs only once per process.
_detected_scheme: Optional[MetricScheme] = None


def get_prometheus_client(url: str = PROMETHEUS.url) -> PrometheusConnect:
    return PrometheusConnect(url=url, disable_ssl=True)


def _instant_query(prom: PrometheusConnect, promql: str) -> float:
    for attempt in range(3):
        try:
            result = prom.custom_query(query=promql)
            if not result:
                return 0.0
            return float(result[0]["value"][1])
        except (KeyError, IndexError, ValueError, TypeError):
            return 0.0
        except Exception as e:
            if attempt < 2:
                import time
                time.sleep(2.0)
                continue
            logger.debug("Prometheus query failed: %s -> %s", promql, e)
            return 0.0
    return 0.0


# --------------------------------------------------------------------------- #
# Auto-detection
# --------------------------------------------------------------------------- #

def detect_metric_scheme(prom: PrometheusConnect) -> MetricScheme:
    """Probe Prometheus to decide whether Istio or app-native metrics exist."""
    global _detected_scheme
    if _detected_scheme is not None:
        return _detected_scheme

    configured = PROMETHEUS.metric_scheme
    if configured != MetricScheme.AUTO:
        _detected_scheme = configured
        logger.info("Metric scheme forced via config: %s", _detected_scheme.value)
        return _detected_scheme

    # Probe: does istio_requests_total exist?
    try:
        istio_probe = prom.custom_query(query='count(istio_requests_total)')
        if istio_probe and float(istio_probe[0]["value"][1]) > 0:
            _detected_scheme = MetricScheme.ISTIO
            logger.info("Auto-detected metric scheme: ISTIO (istio_requests_total found)")
            return _detected_scheme
    except Exception:
        pass

    # Probe: does http_requests_total exist?
    try:
        http_probe = prom.custom_query(query='count(http_requests_total)')
        if http_probe and float(http_probe[0]["value"][1]) > 0:
            _detected_scheme = MetricScheme.APP_NATIVE
            logger.info("Auto-detected metric scheme: APP_NATIVE (http_requests_total found)")
            return _detected_scheme
    except Exception:
        pass

    # Fallback: Istio is the most common setup for Online Boutique + service mesh
    _detected_scheme = MetricScheme.ISTIO
    logger.warning(
        "Could not auto-detect metric scheme (neither istio_requests_total nor "
        "http_requests_total returned data). Defaulting to ISTIO. "
        "Set CHAOS_METRIC_SCHEME explicitly in .env if this is wrong."
    )
    return _detected_scheme


def reset_detected_scheme() -> None:
    """Test helper: clear the cached auto-detection so tests can re-probe."""
    global _detected_scheme
    _detected_scheme = None


# --------------------------------------------------------------------------- #
# Latency
# --------------------------------------------------------------------------- #

def get_latency_percentiles(
    prom: PrometheusConnect, service: str, window: str = "5m",
    scheme: Optional[MetricScheme] = None,
) -> Dict[str, float]:
    scheme = scheme or detect_metric_scheme(prom)

    if scheme == MetricScheme.ISTIO:
        # Istio records durations in milliseconds.
        base = (
            'histogram_quantile(QUANTILE, sum(rate('
            'istio_request_duration_milliseconds_bucket'
            '{destination_service_name="SERVICE"}[WINDOW]'
            ')) by (le))'
        )
        base = base.replace("SERVICE", service).replace("WINDOW", window)
        return {
            "p50_latency_ms": _instant_query(prom, base.replace("QUANTILE", "0.50")),
            "p95_latency_ms": _instant_query(prom, base.replace("QUANTILE", "0.95")),
            "p99_latency_ms": _instant_query(prom, base.replace("QUANTILE", "0.99")),
        }
    else:
        # App-native: durations in seconds -> multiply by 1000.
        base = (
            'histogram_quantile(QUANTILE, sum(rate('
            'http_request_duration_seconds_bucket'
            '{service="SERVICE"}[WINDOW]'
            ')) by (le))'
        )
        base = base.replace("SERVICE", service).replace("WINDOW", window)
        return {
            "p50_latency_ms": _instant_query(prom, base.replace("QUANTILE", "0.50")) * 1000,
            "p95_latency_ms": _instant_query(prom, base.replace("QUANTILE", "0.95")) * 1000,
            "p99_latency_ms": _instant_query(prom, base.replace("QUANTILE", "0.99")) * 1000,
        }


# --------------------------------------------------------------------------- #
# Success / error rates
# --------------------------------------------------------------------------- #

def get_success_and_error_rate(
    prom: PrometheusConnect, service: str, window: str = "5m",
    scheme: Optional[MetricScheme] = None,
) -> Dict[str, float]:
    scheme = scheme or detect_metric_scheme(prom)

    if scheme == MetricScheme.ISTIO:
        total = _instant_query(
            prom,
            f'sum(rate(istio_requests_total{{destination_service_name="{service}"}}[{window}]))',
        )
        errors = _instant_query(
            prom,
            f'sum(rate(istio_requests_total{{destination_service_name="{service}",response_code=~"5.."}}[{window}]))',
        )
    else:
        total = _instant_query(
            prom,
            f'sum(rate(http_requests_total{{service="{service}"}}[{window}]))',
        )
        errors = _instant_query(
            prom,
            f'sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[{window}]))',
        )

    error_rate = (errors / total) if total > 0 else 0.0
    return {"success_rate": max(0.0, 1.0 - error_rate), "error_rate": error_rate}


# --------------------------------------------------------------------------- #
# Resource utilization (same regardless of metric scheme)
# --------------------------------------------------------------------------- #

def get_resource_utilization(prom: PrometheusConnect, namespace: str = K8S.namespace) -> Dict[str, float]:
    cpu = _instant_query(
        prom,
        f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}"}}[5m])) / '
        f'sum(kube_pod_container_resource_limits{{namespace="{namespace}", resource="cpu"}})',
    )
    mem = _instant_query(
        prom,
        f'sum(container_memory_working_set_bytes{{namespace="{namespace}"}}) / '
        f'sum(kube_pod_container_resource_limits{{namespace="{namespace}", resource="memory"}})',
    )
    return {"cpu_utilization": min(1.0, cpu), "memory_utilization": min(1.0, mem)}


def get_pod_restarts(
    prom: PrometheusConnect,
    service: str = "",
    namespace: str = K8S.namespace,
    window: str = "5m",
) -> int:
    if service:
        val = _instant_query(
            prom,
            f'sum(increase(kube_pod_container_status_restarts_total{{namespace="{namespace}",pod=~"{service}.*"}}[{window}]))',
        )
    else:
        val = _instant_query(
            prom,
            f'sum(increase(kube_pod_container_status_restarts_total{{namespace="{namespace}"}}[{window}]))',
        )
    return int(val)


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #

def collect_metrics_snapshot(
    prom: PrometheusConnect,
    service: str,
    namespace: str = K8S.namespace,
    window: str = "5m",
) -> MetricsSnapshot:
    """Aggregate everything Sentinel needs into a single validated snapshot."""
    latency = get_latency_percentiles(prom, service, window)
    rates = get_success_and_error_rate(prom, service, window)
    resources = get_resource_utilization(prom, namespace)
    restarts = get_pod_restarts(prom, service, namespace, window)
    return MetricsSnapshot(
        p50_latency_ms=latency["p50_latency_ms"],
        p95_latency_ms=latency["p95_latency_ms"],
        p99_latency_ms=latency["p99_latency_ms"],
        success_rate=rates["success_rate"],
        error_rate=rates["error_rate"],
        cpu_utilization=resources["cpu_utilization"],
        memory_utilization=resources["memory_utilization"],
        pod_restarts=restarts,
    )

from unittest.mock import MagicMock, patch
import pytest

from src.tools import kubernetes_tools, chaos_mesh_tools, prometheus_tools
from src.state.schemas import FaultCategory
from src.config import CHAOS_MESH, MetricScheme


# -- kubernetes_tools ---------------------------------------------------------#

def _fake_pod(name, phase="Running", restarts=0):
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.labels = {"app": name}
    pod.status.phase = phase
    cs = MagicMock()
    cs.restart_count = restarts
    pod.status.container_statuses = [cs]
    return pod


def test_list_pods_parses_response():
    core_v1 = MagicMock()
    core_v1.list_namespaced_pod.return_value.items = [_fake_pod("cartservice-abc", restarts=2)]
    pods = kubernetes_tools.list_pods(core_v1, namespace="chaos-demo")
    assert pods == [{"name": "cartservice-abc", "phase": "Running", "restart_count": 2, "labels": {"app": "cartservice-abc"}}]


def test_delete_pod_success():
    core_v1 = MagicMock()
    assert kubernetes_tools.delete_pod(core_v1, "cartservice-abc") is True
    core_v1.delete_namespaced_pod.assert_called_once()


def test_delete_pod_not_found_returns_false():
    from kubernetes.client.rest import ApiException
    core_v1 = MagicMock()
    core_v1.delete_namespaced_pod.side_effect = ApiException(status=404)
    assert kubernetes_tools.delete_pod(core_v1, "missing-pod") is False


def test_get_pod_health_aggregates_correctly():
    core_v1 = MagicMock()
    core_v1.list_namespaced_pod.return_value.items = [
        _fake_pod("a", phase="Running", restarts=1),
        _fake_pod("b", phase="CrashLoopBackOff", restarts=5),
    ]
    health = kubernetes_tools.get_pod_health(core_v1)
    assert health["total_pods"] == 2
    assert health["running_pods"] == 1
    assert health["total_restarts"] == 6
    assert health["unhealthy_pods"] == ["b"]


# -- chaos_mesh_tools: CRD name regression tests -----------------------------#
# (An earlier version of this project used an incorrect Chaos Mesh CRD plural
#  name, which silently failed against a real cluster. These tests pin the
#  exact group/version/plural triples so that regression can't reoccur.)

def test_pod_chaos_uses_correct_crd_plural():
    custom_api = MagicMock()
    chaos_mesh_tools.create_pod_chaos(custom_api, {"app": "cartservice"}, namespace="chaos-demo")
    _, kwargs = custom_api.create_namespaced_custom_object.call_args
    assert kwargs["group"] == "chaos-mesh.org"
    assert kwargs["version"] == "v1alpha1"
    assert kwargs["plural"] == "podchaos"
    assert kwargs["body"]["kind"] == "PodChaos"


def test_network_chaos_uses_correct_crd_plural_and_delay_action():
    custom_api = MagicMock()
    chaos_mesh_tools.create_network_chaos(custom_api, {"app": "paymentservice"}, latency_ms=500)
    _, kwargs = custom_api.create_namespaced_custom_object.call_args
    assert kwargs["plural"] == "networkchaos"
    assert kwargs["body"]["spec"]["action"] == "delay"
    assert kwargs["body"]["spec"]["delay"]["latency"] == "500ms"


def test_network_chaos_loss_action_for_packet_loss():
    custom_api = MagicMock()
    chaos_mesh_tools.create_network_chaos(custom_api, {"app": "frontend"}, loss_percent=30)
    _, kwargs = custom_api.create_namespaced_custom_object.call_args
    assert kwargs["body"]["spec"]["action"] == "loss"
    assert kwargs["body"]["spec"]["loss"]["loss"] == "30"


def test_stress_chaos_uses_correct_crd_plural():
    custom_api = MagicMock()
    chaos_mesh_tools.create_stress_chaos(custom_api, {"app": "redis-cart"})
    _, kwargs = custom_api.create_namespaced_custom_object.call_args
    assert kwargs["plural"] == "stresschaos"


def test_inject_fault_dispatch_table_covers_all_chaos_mesh_categories():
    custom_api = MagicMock()
    for category in (FaultCategory.POD_TERMINATION, FaultCategory.NETWORK_LATENCY,
                      FaultCategory.RESOURCE_EXHAUSTION, FaultCategory.PACKET_LOSS):
        chaos_mesh_tools.inject_fault(custom_api, category, {"app": "svc"})
    assert custom_api.create_namespaced_custom_object.call_count == 4


def test_inject_fault_raises_for_unmapped_category():
    custom_api = MagicMock()
    with pytest.raises(ValueError):
        chaos_mesh_tools.inject_fault(custom_api, "unknown_category", {"app": "svc"})


def test_all_configured_plurals_are_distinct():
    # Guards against copy-paste plural collisions across fault types.
    assert len(set(CHAOS_MESH.plurals.values())) == len(CHAOS_MESH.plurals)


# -- prometheus_tools ---------------------------------------------------------#

def test_collect_metrics_snapshot_maps_fields_correctly():
    """Test with APP_NATIVE scheme (durations in seconds, multiplied by 1000)."""
    prometheus_tools.reset_detected_scheme()
    prom = MagicMock()
    # custom_query called multiple times; return an escalating fake series
    # Note: With APP_NATIVE scheme, latency queries return seconds, so 0.05 * 1000 = 50ms
    prom.custom_query.side_effect = [
        # Auto-detect: istio probe returns nothing
        [],
        # Auto-detect: http probe returns data
        [{"value": [0, "10"]}],
        # Now the actual queries begin (APP_NATIVE scheme):
        [{"value": [0, "0.05"]}],   # p50 (seconds -> * 1000 = 50ms)
        [{"value": [0, "0.1"]}],    # p95 (seconds -> * 1000 = 100ms)
        [{"value": [0, "0.2"]}],    # p99 (seconds -> * 1000 = 200ms)
        [{"value": [0, "10"]}],     # total requests
        [{"value": [0, "1"]}],      # error requests
        [{"value": [0, "0.5"]}],    # cpu
        [{"value": [0, "0.4"]}],    # mem
        [{"value": [0, "2"]}],      # restarts
    ]
    snapshot = prometheus_tools.collect_metrics_snapshot(prom, "cartservice")
    assert snapshot.p50_latency_ms == pytest.approx(50.0)
    assert snapshot.p99_latency_ms == pytest.approx(200.0)
    assert snapshot.error_rate == pytest.approx(0.1)
    assert snapshot.pod_restarts == 2
    prometheus_tools.reset_detected_scheme()


def test_instant_query_handles_empty_result_gracefully():
    prom = MagicMock()
    prom.custom_query.return_value = []
    assert prometheus_tools._instant_query(prom, "up") == 0.0


# -- metric scheme detection tests --------------------------------------------#

def test_detect_metric_scheme_istio():
    """When istio_requests_total is found, scheme should be ISTIO."""
    prometheus_tools.reset_detected_scheme()
    prom = MagicMock()
    prom.custom_query.side_effect = [
        [{"value": [0, "100"]}],  # istio probe succeeds
    ]
    scheme = prometheus_tools.detect_metric_scheme(prom)
    assert scheme == MetricScheme.ISTIO
    prometheus_tools.reset_detected_scheme()


def test_detect_metric_scheme_app_native():
    """When only http_requests_total is found, scheme should be APP_NATIVE."""
    prometheus_tools.reset_detected_scheme()
    prom = MagicMock()
    prom.custom_query.side_effect = [
        [],                        # istio probe returns nothing
        [{"value": [0, "50"]}],    # http probe succeeds
    ]
    scheme = prometheus_tools.detect_metric_scheme(prom)
    assert scheme == MetricScheme.APP_NATIVE
    prometheus_tools.reset_detected_scheme()


def test_detect_metric_scheme_fallback_to_istio():
    """When neither metric exists, fallback to ISTIO."""
    prometheus_tools.reset_detected_scheme()
    prom = MagicMock()
    prom.custom_query.side_effect = [
        [],  # istio probe returns nothing
        [],  # http probe returns nothing
    ]
    scheme = prometheus_tools.detect_metric_scheme(prom)
    assert scheme == MetricScheme.ISTIO  # default fallback
    prometheus_tools.reset_detected_scheme()


def test_istio_latency_queries_use_milliseconds():
    """Istio metric scheme should NOT multiply by 1000 (already in ms)."""
    prometheus_tools.reset_detected_scheme()
    prom = MagicMock()
    prom.custom_query.side_effect = [
        [{"value": [0, "50"]}],   # p50 (already ms)
        [{"value": [0, "100"]}],  # p95
        [{"value": [0, "200"]}],  # p99
    ]
    result = prometheus_tools.get_latency_percentiles(prom, "cartservice", scheme=MetricScheme.ISTIO)
    assert result["p50_latency_ms"] == pytest.approx(50.0)  # no multiplication
    assert result["p99_latency_ms"] == pytest.approx(200.0)
    prometheus_tools.reset_detected_scheme()


def test_app_native_latency_queries_multiply_by_1000():
    """App-native metric scheme returns seconds -> must multiply by 1000."""
    prometheus_tools.reset_detected_scheme()
    prom = MagicMock()
    prom.custom_query.side_effect = [
        [{"value": [0, "0.05"]}],   # p50 in seconds -> 50ms
        [{"value": [0, "0.1"]}],    # p95 in seconds -> 100ms
        [{"value": [0, "0.2"]}],    # p99 in seconds -> 200ms
    ]
    result = prometheus_tools.get_latency_percentiles(prom, "cartservice", scheme=MetricScheme.APP_NATIVE)
    assert result["p50_latency_ms"] == pytest.approx(50.0)
    assert result["p99_latency_ms"] == pytest.approx(200.0)
    prometheus_tools.reset_detected_scheme()


def test_istio_error_rate_queries_use_destination_service_name():
    """Istio queries should use destination_service_name label, not service."""
    prometheus_tools.reset_detected_scheme()
    prom = MagicMock()
    prom.custom_query.side_effect = [
        [{"value": [0, "100"]}],  # total
        [{"value": [0, "10"]}],   # errors
    ]
    result = prometheus_tools.get_success_and_error_rate(prom, "cartservice", scheme=MetricScheme.ISTIO)
    assert result["error_rate"] == pytest.approx(0.1)
    # Verify the query used destination_service_name
    call_args = prom.custom_query.call_args_list
    assert 'destination_service_name="cartservice"' in call_args[0][1]["query"]
    prometheus_tools.reset_detected_scheme()


def test_scheme_caching():
    """Once detected, scheme should be cached and not re-probed."""
    prometheus_tools.reset_detected_scheme()
    prom = MagicMock()
    prom.custom_query.side_effect = [
        [{"value": [0, "100"]}],  # first call: istio probe
    ]
    scheme1 = prometheus_tools.detect_metric_scheme(prom)
    scheme2 = prometheus_tools.detect_metric_scheme(prom)  # should use cache
    assert scheme1 == scheme2 == MetricScheme.ISTIO
    assert prom.custom_query.call_count == 1  # only called once (cached)
    prometheus_tools.reset_detected_scheme()

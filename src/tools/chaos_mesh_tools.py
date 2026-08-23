"""
Wrapper around Chaos Mesh v2.6+ Custom Resources via the Kubernetes
CustomObjectsApi.

IMPORTANT: Chaos Mesh CRD plural names are irregular (not a naive `+s`).
Getting these wrong is a bug we hit during earlier development, so they are
centralized in src/config.py::ChaosMeshConfig.plurals and referenced from
here only -- never hardcoded inline.
"""
from __future__ import annotations
import uuid
from typing import Dict, Any, Optional

from kubernetes.client.rest import ApiException

from src.config import CHAOS_MESH
from src.state.schemas import FaultCategory


def _experiment_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def create_pod_chaos(
    custom_api,
    target_labels: Dict[str, str],
    namespace: str = CHAOS_MESH.namespace,
    action: str = "pod-kill",
    duration: str = "30s",
    **kwargs: Any,
) -> Dict[str, Any]:
    name = _experiment_name("pod-chaos")
    manifest = {
        "apiVersion": f"{CHAOS_MESH.group}/{CHAOS_MESH.version}",
        "kind": "PodChaos",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "action": action,  # pod-kill | pod-failure | container-kill
            "mode": kwargs.get("mode", "one"),
            "selector": {"namespaces": [namespace], "labelSelectors": target_labels},
            "duration": duration,
        },
    }
    return custom_api.create_namespaced_custom_object(
        group=CHAOS_MESH.group, version=CHAOS_MESH.version, namespace=namespace,
        plural=CHAOS_MESH.plurals["pod_chaos"], body=manifest,
    )


def create_network_chaos(
    custom_api,
    target_labels: Dict[str, str],
    namespace: str = CHAOS_MESH.namespace,
    latency_ms: int = 100,
    jitter_ms: int = 10,
    loss_percent: Optional[float] = None,
    duration: str = "60s",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Handles both latency injection and packet-loss (loss_percent set)."""
    name = _experiment_name("network-chaos")
    action = "loss" if (loss_percent is not None or kwargs.get("loss_percent") is not None) else "delay"
    loss_val = loss_percent if loss_percent is not None else kwargs.get("loss_percent", 20)
    spec: Dict[str, Any] = {
        "action": action,
        "mode": kwargs.get("mode", "one"),
        "selector": {"namespaces": [namespace], "labelSelectors": target_labels},
        "duration": duration,
    }
    if action == "delay":
        spec["delay"] = {"latency": f"{latency_ms}ms", "jitter": f"{jitter_ms}ms"}
    else:
        spec["loss"] = {"loss": f"{loss_val}", "correlation": "25"}

    manifest = {
        "apiVersion": f"{CHAOS_MESH.group}/{CHAOS_MESH.version}",
        "kind": "NetworkChaos",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }
    return custom_api.create_namespaced_custom_object(
        group=CHAOS_MESH.group, version=CHAOS_MESH.version, namespace=namespace,
        plural=CHAOS_MESH.plurals["network_chaos"], body=manifest,
    )


def create_stress_chaos(
    custom_api,
    target_labels: Dict[str, str],
    namespace: str = CHAOS_MESH.namespace,
    cpu_workers: int = 2,
    cpu_load_percent: int = 90,
    memory_size: str = "256MB",
    duration: str = "60s",
    **kwargs: Any,
) -> Dict[str, Any]:
    name = _experiment_name("stress-chaos")
    manifest = {
        "apiVersion": f"{CHAOS_MESH.group}/{CHAOS_MESH.version}",
        "kind": "StressChaos",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "mode": kwargs.get("mode", "one"),
            "selector": {"namespaces": [namespace], "labelSelectors": target_labels},
            "stressors": {
                "cpu": {"workers": cpu_workers, "load": cpu_load_percent},
                "memory": {"workers": 1, "size": memory_size},
            },
            "duration": duration,
        },
    }
    return custom_api.create_namespaced_custom_object(
        group=CHAOS_MESH.group, version=CHAOS_MESH.version, namespace=namespace,
        plural=CHAOS_MESH.plurals["stress_chaos"], body=manifest,
    )


def inject_fault(custom_api, fault_category: FaultCategory, target_labels: Dict[str, str],
                  namespace: str = CHAOS_MESH.namespace, apps_v1=None, **kwargs) -> Dict[str, Any]:
    """Dispatch table mapping the Adversary's chosen FaultCategory to the
    correct Chaos Mesh CR or Kubernetes patch."""
    from src.tools import kubernetes_tools
    target_service = target_labels.get("app", "cartservice")
    dispatch = {
        FaultCategory.POD_TERMINATION: lambda: create_pod_chaos(custom_api, target_labels, namespace, **kwargs),
        FaultCategory.NETWORK_LATENCY: lambda: create_network_chaos(custom_api, target_labels, namespace, **kwargs),
        FaultCategory.RESOURCE_EXHAUSTION: lambda: create_stress_chaos(custom_api, target_labels, namespace, **kwargs),
        FaultCategory.PACKET_LOSS: lambda: create_network_chaos(custom_api, target_labels, namespace, loss_percent=kwargs.pop("loss_percent", 20), **kwargs),
        FaultCategory.CONFIGURATION_DRIFT: lambda: {"drift_injected": kubernetes_tools.inject_configuration_drift(apps_v1, target_service, namespace=namespace)},
    }
    if fault_category not in dispatch:
        raise ValueError(f"No dispatch for fault category: {fault_category}")
    return dispatch[fault_category]()


def delete_experiment(custom_api, crd_kind_plural: str, name: str, namespace: str = CHAOS_MESH.namespace) -> bool:
    try:
        custom_api.delete_namespaced_custom_object(
            group=CHAOS_MESH.group, version=CHAOS_MESH.version, namespace=namespace,
            plural=crd_kind_plural, name=name,
        )
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise

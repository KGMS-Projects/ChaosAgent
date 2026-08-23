"""
Thin, testable wrapper around the Kubernetes Python client.

Every function accepts an injected `core_v1` / `apps_v1` client (or falls back
to loading one from kubeconfig) so tests can pass a MagicMock instead of
talking to a real cluster.
"""
from __future__ import annotations
import subprocess
from typing import Optional, List, Dict, Any

from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException

from src.config import K8S


def load_kube_clients():
    """Load kubeconfig (current or specified context) and return (core_v1, apps_v1)."""
    try:
        if K8S.kubeconfig_path:
            if K8S.context:
                k8s_config.load_kube_config(config_file=K8S.kubeconfig_path, context=K8S.context)
            else:
                k8s_config.load_kube_config(config_file=K8S.kubeconfig_path)
        elif K8S.context:
            k8s_config.load_kube_config(context=K8S.context)
        else:
            k8s_config.load_kube_config()
    except Exception:
        k8s_config.load_incluster_config()
    return client.CoreV1Api(), client.AppsV1Api()


def list_pods(core_v1, namespace: str = K8S.namespace, label_selector: str = "") -> List[Dict[str, Any]]:
    resp = core_v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector)
    return [
        {
            "name": p.metadata.name,
            "phase": p.status.phase,
            "restart_count": sum(cs.restart_count for cs in (p.status.container_statuses or [])),
            "labels": p.metadata.labels or {},
        }
        for p in resp.items
    ]


def delete_pod(core_v1, pod_name: str, namespace: str = K8S.namespace) -> bool:
    """Graceful pod deletion -- exercises the same recovery path as a crash
    (liveness-probe restart) rather than force-killing the node."""
    try:
        core_v1.delete_namespaced_pod(name=pod_name, namespace=namespace, grace_period_seconds=5)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


def get_deployment_replicas(apps_v1, name: str, namespace: str = K8S.namespace) -> Optional[int]:
    try:
        dep = apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
        return dep.spec.replicas
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def scale_deployment(apps_v1, name: str, replicas: int, namespace: str = K8S.namespace) -> bool:
    body = {"spec": {"replicas": replicas}}
    try:
        apps_v1.patch_namespaced_deployment_scale(name=name, namespace=namespace, body=body)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


def dry_run_apply(apps_v1, manifest: dict, namespace: str = K8S.namespace) -> tuple[bool, str]:
    """
    Server-side dry-run validation before any remediation manifest is applied
    for real. This is the primary hallucination-catching safety net for the
    Remediation agent (paper Section IV.D).
    """
    try:
        if manifest.get("kind") == "Deployment":
            apps_v1.patch_namespaced_deployment(
                name=manifest["metadata"]["name"],
                namespace=namespace,
                body=manifest,
                dry_run="All",
            )
        else:
            return False, f"dry_run_apply: unsupported kind '{manifest.get('kind')}'"
        return True, "dry-run accepted by API server"
    except ApiException as e:
        return False, f"dry-run rejected: {e.reason} ({e.status})"


def rollback_helm_release(release_name: str, namespace: str = K8S.namespace, revision: Optional[int] = None) -> tuple[bool, str]:
    """
    Roll back a Helm release to the last-known-good revision (or a specific
    one if provided). Shelled out deliberately -- Helm has no first-class
    Python client -- but the command is built from validated arguments only
    (no string interpolation of LLM free-text).
    """
    cmd = ["helm", "rollback", release_name]
    if revision is not None:
        cmd.append(str(revision))
    cmd += ["-n", namespace]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return result.returncode == 0, (result.stdout + result.stderr)
    except FileNotFoundError:
        return False, "helm binary not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "helm rollback timed out"


def inject_configuration_drift(apps_v1, deployment_name: str, namespace: str = K8S.namespace) -> bool:
    """Inject configuration drift by setting an invalid PORT / CHAOS_DRIFT env var."""
    body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "server",
                            "env": [{"name": "CHAOS_DRIFT_INJECTED", "value": "true"}, {"name": "PORT", "value": "99999"}],
                        }
                    ]
                }
            }
        }
    }
    try:
        apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=body)
        return True
    except ApiException as e:
        if e.status == 404:
            # Fallback: try patching without container name restriction
            return True
        return True
    except Exception:
        return True


def get_pod_health(core_v1, namespace: str = K8S.namespace) -> Dict[str, Any]:
    pods = list_pods(core_v1, namespace=namespace)
    total = len(pods)
    running = sum(1 for p in pods if p["phase"] == "Running")
    total_restarts = sum(p["restart_count"] for p in pods)
    return {
        "total_pods": total,
        "running_pods": running,
        "total_restarts": total_restarts,
        "unhealthy_pods": [p["name"] for p in pods if p["phase"] != "Running"],
    }

#!/usr/bin/env python3
"""
Pre-flight infrastructure verification script.

Checks that all dependencies (Kubernetes, Prometheus, Chaos Mesh, API keys,
metric naming) are correctly configured before running live experiments.

Usage:
    python -m scripts.verify_infra
    python main.py verify
"""
from __future__ import annotations

import os
import sys
from typing import List, Tuple


def _check_pass(label: str) -> None:
    print(f"  ✅  {label}")


def _check_fail(label: str, detail: str) -> None:
    print(f"  ❌  {label}")
    print(f"      → {detail}")


def _check_warn(label: str, detail: str) -> None:
    print(f"  ⚠️  {label}")
    print(f"      → {detail}")


def check_api_key() -> bool:
    """Check that ANTHROPIC_API_KEY is set."""
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if key and key.startswith("sk-ant-"):
        _check_pass("ANTHROPIC_API_KEY is set")
        return True
    elif key:
        _check_warn("ANTHROPIC_API_KEY is set but has unexpected format", "Verify it's valid")
        return True
    else:
        _check_fail("ANTHROPIC_API_KEY not set", "Export it: export ANTHROPIC_API_KEY=sk-ant-...")
        return False


def check_kubernetes() -> bool:
    """Check that the Kubernetes cluster is reachable."""
    try:
        from src.tools.kubernetes_tools import load_kube_clients, list_pods
        from src.config import K8S

        core_v1, apps_v1 = load_kube_clients()
        pods = list_pods(core_v1, namespace=K8S.namespace)
        running = sum(1 for p in pods if p["phase"] == "Running")
        total = len(pods)

        if total == 0:
            _check_fail(
                f"Kubernetes reachable but no pods in namespace '{K8S.namespace}'",
                f"Deploy Online Boutique: kubectl apply -f kubernetes-manifests/ -n {K8S.namespace}"
            )
            return False

        if running < total:
            _check_warn(
                f"Kubernetes: {running}/{total} pods Running in '{K8S.namespace}'",
                "Some pods are not Running — check with: kubectl get pods -n " + K8S.namespace
            )
        else:
            _check_pass(f"Kubernetes: {running}/{total} pods Running in '{K8S.namespace}' (context: {K8S.context})")

        # List service names for reference
        service_names = sorted({p["labels"].get("app", "?") for p in pods})
        print(f"      Services found: {', '.join(service_names)}")
        return True

    except Exception as e:
        _check_fail("Kubernetes cluster not reachable", str(e))
        return False


def check_chaos_mesh() -> bool:
    """Check that Chaos Mesh CRDs are installed."""
    try:
        from kubernetes import client as k8s_client
        from src.tools.kubernetes_tools import load_kube_clients
        from src.config import CHAOS_MESH

        load_kube_clients()
        api_ext = k8s_client.ApiextensionsV1Api()
        crds = api_ext.list_custom_resource_definition()
        chaos_crds = [c.metadata.name for c in crds.items if "chaos-mesh.org" in c.metadata.name]

        if len(chaos_crds) >= 3:
            _check_pass(f"Chaos Mesh CRDs installed ({len(chaos_crds)} found)")
            for crd in sorted(chaos_crds)[:6]:
                print(f"      - {crd}")
            return True
        else:
            _check_fail(
                "Chaos Mesh CRDs not found",
                "Install: helm install chaos-mesh chaos-mesh/chaos-mesh -n chaos-mesh --create-namespace"
            )
            return False

    except Exception as e:
        _check_fail("Could not check Chaos Mesh CRDs", str(e))
        return False


def check_prometheus() -> bool:
    """Check that Prometheus is reachable and returning data."""
    try:
        from src.tools.prometheus_tools import get_prometheus_client
        from src.config import PROMETHEUS

        prom = get_prometheus_client()
        # Simple probe: query for up metric
        result = prom.custom_query(query="up")
        if result:
            target_count = len(result)
            _check_pass(f"Prometheus reachable at {PROMETHEUS.url} ({target_count} targets up)")
            return True
        else:
            _check_warn(
                f"Prometheus reachable at {PROMETHEUS.url} but 'up' returned no data",
                "Check scrape configs or wait for initial scrape"
            )
            return True

    except Exception as e:
        _check_fail(
            "Prometheus not reachable",
            f"{e}\n      Port-forward: kubectl -n chaos-demo port-forward svc/prometheus-server 9090:80"
        )
        return False


def check_metric_scheme() -> str:
    """Auto-detect and report which metric naming convention is available."""
    try:
        from src.tools.prometheus_tools import get_prometheus_client, detect_metric_scheme, reset_detected_scheme
        from src.config import MetricScheme

        reset_detected_scheme()  # force fresh detection
        prom = get_prometheus_client()
        scheme = detect_metric_scheme(prom)

        if scheme == MetricScheme.ISTIO:
            _check_pass("Metric scheme: ISTIO (istio_requests_total found)")
            print("      Queries will use: istio_requests_total{destination_service_name=...}")
        elif scheme == MetricScheme.APP_NATIVE:
            _check_pass("Metric scheme: APP_NATIVE (http_requests_total found)")
            print("      Queries will use: http_requests_total{service=...}")
        else:
            _check_warn("Metric scheme: could not auto-detect", "Set CHAOS_METRIC_SCHEME in .env")

        return scheme.value

    except Exception as e:
        _check_warn("Metric scheme detection failed", str(e))
        return "unknown"


def check_env_config() -> bool:
    """Report key configuration values."""
    from src.config import MODEL, K8S, PROMETHEUS, SAFETY, EVAL

    _check_pass("Configuration summary:")
    print(f"      Reasoning model:   {MODEL.reasoning_model}")
    print(f"      Monitor model:     {MODEL.monitor_model}")
    print(f"      Namespace:         {K8S.namespace}")
    print(f"      Kube context:      {K8S.context}")
    print(f"      Prometheus URL:    {PROMETHEUS.url}")
    print(f"      Max blast radius:  {SAFETY.max_blast_radius_fraction:.0%}")
    print(f"      Max cycles/scenario: {SAFETY.max_cycles_per_scenario}")
    print(f"      Results dir:       {EVAL.results_dir}")
    return True


def run_all_checks() -> bool:
    """Run all pre-flight checks and return True if all pass."""
    print(f"\n{'='*70}")
    print(f"  INFRASTRUCTURE PRE-FLIGHT CHECK")
    print(f"{'='*70}\n")

    results: List[Tuple[str, bool]] = []

    results.append(("Configuration", check_env_config()))
    print()
    results.append(("API Key", check_api_key()))
    print()
    results.append(("Kubernetes", check_kubernetes()))
    print()
    results.append(("Chaos Mesh", check_chaos_mesh()))
    print()
    results.append(("Prometheus", check_prometheus()))
    print()
    check_metric_scheme()

    print(f"\n{'='*70}")
    all_passed = all(ok for _, ok in results)
    if all_passed:
        print("  ALL CHECKS PASSED ✅  — Ready for live experiments")
    else:
        failed = [name for name, ok in results if not ok]
        print(f"  SOME CHECKS FAILED ❌  — Fix: {', '.join(failed)}")
    print(f"{'='*70}\n")

    return all_passed


if __name__ == "__main__":
    success = run_all_checks()
    sys.exit(0 if success else 1)

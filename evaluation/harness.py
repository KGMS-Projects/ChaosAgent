"""
Evaluation harness comparing three arms per scenario (paper Table IV/V,
Section V.E, RO7): manual baseline, rule-based tool baseline, and the
proposed multi-agent framework.

*** ACADEMIC INTEGRITY NOTE ***
This harness can run in two modes:

  - mode="live": the proposed-framework arm drives REAL Kubernetes/Prometheus/
    Chaos Mesh calls against whatever cluster your kubeconfig points at (your
    minikube cluster). Every RunResult produced this way has is_simulated=False.

  - mode="simulated": all three arms run against an in-memory FaultSimulator
    instead of a real cluster. This exists ONLY so you can exercise/debug the
    harness logic without a cluster available. Every RunResult produced this
    way is tagged is_simulated=True, and `export_markdown` / `export_csv`
    print a loud warning banner if any simulated rows are present.

Your methodology (Section 3.4 / Phase 2) states manual and rule-based
baselines are established by the researcher performing runbooks -- do NOT
report simulated-mode numbers as thesis results. Use `load_manual_baseline()`
to bring in your own hand-timed runbook data, and `mode="live"` for the
proposed-framework arm once your minikube/Prometheus/Chaos Mesh stack is up.
"""
from __future__ import annotations
import csv
import json
import logging
import time
import random
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Literal

from src.config import SAFETY, EVAL, K8S, PROMETHEUS
from src.state.schemas import GraphState, MetricsSnapshot, FaultReport, HealthStatus
from src.agents.adversary import AdversaryAgent
from src.agents.remediation import RemediationAgent
from src.agents.sentinel import SentinelAgent
from src.agents.llm_client import build_reasoning_client, build_monitor_client
from src.orchestration.graph import build_chaos_graph
from evaluation.scenarios import ScenarioDefinition, generate_scenarios

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    scenario_id: str
    arm: Literal["manual", "rule_based", "proposed"]
    fault_category: str
    ttr_seconds: Optional[float]
    recovered: bool
    vulnerability_detected: bool
    is_simulated: bool
    notes: str = ""
    api_cost_usd: Optional[float] = None
    cycles_used: Optional[int] = None
    wall_clock_seconds: Optional[float] = None  # real wall-clock time for live runs
    repetition: int = 1
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# --------------------------------------------------------------------------- #
# In-memory simulator (debugging aid only -- see integrity note above)
# --------------------------------------------------------------------------- #

class FaultSimulator:
    """Deterministic-ish, seedable model of fault impact + recovery, used only
    in mode='simulated'. Reproduces the qualitative shape of the paper's
    scenarios (config-drift/cascading are harder for the rule-based arm to
    fully resolve) without claiming to be real infrastructure."""

    def __init__(self, scenario: ScenarioDefinition, seed: int = 42):
        self._rng = random.Random(seed + hash(scenario.scenario_id) % 10_000)
        self.scenario = scenario
        self._injected = False
        self._remediated = False
        self._ticks_since_injection = 0

    def inject(self):
        self._injected = True
        self._ticks_since_injection = 0

    def remediate(self, category: str) -> bool:
        # Configuration-drift responds only to configuration_based remediation.
        if self.scenario.fault_category.value == "configuration_drift" and category != "configuration_based":
            return False
        self._remediated = True
        return True

    def tick_metrics(self) -> MetricsSnapshot:
        if not self._injected or self._remediated:
            return MetricsSnapshot(error_rate=0.0, p99_latency_ms=self._rng.uniform(40, 90))
        self._ticks_since_injection += 1
        severity = {
            "pod_termination": 0.35, "network_latency": 0.25, "resource_exhaustion": 0.30,
            "packet_loss": 0.20, "configuration_drift": 0.40,
        }.get(self.scenario.fault_category.value, 0.25)
        decay = max(0.0, 1.0 - self._ticks_since_injection * 0.15)
        return MetricsSnapshot(
            error_rate=severity * decay + self._rng.uniform(0, 0.02),
            p99_latency_ms=300 + severity * 1500 * decay,
            pod_restarts=1 if self.scenario.fault_category.value == "pod_termination" and decay > 0.5 else 0,
            cpu_utilization=0.5 + (0.4 * decay if self.scenario.fault_category.value == "resource_exhaustion" else 0),
        )


# --------------------------------------------------------------------------- #
# Live health polling helper
# --------------------------------------------------------------------------- #

def _poll_until_healthy(
    prom,
    service: str,
    namespace: str,
    timeout_seconds: float = 300.0,
    poll_interval: float = 10.0,
) -> Optional[float]:
    """Poll Prometheus until the target service returns to healthy state.
    Returns seconds to recovery, or None if timeout is reached."""
    from src.tools.prometheus_tools import collect_metrics_snapshot

    start = time.monotonic()
    while (time.monotonic() - start) < timeout_seconds:
        try:
            metrics = collect_metrics_snapshot(prom, service, namespace)
            if metrics.health_score() >= SAFETY.degraded_health_threshold:
                return time.monotonic() - start
        except Exception as e:
            logger.warning("Health poll failed: %s", e)
        time.sleep(poll_interval)
    return None


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

class EvaluationHarness:
    def __init__(self, mode: Literal["live", "simulated"] = "simulated", namespace: str = K8S.namespace,
                 results_dir: str = EVAL.results_dir, auto_approve: bool = True,
                 cooldown_seconds: float = 30.0, repetitions: int = 1):
        self.mode = mode
        self.namespace = namespace
        self.results_dir = Path(results_dir)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.auto_approve = auto_approve
        self.cooldown_seconds = cooldown_seconds
        self.repetitions = repetitions
        self.results: List[RunResult] = []

    # -- proposed framework ------------------------------------------------ #

    def run_proposed_framework(self, scenario: ScenarioDefinition, repetition: int = 1) -> RunResult:
        if self.mode == "live":
            return self._run_proposed_live(scenario, repetition)
        return self._run_proposed_simulated(scenario, repetition)

    def _run_proposed_simulated(self, scenario: ScenarioDefinition, repetition: int = 1) -> RunResult:
        from evaluation.scenarios import ONLINE_BOUTIQUE_DEPENDENCY_GRAPH
        sim = FaultSimulator(scenario)

        adversary = AdversaryAgent(client=None)
        adversary.propose_fault = lambda *a, **k: FaultReport(
            message_id=f"fault-{scenario.scenario_id}", fault_category=scenario.fault_category,
            target_service=scenario.target_service, target_namespace=scenario.namespace,
            parameters=scenario.parameters, expected_observable_impact="simulated",
            blast_radius_fraction=0.1,
        )
        remediation = RemediationAgent(client=None)

        def stub_diagnose(diag_req):
            category = "configuration_based" if scenario.fault_category.value == "configuration_drift" else "restart_based"
            return {"remediation_category": _rc(category), "target_service": scenario.target_service,
                     "action_command": f"kubectl delete pod -l app={scenario.target_service}",
                     "root_cause_summary": "simulated", "reasoning_trace": "simulated", "confidence": 0.8}
        remediation.diagnose_and_propose = stub_diagnose

        def stub_execute(core_v1, apps_v1, diag_req, proposal, namespace):
            ok = sim.remediate(proposal["remediation_category"].value)
            from src.state.schemas import HealingConfirmation
            return HealingConfirmation(
                message_id=f"heal-{scenario.scenario_id}", related_diagnostic_request_id=diag_req.message_id,
                remediation_category=proposal["remediation_category"], action_taken=proposal["action_command"],
                dry_run_validated=True, success=ok, root_cause_summary="simulated",
            )
        remediation.execute = stub_execute

        sentinel = SentinelAgent(auto_approve_callback=lambda f: self.auto_approve)

        def metrics_provider():
            return sim.tick_metrics()

        def fault_injector(fault: FaultReport):
            sim.inject()

        graph = build_chaos_graph(
            adversary=adversary, remediation=remediation, sentinel=sentinel,
            dependency_graph=ONLINE_BOUTIQUE_DEPENDENCY_GRAPH, namespace=scenario.namespace,
            metrics_provider=metrics_provider, fault_injector=fault_injector,
            label_resolver=lambda s: {"app": s},
        )
        init_state = GraphState(scenario_id=scenario.scenario_id, max_cycles=SAFETY.max_cycles_per_scenario)
        start = time.monotonic()
        result = graph.invoke(init_state, config={"recursion_limit": 200})
        wall_seconds = time.monotonic() - start

        ttr = self._compute_ttr_from_log(result["experiment_log"])
        recovered = result["health_status"] == HealthStatus.STEADY or result.get("recovered", False)
        return RunResult(
            scenario_id=scenario.scenario_id, arm="proposed", fault_category=scenario.fault_category.value,
            ttr_seconds=ttr, recovered=bool(recovered), vulnerability_detected=True, is_simulated=True,
            cycles_used=result["cycle_count"], notes=f"simulated wall-clock {wall_seconds:.2f}s",
            wall_clock_seconds=round(wall_seconds, 2), repetition=repetition,
        )

    def _run_proposed_live(self, scenario: ScenarioDefinition, repetition: int = 1) -> RunResult:
        """Runs the REAL multi-agent framework against your cluster.
        Requires: kubeconfig context reachable, Chaos Mesh installed, a
        Prometheus instance reachable at PROMETHEUS_URL, and ANTHROPIC_API_KEY
        set in the environment."""
        from evaluation.scenarios import ONLINE_BOUTIQUE_DEPENDENCY_GRAPH
        from kubernetes import client as k8s_client
        from src.tools import kubernetes_tools, prometheus_tools, chaos_mesh_tools

        core_v1, apps_v1 = kubernetes_tools.load_kube_clients()
        custom_api = k8s_client.CustomObjectsApi()
        prom = prometheus_tools.get_prometheus_client()

        adversary = AdversaryAgent(client=build_reasoning_client())
        remediation = RemediationAgent(client=build_reasoning_client())
        sentinel = SentinelAgent(monitor_client=build_monitor_client(),
                                  auto_approve_callback=lambda f: self.auto_approve)

        def metrics_provider():
            return prometheus_tools.collect_metrics_snapshot(prom, scenario.target_service, self.namespace)

        def fault_injector(fault: FaultReport):
            chaos_mesh_tools.inject_fault(
                custom_api, fault.fault_category, {"app": fault.target_service},
                namespace=self.namespace, apps_v1=apps_v1, **fault.parameters,
            )

        graph = build_chaos_graph(
            adversary=adversary, remediation=remediation, sentinel=sentinel,
            dependency_graph=ONLINE_BOUTIQUE_DEPENDENCY_GRAPH, namespace=self.namespace,
            metrics_provider=metrics_provider, fault_injector=fault_injector,
            label_resolver=lambda s: {"app": s}, core_v1=core_v1, apps_v1=apps_v1,
        )
        init_state = GraphState(scenario_id=scenario.scenario_id, max_cycles=SAFETY.max_cycles_per_scenario)

        wall_start = time.monotonic()
        result = graph.invoke(init_state, config={"recursion_limit": 200})
        wall_seconds = time.monotonic() - wall_start

        recovered = result.get("health_status") == HealthStatus.STEADY or result.get("recovered", False)
        ttr = self._compute_ttr_from_log(result.get("experiment_log", []))
        if ttr is None and recovered:
            # Fall back to real measured wall-clock elapsed time for live runs
            ttr = round(wall_seconds, 2)

        # Estimate API cost from cycle count (rough: ~800 tokens per cycle, ~$0.003/1k for sonnet)
        cycles = result.get("cycle_count", len(result.get("experiment_log", [])))
        estimated_cost = round(cycles * 800 * 0.003 / 1000, 4)  # rough estimate

        return RunResult(
            scenario_id=scenario.scenario_id, arm="proposed", fault_category=scenario.fault_category.value,
            ttr_seconds=ttr, recovered=bool(recovered),
            vulnerability_detected=len(result.get("fault_reports", [])) > 0, is_simulated=False,
            cycles_used=cycles, api_cost_usd=estimated_cost,
            wall_clock_seconds=round(wall_seconds, 2), repetition=repetition,
        )

    # -- rule-based baseline ----------------------------------------------- #

    def run_rule_based_baseline(self, scenario: ScenarioDefinition, repetition: int = 1) -> RunResult:
        """Rule-based tool comparison arm.

        Live mode: Injects real fault via Chaos Mesh, waits a fixed detection
        delay, applies a static scripted remediation (pod restart), then polls
        Prometheus until recovery — producing REAL timed results.

        Cannot resolve configuration_drift (matches paper Table IV: 'N/A
        (unsupported)')."""
        if self.mode == "live":
            return self._run_rule_based_live(scenario, repetition)
        return self._run_rule_based_simulated(scenario, repetition)

    def _run_rule_based_live(self, scenario: ScenarioDefinition, repetition: int = 1) -> RunResult:
        """Real timed rule-based workflow using Chaos Mesh + scripted remediation."""
        from kubernetes import client as k8s_client
        from src.tools import kubernetes_tools, prometheus_tools, chaos_mesh_tools

        # Configuration drift is unsupported by rule-based tools
        if scenario.fault_category.value == "configuration_drift":
            return RunResult(
                scenario.scenario_id, "rule_based", scenario.fault_category.value,
                ttr_seconds=None, recovered=False, vulnerability_detected=False,
                is_simulated=False, notes="unsupported by rule-based tool",
                repetition=repetition,
            )

        core_v1, apps_v1 = kubernetes_tools.load_kube_clients()
        custom_api = k8s_client.CustomObjectsApi()
        prom = prometheus_tools.get_prometheus_client()

        wall_start = time.monotonic()

        # Step 1: Inject fault via Chaos Mesh (same as proposed arm)
        try:
            target_labels = {"app": scenario.target_service}
            chaos_mesh_tools.inject_fault(
                custom_api, scenario.fault_category, target_labels,
                namespace=self.namespace, **scenario.parameters,
            )
            logger.info("[rule_based] Fault injected: %s on %s",
                        scenario.fault_category.value, scenario.target_service)
        except Exception as e:
            logger.error("[rule_based] Fault injection failed: %s", e)
            return RunResult(
                scenario.scenario_id, "rule_based", scenario.fault_category.value,
                ttr_seconds=None, recovered=False, vulnerability_detected=False,
                is_simulated=False, notes=f"injection failed: {e}",
                repetition=repetition,
            )

        # Step 2: Simulate fixed detection delay (rule-based tools don't have
        # intelligent monitoring -- they rely on alerts/thresholds with latency)
        detection_delay = 5.0  # seconds
        time.sleep(detection_delay)

        # Step 3: Apply scripted static remediation (always: pod restart)
        try:
            pods = kubernetes_tools.list_pods(
                core_v1, namespace=self.namespace,
                label_selector=f"app={scenario.target_service}",
            )
            for pod in pods:
                kubernetes_tools.delete_pod(core_v1, pod["name"], namespace=self.namespace)
            logger.info("[rule_based] Scripted remediation: restarted %d pods", len(pods))
        except Exception as e:
            logger.warning("[rule_based] Remediation failed: %s", e)

        # Step 4: Poll Prometheus until recovery or timeout
        recovery_time = _poll_until_healthy(
            prom, scenario.target_service, self.namespace,
            timeout_seconds=300.0, poll_interval=3.0,
        )

        wall_seconds = time.monotonic() - wall_start

        if recovery_time is not None:
            return RunResult(
                scenario.scenario_id, "rule_based", scenario.fault_category.value,
                ttr_seconds=round(wall_seconds, 2), recovered=True,
                vulnerability_detected=False, is_simulated=False,
                wall_clock_seconds=round(wall_seconds, 2),
                notes="live rule-based: inject + fixed delay + pod restart + poll",
                repetition=repetition,
            )
        else:
            return RunResult(
                scenario.scenario_id, "rule_based", scenario.fault_category.value,
                ttr_seconds=None, recovered=False, vulnerability_detected=False,
                is_simulated=False, wall_clock_seconds=round(wall_seconds, 2),
                notes="live rule-based: recovery timed out (600s)",
                repetition=repetition,
            )

    def _run_rule_based_simulated(self, scenario: ScenarioDefinition, repetition: int = 1) -> RunResult:
        """Simulated rule-based baseline reproducing qualitative pattern from Table IV."""
        if scenario.fault_category.value == "configuration_drift":
            return RunResult(scenario.scenario_id, "rule_based", scenario.fault_category.value,
                              ttr_seconds=None, recovered=False, vulnerability_detected=False,
                              is_simulated=True, notes="unsupported by rule-based tool (simulated)",
                              repetition=repetition)
        base = {"pod_termination": 9.2, "network_latency": 28.4, "resource_exhaustion": 31.6,
                "packet_loss": 20.0}.get(scenario.fault_category.value, 25.0)
        jitter = random.Random(hash(scenario.scenario_id) % 1000 + repetition).uniform(-2, 2)
        return RunResult(scenario.scenario_id, "rule_based", scenario.fault_category.value,
                          ttr_seconds=max(1.0, base + jitter) * 60, recovered=True,
                          vulnerability_detected=False, is_simulated=True,
                          notes="illustrative only -- replace with real Chaos Mesh timing",
                          repetition=repetition)

    # -- manual baseline: load real researcher-collected data --------------- #

    def load_manual_baseline(self, csv_path: str) -> List[RunResult]:
        """Load YOUR hand-timed runbook results (per methodology Phase 2).
        Expected columns: scenario_id,fault_category,ttr_seconds,recovered,vulnerability_detected,notes
        """
        rows = []
        with open(csv_path, newline="") as f:
            for row in csv.DictReader(f):
                ttr_raw = row.get("ttr_seconds", "").strip()
                if not ttr_raw or ttr_raw.upper().startswith("TODO"):
                    logger.warning("Skipping row %s: ttr_seconds not filled in yet", row.get("scenario_id"))
                    continue
                rows.append(RunResult(
                    scenario_id=row["scenario_id"], arm="manual", fault_category=row["fault_category"],
                    ttr_seconds=float(ttr_raw) if ttr_raw else None,
                    recovered=row.get("recovered", "true").lower() == "true",
                    vulnerability_detected=row.get("vulnerability_detected", "true").lower() == "true",
                    is_simulated=False, notes=row.get("notes", "researcher-timed runbook"),
                ))
        self.results.extend(rows)
        return rows

    # -- orchestrating a full comparison ------------------------------------ #

    def run_full_comparison(self, scenarios: Optional[List[ScenarioDefinition]] = None,
                             include_rule_based: bool = True) -> List[RunResult]:
        scenarios = scenarios or generate_scenarios(namespace=self.namespace)

        total = len(scenarios) * self.repetitions * (2 if include_rule_based else 1)
        completed = 0

        for rep in range(1, self.repetitions + 1):
            for sc_idx, sc in enumerate(scenarios):
                if self.repetitions > 1:
                    print(f"\n--- Repetition {rep}/{self.repetitions}, "
                          f"Scenario {sc_idx+1}/{len(scenarios)}: {sc.scenario_id} ---")
                else:
                    print(f"\n--- Scenario {sc_idx+1}/{len(scenarios)}: {sc.scenario_id} ---")

                # Proposed framework arm
                result = self.run_proposed_framework(sc, repetition=rep)
                self.results.append(result)
                completed += 1
                _print_progress(completed, total, result)
                self.export_csv()

                # Rule-based arm
                if include_rule_based:
                    rb_result = self.run_rule_based_baseline(sc, repetition=rep)
                    self.results.append(rb_result)
                    completed += 1
                    _print_progress(completed, total, rb_result)
                    self.export_csv()

                # Inter-scenario cooldown for live mode
                if self.mode == "live" and self.cooldown_seconds > 0 and sc_idx < len(scenarios) - 1:
                    self._verify_cluster_health()
                    print(f"  Cooldown: {self.cooldown_seconds}s...", flush=True)
                    time.sleep(self.cooldown_seconds)

        return self.results

    def _verify_cluster_health(self) -> None:
        """Confirm cluster is healthy before next scenario (live mode only)."""
        if self.mode != "live":
            return
        try:
            from src.tools import kubernetes_tools
            core_v1, _ = kubernetes_tools.load_kube_clients()
            health = kubernetes_tools.get_pod_health(core_v1, namespace=self.namespace)
            unhealthy = health.get("unhealthy_pods", [])
            if unhealthy:
                logger.warning("Pre-scenario health check: %d unhealthy pods: %s",
                               len(unhealthy), unhealthy)
                print(f"  ⚠️  Waiting for unhealthy pods to recover: {unhealthy}")
                time.sleep(30)
        except Exception as e:
            logger.warning("Pre-scenario health check failed: %s", e)

    # -- helpers -------------------------------------------------------------#

    @staticmethod
    def _compute_ttr_from_log(log_entries, interval_seconds: Optional[float] = None) -> Optional[float]:
        """
        TTR = (cycle_of_recovery - cycle_of_fault_injection) * monitoring_interval.

        We deliberately use cycle deltas rather than raw wall-clock timestamp
        deltas: in live mode, Sentinel's real monitoring loop polls Prometheus
        every `interval_seconds`, so cycle-delta * interval IS the real elapsed
        time. In simulated mode there is no real sleep between cycles (the
        graph runs as fast as Python allows), so raw wall-clock timestamps
        would understate TTR to ~0s regardless of how many recovery cycles
        were actually needed. Cycle-based TTR gives a meaningful, comparable
        number in both modes.
        """
        interval_seconds = interval_seconds if interval_seconds is not None else PROMETHEUS.scrape_interval_seconds
        inject_cycle, recovered_cycle = None, None
        for entry in log_entries:
            cycle = entry.cycle if hasattr(entry, "cycle") else (entry.get("cycle", 0) if isinstance(entry, dict) else 0)
            phase = entry.phase if hasattr(entry, "phase") else (entry.get("phase", "") if isinstance(entry, dict) else "")
            if phase in ("adversary_inject", "fault_injection") and inject_cycle is None:
                inject_cycle = cycle
            if phase in ("confirm_recovered", "remediation_success", "recovering") and inject_cycle is not None and recovered_cycle is None:
                recovered_cycle = cycle
            if phase == "sentinel_monitor" and inject_cycle is not None and cycle > inject_cycle:
                health = getattr(entry, "health_status", entry.get("health_status") if isinstance(entry, dict) else "")
                if health in ("steady", "recovering") and recovered_cycle is None:
                    recovered_cycle = cycle
        if inject_cycle is not None and recovered_cycle is not None:
            return max(interval_seconds, (recovered_cycle - inject_cycle) * interval_seconds)
        return None

    def export_csv(self, filename: str = "results.csv") -> str:
        path = self.results_dir / filename
        self._maybe_warn_simulated()
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(asdict(self.results[0]).keys()) if self.results else [])
            writer.writeheader()
            for r in self.results:
                writer.writerow(asdict(r))
        return str(path)

    def export_json(self, filename: str = "results.json") -> str:
        path = self.results_dir / filename
        self._maybe_warn_simulated()
        with open(path, "w") as f:
            json.dump([asdict(r) for r in self.results], f, indent=2)
        return str(path)

    def _maybe_warn_simulated(self):
        if any(r.is_simulated for r in self.results):
            print(
                "\n*** WARNING: results include SIMULATED rows (harness self-test mode). "
                "Do NOT report these as thesis experimental data -- rerun with mode='live' "
                "against your minikube cluster, and load real manual-baseline timings via "
                "load_manual_baseline(). ***\n"
            )


def _print_progress(completed: int, total: int, result: RunResult) -> None:
    """Print a compact progress line for the current scenario run."""
    status = "✅ recovered" if result.recovered else "❌ not recovered"
    ttr_str = f"TTR={result.ttr_seconds:.0f}s" if result.ttr_seconds else "TTR=N/A"
    sim_tag = " [SIM]" if result.is_simulated else ""
    print(f"  [{completed}/{total}] {result.arm}: {result.fault_category} | {status} | {ttr_str}{sim_tag}")


def _rc(value: str):
    from src.state.schemas import RemediationCategory
    return RemediationCategory(value)

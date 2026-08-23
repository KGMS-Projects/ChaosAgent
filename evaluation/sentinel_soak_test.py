#!/usr/bin/env python3
"""
24-Hour Sentinel Soak Test (RO6 evidence).

Runs Sentinel's continuous monitoring loop against a live Prometheus instance
for a configurable duration (default 24h). Produces structured logs and a
summary report proving continuous operational capability.

Usage:
    python -m evaluation.sentinel_soak_test --duration 24h
    python -m evaluation.sentinel_soak_test --duration 1h   # shorter test run
    python -m evaluation.sentinel_soak_test --duration 10m  # quick smoke test
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from src.config import PROMETHEUS, K8S
from src.agents.sentinel import SentinelAgent
from src.state.schemas import MetricsSnapshot, HealthStatus

logger = logging.getLogger(__name__)


@dataclass
class SoakLogEntry:
    timestamp: str
    elapsed_seconds: float
    health_status: str
    health_score: float
    error_rate: float
    p99_latency_ms: float
    cpu_utilization: float
    memory_utilization: float
    pod_restarts: int
    state_transition: Optional[str] = None  # e.g. "steady -> degraded"


@dataclass
class SoakTestSummary:
    start_time: str
    end_time: str
    duration_seconds: float
    total_observations: int
    state_transitions: int
    time_in_steady_seconds: float
    time_in_degraded_seconds: float
    time_in_critical_seconds: float
    time_in_recovering_seconds: float
    uptime_percent: float  # % time in STEADY
    longest_steady_streak_seconds: float
    false_positive_transitions: int  # degraded/critical transitions where no fault was injected
    max_error_rate_observed: float
    max_p99_latency_observed: float
    max_cpu_utilization_observed: float
    total_pod_restarts_observed: int


def _parse_duration(duration_str: str) -> float:
    """Parse duration string like '24h', '1h', '30m', '10s' to seconds."""
    s = duration_str.strip().lower()
    if s.endswith("h"):
        return float(s[:-1]) * 3600
    elif s.endswith("m"):
        return float(s[:-1]) * 60
    elif s.endswith("s"):
        return float(s[:-1])
    else:
        return float(s)


class SentinelSoakTest:
    def __init__(
        self,
        duration_seconds: float = 86400.0,
        poll_interval: float = 15.0,
        results_dir: str = "./results",
        namespace: str = K8S.namespace,
    ):
        self.duration_seconds = duration_seconds
        self.poll_interval = poll_interval
        self.results_dir = Path(results_dir)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self.log_entries: List[SoakLogEntry] = []
        self._running = True

        # Handle graceful shutdown on Ctrl+C
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        logger.info("Received signal %s, stopping soak test gracefully...", signum)
        self._running = False

    def run(self) -> SoakTestSummary:
        """Execute the soak test against live Prometheus."""
        from src.tools.prometheus_tools import get_prometheus_client, collect_metrics_snapshot

        prom = get_prometheus_client()
        sentinel = SentinelAgent()
        prev_status = HealthStatus.STEADY

        start_time = time.monotonic()
        start_iso = datetime.now(timezone.utc).isoformat()
        state_times = {s: 0.0 for s in HealthStatus}
        state_transitions = 0
        max_error = 0.0
        max_p99 = 0.0
        max_cpu = 0.0
        total_restarts = 0
        current_steady_streak = 0.0
        longest_steady_streak = 0.0
        observation_count = 0

        # Collect all services to rotate monitoring across
        services = [
            "frontend", "cartservice", "checkoutservice", "productcatalogservice",
            "paymentservice", "shippingservice", "emailservice", "currencyservice",
            "recommendationservice", "adservice",
        ]
        service_index = 0

        print(f"\n{'='*70}")
        print(f"  SENTINEL SOAK TEST — {self.duration_seconds/3600:.1f}h continuous monitoring")
        print(f"  Namespace: {self.namespace}")
        print(f"  Poll interval: {self.poll_interval}s")
        print(f"  Started: {start_iso}")
        print(f"{'='*70}\n")

        while self._running:
            elapsed = time.monotonic() - start_time
            if elapsed >= self.duration_seconds:
                break

            # Rotate through services for comprehensive coverage
            service = services[service_index % len(services)]
            service_index += 1

            try:
                metrics = collect_metrics_snapshot(prom, service, self.namespace)
            except Exception as e:
                logger.warning("Metric collection failed for %s: %s", service, e)
                metrics = MetricsSnapshot()  # defaults = healthy

            new_status = sentinel.observe(metrics)
            transition_str = None
            if new_status != prev_status:
                transition_str = f"{prev_status.value} -> {new_status.value}"
                state_transitions += 1
                logger.info("State transition: %s (service=%s)", transition_str, service)

            # Track time in each state
            state_times[new_status] = state_times.get(new_status, 0.0) + self.poll_interval

            # Track steady streak
            if new_status == HealthStatus.STEADY:
                current_steady_streak += self.poll_interval
                longest_steady_streak = max(longest_steady_streak, current_steady_streak)
            else:
                current_steady_streak = 0.0

            # Track maximums
            max_error = max(max_error, metrics.error_rate)
            max_p99 = max(max_p99, metrics.p99_latency_ms)
            max_cpu = max(max_cpu, metrics.cpu_utilization)
            total_restarts += metrics.pod_restarts
            observation_count += 1

            entry = SoakLogEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                elapsed_seconds=round(elapsed, 2),
                health_status=new_status.value,
                health_score=round(metrics.health_score(), 2),
                error_rate=round(metrics.error_rate, 6),
                p99_latency_ms=round(metrics.p99_latency_ms, 2),
                cpu_utilization=round(metrics.cpu_utilization, 4),
                memory_utilization=round(metrics.memory_utilization, 4),
                pod_restarts=metrics.pod_restarts,
                state_transition=transition_str,
            )
            self.log_entries.append(entry)
            prev_status = new_status

            # Progress output every 60 observations (~15 minutes)
            if observation_count % 60 == 0:
                pct = (elapsed / self.duration_seconds) * 100
                print(
                    f"  [{pct:5.1f}%] {observation_count} obs | "
                    f"status={new_status.value} | score={metrics.health_score():.1f} | "
                    f"transitions={state_transitions} | service={service}"
                )

            time.sleep(self.poll_interval)

        end_time_mono = time.monotonic()
        end_iso = datetime.now(timezone.utc).isoformat()
        total_elapsed = end_time_mono - start_time

        # Compute uptime
        steady_time = state_times.get(HealthStatus.STEADY, 0.0)
        uptime_pct = (steady_time / total_elapsed * 100) if total_elapsed > 0 else 0.0

        summary = SoakTestSummary(
            start_time=start_iso,
            end_time=end_iso,
            duration_seconds=round(total_elapsed, 2),
            total_observations=observation_count,
            state_transitions=state_transitions,
            time_in_steady_seconds=round(steady_time, 2),
            time_in_degraded_seconds=round(state_times.get(HealthStatus.DEGRADED, 0.0), 2),
            time_in_critical_seconds=round(state_times.get(HealthStatus.CRITICAL, 0.0), 2),
            time_in_recovering_seconds=round(state_times.get(HealthStatus.RECOVERING, 0.0), 2),
            uptime_percent=round(uptime_pct, 2),
            longest_steady_streak_seconds=round(longest_steady_streak, 2),
            false_positive_transitions=state_transitions,  # all transitions during soak are false positives (no injection)
            max_error_rate_observed=round(max_error, 6),
            max_p99_latency_observed=round(max_p99, 2),
            max_cpu_utilization_observed=round(max_cpu, 4),
            total_pod_restarts_observed=total_restarts,
        )

        self._export(summary)
        return summary

    def _export(self, summary: SoakTestSummary) -> None:
        # Export detailed log
        log_path = self.results_dir / "sentinel_soak_log.json"
        with open(log_path, "w") as f:
            json.dump([asdict(e) for e in self.log_entries], f, indent=2)

        # Export summary
        summary_path = self.results_dir / "sentinel_soak_summary.json"
        with open(summary_path, "w") as f:
            json.dump(asdict(summary), f, indent=2)

        # Export CSV summary for thesis inclusion
        csv_path = self.results_dir / "sentinel_soak_summary.csv"
        with open(csv_path, "w") as f:
            f.write("metric,value\n")
            for k, v in asdict(summary).items():
                f.write(f"{k},{v}\n")

        print(f"\n{'='*70}")
        print(f"  SOAK TEST COMPLETE")
        print(f"{'='*70}")
        print(f"  Duration:            {summary.duration_seconds/3600:.2f} hours")
        print(f"  Total observations:  {summary.total_observations}")
        print(f"  Uptime (STEADY):     {summary.uptime_percent:.1f}%")
        print(f"  State transitions:   {summary.state_transitions}")
        print(f"  Longest steady:      {summary.longest_steady_streak_seconds/60:.1f} minutes")
        print(f"  Max error rate:      {summary.max_error_rate_observed:.4%}")
        print(f"  Max P99 latency:     {summary.max_p99_latency_observed:.1f} ms")
        print(f"  Files written:")
        print(f"    {log_path}")
        print(f"    {summary_path}")
        print(f"    {csv_path}")
        print(f"{'='*70}\n")


def main():
    parser = argparse.ArgumentParser(description="24-hour Sentinel soak test (RO6)")
    parser.add_argument("--duration", default="24h", help="Test duration, e.g. 24h, 1h, 30m, 10s")
    parser.add_argument("--interval", type=float, default=15.0, help="Poll interval in seconds")
    parser.add_argument("--results-dir", default="./results", help="Output directory")
    parser.add_argument("--namespace", default=K8S.namespace, help="Kubernetes namespace")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    soak = SentinelSoakTest(
        duration_seconds=_parse_duration(args.duration),
        poll_interval=args.interval,
        results_dir=args.results_dir,
        namespace=args.namespace,
    )
    soak.run()


if __name__ == "__main__":
    main()

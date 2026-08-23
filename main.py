#!/usr/bin/env python3
"""
CLI entry point for the Multi-Agent Autonomous Chaos Engineering Framework.

Examples
--------
# Quick self-test with no cluster required (validates the harness end-to-end):
python main.py selftest

# Pre-flight infrastructure check (Kubernetes, Prometheus, Chaos Mesh, API key):
python main.py verify

# Run all 20 scenarios against your live minikube cluster (needs kubeconfig,
# Chaos Mesh, Prometheus reachable, and ANTHROPIC_API_KEY set):
python main.py run --mode live

# Run with 3 repetitions per scenario (for statistical significance):
python main.py run --mode live --repetitions 3

# Run just the pod_termination scenarios, simulated:
python main.py run --mode simulated --category pod_termination

# Load your hand-timed manual-baseline runbook results:
python main.py load-manual results/manual_baseline.csv

# Run the 24-hour Sentinel soak test for RO6 evidence:
python main.py soak-test --duration 24h

# Analyze all results and generate thesis tables:
python main.py analyze
"""
from __future__ import annotations
import argparse
import sys

from evaluation.harness import EvaluationHarness
from evaluation.scenarios import generate_scenarios
from src.state.schemas import FaultCategory


def cmd_selftest(args):
    print("Running harness self-test in SIMULATED mode (no cluster required)...")
    harness = EvaluationHarness(mode="simulated", results_dir=args.results_dir)
    results = harness.run_full_comparison(scenarios=generate_scenarios(), include_rule_based=True)
    proposed = [r for r in results if r.arm == "proposed"]
    recovered = sum(1 for r in proposed if r.recovered)
    print(f"\n{recovered}/{len(proposed)} proposed-framework scenarios recovered.")
    path = harness.export_csv("selftest_results.csv")
    print(f"Results written to {path}")
    print("\nIf this all looks sane, rerun with `python main.py run --mode live` "
          "against your actual minikube cluster for real thesis data.")


def cmd_run(args):
    harness = EvaluationHarness(
        mode=args.mode, namespace=args.namespace, results_dir=args.results_dir,
        cooldown_seconds=args.cooldown, repetitions=args.repetitions,
    )
    scenarios = generate_scenarios(namespace=args.namespace)
    if args.category:
        scenarios = [s for s in scenarios if s.fault_category.value == args.category]
        if not scenarios:
            print(f"No scenarios match category '{args.category}'. Valid: {[c.value for c in FaultCategory]}")
            sys.exit(1)

    print(f"Running {len(scenarios)} scenario(s) x {args.repetitions} repetition(s) "
          f"in mode='{args.mode}'...")
    harness.run_full_comparison(scenarios=scenarios, include_rule_based=not args.skip_rule_based)

    csv_path = harness.export_csv()
    json_path = harness.export_json()
    print(f"\nResults written to:\n  {csv_path}\n  {json_path}")


def cmd_load_manual(args):
    harness = EvaluationHarness(mode="simulated", results_dir=args.results_dir)
    rows = harness.load_manual_baseline(args.csv_path)
    print(f"Loaded {len(rows)} manual-baseline rows from {args.csv_path}")
    harness.export_json("manual_baseline_loaded.json")


def cmd_verify(args):
    from scripts.verify_infra import run_all_checks
    success = run_all_checks()
    sys.exit(0 if success else 1)


def cmd_soak_test(args):
    from evaluation.sentinel_soak_test import SentinelSoakTest, _parse_duration

    duration = _parse_duration(args.duration)
    print(f"Starting {args.duration} Sentinel soak test (RO6 evidence)...")
    soak = SentinelSoakTest(
        duration_seconds=duration,
        poll_interval=args.interval,
        results_dir=args.results_dir,
        namespace=args.namespace,
    )
    soak.run()


def cmd_analyze(args):
    from evaluation.analyze_results import main as analyze_main
    sys.argv = ["analyze_results", "--results-dir", args.results_dir]
    analyze_main()


def cmd_dashboard(args):
    import webbrowser
    from src.dashboard.server import run_dashboard_server
    url = f"http://localhost:{args.port}"
    print(f"\nLaunching Chaos Engineering Web Dashboard at {url}...")
    if not getattr(args, "no_browser", False):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    run_dashboard_server(host=args.host, port=args.port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Multi-Agent Autonomous Chaos Engineering & Self-Healing Framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # selftest
    p_self = sub.add_parser("selftest", help="Quick self-test in simulated mode (no cluster required)")
    p_self.add_argument("--results-dir", default="./results")
    p_self.set_defaults(func=cmd_selftest)

    # run
    p_run = sub.add_parser("run", help="Run the full evaluation comparison")
    p_run.add_argument("--mode", choices=["simulated", "live"], default="simulated",
                        help="Execution mode (default: simulated)")
    p_run.add_argument("--namespace", default="chaos-demo")
    p_run.add_argument("--category", choices=[c.value for c in FaultCategory], default=None,
                        help="Only run scenarios for this fault category (default: all 20)")
    p_run.add_argument("--skip-rule-based", action="store_true",
                        help="Skip the rule-based-tool comparison arm")
    p_run.add_argument("--repetitions", type=int, default=1,
                        help="Number of repetitions per scenario (default: 1, paper uses 3)")
    p_run.add_argument("--cooldown", type=float, default=30.0,
                        help="Seconds to wait between scenarios in live mode (default: 30)")
    p_run.add_argument("--results-dir", default="./results")
    p_run.set_defaults(func=cmd_run)

    # load-manual
    p_manual = sub.add_parser("load-manual", help="Load your hand-timed manual-baseline CSV")
    p_manual.add_argument("csv_path")
    p_manual.add_argument("--results-dir", default="./results")
    p_manual.set_defaults(func=cmd_load_manual)

    # verify
    p_verify = sub.add_parser("verify", help="Pre-flight infrastructure check")
    p_verify.set_defaults(func=cmd_verify)

    # soak-test
    p_soak = sub.add_parser("soak-test", help="Run the 24h Sentinel soak test (RO6)")
    p_soak.add_argument("--duration", default="24h",
                         help="Test duration, e.g. 24h, 1h, 30m (default: 24h)")
    p_soak.add_argument("--interval", type=float, default=15.0,
                         help="Prometheus poll interval in seconds (default: 15)")
    p_soak.add_argument("--namespace", default="chaos-demo")
    p_soak.add_argument("--results-dir", default="./results")
    p_soak.set_defaults(func=cmd_soak_test)

    # analyze
    p_analyze = sub.add_parser("analyze", help="Analyze results and generate thesis tables")
    p_analyze.add_argument("--results-dir", default="./results")
    p_analyze.set_defaults(func=cmd_analyze)

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Launch interactive web dashboard")
    p_dash.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    p_dash.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")
    p_dash.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    p_dash.set_defaults(func=cmd_dashboard)

    return parser


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)

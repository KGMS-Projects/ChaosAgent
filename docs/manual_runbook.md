# Manual Baseline Runbook — Phase 2 Data Collection

This document provides step-by-step instructions for collecting manual-baseline
Time to Recovery (TTR) data for each of the 20 experimental scenarios, as required
by the methodology described in Chapter 3, Section 3.4 (Phase 2).

## Prerequisites

1. Kubernetes cluster running (minikube or GKE) with Online Boutique deployed
   in namespace `chaos-demo`.
2. Chaos Mesh v2.6+ installed.
3. Prometheus reachable (port-forwarded or ingress).
4. A stopwatch or timing tool ready.

## Timing Protocol

For every scenario below:

1. **Confirm steady state**: Open Grafana / Prometheus and verify:
   - All 10+ pods are `Running`
   - Error rate ≈ 0%
   - P99 latency < 200ms
2. **START timer** immediately before executing the fault injection command.
3. **Inject the fault** using the command listed.
4. **Observe degradation** in Prometheus/Grafana — confirm the fault took effect.
5. **Perform manual remediation** using the remediation command listed.
6. **STOP timer** when ALL of the following are true:
   - All pods are `Running`
   - Error rate has returned to pre-injection level (≈ 0%)
   - P99 latency has returned to pre-injection level
   - The affected service responds correctly to test requests
7. **Record** `ttr_seconds` (timer value in seconds) in `results/manual_baseline.csv`.
8. **Wait 3 minutes** for the cluster to fully stabilize before the next scenario.

---

## Scenario Runbooks

### Category 1: Pod Termination

#### Scenario: pod_termination-01 — cartservice
```bash
# Inject
kubectl -n chaos-demo delete pod -l app=cartservice --grace-period=5

# Remediate (wait for Kubernetes auto-restart, then verify)
kubectl -n chaos-demo get pods -l app=cartservice -w
# Stop timer when pod is Running and ready

# Verify
kubectl -n chaos-demo exec deploy/frontend -- wget -qO- http://cartservice:7070/healthz
```

#### Scenario: pod_termination-02 — checkoutservice
```bash
kubectl -n chaos-demo delete pod -l app=checkoutservice --grace-period=5
kubectl -n chaos-demo get pods -l app=checkoutservice -w
```

#### Scenario: pod_termination-03 — productcatalogservice
```bash
kubectl -n chaos-demo delete pod -l app=productcatalogservice --grace-period=5
kubectl -n chaos-demo get pods -l app=productcatalogservice -w
```

#### Scenario: pod_termination-04 — frontend
```bash
kubectl -n chaos-demo delete pod -l app=frontend --grace-period=5
kubectl -n chaos-demo get pods -l app=frontend -w
```

---

### Category 2: Network Latency

Use Chaos Mesh NetworkChaos CRDs. Apply the manifest, observe impact, then delete
the CRD to remove the fault and begin recovery timing.

#### Scenario: network_latency-01 — paymentservice (200ms)
```bash
# Inject
cat <<EOF | kubectl apply -f -
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: manual-net-latency-01
  namespace: chaos-demo
spec:
  action: delay
  mode: one
  selector:
    namespaces: [chaos-demo]
    labelSelectors:
      app: paymentservice
  delay:
    latency: "200ms"
    jitter: "20ms"
  duration: "120s"
EOF

# Wait 60s for impact, then remediate by deleting the experiment
kubectl -n chaos-demo delete networkchaos manual-net-latency-01

# Monitor recovery
kubectl -n chaos-demo get pods -w
```

#### Scenario: network_latency-02 — shippingservice (500ms)
```bash
# Same pattern: apply NetworkChaos with latency 500ms/jitter 50ms targeting shippingservice
# Delete to remediate, time until recovery
```

#### Scenario: network_latency-03 — recommendationservice (1000ms)
```bash
# latency: 1000ms, jitter: 100ms, target: recommendationservice
```

#### Scenario: network_latency-04 — currencyservice (2500ms)
```bash
# latency: 2500ms, jitter: 200ms, target: currencyservice
```

---

### Category 3: Resource Exhaustion

#### Scenario: resource_exhaustion-01 — redis-cart (CPU 70%)
```bash
cat <<EOF | kubectl apply -f -
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: manual-stress-01
  namespace: chaos-demo
spec:
  mode: one
  selector:
    namespaces: [chaos-demo]
    labelSelectors:
      app: redis-cart
  stressors:
    cpu:
      workers: 2
      load: 70
  duration: "120s"
EOF

# Remediate by deleting the experiment
kubectl -n chaos-demo delete stresschaos manual-stress-01
```

#### Scenario: resource_exhaustion-02 — productcatalogservice (CPU 85%)
```bash
# cpu load: 85, target: productcatalogservice
```

#### Scenario: resource_exhaustion-03 — cartservice (CPU 95%)
```bash
# cpu load: 95, target: cartservice
```

#### Scenario: resource_exhaustion-04 — checkoutservice (memory 512MB)
```bash
# stressors.memory.workers: 1, size: "512MB", target: checkoutservice
```

---

### Category 4: Packet Loss

#### Scenario: packet_loss-01 — emailservice (10%)
```bash
cat <<EOF | kubectl apply -f -
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: manual-pkt-loss-01
  namespace: chaos-demo
spec:
  action: loss
  mode: one
  selector:
    namespaces: [chaos-demo]
    labelSelectors:
      app: emailservice
  loss:
    loss: "10"
    correlation: "25"
  duration: "120s"
EOF

kubectl -n chaos-demo delete networkchaos manual-pkt-loss-01
```

#### Scenario: packet_loss-02 — adservice (25%)
#### Scenario: packet_loss-03 — recommendationservice (40%)
#### Scenario: packet_loss-04 — frontend (60%)

---

### Category 5: Configuration Drift

Configuration drift is injected by patching a deployment's environment variable
to an invalid value, then rolling back to remediate.

#### Scenario: configuration_drift-01 — currencyservice
```bash
# Inject: set an invalid env var
kubectl -n chaos-demo set env deployment/currencyservice PORT=invalid

# Observe pods crash / fail readiness checks

# Remediate: rollback
kubectl -n chaos-demo rollout undo deployment/currencyservice

# Or via Helm:
helm -n chaos-demo rollback online-boutique

# Monitor recovery
kubectl -n chaos-demo get pods -l app=currencyservice -w
```

#### Scenario: configuration_drift-02 — paymentservice
```bash
kubectl -n chaos-demo set env deployment/paymentservice PORT=invalid
# Remediate: kubectl -n chaos-demo rollout undo deployment/paymentservice
```

#### Scenario: configuration_drift-03 — checkoutservice
```bash
kubectl -n chaos-demo set env deployment/checkoutservice PORT=invalid
# Remediate: kubectl -n chaos-demo rollout undo deployment/checkoutservice
```

#### Scenario: configuration_drift-04 — shippingservice
```bash
kubectl -n chaos-demo set env deployment/shippingservice PORT=invalid
# Remediate: kubectl -n chaos-demo rollout undo deployment/shippingservice
```

---

## Recording Results

After completing all 20 scenarios, open `results/manual_baseline.csv` and replace
each `TODO` note and empty `ttr_seconds` field with your actual recorded values.

Example of a completed row:
```
pod_termination-01,pod_termination,1476,true,true,Hand-timed: pod restarted in ~24.6 min
```

Then load into the harness:
```bash
python main.py load-manual results/manual_baseline.csv
```

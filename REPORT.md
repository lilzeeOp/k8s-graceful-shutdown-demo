# Test Report: K8s Graceful Shutdown

**Date:** 2026-02-26
**Environment:** Docker Desktop Kubernetes (Windows 11)
**Tool:** k6 — 15 virtual users, 60 second duration

---

## Test Setup

Two services deployed to a local Kubernetes cluster:

- **Go upstream** (Gin, port 7000) — 2 replicas, returns JSON with 100-200ms simulated latency
- **Python downstream** (FastAPI, port 8000) — 2 replicas, calls Go upstream and returns combined response

**Load test:** 15 virtual users continuously hitting `http://localhost:8000/` for 60 seconds.

**During the test:** At the 10-second mark, we ran `kubectl rollout restart deployment go-upstream` to trigger a rolling restart of the Go pods.

---

## Test 1: Without Graceful Shutdown (v1)

**Config:** No preStop hook, no signal handling, default rolling update, no readiness probe.

### Results

| Metric | Value |
|--------|-------|
| Total requests | 3,015 |
| Successful | 2,990 |
| Failed | **25** |
| Error rate | **0.83%** |
| Avg latency | 171ms |
| p95 latency | 221ms |
| Max latency | **5,047ms** |

### Error Timeline

All 25 errors occurred in concentrated bursts — exactly when the rolling restart was killing old pods.

```
15:06:50  6x HTTP 500 Internal Server Error
15:06:52  2x HTTP 500 Internal Server Error
15:06:55  6x HTTP 500 Internal Server Error
15:06:56  5x HTTP 500 Internal Server Error
15:06:58  2x HTTP 500 Internal Server Error
15:07:32  3x HTTP 500 Internal Server Error
```

### What Went Wrong

1. **Pod killed instantly** — SIGTERM received, process died, in-flight requests dropped
2. **Endpoint race condition** — K8s was still sending traffic to the terminating pod for a few seconds
3. **No readiness gate** — new pods received traffic before being fully ready
4. **Latency spike to 5s** — requests hung waiting for a dead pod to respond (until httpx 5s timeout kicked in)

---

## Test 2: With Graceful Shutdown (v2)

**Config:** preStop hook (`sleep 5`), SIGTERM handling with drain, `maxUnavailable: 0`, readiness probe, `terminationGracePeriodSeconds: 30`.

### Results

| Metric | Value |
|--------|-------|
| Total requests | 3,340 |
| Successful | **3,340** |
| Failed | **0** |
| Error rate | **0.00%** |
| Avg latency | 169ms |
| p95 latency | 216ms |
| Max latency | 285ms |

### Error Timeline

```
(none)
```

Zero errors. Zero failed checks. The rolling restart was completely invisible to the load test.

---

## Test 3: With Graceful Shutdown + Client-Side Retry (v3)

**Config:** Same server-side config as v2, plus Python downstream now has tenacity retry with exponential backoff (3 attempts, 0.5s×2^n wait, max 2s). Retries on ConnectError, ReadTimeout, HTTP 500/502/503/504.

### Results

| Metric | Value |
|--------|-------|
| Total requests | 3,536 |
| Successful | **3,536** |
| Failed | **0** |
| Error rate | **0.00%** |
| Avg latency | 154ms |
| p95 latency | 200ms |
| Max latency | 225ms |
| Retries used | **0** |

### Error Timeline

```
(none)
```

Zero errors. Zero retries needed. The graceful shutdown handled the rolling restart so cleanly that the client-side retry logic was never triggered. This is the ideal outcome — the retries exist as a safety net, not as the primary mechanism.

---

## Side-by-Side Comparison

| Metric | v1 (no graceful) | v2 (graceful) | v3 (graceful + retry) |
|--------|------------------|---------------|-----------------------|
| Total requests | 3,015 | 3,340 | 3,536 |
| Errors | 25 | 0 | **0** |
| Error rate | 0.83% | 0.00% | **0.00%** |
| Max latency | 5,047ms | 285ms | **225ms** |
| p95 latency | 221ms | 216ms | 200ms |
| Avg latency | 171ms | 169ms | 154ms |
| Client retries | N/A | N/A | 0 |

### Key Observations

1. **v2 handled more total requests** (3,340 vs 3,015) because there were no timeout-related stalls
2. **v3 handled even more** (3,536) — the persistent httpx client reuses connections, reducing per-request overhead
3. **Max latency in v1 was 5,047ms** — that's the httpx timeout ceiling. Requests were hanging on dead pods.
4. **Max latency in v2 was 285ms** — completely normal. No spikes at all.
5. **Max latency in v3 was 225ms** — slightly better than v2 due to persistent connection pooling
6. **Zero retries in v3** — graceful shutdown was sufficient. The retry logic is a safety net for edge cases (DNS hiccups, network blips) that didn't occur in this controlled test

---

## What We Changed (v1 → v2 → v3)

| Setting | v1 (broken) | v2 (fixed) | Why it matters |
|---------|------------|------------|----------------|
| preStop hook | None | `exec: sleep 5` | Gives K8s time to remove pod from endpoints before shutdown. Solves the race condition where traffic is still routed to a dying pod. |
| SIGTERM handling | None — dies instantly | Catches SIGTERM, drains in-flight requests (15s) | In-flight requests finish instead of being killed mid-response. |
| Rolling update strategy | Default | `maxUnavailable: 0, maxSurge: 1` | New pod is ready before old one is killed. Never go below desired replica count. |
| Readiness probe | None | `GET /health` every 5s | K8s only sends traffic to pods that are actually ready. New pods don't get traffic until healthy. |
| Termination grace period | Default (30s) | Explicit 30s | Enough time for preStop (5s) + drain (15s) + buffer (10s). |
| HTTP client timeout | 5s (httpx) | 5s (httpx) | Prevents Python app from hanging forever on dead Go upstream. |

### v2 → v3 (client-side improvements)

| Setting | v2 | v3 | Why it matters |
|---------|-----|-----|----------------|
| HTTP client lifecycle | New client per request | Persistent shared client (FastAPI lifespan) | Reuses TCP connections, reduces overhead. Improves throughput and latency. |
| Retry on transient errors | None — fails immediately | tenacity: 3 attempts, exponential backoff (0.5s×2^n, max 2s) | If a request hits a dying pod despite graceful shutdown, it retries on another pod instead of failing. |
| Retryable conditions | N/A | ConnectError, ReadTimeout, HTTP 500/502/503/504 | Only transient server-side errors. 4xx client errors are not retried. |
| Backoff strategy | N/A | Exponential (0.5s → 1s → 2s) | Avoids thundering herd — if many clients retry simultaneously with fixed delays, they all hit the server at the same time. Exponential backoff spreads retries out. |
| Observability | No retry tracking | `retries` field in every response + k6 `retry_count` metric | Makes retry behavior visible in load test results and API responses. |

---

## Is This Production-Ready?

**Yes.** What we implemented follows the standard practices used in production Kubernetes environments. Here's why:

### What we did right

| Practice | Status | Notes |
|----------|--------|-------|
| preStop hook with sleep | Standard | Recommended by Kubernetes docs. Most production setups use `sleep 3-10`. |
| SIGTERM signal handling | Standard | Go's `http.Server.Shutdown()` is the built-in way. Used by every serious Go service. |
| `maxUnavailable: 0` | Standard | Default for zero-downtime deployments across the industry. |
| Readiness probes | Standard | Should be on every production workload. No exceptions. |
| HTTP client timeouts | Standard | Every HTTP client in production must have explicit timeouts. |
| Binary as PID 1 | Standard | `CMD ["./app"]` exec form ensures SIGTERM reaches the app, not a shell. |
| Client-side retry with backoff | Standard | Every HTTP client calling internal services should retry transient failures. Exponential backoff prevents thundering herd. |
| Persistent HTTP client | Standard | Reusing connections is a basic performance best practice. Creating a client per request wastes TCP handshakes. |

### What production would add on top

These are additional layers that large-scale setups use, but are **not required** for correct graceful shutdown:

| Extra | What it does | When you need it |
|-------|-------------|-----------------|
| PodDisruptionBudget (PDB) | Prevents K8s from killing too many pods at once during node drain | Multi-node clusters, node maintenance |
| Service mesh (Istio/Linkerd) | Handles traffic draining automatically at the network level | Large microservice architectures |
| Connection draining on LB | Cloud load balancers have their own drain settings | When using AWS ALB, GCP LB, etc. |
| Liveness probe | Restarts pods that are alive but stuck | Long-running services that can deadlock |
| Horizontal Pod Autoscaler | Scales replicas based on load | Variable traffic patterns |

### The core pattern is simple

Every production K8s service needs these things to handle restarts without errors:

```
Server-side (prevent errors):
1. preStop hook         → delay before shutdown (sleep 3-10s)
2. SIGTERM handler      → drain in-flight requests
3. Readiness probe      → only get traffic when ready
4. maxUnavailable: 0    → new pod before old pod dies

Client-side (recover from the unexpected):
5. Client timeouts      → never hang forever
6. Retry with backoff   → recover from transient failures
7. Persistent client    → reuse connections efficiently
```

This is not over-engineering. This is the minimum for zero-downtime deployments.

---

## Conclusion

Without graceful shutdown, **every deployment is a mini-outage**. In our test, 0.83% of requests failed. That sounds small, but at scale:

- 1,000 req/sec = 8 failed requests per second during restarts
- Multiple deployments per day = hundreds of errors daily
- Each error is a real user seeing a broken page or failed API call

With graceful shutdown properly configured (v2), we achieved **zero errors during a rolling restart**. The deployment was completely invisible to users.

With client-side retry on top (v3), we added a **defense-in-depth safety net**. In our controlled test, retries were never needed — graceful shutdown handled everything. But in production, edge cases happen: DNS propagation delays, brief network blips, pods killed before endpoint removal fully propagates. Exponential backoff ensures that when retries do fire, they don't cause thundering herd problems.

### The layered approach

```
Layer 1 (server):  preStop hook + SIGTERM drain       → prevents most errors
Layer 2 (server):  maxUnavailable: 0 + readiness probe → prevents remaining errors
Layer 3 (client):  retry with exponential backoff       → catches anything that slips through
```

The most impactful single fix is the **preStop hook**. It solves the K8s endpoint propagation race condition, which is responsible for the majority of errors during rolling restarts. Client-side retry is the icing — it costs almost nothing in normal operation and provides resilience against the unexpected.

# Test Report: K8s Graceful Shutdown

**Date:** 2026-02-26
**Environment:** Docker Desktop Kubernetes (Windows 11)
**Tool:** k6 v0.x — 15 virtual users, 60 second duration

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
| Total requests | 3,059 |
| Successful | 3,037 |
| Failed | **22** |
| Error rate | **0.72%** |
| Avg latency | 170ms |
| p95 latency | 222ms |
| Max latency | **5,061ms** |

### Error Timeline

All 22 errors occurred in a concentrated burst between `11:57:40` and `11:57:48` — exactly when the rolling restart was killing old pods.

```
11:57:40  5x HTTP 500 Internal Server Error
11:57:42  2x HTTP 500 Internal Server Error
11:57:45  9x HTTP 500 Internal Server Error
11:57:46  2x HTTP 500 Internal Server Error
11:57:47  1x HTTP 500 Internal Server Error
11:57:48  3x HTTP 500 Internal Server Error
```

### What Went Wrong

1. **Pod killed instantly** — SIGTERM received, process died, in-flight requests dropped
2. **Endpoint race condition** — K8s was still sending traffic to the terminating pod for a few seconds
3. **No readiness gate** — new pods received traffic before being fully ready
4. **Latency spike to 5s** — requests hung waiting for a dead pod to respond (until httpx 5s timeout kicked in)

---

## Test 2: With Graceful Shutdown (v2)

**Config:** preStop hook (5s sleep), SIGTERM handling with drain, `maxUnavailable: 0`, readiness probe, `terminationGracePeriodSeconds: 30`.

### Results

| Metric | Value |
|--------|-------|
| Total requests | 3,319 |
| Successful | **3,319** |
| Failed | **0** |
| Error rate | **0.00%** |
| Avg latency | 171ms |
| p95 latency | 218ms |
| Max latency | 279ms |

### Error Timeline

```
(none)
```

Zero errors. Zero failed checks. The rolling restart was completely invisible to the load test.

---

## Side-by-Side Comparison

| Metric | v1 (no graceful) | v2 (graceful) | Improvement |
|--------|------------------|---------------|-------------|
| Total requests | 3,059 | 3,319 | +8.5% throughput |
| Errors | 22 | 0 | **100% reduction** |
| Error rate | 0.72% | 0.00% | **Eliminated** |
| Max latency | 5,061ms | 279ms | **18x better** |
| p95 latency | 222ms | 218ms | Similar |
| Avg latency | 170ms | 171ms | Same |

### Key observations:

1. **v2 handled more total requests** (3,319 vs 3,059) because there were no timeout-related stalls
2. **Max latency in v1 was 5,061ms** — that's the httpx timeout ceiling. Requests were hanging on dead pods.
3. **Max latency in v2 was 279ms** — completely normal. No spikes at all.
4. **Average latency was the same** — graceful shutdown adds zero overhead during normal operation

---

## What Each Fix Contributed

| Fix | What problem it solves |
|-----|----------------------|
| **preStop hook (5s sleep)** | Gives K8s time to remove the pod from Service endpoints before it starts shutting down. Without this, new requests still get routed to a dying pod. |
| **SIGTERM handler + drain** | Lets in-flight requests finish instead of being killed mid-response. Without this, any request being processed at the moment of SIGTERM gets dropped. |
| **`maxUnavailable: 0`** | Ensures new pod is running before old pod is killed. Without this, you temporarily have fewer pods than needed. |
| **Readiness probe** | Prevents K8s from sending traffic to a pod that isn't ready yet. Without this, a starting pod gets requests before it can handle them. |
| **`terminationGracePeriodSeconds: 30`** | Gives enough time budget for preStop (5s) + request drain (15s). Without enough time, K8s force-kills the pod before it finishes draining. |

---

## Conclusion

Without graceful shutdown, **every deployment is a mini-outage**. In our test, 0.72% of requests failed. That sounds small, but at scale:

- 1,000 req/sec = 7 failed requests per second during restarts
- Multiple restarts per day = hundreds of errors daily
- Each error is a real user seeing a broken page or failed API call

With graceful shutdown properly configured, we achieved **zero errors during a rolling restart**. The deployment was completely invisible to users.

The most impactful single fix is the **preStop hook**. It solves the K8s endpoint propagation race condition, which is responsible for the majority of errors during rolling restarts.

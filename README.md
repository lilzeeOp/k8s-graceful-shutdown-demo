# k8s-graceful-shutdown-demo

A hands-on demo that shows **why graceful shutdown matters in Kubernetes**. We deploy two services, hit them with load, restart pods, and watch what happens — first without any protection (errors!), then with graceful shutdown (zero errors).

## What This Demo Proves

| | v1: No protection | v2: Graceful shutdown | v3: Graceful + retry |
|--|---|---|---|
| Errors during restart | 25 (0.83%) | **0 (0.00%)** | **0 (0.00%)** |
| Worst response time | 5,047ms | 285ms | **225ms** |
| Requests dropped | Yes | **No** | **No** |
| Client retries needed | N/A | N/A | **0** |

## Architecture

```
k6 (load test)
  │
  ▼
Python FastAPI app (port 8000)  ──HTTP──▶  Go Gin app (port 7000)
  (downstream)                              (upstream)
  │                                         │
  └── returns combined response ◀───────────┘
```

- **Go upstream** — returns JSON data, simulates 100-200ms of work
- **Python downstream** — receives user request, calls Go upstream, returns the combined result
- Both run as 2 replicas in Kubernetes

## Prerequisites

- Docker Desktop with Kubernetes enabled
- [k6](https://k6.io/docs/getting-started/installation/) for load testing
- `kubectl` CLI

## Quick Start

### Step 1: Build images

```bash
docker build -t go-upstream:v1 ./go-upstream
docker build -t python-downstream:v1 ./python-downstream
```

### Step 2: Deploy v1 (no graceful shutdown) and test

```bash
kubectl apply -f k8s/v1-no-graceful/
kubectl get pods -w                      # wait until all pods show 1/1 Running
```

Open two terminals:

```bash
# Terminal 1 — start load test
k6 run loadtest/test.js

# Terminal 2 — while k6 is running, restart the Go app
kubectl rollout restart deployment go-upstream
```

You will see **500 errors** in the k6 output. Requests fail because pods die instantly during the restart.

### Step 3: Deploy v2 (with graceful shutdown) and test

```bash
kubectl apply -f k8s/v2-graceful/
kubectl get pods -w                      # wait until new pods are ready
```

Repeat the same test:

```bash
# Terminal 1
k6 run loadtest/test.js

# Terminal 2
kubectl rollout restart deployment go-upstream
```

This time — **zero errors**. The restart happens seamlessly.

### Step 4: Deploy v3 (graceful shutdown + client retry) and test

```bash
# Rebuild python-downstream to include retry logic
docker build -t python-downstream:v1 ./python-downstream

kubectl apply -f k8s/v3-graceful-retry/
kubectl get pods -w                      # wait until new pods are ready
```

Repeat the same test:

```bash
# Terminal 1
k6 run loadtest/test.js

# Terminal 2
kubectl rollout restart deployment go-upstream
```

Same zero errors as v2, but now the k6 summary also shows a `retry_count` metric — how many requests needed a retry before succeeding. This is your safety net: even if a transient error slips past graceful shutdown, the client retries on another pod instead of failing.

### Step 5: Cleanup

```bash
kubectl delete -f k8s/v3-graceful-retry/
```

## What's Different Between v1, v2, and v3?

### v1 — No protection

- Go app ignores SIGTERM, dies instantly
- No preStop hook — K8s kills the pod while traffic is still coming in
- Default rolling update — might kill old pod before new one is ready
- No readiness probe — K8s sends traffic to pods that aren't ready yet

### v2 — Graceful shutdown

Five fixes applied:

**1. preStop hook** — gives K8s time to update its routing

```yaml
lifecycle:
  preStop:
    httpGet:
      path: /prestop
      port: 7000
```

When K8s wants to kill a pod, it first calls `/prestop` on our app. Our app sleeps 5 seconds. During those 5 seconds, K8s removes the pod from the Service endpoint list. By the time the pod actually starts shutting down, no new traffic is being sent to it.

Think of it like a store: turn off the "Open" sign, wait for customers inside to finish, then lock the door.

**2. Graceful shutdown in code** — finish what you started

```go
signal.Notify(quit, syscall.SIGTERM)
<-quit
srv.Shutdown(ctx)  // finish in-flight requests, then exit
```

Instead of dying on SIGTERM, the Go app stops accepting new requests and waits for current requests to finish (up to 15 seconds).

**3. `maxUnavailable: 0`, `maxSurge: 1`** — never go below capacity

```yaml
strategy:
  rollingUpdate:
    maxUnavailable: 0
    maxSurge: 1
```

Start a new pod first, wait until it's healthy, then kill the old one. At no point do you have fewer pods than you need.

**4. Readiness probe** — only send traffic to healthy pods

```yaml
readinessProbe:
  httpGet:
    path: /health
    port: 7000
```

K8s checks `/health` every few seconds. Only pods that respond 200 get traffic. New pods don't get traffic until they're actually ready.

**5. `terminationGracePeriodSeconds: 30`** — enough time to drain

Gives the pod 30 seconds total to finish shutting down. If it's still alive after 30s, K8s force-kills it.

### v3 — Graceful shutdown + client-side retry

Everything from v2, plus the Python downstream now retries transient failures:

- **Persistent HTTP client** — reuses connections across requests instead of creating a new client per request
- **tenacity retry** — up to 3 attempts, exponential backoff (0.5s → 1s → 2s) to avoid thundering herd
- **Retries on**: `ConnectError`, `ReadTimeout`, HTTP 500/502/503/504
- **Does NOT retry on**: 4xx errors (client errors aren't transient)
- **`retries` field in response** — every response now includes how many retries it took, so the load test can track it

This is a defense-in-depth approach: even if something slips past the server-side graceful shutdown (e.g. a brief DNS hiccup, a pod killed before endpoint removal propagates), the client recovers automatically.

## The Full Shutdown Sequence (v2/v3)

```
1. K8s creates a NEW pod (maxSurge: 1)
2. New pod passes readiness probe → starts getting traffic
3. K8s picks an OLD pod to kill
4. preStop hook fires → sleeps 5s
   (K8s removes pod from Service endpoints during this time)
   (no new traffic reaches this pod anymore)
5. SIGTERM sent to the app
6. App stops accepting new connections
7. In-flight requests finish (100-200ms each)
8. App exits cleanly
9. Zero dropped requests
```

## Project Structure

```
k8s-graceful-shutdown-demo/
├── go-upstream/              # Go Gin upstream service
│   ├── main.go               # /api/data, /health, /prestop endpoints
│   ├── go.mod
│   └── Dockerfile            # Multi-stage build, no shell wrapper
├── python-downstream/        # Python FastAPI downstream service
│   ├── main.py               # Calls Go upstream via httpx
│   ├── requirements.txt
│   └── Dockerfile
├── k8s/
│   ├── v1-no-graceful/       # K8s manifests WITHOUT graceful shutdown
│   │   ├── go-upstream.yaml
│   │   └── python-downstream.yaml
│   ├── v2-graceful/          # K8s manifests WITH graceful shutdown
│   │   ├── go-upstream.yaml
│   │   └── python-downstream.yaml
│   └── v3-graceful-retry/    # v2 + client-side retry in Python app
│       ├── go-upstream.yaml
│       └── python-downstream.yaml
├── loadtest/
│   └── test.js               # k6 load test — 15 VUs, 60 seconds
├── REPORT.md                 # Detailed test results and analysis
└── README.md
```

## Key Takeaways

1. **Always add a preStop hook** that sleeps 3-10 seconds. This is the single most important fix.
2. **Handle SIGTERM in your app** — catch the signal, stop accepting new requests, finish in-flight ones.
3. **Use `maxUnavailable: 0`** so you never go below your desired replica count during deploys.
4. **Add readiness probes** so K8s knows when a pod is actually ready for traffic.
5. **Set `terminationGracePeriodSeconds`** high enough to cover preStop + drain time.

Without these, every single deployment is a mini-outage.

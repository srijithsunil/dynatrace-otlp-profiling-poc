# Installation Guide — Dynatrace OTLP Continuous Profiling

## Which path is right for you?

| Your situation | Path |
|---|---|
| **Python app** — want the fastest integration | [Python SDK](#python-sdk-path) — 2 lines of code |
| **Java / Go / Node.js** — no code changes preferred | [Collector path](#collector-path-any-language) — point your profiler at the collector |
| **Starting from scratch** — want the full demo | [Path A — Docker Compose](#path-a--docker-compose-quick-start) |
| **Already have an OTel Collector** | [Path B — Existing Collector](#path-b--existing-otel-collector) |
| **Kubernetes** | [Path C — OTel Operator](#path-c--kubernetes--otel-operator) |

---

## Python SDK path

The fastest way to add profiling to a Python app. No OTel Collector needed —
profiles go directly from your app to Dynatrace.

### Install

```bash
# From this repo
pip install ./sdk/python

# Future (once published to PyPI)
pip install dt-otlp-profiler
```

### Integrate (2 lines)

```python
from dt_profiler import start_profiler
start_profiler()   # call once at startup, before your web server starts
```

### Configure via environment variables

```bash
export DT_ENDPOINT=https://<env>.live.dynatrace.com
export DT_API_TOKEN=<token>
export OTEL_SERVICE_NAME=my-service
export DEPLOYMENT_ENV=production
```

### Framework-specific guides

- **Flask** — [`examples/python-flask/`](examples/python-flask/)
- **Django / Gunicorn** — [`examples/python-django/`](examples/python-django/)
- **FastAPI** — see [`sdk/python/README.md`](sdk/python/README.md)

### Test locally first (no DT tenant)

```bash
docker compose -f docker-compose.infra.yml up -d
export DT_ENDPOINT=http://localhost:8888
export DT_API_TOKEN=dev
python your-app.py
# Validator logs show flame-graph summaries every 30s
```

---

## Collector path (any language)

Run the OTel Collector alongside your existing app — no code changes required.
Your existing profiler pushes pprof to the collector, which forwards OTLP profiles to Dynatrace.

### Step 1 — Start the infrastructure

```bash
cp .env.example .env   # set DT_ENDPOINT and DT_API_TOKEN
docker compose -f docker-compose.infra.yml up -d
```

### Step 2 — Push profiles from your app

**Go** (stdlib, zero dependencies):
```bash
# Push a 30s CPU profile to the collector's pprof receiver
curl http://your-go-app:6060/debug/pprof/profile?seconds=30 \
  | curl -X POST "http://localhost:4040/ingest?name=my-go-service" \
         -H "Content-Type: application/octet-stream" --data-binary @-
```
Full example: [`examples/go/`](examples/go/)

**Java** (async-profiler):
```bash
java -agentpath:/opt/async-profiler/lib/libasyncProfiler.so=start,event=cpu,interval=10ms \
     -jar myapp.jar
```
Full example: [`examples/java/`](examples/java/)

**Node.js** (OTel SDK):
```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
OTEL_SERVICE_NAME=my-node-service \
node --require ./instrumentation.js app.js
```
Full example: [`examples/nodejs/`](examples/nodejs/)

**Any language** (OTel SDK):
```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # OTLP HTTP
# or
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317   # OTLP gRPC
```

---

## Full installation guide (Docker Compose, existing Collector, Kubernetes)

This section covers adding continuous profiling to environments already using
OpenTelemetry. Three paths are covered:

- **[Path A](#path-a--docker-compose-quick-start)** — Docker Compose (new or existing)
- **[Path B](#path-b--existing-otel-collector)** — Add to an existing OTel Collector deployment
- **[Path C](#path-c--kubernetes--otel-operator)** — Kubernetes with the OTel Operator

---

## Prerequisites

### 1. Dynatrace API token

Create a token at **Dynatrace → Settings → Access tokens → Generate new token**.

Required scopes:

| Scope | Purpose |
|---|---|
| `openTelemetryTrace.ingest` | Required for OTLP ingestion |
| `metrics.ingest` | Required for OTLP metrics |
| `continuousProfilingStorage.ingest` | Required for OTLP profiles (enable if available in your environment) |

```
DT_ENDPOINT=https://<your-env-id>.live.dynatrace.com
DT_API_TOKEN=dt0c01.XXXXXXXXXX...
```

### 2. Runtime requirements

| Tool | Version | Notes |
|---|---|---|
| Docker | 24+ | Required for all paths |
| Docker Compose | v2 (plugin) | `docker compose` — note: no hyphen |
| kubectl | 1.28+ | Path C only |
| Helm | 3.12+ | Path C only |

### 3. Verify OTLP connectivity to Dynatrace

Before installing anything, confirm your environment can reach the DT OTLP endpoint:

```bash
curl -s -o /dev/null -w "%{http_code}" \
  -X POST \
  -H "Authorization: Api-Token $DT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"resourceProfiles":[]}' \
  "$DT_ENDPOINT/api/v2/otlp/v1/profiles"
```

Expected: `200` or `204`. A `401` means the token is wrong or missing the scope. A `404` means the profiles endpoint is not yet available on your tenant — use the [validator mode](#development-mode-no-dt-tenant) to test the payload shape locally.

---

## Path A — Docker Compose (quick start)

Use this if you are starting from scratch or want the fastest path to a working demo.

### Step 1 — Clone and configure

```bash
git clone <this-repo>
cd dynatrace-otlp-profiling-poc

cp .env.example .env
```

Edit `.env`:

```dotenv
DT_ENDPOINT=https://your-env-id.live.dynatrace.com
DT_API_TOKEN=dt0c01.XXXXXXXXXX
```

### Step 2 — Choose your mode

**Production mode** (profiles → real Dynatrace tenant):

The `.env` values above are sufficient. The `sample-app` sends profiles directly to
`$DT_ENDPOINT/api/v2/otlp/v1/profiles`.

**Development mode** (profiles → local validator, no DT account needed):

```dotenv
DT_ENDPOINT=http://validator:8888
DT_API_TOKEN=local-dev
```

### Step 3 — Start the stack

```bash
docker compose up --build
```

Four containers start:

| Container | Port | Role |
|---|---|---|
| `sample-app` | 8080 | Flask app with CPU hotspots + built-in profiler |
| `load-generator` | — | Continuously drives traffic to sample-app |
| `otel-collector` | 4317, 4318, 4040 | Language-agnostic profiling pipeline |
| `validator` | 8888 | Mock DT endpoint + hotspot decoder (dev mode) |

### Step 4 — Verify

**In development mode** — watch the validator container logs:

```
docker compose logs -f validator
```

After 30 seconds you should see a hotspot summary:

```
──────────────────────────────────────────────────────────────────────
  SERVICE   : profiling-demo-app
  WINDOW    : 2026-06-08T17:00:00+00:00
  DURATION  : 30.0s   PERIOD: 10.0ms
  SAMPLES   : 2847

  FUNCTION                       FILE                  CPU   BAR
  ------------------------------ -------------------- ------   ─────────────────────────────────────────
  fibonacci                      app.py               8420ms   ████████████████████████████████████████
  repeated_sort                  app.py               3120ms   ██████████████▊
  matrix_multiply                app.py               2860ms   █████████████▌
  sieve_of_eratosthenes          app.py               1540ms   ███████▎
──────────────────────────────────────────────────────────────────────
```

**In production mode** — query the Dynatrace API:

```bash
curl -s -H "Authorization: Api-Token $DT_API_TOKEN" \
  "$DT_ENDPOINT/api/v2/bizevents/search" \
  | jq '.results[] | select(."event.provider" == "otel-profiler")'
```

Or open **Dynatrace → Distributed Traces → Profiling** to see the flame graph.

### Step 5 — Stop

```bash
docker compose down
```

---

## Path B — Existing OTel Collector

Use this if your environment already runs an OTel Collector and you want to add the
profiling pipeline without replacing anything.

### Step 1 — Add the profiling receiver to your Collector config

Locate your existing `otel-collector-config.yaml` and add the following blocks.
**Do not replace existing pipelines** — append to them.

```yaml
# ── Add to receivers: ──────────────────────────────────────────────────────────
receivers:
  # ... your existing receivers ...

  # Accepts pprof profiles pushed by any runtime
  pyroscope:
    endpoint: 0.0.0.0:4040

# ── Add to exporters: ──────────────────────────────────────────────────────────
exporters:
  # ... your existing exporters ...

  otlphttp/dynatrace-profiles:
    endpoint: "${DT_ENDPOINT}/api/v2/otlp"
    headers:
      Authorization: "Api-Token ${DT_API_TOKEN}"

# ── Add to service.pipelines: ─────────────────────────────────────────────────
service:
  pipelines:
    # ... your existing pipelines ...

    profiles:
      receivers:  [otlp, pyroscope]
      processors: [batch]
      exporters:  [otlphttp/dynatrace-profiles]
```

> **Note:** The `pyroscope` receiver is in `otel/opentelemetry-collector-contrib`.
> If you are running the core `otel/opentelemetry-collector`, swap to the contrib
> image or the profiles pipeline won't have the pprof ingest receiver.

### Step 2 — Expose the pprof port

If your Collector runs in Docker, expose port 4040:

```yaml
# docker-compose.yml
services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.101.0
    ports:
      - "4040:4040"   # add this
```

If it runs in Kubernetes, add to the Collector Service:

```yaml
# collector-service.yaml
spec:
  ports:
    - name: pprof
      port: 4040
      targetPort: 4040
```

### Step 3 — Add profiling to your application

**Option 1 — Built-in Python profiler (no external dependency)**

Copy `sample-app/profiler/` into your Python project:

```
your-app/
├── profiler/
│   ├── __init__.py
│   ├── sampler.py
│   └── otlp_exporter.py
└── app.py
```

In your app entry point:

```python
import os, time, threading
from profiler import StackSampler, DynatraceOTLPProfileExporter

sampler  = StackSampler(interval_ms=10)
exporter = DynatraceOTLPProfileExporter(
    endpoint=os.environ["DT_ENDPOINT"],
    api_token=os.environ["DT_API_TOKEN"],
    service_name=os.environ.get("OTEL_SERVICE_NAME", "my-service"),
)

def flush_loop():
    while True:
        time.sleep(30)
        samples, start_ns, dur_ns = sampler.flush()
        exporter.export(samples, start_ns, dur_ns)

sampler.start()
threading.Thread(target=flush_loop, daemon=True).start()
```

**Option 2 — pyroscope SDK (Java / Go / Ruby / .NET)**

Install the pyroscope SDK for your language and point it at the Collector's pprof port.

*Java (Maven):*

```xml
<dependency>
  <groupId>io.pyroscope</groupId>
  <artifactId>agent</artifactId>
  <version>0.14.0</version>
</dependency>
```

```java
PyroscopeAgent.start(
    new Config.Builder()
        .setServerAddress("http://otel-collector:4040")
        .setApplicationName("my-java-service")
        .setProfilingEvent(EventType.ITIMER)
        .build()
);
```

*Go (uses stdlib, zero SDK):*

```go
import _ "net/http/pprof"

// In a goroutine, periodically push to the Collector
go func() {
    for range time.Tick(30 * time.Second) {
        resp, _ := http.Get("http://localhost:6060/debug/pprof/profile?seconds=30")
        data, _ := io.ReadAll(resp.Body)
        http.Post("http://otel-collector:4040/ingest",
            "application/octet-stream", bytes.NewReader(data))
    }
}()
```

*Node.js:*

```bash
npm install @pyroscope/nodejs
```

```js
const Pyroscope = require('@pyroscope/nodejs');
Pyroscope.init({ serverAddress: 'http://otel-collector:4040',
                 appName: 'my-node-service' });
Pyroscope.start();
```

### Step 4 — Set environment variables

For Docker, add to your service's environment block:

```yaml
environment:
  - DT_ENDPOINT=https://your-env-id.live.dynatrace.com
  - DT_API_TOKEN=dt0c01.XXXXXXXXXX
  - OTEL_SERVICE_NAME=my-service
```

For Kubernetes, store credentials in a Secret and reference them:

```yaml
# secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: dynatrace-otlp-profiling
  namespace: my-namespace
type: Opaque
stringData:
  DT_ENDPOINT: "https://your-env-id.live.dynatrace.com"
  DT_API_TOKEN: "dt0c01.XXXXXXXXXX"
```

```yaml
# deployment.yaml  — envFrom reference
envFrom:
  - secretRef:
      name: dynatrace-otlp-profiling
```

### Step 5 — Verify the Collector is receiving profiles

```bash
# Check Collector logs for the profiles pipeline
docker logs otel-collector 2>&1 | grep -i "profile"

# Hit the pprof ingest endpoint directly
curl -s -o /dev/null -w "%{http_code}" \
  -X POST http://otel-collector:4040/ingest \
  -H "Content-Type: application/octet-stream" \
  --data-binary ""
# Expected: 200 or 204
```

---

## Path C — Kubernetes + OTel Operator

Use this for production Kubernetes environments that use the OpenTelemetry Operator
to manage Collector instances.

### Step 1 — Install the OTel Operator (if not already installed)

```bash
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update

helm install opentelemetry-operator open-telemetry/opentelemetry-operator \
  --namespace opentelemetry-operator-system \
  --create-namespace \
  --set "manager.collectorImage.repository=otel/opentelemetry-collector-contrib"
```

Verify:

```bash
kubectl get pods -n opentelemetry-operator-system
# NAME                                      READY   STATUS
# opentelemetry-operator-xxxxxxxxx-xxxxx    1/1     Running
```

### Step 2 — Create the Dynatrace credentials Secret

```bash
kubectl create secret generic dynatrace-otlp-profiling \
  --from-literal=DT_ENDPOINT="https://your-env-id.live.dynatrace.com" \
  --from-literal=DT_API_TOKEN="dt0c01.XXXXXXXXXX" \
  --namespace default
```

### Step 3 — Deploy the OpenTelemetryCollector resource

```yaml
# otel-collector-profiling.yaml
apiVersion: opentelemetry.io/v1alpha1
kind: OpenTelemetryCollector
metadata:
  name: profiling-collector
  namespace: default
spec:
  image: otel/opentelemetry-collector-contrib:0.101.0
  mode: deployment
  envFrom:
    - secretRef:
        name: dynatrace-otlp-profiling
  ports:
    - name: pprof
      port: 4040
    - name: otlp-grpc
      port: 4317
    - name: otlp-http
      port: 4318
  config: |
    receivers:
      pyroscope:
        endpoint: 0.0.0.0:4040
      otlp:
        protocols:
          grpc:
            endpoint: 0.0.0.0:4317
          http:
            endpoint: 0.0.0.0:4318

    processors:
      resourcedetection:
        detectors: [k8snode, env]
        timeout: 5s
      batch:
        timeout: 10s

    exporters:
      otlphttp/dynatrace:
        endpoint: "${DT_ENDPOINT}/api/v2/otlp"
        headers:
          Authorization: "Api-Token ${DT_API_TOKEN}"

    service:
      pipelines:
        profiles:
          receivers:  [otlp, pyroscope]
          processors: [resourcedetection, batch]
          exporters:  [otlphttp/dynatrace]
        traces:
          receivers:  [otlp]
          processors: [resourcedetection, batch]
          exporters:  [otlphttp/dynatrace]
        metrics:
          receivers:  [otlp]
          processors: [resourcedetection, batch]
          exporters:  [otlphttp/dynatrace]
```

```bash
kubectl apply -f otel-collector-profiling.yaml
kubectl get pods -l app.kubernetes.io/component=opentelemetry-collector
```

### Step 4 — Deploy the sample application

```yaml
# sample-app-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: profiling-demo-app
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: profiling-demo-app
  template:
    metadata:
      labels:
        app: profiling-demo-app
    spec:
      containers:
        - name: app
          image: your-registry/profiling-demo-app:latest
          ports:
            - containerPort: 8080
          envFrom:
            - secretRef:
                name: dynatrace-otlp-profiling
          env:
            - name: OTEL_SERVICE_NAME
              value: "profiling-demo-app"
            - name: OTEL_SERVICE_VERSION
              value: "1.0.0"
            - name: DEPLOYMENT_ENV
              value: "production"
            - name: PROFILER_SAMPLE_INTERVAL_MS
              value: "10"
            - name: PROFILER_FLUSH_INTERVAL_S
              value: "30"
```

```bash
kubectl apply -f sample-app-deployment.yaml
kubectl rollout status deployment/profiling-demo-app
```

### Step 5 — Use the OTel Operator's Instrumentation CRD (optional)

If your apps don't have the built-in profiler, the Operator's `Instrumentation`
resource can auto-inject the profiling agent via an init container:

```yaml
# instrumentation.yaml
apiVersion: opentelemetry.io/v1alpha1
kind: Instrumentation
metadata:
  name: profiling-instrumentation
  namespace: default
spec:
  exporter:
    endpoint: http://profiling-collector-collector:4317
  propagators:
    - tracecontext
    - baggage
  python:
    env:
      - name: PROFILER_FLUSH_INTERVAL_S
        value: "30"
```

Then annotate any Pod to inject profiling automatically:

```yaml
metadata:
  annotations:
    instrumentation.opentelemetry.io/inject-python: "profiling-instrumentation"
```

### Step 6 — Verify on Kubernetes

```bash
# Check Collector is running
kubectl get pods -l app.kubernetes.io/name=profiling-collector

# Tail Collector logs
kubectl logs -l app.kubernetes.io/name=profiling-collector -f | grep -i profile

# Port-forward the sample app and hit an endpoint
kubectl port-forward deployment/profiling-demo-app 8080:8080
curl http://localhost:8080/fibonacci?n=34

# Check profiles are reaching Dynatrace
curl -s -H "Authorization: Api-Token $DT_API_TOKEN" \
  "$DT_ENDPOINT/api/v2/metrics/query?metricSelector=dt.profiling*" \
  | jq '.resolution'
```

---

## Development mode (no DT tenant)

If you do not yet have access to a Dynatrace environment with OTLP profiles enabled,
use the local validator to confirm the payload is correct.

### Run the validator standalone

```bash
cd dynatrace-otlp-profiling-poc
docker compose up --build validator
```

The validator exposes `POST /api/v2/otlp/v1/profiles` on port 8888. Point your
exporter at `http://localhost:8888` instead of the DT endpoint.

### Inspect received profiles via the API

```bash
# Last 5 profile windows received
curl -s http://localhost:8888/api/v2/otlp/v1/profiles | jq '.profiles[].summary.hotspots[:3]'
```

### Send a test profile manually

```bash
curl -s -X POST http://localhost:8888/api/v2/otlp/v1/profiles \
  -H "Content-Type: application/json" \
  -d '{
    "resourceProfiles": [{
      "resource": {
        "attributes": [{"key": "service.name", "value": {"stringValue": "test-service"}}]
      },
      "scopeProfiles": [{
        "profiles": [{
          "sampleType":    [{"type": "1", "unit": "2"}],
          "sample":        [{"locationId": ["1"], "value": ["1500000000"]}],
          "location":      [{"id": "1", "line": [{"functionId": "1", "line": "42"}]}],
          "function":      [{"id": "1", "name": "3", "filename": "4", "startLine": "42"}],
          "stringTable":   ["", "cpu", "nanoseconds", "my_hot_function", "app.py"],
          "timeNanos":     "1717862400000000000",
          "durationNanos": "30000000000",
          "period":        "10000000"
        }]
      }]
    }]
  }'
```

Expected validator output:

```
──────────────────────────────────────────────────────────────────────
  SERVICE   : test-service
  DURATION  : 30.0s   PERIOD: 10.0ms
  SAMPLES   : 0

  FUNCTION                       FILE                  CPU   BAR
  my_hot_function                app.py               1500ms   ████████████████████████████████████████
──────────────────────────────────────────────────────────────────────
```

---

## Environment variable reference

| Variable | Default | Description |
|---|---|---|
| `DT_ENDPOINT` | _(required)_ | Dynatrace tenant URL |
| `DT_API_TOKEN` | _(required)_ | API token with ingest scopes |
| `OTEL_SERVICE_NAME` | `profiling-demo-app` | Service name shown in DT |
| `OTEL_SERVICE_VERSION` | `1.0.0` | Service version attribute |
| `DEPLOYMENT_ENV` | `poc` | `deployment.environment` attribute |
| `PROFILER_SAMPLE_INTERVAL_MS` | `10` | Stack snapshot frequency in ms |
| `PROFILER_FLUSH_INTERVAL_S` | `30` | Profile window / export interval in seconds |

---

## Troubleshooting

**Profiles not appearing in Dynatrace**

1. Confirm the `/api/v2/otlp/v1/profiles` endpoint exists on your tenant:
   ```bash
   curl -I -H "Authorization: Api-Token $DT_API_TOKEN" \
     "$DT_ENDPOINT/api/v2/otlp/v1/profiles"
   ```
   `405 Method Not Allowed` means the endpoint exists (just needs a POST).
   `404` means the feature is not yet enabled — use development mode.

2. Check the API token has `continuousProfilingStorage.ingest` scope.

3. Check the exporter logs in the sample-app container:
   ```bash
   docker compose logs sample-app | grep -i "export\|profile\|error"
   ```

**OTel Collector not receiving pprof**

```bash
# Check port 4040 is accessible
curl -v -X POST http://localhost:4040/ingest \
  -H "Content-Type: application/octet-stream" \
  --data-binary ""

# Check the Collector config loaded correctly
docker compose logs otel-collector | grep -E "error|profile|receiver"
```

**`pyroscope` receiver not found in Collector**

You are running the core image (`otel/opentelemetry-collector`). Switch to the
contrib image which includes all community receivers:

```yaml
image: otel/opentelemetry-collector-contrib:0.101.0
```

**High CPU from the profiler itself**

The profiler adds ~1-2% CPU overhead at 10ms interval on typical workloads.
Increase the interval to reduce overhead:

```dotenv
PROFILER_SAMPLE_INTERVAL_MS=50    # 5x less overhead, still accurate for hotspots
```

**Profile windows look empty (0 samples)**

The sampler skips its own threads and Python threading internals. If the app
is idle (no load), there are no user-code frames to capture. Run the load
generator or hit an endpoint before the 30-second flush window closes:

```bash
curl http://localhost:8080/fibonacci?n=34
curl http://localhost:8080/matrix?size=100
```

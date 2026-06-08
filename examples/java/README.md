# Java integration — async-profiler + OTel Collector

Zero code changes required. async-profiler runs as a JVM agent and pushes
pprof profiles to the OTel Collector, which forwards them to Dynatrace.

## Prerequisites

- OTel Collector running (see `docker-compose.infra.yml`)
- [async-profiler](https://github.com/async-profiler/async-profiler/releases) downloaded

## Step 1 — Start the infrastructure

```bash
# From the repo root
docker compose -f docker-compose.infra.yml up -d
```

## Step 2 — Add the JVM agent

```bash
# Download async-profiler (Linux x64)
curl -L https://github.com/async-profiler/async-profiler/releases/download/v3.0/async-profiler-3.0-linux-x64.tar.gz \
  | tar -xz

# Run your app with the agent — profiles sent every 30s to the collector
java \
  -agentpath:/path/to/async-profiler/lib/libasyncProfiler.so=start,event=cpu,interval=10ms \
  -jar myapp.jar
```

### Spring Boot (application.properties)

```properties
spring.application.name=my-spring-app
```

```bash
java \
  -agentpath:/opt/async-profiler/lib/libasyncProfiler.so=start,event=cpu,interval=10ms,jfr,file=/tmp/profile.jfr \
  -DOTEL_SERVICE_NAME=my-spring-app \
  -jar target/myapp.jar
```

Then push the JFR to the collector:

```bash
# Push a 30s CPU profile to the collector's pprof receiver
while true; do
  curl -sS -X POST http://localhost:4040/ingest?name=${OTEL_SERVICE_NAME} \
    -H "Content-Type: application/octet-stream" \
    --data-binary @/tmp/profile.jfr
  sleep 30
done
```

## Step 3 — Verify

```bash
# Check the validator output (dev mode)
docker compose -f docker-compose.infra.yml logs -f validator
```

## Docker example

```dockerfile
FROM eclipse-temurin:21-jre
WORKDIR /app

# Download async-profiler
RUN apt-get update && apt-get install -y curl && \
    curl -L https://github.com/async-profiler/async-profiler/releases/download/v3.0/async-profiler-3.0-linux-x64.tar.gz \
    | tar -xz -C /opt/

COPY target/myapp.jar .

CMD ["java", \
     "-agentpath:/opt/async-profiler-3.0-linux-x64/lib/libasyncProfiler.so=start,event=cpu,interval=10ms", \
     "-jar", "myapp.jar"]
```

## OTel Java agent (alternative — no code changes)

The OpenTelemetry Java agent supports profiling via the Pyroscope appender:

```bash
curl -L https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/download/v2.5.0/opentelemetry-javaagent.jar \
  -o otel-agent.jar

java \
  -javaagent:otel-agent.jar \
  -DOTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
  -DOTEL_SERVICE_NAME=my-spring-app \
  -jar myapp.jar
```

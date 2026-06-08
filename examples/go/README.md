# Go integration — zero dependencies

Uses the Go standard library `net/http/pprof` package to capture CPU profiles
and push them to the OTel Collector every 60 seconds. No external SDK required.

## Quick start

```bash
# Start the infra
docker compose -f ../../docker-compose.infra.yml up -d

# Run the example app
DT_COLLECTOR_URL=http://localhost:4040 \
OTEL_SERVICE_NAME=my-go-service \
go run main.go
```

## Adding to an existing Go app

1. Import the pprof package (blank import — registers HTTP handlers):

```go
import _ "net/http/pprof"
```

2. Start the pprof debug server on a non-public port:

```go
go func() { log.Fatal(http.ListenAndServe("localhost:6060", nil)) }()
```

3. Copy `pushProfiles()` from `main.go` into your app and call it in a goroutine.

## OTel Go SDK (alternative — richer data)

```bash
go get go.opentelemetry.io/otel \
       go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp
```

```go
import (
    "go.opentelemetry.io/otel/exporters/otlp/otlpprofile/otlpprofilehttp"
    sdkprofile "go.opentelemetry.io/contrib/profiling/sdk"
)

exporter, _ := otlpprofilehttp.New(ctx,
    otlpprofilehttp.WithEndpoint("http://localhost:4318"),
)
provider := sdkprofile.NewProfilerProvider(sdkprofile.WithExporter(exporter))
```

Note: OTel Go profiling SDK is experimental as of 2025 — check
[opentelemetry-go-contrib](https://github.com/open-telemetry/opentelemetry-go-contrib)
for the latest status.

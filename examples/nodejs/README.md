# Node.js integration — OTel SDK

Uses the OpenTelemetry JS SDK with the profiling extension.

## Install

```bash
npm install @opentelemetry/sdk-node \
            @opentelemetry/exporter-trace-otlp-http \
            @opentelemetry/auto-instrumentations-node
```

## Quick start (`instrumentation.js`)

Create this file and load it before your app code:

```javascript
'use strict';

const { NodeSDK } = require('@opentelemetry/sdk-node');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { getNodeAutoInstrumentations } = require('@opentelemetry/auto-instrumentations-node');

const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter({
    url: `${process.env.DT_COLLECTOR_URL || 'http://localhost:4318'}/v1/traces`,
    headers: {
      Authorization: `Api-Token ${process.env.DT_API_TOKEN}`,
    },
  }),
  instrumentations: [getNodeAutoInstrumentations()],
  serviceName: process.env.OTEL_SERVICE_NAME || 'unknown-node-service',
});

sdk.start();
process.on('SIGTERM', () => sdk.shutdown());
```

```bash
node --require ./instrumentation.js app.js
```

## CPU profiling via V8 inspector (alternative)

For CPU flame graphs, use Node's built-in V8 inspector to capture profiles
and push them to the OTel Collector:

```javascript
const v8Profiler = require('v8-profiler-next');
const fetch = require('node-fetch');

async function pushProfile(durationMs = 30000) {
  v8Profiler.startProfiling('cpu', true);
  await new Promise(resolve => setTimeout(resolve, durationMs));
  const profile = v8Profiler.stopProfiling('cpu');
  profile.delete();

  const collectorUrl = process.env.DT_COLLECTOR_URL || 'http://localhost:4040';
  const serviceName = process.env.OTEL_SERVICE_NAME || 'unknown';

  await fetch(`${collectorUrl}/ingest?name=${serviceName}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(profile),
  });
}

// Push every 60 seconds
setInterval(() => pushProfile(30000).catch(console.error), 60000);
```

```bash
npm install v8-profiler-next node-fetch
DT_COLLECTOR_URL=http://localhost:4040 node app.js
```

## Environment variables

```bash
export DT_COLLECTOR_URL=http://localhost:4318   # OTel Collector OTLP HTTP
export DT_API_TOKEN=<token>
export OTEL_SERVICE_NAME=my-node-service
```

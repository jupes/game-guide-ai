# Service and UI metrics standard

This is the canonical contract for runtime metrics produced by the Python service, browser UI,
and Playwright. Code validates the contract in `service/metrics.py`; producers must not invent
names, units, labels, or categorical values outside that catalog.

## Event shape

Metrics travel in batches of 1–50 points. Every level rejects unknown fields, numeric values must
be finite and non-negative, and labels/categories are bounded.

```json
{
  "points": [{
    "name": "ui.web_vital.lcp_ms",
    "kind": "numeric",
    "unit": "ms",
    "value": 1840.5,
    "labels": {
      "environment": "production",
      "release": "abc123",
      "route_template": "/",
      "browser_family": "chromium"
    }
  }]
}
```

## Catalog

| Name | Kind | Unit / values | Producer |
|---|---|---|---|
| `service.chat.duration_ms` | numeric | `ms` | `/chat` middleware |
| `service.chat.gate.answerable` | boolean | `boolean` | chat handler |
| `service.chat.error` | boolean | `boolean` | `/chat` middleware |
| `service.chat.error_category` | categorical | `validation`, `dependency`, `handler`, `unknown` | `/chat` middleware |
| `ui.web_vital.ttfb_ms` | numeric | `ms` | browser |
| `ui.web_vital.fcp_ms` | numeric | `ms` | browser |
| `ui.web_vital.lcp_ms` | numeric | `ms` | browser |
| `ui.web_vital.cls` | numeric | unitless `ratio` | browser |
| `ui.interaction.chat_round_trip_ms` | numeric | `ms` | browser chat client |
| `ui.interaction.chat_outcome` | categorical | `success`, `http_error`, `network_error`, `aborted` | browser chat client |
| `ui.client.error_count` | numeric | `count` | browser error collector |

Langfuse observations remain the source of generation latency, model name, tokens, and cost.
Trace count remains the request-volume source. The explicit runtime points above add bounded
gate/error/UI series without replacing those native observations.

## Labels

| Label | Allowed values | Applies to |
|---|---|---|
| `environment` | `local`, `test`, `ci`, `staging`, `production` | all |
| `release` | 1–64 safe identifier characters (`A-Z`, `a-z`, digits, `_`, `.`, `-`) | all |
| `mode` | `sage`, `spell`, `rules`, `gm` | service and chat interaction |
| `route_template` | `/`, `/chat`, `/metrics/ui` | all |
| `browser_family` | `chromium`, `firefox`, `webkit`, `other` | UI |

Never record prompts, responses, conversation or user IDs, attachment names/content, stack traces,
filenames, arbitrary URLs/query strings, IP addresses, or free-form exception text. Details needed
for debugging belong in access-controlled structured logs, not metric values or labels.

## Transport, storage, and failure behavior

- Service producers call an injected server-side metrics sink.
- Browser producers send same-origin batches to `POST /metrics/ui`; the browser never receives a
  Langfuse secret.
- The browser buffers at most 50 points per request, prefers `sendBeacon` during page exit, and
  falls back to a keepalive fetch. Web Vital collection uses buffered performance observers.
- The service validates the batch against this allowlist, then records numeric, boolean, or
  categorical Langfuse scores. Invalid input receives `422`.
- Telemetry is off/no-op without configured credentials. Sink or transport failures are logged as
  bounded warnings and never fail chat, navigation, or rendering.
- Langfuse is the durable store and trend dashboard. No second metrics database is introduced.
- This repo pins `langfuse>=3,<4`. Live summaries must extend the existing legacy Metrics API
  query shape, including `timeDimension`; SDK-v4/v2-client migration is separate work.
- Retention is deployment-defined in Langfuse. Changing retention requires an operational review;
  application code must not assume a retention period.

The service owner owns service points and sink behavior, the UI owner owns browser collection, and
the release/observability owner owns the catalog and dashboard. Any catalog change updates this
document, `service/metrics.py`, producer tests, runtime-summary fixtures, and Playwright budgets
together.

## Worked service example

```json
{
  "points": [{
    "name": "service.chat.gate.answerable",
    "kind": "boolean",
    "unit": "boolean",
    "value": true,
    "labels": {
      "environment": "production",
      "release": "abc123",
      "mode": "sage",
      "route_template": "/chat"
    }
  }]
}
```

The handler validates this point and records it server-side as a Langfuse boolean score. The
dashboard trends its true-rate by time and the bounded labels remain score metadata for
record-level inspection. Failure to record is fail-open.

## Worked UI example

The LCP event at the top of this document is queued by the browser, posted to `/metrics/ui`, and
recorded server-side as a numeric Langfuse score. The runtime dashboard and Playwright artifact use
the exact name `ui.web_vital.lcp_ms` and unit `ms`, so production trends and CI budgets are directly
comparable.

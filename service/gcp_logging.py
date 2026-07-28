"""
Structured Cloud Logging entries that can be joined to their request log.

Cloud Run writes an application log line and a *request* log entry as two
separate entries in two separate logs: our `log.warning(...)` lands in the
container log (`textPayload`), while `httpRequest.remoteIp` — the address Google
actually observed — lives on `run.googleapis.com/requests`. Filtering on the
message text therefore returns entries that have no `httpRequest` field at all,
which is why "read the log and compare" silently proved nothing.

The join key Cloud Logging uses is the trace. Cloud Run puts one on every
request in `X-Cloud-Trace-Context`, and an entry carrying
`logging.googleapis.com/trace` is correlated with the request entry that shares
it. So: emit JSON on stdout (Cloud Run parses it into `jsonPayload`) with that
field set, and the two halves can be queried together.

Off Cloud Run this does nothing and the caller falls back to ordinary logging —
local runs and tests keep plain readable output.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

# Cloud Run sets K_SERVICE in every container. Its absence means local/dev/test,
# where JSON-on-stdout would just make the output worse.
_ON_CLOUD_RUN = "K_SERVICE"

# `X-Cloud-Trace-Context: TRACE_ID/SPAN_ID;o=1`, TRACE_ID being 32 hex digits.
# The header is caller-writable, and its value is about to be interpolated into
# a log field, so it is matched exactly rather than trusted.
_TRACE_ID = re.compile(r"^[0-9a-fA-F]{32}$")


def _trace_id(request: Any) -> str | None:
    header = request.headers.get("x-cloud-trace-context", "")
    candidate = header.split("/", 1)[0].strip()
    return candidate if _TRACE_ID.match(candidate) else None


def _clean(value: Any) -> Any:
    """Keep caller-influenced strings from forging log structure."""
    if isinstance(value, str):
        return value.replace("\r", " ").replace("\n", " ")[:256]
    return value


def emit(severity: str, message: str, request: Any, **fields: Any) -> bool:
    """Write one structured entry. Returns False when not on Cloud Run, so the
    caller can fall back to the ordinary logger."""
    if not os.getenv(_ON_CLOUD_RUN):
        return False

    entry: dict[str, Any] = {
        "severity": severity,
        "message": message,
        **{k: _clean(v) for k, v in fields.items()},
    }
    trace = _trace_id(request)
    if trace:
        # The full resource name is what makes Cloud Logging group this entry
        # with its request entry. Without a known project we still emit the bare
        # id — searchable, just not auto-correlated in the console.
        project = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
        entry["logging.googleapis.com/trace"] = (
            f"projects/{project}/traces/{trace}" if project else trace
        )
    print(json.dumps(entry), file=sys.stdout, flush=True)
    return True

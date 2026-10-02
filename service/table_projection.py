"""The one builder of what a table client is shown (agent-forge-harness-1kg.7.2
PR-2; SEC-14, SEC-15, ADR 4).

`build` takes a document type, the **sealed** version's data and the disclosure's
mask, and returns a `TableProjection`, or `None`. It is the only place field
values are copied toward a table: it reads the masked keys and nothing else, in
mask order, and `TableProjection` (the contract) is the second half of the
guarantee, because it refuses any shape that is not a revealable key of the
type holding a present value of that key's kind.

It is built on each read, from the pinned sealed version, not at Confirm
(ID-16): a sealed version is immutable, and `reveals.check_mask` and the
contract share `_PROJECTION_VALUE`, so what Confirm admitted this builds. The
one exception is an asset key, which `reveals.TABLE_ASSETS_SERVED` keeps out of
a Confirm and which this builder therefore never reaches while it is False.

It never raises, and what it logs is a closed label: the type, when it is a
type the contract knows, and `unknown` otherwise. Never a key, a value or an id.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from .workbench_contracts import ContentKind, DocumentTypeId, TableProjection

log = logging.getLogger(__name__)


def build(doc_type: str, data: Mapping[str, Any], mask: Sequence[str]) -> TableProjection | None:
    """The projection of `data` through `mask`, or `None` (and a closed log
    line) when it cannot be expressed. A key `data` does not hold is a failure,
    not a skipped field: a half-shown mask is not what the GM confirmed."""
    try:
        kind = DocumentTypeId(doc_type)
    except ValueError:
        log.warning("table projection refused (type=%s)", "unknown")
        return None
    try:
        return TableProjection.model_validate(
            {
                "content_kind": ContentKind.DOCUMENT,
                "type": kind,
                "fields": [{"key": key, "value": data[key]} for key in mask],
            }
        )
    except (ValidationError, KeyError, TypeError):
        log.warning("table projection refused (type=%s)", kind.value)
        return None

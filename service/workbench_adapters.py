"""Schema adapters for a document type's field definitions (``1kg.5.3``).

A type's ``type_version`` says which revision of its field definitions a stored
document's ``data`` conforms to (``DOC_TYPE_VERSION``). All eight types are at
version 1 and nothing is released, so **this registry is empty**. What it
provides is the frame: when a later bead makes an incompatible change — renames
a key, splits one field into two, changes what a key means — it bumps that
type's version and registers the step that carries a stored document forward.

The frame is here rather than in ``workbench_contracts.py`` for two reasons: the
contract module is edited in parallel by the reveal family (``1kg.1.6``), and a
migration is a different concern from validating a payload. ``upgrade`` walks
one step at a time and **refuses rather than guesses** when a step is missing,
because a document half-migrated by a skipped step is worse than one that will
not load.

What this module does *not* do is touch eligibility rows. That storage is
``agent-forge-harness-1ir.2.1``'s. But the rule an adapter author must follow is
recorded on :meth:`AdapterRegistry.register`, because the adapter is where the
mistake would be made.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from service.workbench_contracts import DOC_TYPE_VERSION, DocumentTypeId

#: One step: the ``data`` of a document at version *n*, returned at *n + 1*.
Adapter = Callable[[Mapping[str, Any]], dict[str, Any]]

#: Requirement 8 (F-11): the range every version this module takes must lie in.
VERSION_MIN = 1
VERSION_MAX = 1000


def _a_version(value: object, what: str) -> int:
    """A version is an integer from 1 to 1000: never a bool, a float or a string.

    Checked explicitly, so ``0.5`` or a ``NaN`` is a :class:`ValueError` here
    rather than a ``TypeError`` out of ``range`` - and in the TypeScript twin,
    rather than a ``NaN`` that every comparison answers ``false`` to and so walks
    straight through as "already current". The message names the argument and
    never the value it was given.
    """
    if isinstance(value, bool) or not isinstance(value, int) or not VERSION_MIN <= value <= VERSION_MAX:
        raise ValueError(f"a version starts at 1: {what} must be an integer from {VERSION_MIN} to {VERSION_MAX}")
    return value


class AdapterError(LookupError):
    """No path from the stored version to the current one. The message names the
    type and the step that is missing, never the document's content (X-7)."""


class AdapterRegistry:
    """``(type, from_version) -> adapter``, walked one step at a time.

    Tests build their own instance with a fixture type, so exercising the frame
    never registers anything on the one the service uses.
    """

    def __init__(self) -> None:
        self._steps: dict[tuple[DocumentTypeId, int], Adapter] = {}

    def register(self, doc_type: DocumentTypeId, from_version: int, adapter: Adapter) -> None:
        """Record the step from ``from_version`` to ``from_version + 1``.

        **An adapter that moves text from one key to another must reset the
        destination to ``unclassified``** (ED-24). A class was granted for the
        text as it sat under the old key; carrying it across would grant it for
        text the classifier never saw, which is a widening no transaction
        covers. The same rule deletes the rows of keys that left the type. The
        eligibility storage itself is ``agent-forge-harness-1ir.2.1``'s — this
        docstring is the contract an adapter author is held to.

        **An adapter must not mutate nested values - it receives a shallow copy**,
        so a list or a mapping inside ``data`` is still the caller's.
        """
        _a_version(from_version, "from_version")
        key = (doc_type, from_version)
        if key in self._steps:
            raise ValueError(f"{doc_type.value}: a step from version {from_version} is already registered")
        self._steps[key] = adapter

    def step(self, doc_type: DocumentTypeId, from_version: int) -> Adapter | None:
        return self._steps.get((doc_type, from_version))

    def can_upgrade(self, doc_type: DocumentTypeId, from_version: int, *, to: int | None = None) -> bool:
        """Whether every step exists, without running any of them. A version that
        is not a version is a :class:`ValueError`, not an answer."""
        _a_version(from_version, "from_version")
        target = DOC_TYPE_VERSION[doc_type] if to is None else _a_version(to, "to")
        return all(self.step(doc_type, version) is not None for version in range(from_version, target))

    def upgrade(
        self, doc_type: DocumentTypeId, from_version: int, data: Mapping[str, Any], *, to: int | None = None
    ) -> tuple[int, dict[str, Any]]:
        """Carry ``data`` forward to the type's current version, one step at a time.

        Returns the version reached and the new ``data``. Raises
        :class:`AdapterError` when a step is missing and :class:`ValueError`
        when asked to go backwards or given a version that is not an integer
        from 1 to 1000 - never a partially-applied result.

        **The caller validates the adapted result with ``check_fields`` before
        storing it**: an adapter is code, and what it returns is not trusted to be
        a valid document of the new version until the contract says so. **An
        adapter must not mutate nested values - it receives a shallow copy.**
        """
        _a_version(from_version, "from_version")
        target = DOC_TYPE_VERSION[doc_type] if to is None else _a_version(to, "to")
        if from_version > target:
            raise ValueError(
                f"{doc_type.value}: a document at version {from_version} is newer than "
                f"{target}, and cannot be downgraded"
            )
        missing = [v for v in range(from_version, target) if self.step(doc_type, v) is None]
        if missing:
            # Checked before anything runs, so a half-migrated document is never
            # returned and never written.
            raise AdapterError(f"{doc_type.value}: no adapter from version {missing[0]} to {missing[0] + 1}")
        carried = dict(data)
        for version in range(from_version, target):
            adapter = self._steps[(doc_type, version)]
            carried = dict(adapter(carried))
        return target, carried


#: The registry the service uses. Empty: every type is at version 1 and nothing
#: has been released, so no document can need carrying forward yet.
DOC_TYPE_ADAPTERS = AdapterRegistry()

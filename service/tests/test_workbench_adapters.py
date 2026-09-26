"""The schema-adapter frame (``1kg.5.3``).

No real type has a version 2 yet, so the walk is exercised with a **fixture**
type on a registry the tests build themselves — the shipped one stays empty,
which is itself one of the assertions below.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from service import workbench_adapters as wa
from service.workbench_contracts import DOC_TYPE_VERSION, DocumentTypeId

#: Requirement 7 asks for a fixture type. ``DocumentTypeId`` is a closed
#: vocabulary, so a *synthetic* id cannot be minted without changing the
#: contract; instead a real id stands in on a registry these tests own, and
#: the target version is passed explicitly. Nothing here touches the shipped
#: registry, which a test below asserts is still empty.
FIXTURE_TYPE = DocumentTypeId.NPC


def _rename(old: str, new: str) -> wa.Adapter:
    """A step that moves text from one key to another — the case ED-24 is about."""

    def adapter(data: Mapping[str, Any]) -> dict[str, Any]:
        carried = dict(data)
        if old in carried:
            carried[new] = carried.pop(old)
        return carried

    return adapter


def test_the_shipped_registry_is_empty_because_every_type_is_at_version_one() -> None:
    assert set(DOC_TYPE_VERSION.values()) == {1}
    for doc_type in DocumentTypeId:
        assert wa.DOC_TYPE_ADAPTERS.step(doc_type, 1) is None
    # Nothing to do, so the walk is a no-op that still answers.
    assert wa.DOC_TYPE_ADAPTERS.upgrade(FIXTURE_TYPE, 1, {"name": "x"}) == (1, {"name": "x"})


def test_a_document_walks_one_step_at_a_time_to_the_current_version() -> None:
    registry = wa.AdapterRegistry()
    registry.register(FIXTURE_TYPE, 1, _rename("motives", "wants"))
    registry.register(FIXTURE_TYPE, 2, _rename("wants", "wants_now"))

    version, data = registry.upgrade(FIXTURE_TYPE, 1, {"name": "Ondrey", "motives": "The signet."}, to=3)
    assert version == 3
    assert data == {"name": "Ondrey", "wants_now": "The signet."}

    # A document already at the target is returned unchanged, not re-run.
    assert registry.upgrade(FIXTURE_TYPE, 3, {"name": "Ondrey"}, to=3) == (3, {"name": "Ondrey"})
    # And a partial walk stops where it is asked to.
    assert registry.upgrade(FIXTURE_TYPE, 1, {"motives": "x"}, to=2) == (2, {"wants": "x"})


def test_a_missing_step_is_reported_before_anything_runs() -> None:
    """A document half-migrated by a skipped step is worse than one that will
    not load, so the gap is found first and nothing is applied."""
    registry = wa.AdapterRegistry()
    ran: list[str] = []

    def noisy(data: Mapping[str, Any]) -> dict[str, Any]:
        ran.append("step 1")
        return dict(data)

    registry.register(FIXTURE_TYPE, 1, noisy)
    # Step 2 is missing.
    assert registry.can_upgrade(FIXTURE_TYPE, 1, to=2)
    assert not registry.can_upgrade(FIXTURE_TYPE, 1, to=3)
    with pytest.raises(wa.AdapterError, match="no adapter from version 2 to 3"):
        registry.upgrade(FIXTURE_TYPE, 1, {"name": "x"}, to=3)
    assert ran == [], "nothing runs when the walk cannot complete"


def test_the_message_names_the_step_and_never_the_document() -> None:
    """X-7: a GM's private text cannot ride out in an error."""
    registry = wa.AdapterRegistry()
    secret = "Drown the harbourmaster."
    with pytest.raises(wa.AdapterError) as caught:
        registry.upgrade(FIXTURE_TYPE, 1, {"name": "x", "notes": secret}, to=2)
    assert secret not in str(caught.value)
    assert "notes" not in str(caught.value)


def test_a_newer_document_is_refused_rather_than_downgraded() -> None:
    registry = wa.AdapterRegistry()
    with pytest.raises(ValueError, match="cannot be downgraded"):
        registry.upgrade(FIXTURE_TYPE, 3, {"name": "x"}, to=1)


def test_a_step_cannot_be_registered_twice_or_from_a_version_that_does_not_exist() -> None:
    registry = wa.AdapterRegistry()
    registry.register(FIXTURE_TYPE, 1, _rename("a", "b"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(FIXTURE_TYPE, 1, _rename("a", "c"))
    with pytest.raises(ValueError, match="a version starts at 1"):
        registry.register(FIXTURE_TYPE, 0, _rename("a", "b"))


def test_the_register_docstring_carries_ED24s_rule() -> None:
    """The rule has no code to enforce here — eligibility storage is
    ``1ir.2.1``'s — so the contract an adapter author is held to lives in the
    docstring, and this test is what keeps it there."""
    doc = wa.AdapterRegistry.register.__doc__ or ""
    assert "unclassified" in doc
    assert "ED-24" in doc


def test_one_type_s_steps_are_not_another_s() -> None:
    registry = wa.AdapterRegistry()
    registry.register(DocumentTypeId.NPC, 1, _rename("a", "b"))
    assert registry.step(DocumentTypeId.NPC, 1) is not None
    assert registry.step(DocumentTypeId.LORE, 1) is None



# ── Requirement 8 (F-11): a version that is not a version (1kg.5.7.2) ────────

_NOT_VERSIONS = [float("nan"), 0.5, -1, 0, 1001, "1", True]


@pytest.mark.parametrize("version", _NOT_VERSIONS, ids=["NaN", "0.5", "-1", "0", "1001", "a string", "True"])
def test_a_version_that_is_not_a_version_is_refused_wherever_one_is_taken(version: Any) -> None:
    """AC 19: an integer from 1 to 1000, checked explicitly, and a ``ValueError``
    rather than a ``TypeError`` out of ``range`` or an :class:`AdapterError`. A
    bool is refused although ``True == 1``: a flag is not a version."""
    registry = wa.AdapterRegistry()
    registry.register(FIXTURE_TYPE, 1, _rename("a", "b"))
    calls = [
        lambda: registry.upgrade(FIXTURE_TYPE, version, {"name": "x"}, to=2),
        lambda: registry.upgrade(FIXTURE_TYPE, 1, {"name": "x"}, to=version),
        lambda: registry.can_upgrade(FIXTURE_TYPE, version, to=2),
        lambda: registry.can_upgrade(FIXTURE_TYPE, 1, to=version),
        lambda: registry.register(DocumentTypeId.LORE, version, _rename("a", "b")),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="must be an integer from 1 to 1000") as caught:
            call()
        assert type(caught.value) is ValueError


def test_the_upgrade_docstring_says_what_the_caller_and_an_adapter_must_do() -> None:
    """Requirement 8's two sentences, kept where an adapter author reads them."""
    doc = " ".join((wa.AdapterRegistry.upgrade.__doc__ or "").split())
    assert "The caller validates the adapted result with ``check_fields`` before storing it" in doc
    assert "An adapter must not mutate nested values - it receives a shallow copy." in doc
    assert "it receives a shallow copy" in (wa.AdapterRegistry.register.__doc__ or "")

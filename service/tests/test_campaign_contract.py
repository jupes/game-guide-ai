"""The campaigns and seats family of the Workbench wire contract (bead 1kg.2.2).

The shared fixtures under `contracts/workbench/v1/{Campaign,Seat,PlayerSeat}*.json`
are the specification and `test_workbench_contracts.py` runs them; this file
holds what a fixture cannot say: that the family's numbers are the stores' and
the migrations', that the address rule is `_validate_email`'s, that a refusal
names the field and never the value, that no request can smuggle an account id,
and that the password is write-only.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from service import campaign_store, participant_store
from service import models as legacy
from service import workbench_contracts as wc
from service.seat_offer_store import check_address

CANARY = "Zx9CanaryQ7"


def test_the_familys_bounds_are_the_stores_and_the_migrations() -> None:
    assert wc.CAMPAIGN_NAME_MAX_CHARS == campaign_store.NAME_MAX_CHARS
    assert wc.SEAT_ALIAS_MAX_CHARS == participant_store.ALIAS_MAX_CHARS
    assert wc.CAMPAIGN_PAGE_MAX_ITEMS == campaign_store.PAGE_MAX
    assert wc.EMAIL_MAX_CHARS == legacy.MAX_EMAIL_LENGTH
    assert wc.PASSWORD_MAX_CHARS == legacy.MAX_PASSWORD_LENGTH


@pytest.mark.parametrize(
    "address",
    ["wren@example.com", "@example.com", "wren@", "wren hidden@example.com", "no-at-sign", "a@b", "a@@b", "é@ü"],
)
def test_the_address_rule_is_validate_emails_restated(address: str) -> None:
    """Restated rather than called (L-7): `_validate_email` strips with
    `str.strip`, so the two agree on everything that needs no stripping."""
    try:
        legacy._validate_email(address)
    except ValueError:
        accepted = False
    else:
        accepted = True
    assert wc.is_email_shaped(address) is accepted


@pytest.mark.parametrize(
    ("schema", "body"),
    [
        (wc.CampaignCreateRequest, {"schema_version": 1, "name": "Mine"}),
        (wc.CampaignPatchRequest, {"schema_version": 1, "archived": True}),
        (wc.SeatCreateRequest, {"schema_version": 1, "alias": "Rook"}),
        (wc.SeatOfferRequest, {"schema_version": 1, "email": "wren@example.com"}),
        (wc.SeatRemoveRequest, {"schema_version": 1, "password": "secret"}),
        (wc.SeatDeclineRequest, {"schema_version": 1, "block": False}),
    ],
    ids=lambda value: getattr(value, "__name__", ""),
)
def test_no_request_of_the_family_carries_an_account_id(schema: type[Any], body: dict[str, Any]) -> None:
    schema.model_validate(body)
    for key in ("user_id", "owner_id", "account_id"):
        with pytest.raises(ValidationError) as refused:
            schema.model_validate({**body, key: 7})
        assert [error["type"] for error in refused.value.errors()] == ["extra_forbidden"]


@pytest.mark.parametrize(
    ("schema", "body", "field"),
    [
        (wc.CampaignCreateRequest, {"schema_version": 1, "name": CANARY + "‮"}, "name"),
        (wc.CampaignCreateRequest, {"schema_version": 1, "name": CANARY * 20}, "name"),
        (wc.SeatCreateRequest, {"schema_version": 1, "alias": CANARY * 5}, "alias"),
        (wc.SeatOfferRequest, {"schema_version": 1, "email": CANARY + " x@example.com"}, "email"),
        (wc.SeatOfferRequest, {"schema_version": 1, "email": CANARY + "\u0000@x"}, "email"),
        (wc.SeatRemoveRequest, {"schema_version": 1, "password": CANARY * 100}, "password"),
        (wc.SeatRemoveRequest, {"schema_version": 1, "password": CANARY + "\ud800"}, "password"),
    ],
)
def test_a_refusal_names_its_field_and_never_its_value(schema: type[Any], body: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError) as refused:
        schema.model_validate(body)
    assert CANARY not in str(refused.value)
    answered = wc.validation_error_body(refused.value.errors())
    assert answered.detail.field == field and CANARY not in answered.model_dump_json()
    assert CANARY not in str(wc.redacted_errors(refused.value.errors()))


def test_the_password_is_write_only_and_hidden_from_repr() -> None:
    request = wc.SeatRemoveRequest.model_validate({"schema_version": 1, "password": CANARY})
    assert CANARY not in repr(request) and CANARY not in request.model_dump_json()
    assert request.password.get_secret_value() == CANARY
    assert wc.SeatRemoveRequest.model_json_schema()["properties"]["password"]["writeOnly"] is True


def test_a_patch_is_emitted_exactly_as_it_was_sent() -> None:
    for sent in ({"schema_version": 1, "name": "Aubade"}, {"schema_version": 1, "archived": False}):
        assert wc.CampaignPatchRequest.model_validate(sent).model_dump(mode="json") == sent


def test_a_name_an_alias_and_an_address_are_stored_trimmed() -> None:
    assert wc.CampaignCreateRequest.model_validate({"schema_version": 1, "name": " 　Nocturne "}).name == "Nocturne"
    assert wc.SeatCreateRequest.model_validate({"schema_version": 1, "alias": " Rook\t"}).alias == "Rook"
    offered = wc.SeatOfferRequest.model_validate({"schema_version": 1, "email": " Wren@Example.com "})
    assert offered.email == "Wren@Example.com", "kept as the GM typed it, up to the trim"
    assert check_address(offered.email) == offered.email


def test_the_new_codes_are_closed_members_and_safe_labels() -> None:
    added = {"alias_taken", "seat_not_open", "seat_not_accepted", "seat_cap_reached", "campaign_archived",
             "reauth_failed"}
    assert added <= {code.value for code in wc.ErrorCode}
    assert [status.value for status in wc.SeatStatus] == [
        "open", "offered", "not_accepted", "awaiting_confirmation", "confirmed", "removed",
    ]

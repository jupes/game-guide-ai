"""Write a generated policy world into PostgreSQL without using the stores."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

import psycopg

from service import campaign_identity as ident
from service.tests import pdp_fixtures as pdp
from service.tests.policy_oracle import AccountId, GrantId, SeatStatus, State


@dataclass(frozen=True)
class WrittenWorld:
    campaign_id: str
    suffix: str
    accounts: dict[AccountId, int]
    screens: dict[GrantId, str] = field(repr=False)

    def id(self, prefix: str, oracle_id: str) -> str:
        return pdp.shaped(prefix, oracle_id) + self.suffix

    def document(self, oracle_id: str) -> str:
        return self.id(ident.DOCUMENT, oracle_id)


def write(conn: psycopg.Connection, state: State) -> WrittenWorld:
    """Commit one world, with distinct IDs and fresh accounts including its owner."""
    world = state.world
    suffix = "_" + uuid4().hex
    now = pdp.T0
    started_at = now - timedelta(hours=2)
    # The matrix models a stale grant one generation before generation one.
    # Shift that entire world's generations to satisfy PostgreSQL's lower bound.
    generation_shift = max(0, 1 - min((grant.generation for grant in state.grants.values()), default=1))
    rows = WrittenWorld(ident.new_id(ident.CAMPAIGN), suffix, {}, {})
    with conn.transaction(), conn.cursor() as cursor:
        accounts = {world.owner} | {p.account for p in world.participants.values() if p.account is not None}
        for account in sorted(accounts):
            owner = cursor.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id",
                (f"{account}{suffix}@example.test",),
            ).fetchone()
            assert owner is not None
            rows.accounts[account] = owner[0]
        owner_id = rows.accounts[world.owner]
        cursor.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')",
            (rows.campaign_id, owner_id),
        )
        cursor.execute(
            "UPDATE campaign.authz_state SET authz_revision = %s, projection_revision = %s WHERE campaign_id = %s",
            (world.authz_revision, world.authz_revision, rows.campaign_id),
        )
        cursor.executemany(
            """INSERT INTO campaign.participants (id, campaign_id, alias, alias_key, user_id, accepted_at,
                                                  confirmed_at, removed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    rows.id(ident.PARTICIPANT, p.id), rows.campaign_id, p.id, p.id,
                    None if p.account is None else rows.accounts[p.account],
                    None if p.account is None else started_at,
                    now if p.status is SeatStatus.CONFIRMED else None,
                    now if p.status is SeatStatus.REMOVED else None,
                )
                for p in world.participants.values()
            ],
        )
        cursor.executemany(
            """INSERT INTO campaign.documents (id, campaign_id, type, type_version, data, write_revision,
                                               field_revisions, name_key, search_key,
                                               linked_participant_id, archived_at)
               VALUES (%s, %s, %s, %s, '{"name":"Fixture"}', 1, '{"name":1}', 'fixture', 'fixture', %s, %s)""",
            [
                (
                    rows.document(d.id), rows.campaign_id, d.type_id, d.type_version,
                    None if d.linked_participant is None else rows.id(ident.PARTICIPANT, d.linked_participant),
                    now if d.archived else None,
                )
                for d in world.documents.values()
            ],
        )
        cursor.executemany(
            "INSERT INTO campaign.groups (id, campaign_id, name, name_fold) VALUES (%s, %s, %s, %s)",
            [(rows.id(ident.GROUP, group), rows.campaign_id, group, group) for group in world.groups],
        )
        cursor.executemany(
            "INSERT INTO campaign.group_members (group_id, participant_id, campaign_id) VALUES (%s, %s, %s)",
            [
                (rows.id(ident.GROUP, group), rows.id(ident.PARTICIPANT, participant), rows.campaign_id)
                for group, members in world.groups.items()
                for participant in members
            ],
        )
        cursor.executemany(
            """INSERT INTO campaign.field_eligibility
                   (document_id, campaign_id, field_key, eligibility_class, principal_ids, classification_source)
               VALUES (%s, %s, %s, %s, %s, 'gm')""",
            [
                (
                    rows.document(doc), rows.campaign_id, key, classification.field_class.value,
                    [item + suffix for item in classification.ids] or None,
                )
                for (doc, key), field_class in world.classes.items()
                if (classification := pdp.eligibility(field_class)) is not None
            ],
        )
        cursor.executemany(
            """INSERT INTO campaign.table_sessions (id, campaign_id, gm_user_id, state, started_at,
                                                    expires_at, ended_at, link_generation, reveal_epoch)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    rows.id(ident.TABLE_SESSION, s.id), rows.campaign_id, owner_id,
                    "live" if s.live else ("expired" if s.expired else "ended"), started_at,
                    now + timedelta(seconds=-1 if s.expired else 3600),
                    None if s.live else (now - timedelta(seconds=1) if s.expired else now),
                    s.generation + generation_shift, s.epoch,
                )
                for s in state.sessions.values()
            ],
        )
        for grant in state.grants.values():
            secret = ident.new_secret()
            rows.screens[grant.id] = secret.secret
            cursor.execute(
                """INSERT INTO campaign.table_credentials
                       (id, session_id, link_generation, credential_digest, revoked_at)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    rows.id(ident.TABLE_CREDENTIAL, grant.id), rows.id(ident.TABLE_SESSION, grant.session),
                    grant.generation + generation_shift, secret.digest, now if grant.revoked else None,
                ),
            )
    return rows

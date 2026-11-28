"""Record a ready campaign image in an active GM conversation."""

from datetime import datetime

from .campaign_store import MissingParent, pg
from .db import TransactionalDatabase, UnitOfWork
from .timeline_store import new_entry_id, validated_entry
from .workbench_contracts import (
    TEXT_FIELD_MAX_CHARS,
    AssetRef,
    AttachedImageEntry,
    ImageAttachRequest,
    check_plain_text,
    trim,
)


def handout_name(description: str, use: str) -> str:
    """Use the original image description within a document title's wire limits."""
    try:
        checked = check_plain_text(description)
    except ValueError:
        return use.capitalize()
    return trim(" ".join(trim(checked).split())[:TEXT_FIELD_MAX_CHARS]) or use.capitalize()


def hold_image_thread(unit: UnitOfWork, campaign_id: str, conversation_id: str, owner_id: int) -> None:
    """Keep the owned campaign and GM thread active for a new reference or replay."""
    row = pg(unit).conn.execute(
        """SELECT 1 FROM chat.conversations c
           JOIN campaign.campaigns k ON k.id = c.campaign_id AND k.owner_id = c.user_id
           WHERE c.conversation_id = %s AND c.user_id = %s AND k.id = %s
             AND (c.started_mode IS NULL OR c.started_mode = 'gm')
             AND c.archived_at IS NULL AND k.archived_at IS NULL
           FOR SHARE OF k, c""",
        (conversation_id, owner_id, campaign_id),
    ).fetchone()
    if row is None:
        raise MissingParent


def ready_image(unit: UnitOfWork, campaign_id: str, asset_id: str) -> AssetRef:
    """Hold the ready image until its new reference commits in the authorized campaign."""
    row = pg(unit).conn.execute(
        """SELECT alt, width, height FROM campaign.assets
           WHERE campaign_id = %s AND id = %s AND kind = 'image' AND state = 'ready' FOR SHARE""",
        (campaign_id, asset_id),
    ).fetchone()
    if row is None:
        raise MissingParent
    return AssetRef(asset_id=asset_id, media_type="image", alt=row[0], width=row[1], height=row[2])


def attach_image(
    db: TransactionalDatabase, campaign_id: str, asset_id: str, owner_id: int, body: ImageAttachRequest, now: datetime,
) -> AttachedImageEntry:
    entry = AttachedImageEntry(
        schema_version=1, entry_kind="attached_image", entry_id=new_entry_id(), created_at=now,
        asset_id=asset_id, use=body.use,
    )
    _, payload = validated_entry(entry, now)
    with db.transaction() as unit:
        conn = pg(unit).conn
        owner = "SELECT 1 FROM campaign.campaigns WHERE id = %s AND owner_id = %s AND archived_at IS NULL"
        if conn.execute(owner, (campaign_id, owner_id)).fetchone() is None:
            raise MissingParent
        unit.lock_campaign(campaign_id, shared=True)
        hold_image_thread(unit, campaign_id, body.conversation_id, owner_id)
        ready_image(unit, campaign_id, asset_id)
        inserted = conn.execute(
            """INSERT INTO chat.timeline_entries
                 (entry_id, conversation_id, entry_kind, schema_version, created_at, payload)
               VALUES (%s,%s,'attached_image',1,%s,%s::jsonb)
               ON CONFLICT (conversation_id,(payload->>'asset_id'),(payload->>'use'))
                 WHERE entry_kind = 'attached_image' DO NOTHING RETURNING entry_id""",
            (entry.entry_id, body.conversation_id, now, payload),
        ).fetchone()
        if inserted:
            return entry
        saved = conn.execute(
            """SELECT payload FROM chat.timeline_entries WHERE conversation_id = %s AND entry_kind = 'attached_image'
                 AND payload->>'asset_id' = %s AND payload->>'use' = %s""",
            (body.conversation_id, asset_id, body.use),
        ).fetchone()
        if saved is None:
            raise MissingParent
        return AttachedImageEntry.model_validate(saved[0])

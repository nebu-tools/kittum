"""The open protocol draft of a project: the text its leads are writing and have not published.

A project has at most one draft, and everyone on it edits that one. Every save names the revision
it was written on and raises it by one; a save, discard, or publication that names an older
revision raises `StaleDraft` with the draft as it is now, so no one overwrites a change they have
not seen. Publishing compiles the text, publishes it (`kittum.protocol.publish`, which assigns
the label and appends `protocol.published`), and deletes the draft, all in the caller's
transaction.

A draft is not a record of the review. Saving one appends nothing to the ledger, and no sealed
bundle holds it; only what is published is kept.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from kittum.core import Id, KittumError, Record
from kittum.protocol.compiler import compile
from kittum.protocol.store import StoredProtocol, publish

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.ledger import Ledger


class ProtocolDraft(Record):
    project_id: Id
    revision: int
    """Raised by one on every save; a save names the revision it was written on."""
    source: str
    """The text as written, YAML or JSON. It need not compile until it is published."""
    base_revision_id: Id | None
    """The assistant revision the draft was edited from, if any."""
    created_at: datetime
    updated_at: datetime


class StaleDraft(KittumError):
    """The draft changed since the revision the caller named, or it is gone."""

    def __init__(self, current: ProtocolDraft | None) -> None:
        super().__init__(
            "the draft was changed elsewhere"
            if current
            else "the draft was published or discarded elsewhere"
        )
        self.current = current


_COLUMNS = "project_id, revision, source, base_revision_id, created_at, updated_at"


def get_draft(tx: Tx, project_id: Id) -> ProtocolDraft | None:
    row = tx.execute(
        f"select {_COLUMNS} from protocol_draft where project_id = %s", (project_id,)
    ).fetchone()
    return ProtocolDraft.model_validate(row) if row else None


def start_draft(
    tx: Tx, project_id: Id, source: str, base_revision_id: Id | None = None
) -> ProtocolDraft:
    """Open the project's draft with `source`, or return the draft already open, unchanged. The
    caller can tell which by the source."""
    row = tx.execute(
        f"insert into protocol_draft (project_id, revision, source, base_revision_id)"
        f" values (%s, 1, %s, %s) on conflict (project_id) do nothing returning {_COLUMNS}",
        (project_id, source, base_revision_id),
    ).fetchone()
    if row:
        return ProtocolDraft.model_validate(row)
    existing = get_draft(tx, project_id)
    if existing is None:  # discarded between the insert and the read: open it again
        return start_draft(tx, project_id, source, base_revision_id)
    return existing


def save_draft(tx: Tx, project_id: Id, revision: int, source: str) -> ProtocolDraft:
    """Replace the text of the draft at `revision`. Raises `StaleDraft` if it is not at it."""
    row = tx.execute(
        f"update protocol_draft set source = %s, revision = revision + 1, updated_at = now()"
        f" where project_id = %s and revision = %s returning {_COLUMNS}",
        (source, project_id, revision),
    ).fetchone()
    if row is None:
        raise StaleDraft(get_draft(tx, project_id))
    return ProtocolDraft.model_validate(row)


def discard_draft(tx: Tx, project_id: Id, revision: int) -> None:
    """Delete the draft at `revision`. Raises `StaleDraft` if it is not at it."""
    deleted = tx.execute(
        "delete from protocol_draft where project_id = %s and revision = %s returning project_id",
        (project_id, revision),
    ).fetchone()
    if deleted is None:
        raise StaleDraft(get_draft(tx, project_id))


def publish_draft(
    tx: Tx,
    project_id: Id,
    revision: int,
    approved_by: Id,
    *,
    ledger: Ledger,
    reason: str | None = None,
) -> StoredProtocol:
    """Publish the draft at `revision` and delete it. Raises `StaleDraft` if it is not at it,
    `ProtocolError` if it does not compile, and what `publish` raises."""
    row = tx.execute(
        f"select {_COLUMNS} from protocol_draft where project_id = %s for update", (project_id,)
    ).fetchone()
    draft = ProtocolDraft.model_validate(row) if row else None
    if draft is None or draft.revision != revision:
        raise StaleDraft(draft)
    stored = publish(
        tx, project_id, compile(draft.source), approved_by, ledger=ledger, reason=reason
    )
    tx.execute("delete from protocol_draft where project_id = %s", (project_id,))
    return stored

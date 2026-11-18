"""Published protocol versions in Postgres: publish a compiled version with an approval, read it
back. The text a lead is still writing is a draft (`kittum.protocol.draft`), never a version.

Publishing writes the version, frozen (a trigger rejects later changes), and appends a
`protocol.published` event in the caller's transaction. Kittum assigns the version label: the
first version keeps the label its document gives, and every later one takes the label after the
version in force (`next_label`), so no two versions of a project share a label. A version that changes a topic's screening
mode relative to the last published version needs a reason, which the event records. So does every
field of the version a model wrote (`ai_parts`): which field, from which revision, by which model.
Publishing asks every registered attribution (`register_attribution`; `kittum.assist` registers
its own when imported) whatever saved the version: the workbench, an assistant revision, or the
CLI. With none registered, a project with assistant revisions is refused, never published as if
no model had written any of it.
"""

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from psycopg.types.json import Jsonb

from kittum.core import Id, KittumError, NotFoundError, Record, new_id
from kittum.ledger import EventDraft
from kittum.protocol.compiler import ProtocolVersion, compile
from kittum.protocol.diffing import diff

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from kittum.db import Tx
    from kittum.ledger import AuditEvent, Ledger

PUBLISHED_EVENT = "protocol.published"

type Attribution = Callable[[Tx, Id, ProtocolVersion], Sequence[Mapping[str, Any]]]
"""The fields of a version that a model wrote, as `protocol.published` records them."""

_ATTRIBUTIONS: dict[str, Attribution] = {}


def register_attribution(name: str, attribution: Attribution) -> None:
    """Have every publication record what `attribution` finds. Registering a name again replaces
    its function."""
    _ATTRIBUTIONS[name] = attribution


def attributed(tx: Tx, project_id: Id, version: ProtocolVersion) -> list[dict[str, Any]]:
    """What every registered attribution finds. Fails closed: a project whose protocol was
    written with the assistant is never published as if no model had written any of it."""
    if not _ATTRIBUTIONS and _assisted(tx, project_id):
        raise ProtocolStateError(
            "a model helped write this project's protocol, and nothing is registered to name"
            " what it wrote; publish through kittum.app, or import kittum.assist first"
        )
    return [dict(p) for f in _ATTRIBUTIONS.values() for p in f(tx, project_id, version)]


def _assisted(tx: Tx, project_id: Id) -> bool:
    """Whether the protocol assistant has revisions in the project (its table, read only here)."""
    row = tx.execute("select to_regclass('assist_revision') is not null as present").fetchone()
    if row is None or not row["present"]:
        return False
    found = tx.execute(
        "select exists (select 1 from assist_revision where project_id = %s) as found",
        (project_id,),
    ).fetchone()
    return bool(found and found["found"])


class ProtocolStateError(KittumError):
    """The request does not fit the version's state, e.g. publishing it twice."""


class StoredProtocol(Record):
    """A published version, with its approval."""

    id: Id
    project_id: Id
    parent_id: Id | None
    """The version that was in force when this one was published."""
    version: ProtocolVersion
    created_at: datetime
    published_at: datetime
    approved_by: Id
    approved_event_id: Id
    publish_reason: str | None = None


class ProtocolStore(Protocol):
    """The persistence seam. `PostgresProtocolStore` and `testing.InMemoryProtocolStore` fit it."""

    def publish(
        self,
        tx: Tx,
        project_id: Id,
        version: ProtocolVersion,
        approved_by: Id,
        *,
        reason: str | None = None,
    ) -> StoredProtocol: ...

    def get_version(self, tx: Tx, project_id: Id, version_id: Id) -> StoredProtocol: ...

    def list_versions(self, tx: Tx, project_id: Id) -> list[StoredProtocol]: ...

    def latest_published(self, tx: Tx, project_id: Id) -> StoredProtocol | None: ...


def next_label(label: str) -> str:
    """The label after `label`, by its last number: "1.1" to "1.2", "1" to "2", "0.3" to "0.4".
    A label with no number gets ".1" added."""
    found = re.fullmatch(r"(.*?)(\d+)(\D*)", label.strip())
    if found is None:
        return f"{label.strip()}.1"
    return f"{found[1]}{int(found[2]) + 1}{found[3]}"


def labelled(version: ProtocolVersion, previous: StoredProtocol | None) -> ProtocolVersion:
    """`version` under the label Kittum publishes it as, after `previous`, the version in force."""
    if previous is None:
        return version
    label = next_label(previous.version.protocol.version_label)
    if version.protocol.version_label == label:
        return version
    return compile({**version.protocol.model_dump(mode="json"), "version_label": label})


def _unlabelled(version: ProtocolVersion) -> dict[str, Any]:
    return {**version.protocol.model_dump(mode="json"), "version_label": None}


def check_publishable(
    version: ProtocolVersion, previous: StoredProtocol | None, reason: str | None
) -> None:
    """Refuse a version that changes nothing but its label, and a screening mode change without
    a reason."""
    if previous and _unlabelled(version) == _unlabelled(previous.version):
        label = previous.version.protocol.version_label
        raise ProtocolStateError(f"this is version {label}, the one in force, unchanged")
    if previous and not (reason and reason.strip()):
        changed = diff(previous.version, version).mode_changes
        if changed:
            topics = ", ".join(f"{m.topic_id} {m.before}→{m.after}" for m in changed)
            raise ProtocolStateError(f"changing a screening mode needs a reason ({topics})")


def to_publish(
    project_id: Id,
    version: ProtocolVersion,
    previous: StoredProtocol | None,
    approved_by: Id,
    reason: str | None,
    ai_parts: Sequence[Mapping[str, Any]] = (),
    *,
    append: Callable[[EventDraft], AuditEvent],
) -> StoredProtocol:
    """Check `version`, label it, and append its `protocol.published` event through `append`;
    the version as published, for the store to keep. Shared with the in-memory store."""
    version = labelled(version, previous)
    check_publishable(version, previous, reason)
    version_id = new_id()
    changes = diff(previous.version, version) if previous else None
    event = append(
        EventDraft(
            project_id=project_id,
            type=PUBLISHED_EVENT,
            schema_version=1,
            actor_id=approved_by,
            payload={
                "protocol_version_id": str(version_id),
                "content_hash": version.content_hash,
                "version_label": version.protocol.version_label,
                "previous_version_id": str(previous.id) if previous else None,
                "previous_content_hash": previous.version.content_hash if previous else None,
                "reason": reason,
                "mode_changes": [m.model_dump(mode="json") for m in changes.mode_changes]
                if changes
                else [],
                "added_criteria": list(changes.added_criteria) if changes else [],
                "removed_criteria": list(changes.removed_criteria) if changes else [],
                "changed_criteria": list(changes.changed_criteria) if changes else [],
                "ai_parts": [dict(p) for p in ai_parts],
            },
        )
    )
    return StoredProtocol(
        id=version_id,
        project_id=project_id,
        parent_id=previous.id if previous else None,
        # A snapshot validated from JSON, as the Postgres store reads the document back.
        version=ProtocolVersion.model_validate(version.model_dump(mode="json")),
        created_at=event.recorded_at,
        published_at=event.recorded_at,
        approved_by=approved_by,
        approved_event_id=event.event_id,
        publish_reason=reason,
    )


# Postgres

_COLUMNS = (
    "id, project_id, parent_id, content_hash, document, created_at, published_at, approved_by,"
    " approved_event_id, publish_reason"
)


def _from_row(row: dict[str, Any]) -> StoredProtocol:
    version = compile(row["document"])
    if version.content_hash != row["content_hash"]:
        raise KittumError(f"protocol version {row['id']} does not match its content hash")
    return StoredProtocol(
        id=row["id"],
        project_id=row["project_id"],
        parent_id=row["parent_id"],
        version=version,
        created_at=row["created_at"],
        published_at=row["published_at"],
        approved_by=row["approved_by"],
        approved_event_id=row["approved_event_id"],
        publish_reason=row["publish_reason"],
    )


def publish(
    tx: Tx,
    project_id: Id,
    version: ProtocolVersion,
    approved_by: Id,
    *,
    ledger: Ledger,
    reason: str | None = None,
) -> StoredProtocol:
    """Publish `version` with a human approval, under the label Kittum assigns, and append
    `protocol.published`, naming the fields a model wrote (every registered attribution) for the
    AI disclosure. Refused (`ProtocolStateError`) if it changes nothing but the label.

    Publications serialize per project, so the version in force is read as of this one's commit:
    two concurrent publications cannot take the same label or both skip the mode-change reason.
    """
    tx.execute(
        "select pg_advisory_xact_lock(hashtext('kittum.protocol.publish'), hashtext(%s))",
        (str(project_id),),
    )
    actor = tx.execute("select kind from actor where id = %s", (approved_by,)).fetchone()
    if actor is None:
        raise ProtocolStateError(f"actor {approved_by} does not exist")
    if actor["kind"] != "human":
        raise ProtocolStateError("publishing a protocol needs the approval of a human actor")
    previous = latest_published(tx, project_id)
    ai_parts = attributed(tx, project_id, labelled(version, previous))
    stored = to_publish(
        project_id,
        version,
        previous,
        approved_by,
        reason,
        ai_parts,
        append=lambda event: ledger.append(tx, event),
    )
    tx.execute(
        "insert into protocol_version (id, project_id, parent_id, version_label, content_hash,"
        " document, created_at, published_at, approved_by, approved_event_id, publish_reason)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            stored.id,
            project_id,
            stored.parent_id,
            stored.version.protocol.version_label,
            stored.version.content_hash,
            Jsonb(stored.version.protocol.model_dump(mode="json")),
            stored.created_at,
            stored.published_at,
            approved_by,
            stored.approved_event_id,
            reason,
        ),
    )
    return stored


def get_version(tx: Tx, project_id: Id, version_id: Id) -> StoredProtocol:
    row = tx.execute(
        f"select {_COLUMNS} from protocol_version where id = %s and project_id = %s",
        (version_id, project_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"protocol version {version_id} not found in project {project_id}")
    return _from_row(row)


def list_versions(tx: Tx, project_id: Id) -> list[StoredProtocol]:
    """Every published version of a project, oldest first."""
    rows = tx.execute(
        f"select {_COLUMNS} from protocol_version where project_id = %s order by published_at, id",
        (project_id,),
    ).fetchall()
    return [_from_row(r) for r in rows]


def latest_published(tx: Tx, project_id: Id) -> StoredProtocol | None:
    """The version in force: the one published last."""
    row = tx.execute(
        f"select {_COLUMNS} from protocol_version where project_id = %s"
        " order by published_at desc, id desc limit 1",
        (project_id,),
    ).fetchone()
    return _from_row(row) if row else None


class PostgresProtocolStore:
    """The functions above behind the `ProtocolStore` seam."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def publish(
        self,
        tx: Tx,
        project_id: Id,
        version: ProtocolVersion,
        approved_by: Id,
        *,
        reason: str | None = None,
    ) -> StoredProtocol:
        return publish(tx, project_id, version, approved_by, ledger=self._ledger, reason=reason)

    def get_version(self, tx: Tx, project_id: Id, version_id: Id) -> StoredProtocol:
        return get_version(tx, project_id, version_id)

    def list_versions(self, tx: Tx, project_id: Id) -> list[StoredProtocol]:
        return list_versions(tx, project_id)

    def latest_published(self, tx: Tx, project_id: Id) -> StoredProtocol | None:
        return latest_published(tx, project_id)

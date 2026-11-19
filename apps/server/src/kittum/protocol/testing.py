"""An in-memory `ProtocolStore` for tests in any module. It follows the Postgres store's rules.

It ignores the transaction; pass `kittum.db.testing.null_tx()`. It asks no registered attribution,
which reads the database, so its publications name no AI parts. It cannot check that the approver
is a human actor, because it has no actor table, so it trusts the ID.
"""

from typing import TYPE_CHECKING

from kittum.core import Id, KittumError, NotFoundError
from kittum.protocol.compiler import protocol_hash
from kittum.protocol.store import StoredProtocol, to_publish

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.ledger import Ledger
    from kittum.protocol.compiler import ProtocolVersion


class InMemoryProtocolStore:
    def __init__(self, ledger: Ledger) -> None:
        """Pass a `kittum.ledger.testing.InMemoryLedger`; its clock dates the versions."""
        self.ledger = ledger
        self._versions: dict[Id, StoredProtocol] = {}

    def publish(
        self,
        tx: Tx,
        project_id: Id,
        version: ProtocolVersion,
        approved_by: Id,
        *,
        reason: str | None = None,
    ) -> StoredProtocol:
        previous = self.latest_published(tx, project_id)
        stored = to_publish(
            project_id,
            version,
            previous,
            approved_by,
            reason,
            append=lambda event: self.ledger.append(tx, event),
        )
        self._versions[stored.id] = stored
        return stored

    def get_version(self, tx: Tx, project_id: Id, version_id: Id) -> StoredProtocol:
        stored = self._versions.get(version_id)
        if stored is None or stored.project_id != project_id:
            raise NotFoundError(f"protocol version {version_id} not found in project {project_id}")
        if protocol_hash(stored.version.protocol) != stored.version.content_hash:
            raise KittumError(f"protocol version {version_id} does not match its content hash")
        return stored

    def list_versions(self, tx: Tx, project_id: Id) -> list[StoredProtocol]:
        return sorted(
            (s for s in self._versions.values() if s.project_id == project_id),
            key=lambda s: (s.published_at, s.id),
        )

    def latest_published(self, tx: Tx, project_id: Id) -> StoredProtocol | None:
        published = [s for s in self._versions.values() if s.project_id == project_id]
        if not published:
            return None
        return max(published, key=lambda s: (s.published_at, s.id))

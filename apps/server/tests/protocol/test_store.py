import os
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg.rows import dict_row

from kittum.core import FixedClock, Id, KittumError, NotFoundError, new_id
from kittum.db.testing import null_tx
from kittum.ledger import PostgresLedger
from kittum.ledger.testing import InMemoryLedger
from kittum.protocol import (
    PUBLISHED_EVENT,
    PostgresProtocolStore,
    ProtocolStateError,
    ProtocolStore,
    compile,
    get_version,
    next_label,
    protocol_hash,
    publish,
)
from kittum.protocol.testing import InMemoryProtocolStore
from tests.protocol.support import example_source, example_text, seed_project

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.ledger import Ledger


def _switch_to_m2() -> dict:
    source = example_source()
    topic = source["search_topics"][0]
    topic["mode"] = "M2"
    topic["mode_rationale"] = {"en": "The update search returned 6,000 records."}
    topic["validation_pack"] = "kittum-validation-pack/2026-10"
    topic["audit"] = {"sentinel_percent": "1", "random_audit_percent": "10"}
    return source


def _publish_flow(store: ProtocolStore, ledger: Ledger, tx: Tx, project: Id, approver: Id) -> None:
    """The rules both stores follow: labels Kittum assigns, no unchanged version, reasons for
    mode changes."""
    v1 = compile(example_text())
    assert store.latest_published(tx, project) is None

    published = store.publish(tx, project, v1, approver)
    assert (published.parent_id, published.version, published.approved_by) == (None, v1, approver)
    assert store.get_version(tx, project, published.id) == published
    assert store.latest_published(tx, project) == published
    (event,) = ledger.events(tx, project)
    assert event.event_id == published.approved_event_id
    assert (event.type, event.actor_id, event.recorded_at) == (
        PUBLISHED_EVENT,
        approver,
        published.published_at,
    )
    assert event.payload["content_hash"] == v1.content_hash
    assert event.payload["version_label"] == "0.3"  # the first version keeps its own label
    assert event.payload["previous_version_id"] is None

    # The same protocol again, under its own label or another, is not a new version.
    for label in ("0.3", "7"):
        again = compile({**example_source(), "version_label": label})
        with pytest.raises(ProtocolStateError, match=r"version 0\.3, the one in force, unchanged"):
            store.publish(tx, project, again, approver)

    to_m2 = compile({**_switch_to_m2(), "version_label": "0.3"})
    with pytest.raises(ProtocolStateError, match=r"needs a reason \(TRIALS M1→M2\)"):
        store.publish(tx, project, to_m2, approver)
    reason = "Validation pack approved; the update search is too large for two screeners."
    v2 = store.publish(tx, project, to_m2, approver, reason=reason)
    assert (v2.parent_id, v2.version.protocol.version_label) == (published.id, "0.4")
    assert v2.version == compile({**_switch_to_m2(), "version_label": "0.4"})
    second = ledger.events(tx, project)[-1]
    assert second.payload["version_label"] == "0.4"
    assert second.payload["reason"] == reason
    assert second.payload["mode_changes"] == [{"topic_id": "TRIALS", "before": "M1", "after": "M2"}]
    assert second.payload["previous_version_id"] == str(published.id)
    assert store.latest_published(tx, project) == v2
    assert [s.id for s in store.list_versions(tx, project)] == [published.id, v2.id]
    assert ledger.verify(tx, project).ok

    with pytest.raises(NotFoundError):
        store.get_version(tx, new_id(), published.id)


def test_in_memory_store_follows_the_publish_rules() -> None:
    ledger = InMemoryLedger(FixedClock(datetime(2026, 10, 28, 9, 0, tzinfo=UTC)))
    _publish_flow(InMemoryProtocolStore(ledger), ledger, null_tx(), new_id(), new_id())


@pytest.mark.parametrize(
    ("label", "after"),
    [("0.3", "0.4"), ("1", "2"), ("1.9", "1.10"), ("v2-draft", "v3-draft"), ("final", "final.1")],
)
def test_the_next_label_counts_up_the_last_number(label: str, after: str) -> None:
    assert next_label(label) == after


@pytest.mark.postgres
def test_example_publishes_into_postgres_with_a_ledger_event(pg: Tx) -> None:
    project, approver, _ = seed_project(pg)
    ledger = PostgresLedger()
    _publish_flow(PostgresProtocolStore(ledger), ledger, pg, project, approver)

    row = pg.execute(
        "select v.content_hash, e.type, e.payload ->> 'content_hash' as event_hash"
        " from protocol_version v join audit_event e on e.event_id = v.approved_event_id"
        " where v.project_id = %s order by v.created_at, v.id limit 1",
        (project,),
    ).fetchone()
    assert row == {
        "content_hash": compile(example_text()).content_hash,
        "type": PUBLISHED_EVENT,
        "event_hash": compile(example_text()).content_hash,
    }


@pytest.mark.postgres
def test_published_versions_are_frozen(pg: Tx) -> None:
    project, approver, _ = seed_project(pg)
    published = publish(pg, project, compile(example_text()), approver, ledger=PostgresLedger())
    for statement in (
        "update protocol_version set document = '{}'::jsonb where id = %s",
        "update protocol_version set publish_reason = 'x' where id = %s",
        "delete from protocol_version where id = %s",
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="frozen"), pg.transaction():
            pg.execute(statement, (published.id,))


@pytest.mark.postgres
def test_a_tampered_document_is_detected_on_read(pg: Tx) -> None:
    project, approver, _ = seed_project(pg)
    draft = publish(pg, project, compile(example_text()), approver, ledger=PostgresLedger())
    pg.execute("alter table protocol_version disable trigger protocol_version_frozen")
    pg.execute(
        "update protocol_version set document = jsonb_set(document, '{version_label}', '\"9\"')"
        " where id = %s",
        (draft.id,),
    )
    with pytest.raises(KittumError, match="does not match its content hash"):
        get_version(pg, project, draft.id)


@pytest.mark.postgres
def test_only_a_human_can_approve(pg: Tx) -> None:
    project, _, system = seed_project(pg)
    version = compile(example_text())
    with pytest.raises(ProtocolStateError, match="human actor"):
        publish(pg, project, version, system, ledger=PostgresLedger())
    with pytest.raises(ProtocolStateError, match="does not exist"):
        publish(pg, project, version, new_id(), ledger=PostgresLedger())


def test_returned_protocols_cannot_change_what_the_store_holds() -> None:
    store = InMemoryProtocolStore(InMemoryLedger(FixedClock(datetime(2026, 10, 28, tzinfo=UTC))))
    project = new_id()
    version = compile(example_text())
    published = store.publish(null_tx(), project, version, new_id())

    with pytest.raises(TypeError, match="immutable"):
        published.version.protocol.criteria[0].text["en"] = "Children who smoke."
    with pytest.raises(TypeError, match="immutable"):
        version.protocol.questions[0].label.update(en="Everyone")
    with pytest.raises(TypeError, match="immutable"):
        published.version.protocol.title.pop("en")

    again = store.get_version(null_tx(), project, published.id)
    assert again.version.protocol.criteria[0].text["en"] == (
        "Adults (18 years or older) who smoke tobacco."
    )
    assert (
        protocol_hash(again.version.protocol)
        == again.version.content_hash
        == (version.content_hash)
    )


@pytest.mark.postgres
def test_concurrent_publications_cannot_skip_the_mode_change_reason(pg: Tx) -> None:
    """B's version keeps M1. Against the M2 version A is publishing, that is a mode change."""
    del pg  # skips without a database; the test needs its own committing connections
    url = os.environ["KITTUM_DATABASE_URL"]
    ledger = PostgresLedger()
    with psycopg.connect(url, row_factory=dict_row) as setup:
        project, approver, _ = seed_project(setup)
        publish(setup, project, compile(example_text()), approver, ledger=ledger)
        setup.commit()
    to_m2 = compile(_switch_to_m2())
    still_m1 = example_source()
    still_m1["title"] = {"en": "Nicotine replacement, retitled"}

    outcome: list[object] = []
    with (
        psycopg.connect(url, row_factory=dict_row) as a,
        psycopg.connect(url, row_factory=dict_row) as b,
        psycopg.connect(url, autocommit=True) as monitor,
    ):
        publish(a, project, to_m2, approver, ledger=ledger, reason="Validation pack approved.")

        def publish_b() -> None:
            try:
                outcome.append(publish(b, project, compile(still_m1), approver, ledger=ledger))
                b.commit()
            except ProtocolStateError as e:
                outcome.append(e)
                b.rollback()

        worker = threading.Thread(target=publish_b)
        worker.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            waiting = monitor.execute(
                "select 1 from pg_stat_activity where pid = %s and wait_event_type = 'Lock'",
                (b.info.backend_pid,),
            ).fetchone()
            if waiting:
                break
            time.sleep(0.02)
        a.commit()
        worker.join(timeout=10)

    (result,) = outcome
    assert isinstance(result, ProtocolStateError)
    assert "needs a reason (TRIALS M2→M1)" in str(result)

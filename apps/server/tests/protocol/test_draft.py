"""The open protocol draft: one per project, saved by revision, and gone once it is published.

The tests that run two writers at once use committing connections of their own, as two
requests would, and hold the first writer's transaction open until the second is waiting on it.
"""

import os
import threading
import time
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.rows import dict_row

from kittum.ledger import PostgresLedger
from kittum.protocol import (
    PUBLISHED_EVENT,
    StaleDraft,
    discard_draft,
    get_draft,
    list_versions,
    publish_draft,
    save_draft,
    start_draft,
)
from tests.protocol.support import example_text, seed_project

if TYPE_CHECKING:
    from collections.abc import Callable

    from kittum.db import Tx

pytestmark = pytest.mark.postgres


def _url() -> str:
    url = os.environ.get("KITTUM_DATABASE_URL")
    if not url:
        pytest.skip("KITTUM_DATABASE_URL is not set")
    return url


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(_url(), row_factory=dict_row)


def _seeded() -> tuple[Any, Any]:
    with _connect() as setup:
        project, approver, _ = seed_project(setup)
        setup.commit()
    return project, approver


def _while_held(first: psycopg.Connection[Any], second: Callable[[], object]) -> object:
    """Run `second` on another thread until it waits on a lock `first` holds, then commit
    `first` and return what `second` returned or raised."""
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(second())
        except Exception as e:
            outcome.append(e)

    worker = threading.Thread(target=run)
    with psycopg.connect(_url(), autocommit=True) as monitor:
        worker.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and worker.is_alive():
            waiting = monitor.execute(
                "select 1 from pg_stat_activity"
                " where wait_event_type = 'Lock' and query like '%protocol_draft%'"
            ).fetchone()
            if waiting:
                break
            time.sleep(0.02)
        else:
            pytest.fail("the second writer never waited on the first")
    first.commit()
    worker.join(timeout=10)
    (result,) = outcome
    return result


def test_a_draft_is_saved_by_revision_and_appends_nothing_to_the_ledger(pg: Tx) -> None:
    project, _, _ = seed_project(pg)
    assert get_draft(pg, project) is None
    draft = start_draft(pg, project, "title: one")
    assert (draft.revision, draft.source) == (1, "title: one")
    assert start_draft(pg, project, "title: other") == draft  # the open draft, unchanged

    saved = save_draft(pg, project, 1, "title: two")
    assert (saved.revision, saved.source) == (2, "title: two")
    with pytest.raises(StaleDraft, match="changed elsewhere") as stale:
        save_draft(pg, project, 1, "title: written on the old text")
    assert stale.value.current == saved
    assert get_draft(pg, project) == saved

    with pytest.raises(StaleDraft):
        discard_draft(pg, project, 1)
    discard_draft(pg, project, 2)
    assert get_draft(pg, project) is None
    with pytest.raises(StaleDraft, match="published or discarded elsewhere") as gone:
        save_draft(pg, project, 2, "title: three")
    assert gone.value.current is None
    assert PostgresLedger().events(pg, project) == []


def test_publishing_a_draft_removes_it_and_the_next_one_starts_afresh(pg: Tx) -> None:
    project, approver, _ = seed_project(pg)
    ledger = PostgresLedger()
    draft = start_draft(pg, project, example_text())
    published = publish_draft(pg, project, draft.revision, approver, ledger=ledger)
    assert get_draft(pg, project) is None
    assert [v.id for v in list_versions(pg, project)] == [published.id]
    assert [e.type for e in ledger.events(pg, project)] == [PUBLISHED_EVENT]
    again = start_draft(pg, project, "the next version")
    assert (again.revision, again.source) == (1, "the next version")


def test_a_stale_publication_writes_nothing(pg: Tx) -> None:
    project, approver, _ = seed_project(pg)
    ledger = PostgresLedger()
    start_draft(pg, project, example_text())
    saved = save_draft(pg, project, 1, example_text().replace("smoking", "tobacco", 1))
    with pytest.raises(StaleDraft) as stale:
        publish_draft(pg, project, 1, approver, ledger=ledger)
    assert stale.value.current == saved
    assert get_draft(pg, project) == saved
    assert list_versions(pg, project) == []
    assert ledger.events(pg, project) == []


def test_two_starts_at_once_open_one_draft() -> None:
    project, _ = _seeded()
    with _connect() as a, _connect() as b:
        mine = start_draft(a, project, "from a")
        theirs = _while_held(a, lambda: start_draft(b, project, "from b"))
        b.commit()
    assert theirs == mine
    with _connect() as check:
        (count,) = (
            check.execute("select count(*) from protocol_draft where project_id = %s", (project,))
            .fetchone()
            .values()
        )
    assert count == 1


def test_two_saves_on_one_revision_at_once_keep_the_first() -> None:
    project, _ = _seeded()
    with _connect() as setup:
        start_draft(setup, project, "text 1")
        setup.commit()
    with _connect() as a, _connect() as b:
        first = save_draft(a, project, 1, "from a")
        second = _while_held(a, lambda: save_draft(b, project, 1, "from b"))
        b.rollback()
    assert isinstance(second, StaleDraft)
    assert second.current == first
    with _connect() as check:
        assert get_draft(check, project) == first


def test_two_publications_of_one_draft_at_once_publish_it_once() -> None:
    project, approver = _seeded()
    ledger = PostgresLedger()
    with _connect() as setup:
        start_draft(setup, project, example_text())
        setup.commit()
    with _connect() as a, _connect() as b:
        published = publish_draft(a, project, 1, approver, ledger=ledger)
        second = _while_held(a, lambda: publish_draft(b, project, 1, approver, ledger=ledger))
        b.rollback()
    assert isinstance(second, StaleDraft)
    assert second.current is None
    with _connect() as check:
        assert [v.id for v in list_versions(check, project)] == [published.id]
        assert get_draft(check, project) is None

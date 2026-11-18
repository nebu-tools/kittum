import os
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest

from kittum.db import Database
from kittum.db.testing import rollback_tx

if TYPE_CHECKING:
    from collections.abc import Iterator

    from kittum.db import Tx

MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"


@pytest.fixture
def pg() -> Iterator[Tx]:
    """A migrated test database connection, rolled back after the test.

    Start one with `docker compose -f infra/compose.yaml up -d --wait postgres` and
    `docker compose -f infra/compose.yaml run --rm migrate`, then set KITTUM_DATABASE_URL.
    """
    if not os.environ.get("KITTUM_DATABASE_URL"):
        pytest.skip("KITTUM_DATABASE_URL is not set")
    with rollback_tx() as tx:
        yield tx


def _with_database(url: str, name: str) -> str:
    split = urlsplit(url)
    return urlunsplit((split.scheme, split.netloc, f"/{name}", split.query, split.fragment))


def _admin(url: str) -> psycopg.Connection[Any]:
    return psycopg.connect(_with_database(url, "postgres"), autocommit=True)


@pytest.fixture(scope="session")
def schema_template() -> Iterator[str]:
    """A database built in this test run from the one schema file, for `empty_database` to copy.

    It is built from the file's `migrate:up` section as dbmate runs it, so a test on a copy sees
    exactly what a new deployment sees, and nothing an earlier test or an older schema left."""
    url = os.environ.get("KITTUM_DATABASE_URL")
    if not url:
        pytest.skip("KITTUM_DATABASE_URL is not set")
    (schema,) = MIGRATIONS.glob("*.sql")
    up = schema.read_text().split("-- migrate:down")[0]
    name = f"kittum_schema_{uuid4().hex[:12]}"
    with _admin(url) as admin:
        admin.execute(f'create database "{name}"')
    try:
        with psycopg.connect(_with_database(url, name)) as conn:
            conn.execute(up)  # ty: ignore[no-matching-overload]
        yield name
    finally:
        with _admin(url) as admin:
            admin.execute(f'drop database if exists "{name}" with (force)')


@contextmanager
def fresh_database(schema_template: str) -> Iterator[Database]:
    """A database of its own, created empty from the schema file in this run.

    KITTUM_DATABASE_URL points at it while it is open, so the command line and the API use it
    too. A test that claims to prove the demo or the handover bundle uses this, not the shared
    test database, where rows other tests left behind could stand in for a step that never ran."""
    url = os.environ["KITTUM_DATABASE_URL"]
    name = f"kittum_empty_{uuid4().hex[:12]}"
    with _admin(url) as admin:
        admin.execute(f'create database "{name}" template "{schema_template}"')
    own = _with_database(url, name)
    db = Database(own, max_size=4)
    try:
        with pytest.MonkeyPatch.context() as env:
            env.setenv("KITTUM_DATABASE_URL", own)
            yield db
    finally:
        db.close()
        with _admin(url) as admin:
            admin.execute(f'drop database if exists "{name}" with (force)')


@pytest.fixture
def empty_database(schema_template: str) -> Iterator[Database]:
    """`fresh_database` for one test."""
    with fresh_database(schema_template) as db:
        yield db

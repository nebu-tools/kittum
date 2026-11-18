"""A committing database of the module's own and the two reviews, built once per module: every
test reads them at fixed ledger positions, so the renders and seals that tests add later do not
change what earlier positions show."""

from typing import TYPE_CHECKING

import pytest

from kittum.db.testing import rollback_tx
from tests.conftest import fresh_database
from tests.extraction.conftest import env  # noqa: F401 - extraction's project, for test_extracted
from tests.report.support import nrt_review, tranexamic_review

if TYPE_CHECKING:
    from collections.abc import Iterator

    from kittum.db import Database, Tx
    from tests.report.support import NrtSeqs, ReviewWorld


@pytest.fixture(scope="module")
def database(schema_template: str) -> Iterator[Database]:
    """The module's own database, empty from the schema file: the sealed bundles prove only
    what these reviews put there."""
    with fresh_database(schema_template) as db:
        yield db


@pytest.fixture(scope="module")
def nrt(database: Database) -> tuple[ReviewWorld, NrtSeqs]:
    return nrt_review(database)


@pytest.fixture(scope="module")
def tranexamic(database: Database) -> ReviewWorld:
    return tranexamic_review(database)


@pytest.fixture
def pg(database: Database) -> Iterator[Tx]:  # noqa: ARG001 - points KITTUM_DATABASE_URL at it
    """A rolled-back connection to the module's own database, for the tests that seal on one."""
    with rollback_tx() as tx:
        yield tx

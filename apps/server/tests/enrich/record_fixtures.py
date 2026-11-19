"""Record the live responses that the enrichment tests replay.

Run once, by hand, with a database available (the enrichment writes rows):

    KITTUM_DATABASE_URL=... uv run python -m tests.enrich.record_fixtures

This is not a test. It talks to OpenAlex, Crossref, Europe PMC, Unpaywall, and PubTator 3. A URL
the manifest already holds is answered from it, so a run records only what is new (`--all`
records everything again).
"""

import json
import os
import uuid
from typing import TYPE_CHECKING

import psycopg

from kittum.core import sha256_hex
from kittum.enrich import EnrichmentConfig, crossref, enrich
from kittum.gateway import (
    Credentials,
    HttpRequest,
    HttpResponse,
    HttpxTransport,
    recording_key,
    unreached,
)
from kittum.gateway.testing import PassThroughGateway
from tests.enrich.support import (
    CREDENTIALS,
    DERIVED_RELATION_FIXTURE,
    FIXTURES,
    RETRACTION_NOTICE,
    RETRACTION_ORIGINAL,
    config,
    doi_only_records,
    non_oa_records,
    source_records,
)

if TYPE_CHECKING:
    from pathlib import Path

    from kittum.db import Tx
    from kittum.gateway import Gateway
    from kittum.search import SourceRecord


class RecordingTransport:
    """Sends real requests and keeps each response, scrubbed of `credentials`, under the
    request's public key."""

    def __init__(
        self, directory: Path, credentials: Credentials = CREDENTIALS, *, again: bool = False
    ) -> None:
        self._directory = directory
        self._credentials = credentials
        self._inner = HttpxTransport()
        self._manifest: dict[str, str] = {}
        manifest = directory / "manifest.json"
        self._have: dict[str, str] = (
            {} if again or not manifest.exists() else json.loads(manifest.read_text())
        )

    def send(self, request: HttpRequest) -> HttpResponse:
        key = recording_key(request, self._credentials)
        if key in self._have:
            body = (self._directory / self._have[key]).read_bytes()
            self._manifest[key] = self._have[key]
            return HttpResponse(status=200, headers=(), body=body, body_hash=sha256_hex(body))
        response = self._credentials.scrub(self._inner.send(request))
        filename = f"{sha256_hex(key.encode())[:16]}.json"
        (self._directory / filename).write_bytes(response.body)
        self._manifest[key] = filename
        return response

    def write_manifest(self) -> None:
        manifest = self._directory / "manifest.json"
        existing: dict[str, str] = json.loads(manifest.read_text()) if manifest.exists() else {}
        existing.update(self._manifest)
        manifest.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")


class DownSources:
    """A gateway where some sources are out of budget, for the fallback recordings."""

    def __init__(self, inner: PassThroughGateway, *sources: str) -> None:
        self._inner = inner
        self._sources = set(sources)

    def fetch(self, source: str, request: HttpRequest) -> HttpResponse:
        if source in self._sources:
            raise unreached(source, TimeoutError(f"{source} is out of budget"))
        return self._inner.fetch(source, request)

    def configured(self, source: str) -> bool:
        return self._inner.configured(source)


def _run(
    records: tuple[SourceRecord, ...],
    gateway: Gateway,
    tx: Tx,
    config: EnrichmentConfig,
) -> None:
    enrich(tx, uuid.UUID(int=6), records, gateway, config=config)


def main() -> None:
    import sys

    url = os.environ["KITTUM_DATABASE_URL"]
    transport = RecordingTransport(FIXTURES, again="--all" in sys.argv)
    gateway = PassThroughGateway(transport, CREDENTIALS)
    openalex_down = DownSources(gateway, "openalex")
    metadata_down = DownSources(gateway, "openalex", "europepmc")
    with psycopg.connect(url) as connection, connection.transaction(force_rollback=True):
        connection.execute(
            "insert into project (id, name, policy_version) values (%s, 'wp06', '1')",
            (uuid.UUID(int=6),),
        )
        _run(source_records(), gateway, connection, config())
        _run(source_records(), openalex_down, connection, config())
        _run(doi_only_records(), gateway, connection, config())
        _run(doi_only_records(), metadata_down, connection, config())
        _run(non_oa_records(), gateway, connection, config())
        for doi in (RETRACTION_ORIGINAL, RETRACTION_NOTICE):
            crossref.work(gateway, doi)
    transport.write_manifest()
    _derive_relation_fixture(transport)
    print(f"recorded {len(transport._manifest)} responses in {FIXTURES}")


def _derive_relation_fixture(transport: RecordingTransport) -> None:
    """Copy a recorded work and state an incoming `is-retracted-by` relation.

    Crossref's live API returns `updated-by` for the cases we could find and rejects
    `is-retracted-by` as a relation filter, but publishers can deposit relation kinds that the
    filter list does not cover. The rest of the document is the real recorded original.
    """
    url = crossref._url(RETRACTION_ORIGINAL)
    body = json.loads((FIXTURES / transport._manifest[url]).read_bytes())
    message = body["message"]
    message["updated-by"] = []
    message["relation"] = {
        "is-retracted-by": [{"id-type": "doi", "id": RETRACTION_NOTICE, "asserted-by": "object"}]
    }
    (FIXTURES / DERIVED_RELATION_FIXTURE).write_text(json.dumps(body, indent=1) + "\n")


if __name__ == "__main__":
    main()

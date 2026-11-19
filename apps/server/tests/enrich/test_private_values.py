"""The deployment's private values reach a source and nothing else.

Each test gives the gateway marker values, has a source echo them back in a different place, and
checks what the gateway returns, caches (`gateway_cache` and its artifact), records
(`gateway_attempt`), and raises. The last test walks every registered source through its own
adapter.
"""

import contextlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote_plus, urlencode, urlsplit

import pytest

from kittum.core import FixedClock, sha256_hex
from kittum.db import Database
from kittum.enrich import WorkIds, crossref, europepmc, lookup_full_text, openalex, pubtator
from kittum.enrich import unpaywall as unpaywall_adapter
from kittum.gateway import (
    CONTACT_PLACEHOLDER,
    PLACEMENTS,
    Credentials,
    HttpRequest,
    HttpResponse,
    SourceGateway,
    SourceUnavailable,
    default_policies,
    placements,
    recording_key,
)
from kittum.search import CtgovSource, PubMedSource, SearchPlan
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from kittum.gateway import Gateway

pytestmark = pytest.mark.postgres

ADDRESS = "librarian@example.org"
MARKERS = {
    "contact_email": ADDRESS,
    "ncbi_api_key": "ncbi-marker-7f3a",
    "openalex_api_key": "openalex-marker-19c2",
    "semantic_scholar_api_key": "s2-marker-55d0",
    "core_api_key": "core-marker-a81e",
}
PASSWORD = "db-password-marker-0b4e"
CREDENTIALS = Credentials.of((PASSWORD,), **MARKERS)
UNPAYWALL = "https://api.unpaywall.org/v2/10.1000/x"
GATEWAY_TABLES = "gateway_attempt, gateway_cache, gateway_budget, gateway_cooldown"


@pytest.fixture
def database() -> Iterator[Database]:
    """The test database, with the gateway's tables emptied before and after each test."""
    url = os.environ.get("KITTUM_DATABASE_URL")
    if not url:
        pytest.skip("KITTUM_DATABASE_URL is not set")
    db = Database(url, max_size=2)
    try:
        with db.transaction() as tx:
            tx.execute(f"truncate {GATEWAY_TABLES}")
        yield db
        with db.transaction() as tx:
            tx.execute(f"truncate {GATEWAY_TABLES}")
    finally:
        db.close()


def _wire_url(wire: HttpRequest) -> str:
    return f"{wire.url}?{urlencode(wire.params)}" if wire.params else wire.url


def _json(data: object) -> HttpResponse:
    body = json.dumps(data).encode()
    return HttpResponse(
        status=200,
        headers=(("Content-Type", "application/json"),),
        body=body,
        body_hash=sha256_hex(body),
    )


class Script:
    """Answers each request with `answer(wire)`, the request as it went out, and keeps both."""

    def __init__(self, answer: Callable[[HttpRequest], HttpResponse]) -> None:
        self._answer = answer
        self.sent: list[HttpRequest] = []
        self.wire: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.sent.append(request)
        wire = request.on_the_wire()
        self.wire.append(wire)
        return self._answer(wire)


def _echo(wire: HttpRequest) -> HttpResponse:
    """A source that says back everything it was sent: in the body, a `Content-Location`, and
    the URL it redirected to."""
    url = _wire_url(wire)
    answer = _json({"url": url, "headers": list(wire.headers), "said": ADDRESS})
    return answer.model_copy(
        update={
            "headers": (*answer.headers, ("Content-Location", url), ("Location", url)),
            "url": url,
        }
    )


def _gateway(
    database: Database,
    transport: Script,
    store: MemoryStore,
    credentials: Credentials = CREDENTIALS,
) -> SourceGateway:
    clock = FixedClock(datetime(2026, 11, 18, 9, 0, tzinfo=UTC))
    return SourceGateway(
        database=database,
        transport=transport,
        artifacts=store,
        policies=default_policies(credentials),
        credentials=credentials,
        clock=clock,
        buckets="memory",
        max_attempts=2,
        sleep=lambda seconds: clock.advance(timedelta(seconds=seconds)),
    )


def _kept(database: Database, store: MemoryStore) -> bytes:
    """Everything the gateway kept: its cache rows with their bodies, and its attempts."""
    with database.transaction() as tx:
        cache = tx.execute("select * from gateway_cache order by request_hash").fetchall()
        attempts = tx.execute("select * from gateway_attempt order by id").fetchall()
    bodies = [store.get(row["body_hash"]) for row in cache]
    return json.dumps([cache, attempts], default=str).encode() + b"".join(bodies)


def _assert_kept_clean(database: Database, store: MemoryStore) -> None:
    kept = _kept(database, store)
    assert not CREDENTIALS.holds(kept), kept[:2000]


def test_a_200_body_that_echoes_the_address_is_kept_and_returned_without_it(
    database: Database,
) -> None:
    store = MemoryStore()
    transport = Script(_echo)
    got = _gateway(database, transport, store).fetch("unpaywall", HttpRequest(url=UNPAYWALL))

    assert ("email", ADDRESS) in transport.wire[0].params
    assert not CREDENTIALS.holds(got.body)
    assert json.loads(got.body)["said"] == CONTACT_PLACEHOLDER
    assert got.body_hash == sha256_hex(got.body)
    _assert_kept_clean(database, store)
    with database.transaction() as tx:
        (row,) = tx.execute("select body_hash from gateway_cache").fetchall()
    assert store.get(row["body_hash"]) == got.body


def test_a_content_location_that_echoes_the_address_is_kept_without_it(
    database: Database,
) -> None:
    store = MemoryStore()
    got = _gateway(database, Script(_echo), store).fetch("unpaywall", HttpRequest(url=UNPAYWALL))

    location = dict(got.headers)["Content-Location"]
    assert location == f"{UNPAYWALL}?email={quote_plus(CONTACT_PLACEHOLDER)}"
    with database.transaction() as tx:
        (row,) = tx.execute("select headers from gateway_cache").fetchall()
    assert ["Content-Location", location] in row["headers"]
    _assert_kept_clean(database, store)


def test_an_error_body_with_a_bare_address_is_quoted_without_it(database: Database) -> None:
    def refusing(_: HttpRequest) -> HttpResponse:
        body = json.dumps({"message": f"Too many requests for {ADDRESS}."}).encode()
        return HttpResponse(status=503, headers=(), body=body, body_hash=sha256_hex(body))

    store = MemoryStore()
    with pytest.raises(SourceUnavailable) as raised:
        _gateway(database, Script(refusing), store).fetch("unpaywall", HttpRequest(url=UNPAYWALL))

    assert raised.value.message == f"Too many requests for {CONTACT_PLACEHOLDER}."
    assert not CREDENTIALS.holds(str(raised.value).encode())
    _assert_kept_clean(database, store)


def test_an_error_that_quotes_a_relative_url_is_recorded_without_the_address(
    database: Database,
) -> None:
    def broken(wire: HttpRequest) -> HttpResponse:
        path = urlsplit(wire.url).path
        raise ConnectionError(f"redirect loop at {path}?{urlencode(wire.params)}")

    store = MemoryStore()
    with pytest.raises(SourceUnavailable) as raised:
        _gateway(database, Script(broken), store).fetch("unpaywall", HttpRequest(url=UNPAYWALL))

    assert raised.value.reason == "the connection broke off (ConnectionError)"
    with database.transaction() as tx:
        errors = [r["error"] for r in tx.execute("select error from gateway_attempt").fetchall()]
    assert errors == [
        f"ConnectionError: redirect loop at /v2/10.1000/x?email={quote_plus(CONTACT_PLACEHOLDER)}"
    ]
    _assert_kept_clean(database, store)


def test_query_data_in_a_param_named_key_reaches_the_source_and_is_kept(
    database: Database,
) -> None:
    store = MemoryStore()
    transport = Script(_echo)
    request = HttpRequest(
        url="https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        params=(("query", "diabetes"), ("key", "HbA1c")),
    )
    got = _gateway(database, transport, store).fetch("europepmc", request)

    assert transport.wire[0].params == (("query", "diabetes"), ("key", "HbA1c"))
    assert json.loads(got.body)["url"].endswith("?query=diabetes&key=HbA1c")
    assert recording_key(request, CREDENTIALS).endswith("?query=diabetes&key=HbA1c")
    _assert_kept_clean(database, store)


@pytest.mark.parametrize("source", ["unpaywall", "openalex", "semanticscholar", "core"])
def test_a_pdf_link_a_source_found_gets_none_of_its_values_and_needs_none(
    database: Database, source: str
) -> None:
    """A publisher's PDF link is fetched under the source that found it, but only that source's
    API gets its settings: the publisher sees no key and no address but the User-Agent's, and a
    source whose API needs a setting still lets its links be fetched without it."""
    pdf = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n\xff\xfe binary content"
    answer = HttpResponse(
        status=200,
        headers=(("Content-Type", "application/pdf"),),
        body=pdf,
        body_hash=sha256_hex(pdf),
    )
    link = HttpRequest(url="https://publisher.example.org/article.pdf")
    for credentials in (CREDENTIALS, Credentials.of()):
        transport = Script(lambda _: answer)
        got = _gateway(database, transport, MemoryStore(), credentials).fetch(source, link)
        (wire,) = transport.wire
        assert wire.params == ()
        names = {n.lower() for n, _ in wire.headers}
        assert not names & {"authorization", "x-api-key"}, wire.headers
        assert got.body == pdf  # a binary body is left as it is
        with database.transaction() as tx:
            tx.execute(f"truncate {GATEWAY_TABLES}")


def _plan(source: str, strategy: str) -> SearchPlan:
    return SearchPlan(topic_id="t", source=source, platform="", strategy=strategy)


ADAPTERS: dict[str, Callable[[Gateway], object]] = {
    "pubmed": lambda g: PubMedSource(g, MemoryStore()).run(_plan("pubmed", "asthma[tiab]")),
    "clinicaltrials": lambda g: CtgovSource(g, MemoryStore()).run(_plan("ctgov", "asthma")),
    "europepmc": lambda g: europepmc.works(g, pmids=["40116747"]),
    "openalex": lambda g: openalex.works(g, dois=["10.1000/x"]),
    "crossref": lambda g: crossref.work(g, "10.1000/x"),
    "unpaywall": lambda g: unpaywall_adapter.best_oa_location(g, "10.1000/x"),
    "pubtator": lambda g: pubtator.annotations(g, ["40116747"]),
    "pmc": lambda g: lookup_full_text(g, "pmc", WorkIds(doi="10.1000/x", pmid="40116747")),
    "biorxiv": lambda g: lookup_full_text(g, "biorxiv", WorkIds(doi="10.1101/2023.12.01.569634")),
    "arxiv": lambda g: lookup_full_text(g, "arxiv", WorkIds(doi="10.48550/arXiv.2101.00001")),
    "semanticscholar": lambda g: lookup_full_text(g, "semanticscholar", WorkIds(doi="10.1000/x")),
    "core": lambda g: lookup_full_text(g, "core", WorkIds(doi="10.1000/x")),
}


def test_every_source_adapter_sends_its_private_values_and_keeps_none(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    assert set(ADAPTERS) == set(default_policies())
    assert set(PLACEMENTS) <= set(ADAPTERS)
    caplog.set_level(logging.DEBUG)
    for source, adapter in ADAPTERS.items():
        store = MemoryStore()
        transport = Script(_echo)
        result: object = None
        with contextlib.suppress(Exception):  # the echo is not the source's format
            result = adapter(_gateway(database, transport, store))
        assert transport.sent, f"{source}: the adapter sent nothing"

        # On the wire: every placement of the source, on the host it names.
        for placement in placements(source):
            value = placement.template.format(MARKERS[placement.setting])
            hosts = [urlsplit(w.url).hostname for w in transport.wire]
            assert placement.host is None or placement.host in hosts, (source, placement, hosts)
            for wire in transport.wire:
                if placement.host not in (None, urlsplit(wire.url).hostname):
                    continue
                said = [*wire.params, *wire.headers]
                if placement.kind == "user_agent":
                    assert any(n == "User-Agent" and value in v for n, v in said), source
                else:
                    assert (placement.name, value) in said, (source, placement.name)

        # Kept: the request as the adapter built it, what it made of the answer, the cache,
        # the attempts, and the log.
        for request in transport.sent:
            assert not CREDENTIALS.holds(request.model_dump_json().encode()), source
            assert not CREDENTIALS.holds(repr(request).encode()), source
        assert not CREDENTIALS.holds(repr(result).encode()), source
        _assert_kept_clean(database, store)
        assert not CREDENTIALS.holds(caplog.text.encode()), source
        with database.transaction() as tx:
            tx.execute(f"truncate {GATEWAY_TABLES}")

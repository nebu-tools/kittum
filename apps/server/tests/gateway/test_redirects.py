"""The gateway follows redirects itself, one hop at a time, through the real HTTP client.

Each hop is built again without private values, and gets only the values for its own host, none
over plain http. Every hop's response is scrubbed, and the attempt row lists the hops as built.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx2
import pytest

from kittum.core import FixedClock
from kittum.gateway import (
    MAX_REDIRECTS,
    Credentials,
    HttpRequest,
    HttpxTransport,
    SourceGateway,
    SourceUnavailable,
    default_policies,
)
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from kittum.db import Database

pytestmark = pytest.mark.postgres

ADDRESS = "librarian@example.org"
OPENALEX_KEY = "openalex-marker-19c2"
NCBI_KEY = "ncbi-marker-7f3a"
CREDENTIALS = Credentials.of(
    contact_email=ADDRESS, openalex_api_key=OPENALEX_KEY, ncbi_api_key=NCBI_KEY
)
WORKS = "https://api.openalex.org/works/W1"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


class Server:
    """Answers through `httpx2.MockTransport` by URL, and keeps every request it was sent."""

    def __init__(self, answer: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.sent: list[httpx2.Request] = []

        def handle(request: httpx2.Request) -> httpx2.Response:
            self.sent.append(request)
            return answer(request)

        self.transport = HttpxTransport(
            client=httpx2.Client(transport=httpx2.MockTransport(handle), follow_redirects=False)
        )

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.sent]

    def wire(self, index: int) -> bytes:
        """The request as it went out: its URL and headers."""
        request = self.sent[index]
        return json.dumps([str(request.url), list(request.headers.multi_items())]).encode()


def _moved(location: str, status: int = 302) -> httpx2.Response:
    return httpx2.Response(status, headers={"Location": location})


def _gateway(database: Database, server: Server, store: MemoryStore) -> SourceGateway:
    clock = FixedClock(datetime(2026, 11, 18, 9, 0, tzinfo=UTC))
    return SourceGateway(
        database=database,
        transport=server.transport,
        artifacts=store,
        policies=default_policies(CREDENTIALS),
        credentials=CREDENTIALS,
        clock=clock,
        buckets="memory",
        max_attempts=1,
        sleep=lambda seconds: clock.advance(timedelta(seconds=seconds)),
    )


def _attempts(database: Database) -> list[dict[str, object]]:
    with database.transaction() as tx:
        return tx.execute(
            "select status, error, redirects from gateway_attempt order by id"
        ).fetchall()


def _kept(database: Database, store: MemoryStore) -> bytes:
    with database.transaction() as tx:
        cache = tx.execute("select * from gateway_cache").fetchall()
        attempts = tx.execute("select * from gateway_attempt").fetchall()
    bodies = b"".join(store.get(row["body_hash"]) for row in cache)
    return json.dumps([cache, attempts], default=str).encode() + bodies


def test_a_redirect_on_the_same_host_keeps_the_hosts_key(database: Database) -> None:
    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/works/W1":
            return _moved("/works/W2", 301)
        return httpx2.Response(200, json={"id": "W2"})

    server = Server(answer)
    store = MemoryStore()
    got = _gateway(database, server, store).fetch("openalex", HttpRequest(url=WORKS))

    assert json.loads(got.body) == {"id": "W2"}
    assert got.url == "https://api.openalex.org/works/W2"
    assert [r.headers["authorization"] for r in server.sent] == [f"Bearer {OPENALEX_KEY}"] * 2
    assert _attempts(database) == [
        {"status": 200, "error": None, "redirects": ["https://api.openalex.org/works/W2"]}
    ]
    assert not CREDENTIALS.holds(_kept(database, store))


def test_a_redirect_to_another_host_gets_none_of_the_first_hosts_values(
    database: Database,
) -> None:
    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "api.openalex.org":
            return _moved("https://files.example.org/W1.pdf")
        return httpx2.Response(200, content=b"%PDF-1.7 ...")

    server = Server(answer)
    store = MemoryStore()
    _gateway(database, server, store).fetch(
        "openalex", HttpRequest(url=WORKS, headers=(("Cookie", "session=1"),))
    )

    assert server.urls() == [WORKS, "https://files.example.org/W1.pdf"]
    assert OPENALEX_KEY.encode() in server.wire(0)
    assert not Credentials.of(openalex_api_key=OPENALEX_KEY).holds(server.wire(1))
    assert "authorization" not in server.sent[1].headers
    assert "cookie" not in server.sent[1].headers
    assert not CREDENTIALS.holds(_kept(database, store))


def test_a_redirect_to_plain_http_carries_no_private_value(database: Database) -> None:
    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.scheme == "https":
            return _moved("http://api.openalex.org/works/W1", 301)
        return httpx2.Response(200, json={})

    server = Server(answer)
    _gateway(database, server, MemoryStore()).fetch("openalex", HttpRequest(url=WORKS))

    assert server.urls() == [WORKS, "http://api.openalex.org/works/W1"]
    assert ADDRESS.encode() in server.wire(0)
    assert not CREDENTIALS.holds(server.wire(1))


def test_a_redirect_loop_is_a_source_error_with_the_chain_recorded(database: Database) -> None:
    def answer(request: httpx2.Request) -> httpx2.Response:
        return _moved("/works/W2" if request.url.path == "/works/W1" else "/works/W1")

    server = Server(answer)
    with pytest.raises(SourceUnavailable, match="the redirects loop"):
        _gateway(database, server, MemoryStore()).fetch("openalex", HttpRequest(url=WORKS))

    assert len(server.sent) == 2
    assert _attempts(database) == [
        {
            "status": 302,
            "error": "the redirects loop",
            "redirects": ["https://api.openalex.org/works/W2", WORKS],
        }
    ]


def test_too_many_redirects_is_a_source_error(database: Database) -> None:
    def answer(request: httpx2.Request) -> httpx2.Response:
        return _moved(f"/works/W{int(request.url.path.rsplit('W', 1)[1]) + 1}", 307)

    server = Server(answer)
    with pytest.raises(SourceUnavailable, match=f"more than {MAX_REDIRECTS} redirects"):
        _gateway(database, server, MemoryStore()).fetch("openalex", HttpRequest(url=WORKS))

    assert len(server.sent) == MAX_REDIRECTS + 1
    (attempt,) = _attempts(database)
    assert len(attempt["redirects"]) == MAX_REDIRECTS  # type: ignore[arg-type]


def test_a_key_in_a_location_query_is_dropped_and_placed_again_for_its_host(
    database: Database,
) -> None:
    def answer(request: httpx2.Request) -> httpx2.Response:
        if "efetch" in request.url.path:
            # The source echoes the key back in the redirect, percent-encoded.
            return _moved(f"/entrez/eutils/esummary.fcgi?id=1&api_key={quote(NCBI_KEY)}%20")
        return httpx2.Response(200, text="<ok/>", headers={"content-type": "text/xml"})

    server = Server(answer)
    store = MemoryStore()
    _gateway(database, server, store).fetch(
        "pubmed", HttpRequest(url=EFETCH, params=(("id", "1"),))
    )

    second = server.sent[1]
    assert second.url.path == "/entrez/eutils/esummary.fcgi"
    assert second.url.params.get_list("api_key") == [NCBI_KEY]
    assert second.url.params["id"] == "1"
    ((redirect,),) = [a["redirects"] for a in _attempts(database)]
    assert redirect == "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?id=1"
    assert not CREDENTIALS.holds(_kept(database, store))


def test_a_location_that_holds_a_private_value_outside_the_query_is_refused(
    database: Database,
) -> None:
    def answer(_: httpx2.Request) -> httpx2.Response:
        return _moved(f"https://mirror.example.org/{quote(ADDRESS, safe='')}/W1")

    server = Server(answer)
    store = MemoryStore()
    with pytest.raises(SourceUnavailable, match="holds a configured private value"):
        _gateway(database, server, store).fetch("openalex", HttpRequest(url=WORKS))

    assert len(server.sent) == 1
    assert not CREDENTIALS.holds(_kept(database, store))

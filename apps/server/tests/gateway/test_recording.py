"""Recording the sources' responses in a live run, and replaying them offline.

A recording is keyed by the request as the caller built it, which holds no private value: the
gateway attaches the contact address and the keys only for the send. A request that would put a
private value into a key anyway is refused before it is sent, and what a source echoes back is
scrubbed before it is written. A replay must answer as the sources did, in order: a source that
failed and later recovered fails and recovers again. The live run sees the same bytes its replay
will, so their hashes agree.
"""

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx2
import pytest

from kittum.core import FixedClock, sha256_hex
from kittum.gateway import (
    CONTACT_PLACEHOLDER,
    REDACTED,
    Credentials,
    HttpRequest,
    HttpResponse,
    NotRecorded,
    RecordedTransport,
    RecordingRefused,
    RecordingTransport,
    SourceGateway,
    SourcePolicy,
    SourceUnavailable,
)
from kittum.gateway.testing import response
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    from pathlib import Path

    from kittum.db import Database

ALICE = "alice@example.org"
SECRET = "cs-9f8e7d6c5b4a"
ALICES = Credentials.of((SECRET,), contact_email=ALICE, ncbi_api_key="ncbi-key-1234")
UNPAYWALL = "https://api.unpaywall.org/v2/10.1186%2Fs12916-026-04943-4"
UNREACHABLE = "https://api.unpaywall.org/v2/10.1186%2Fs12916-026-05037-x"


class Sources:
    """Answers each URL from its own script, in order, and remembers what was sent."""

    def __init__(self, scripts: dict[str, list[HttpResponse | Exception]]) -> None:
        self.scripts = scripts
        self.sent: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.sent.append(request)
        answer = self.scripts[request.url].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _written(directory: Path) -> bytes:
    return b"".join(p.read_bytes() for p in directory.iterdir() if p.is_file())


def test_a_recording_is_keyed_by_the_public_request_and_holds_no_private_value(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "sources"
    echo = json.dumps({"message": "ok", "asked_by": ALICE, "key": "ncbi-key-1234"}).encode()
    ok = response(200, echo, (("Content-Type", "application/json"), ("X-Echo", SECRET)))
    live = Sources(
        {
            "https://api.crossref.org/works/10.1000%2Fx": [ok],
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi": [ok],
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={CONTACT_PLACEHOLDER}": [
                ok
            ],
        }
    )
    recording = RecordingTransport(live, directory, credentials=ALICES)

    def refused(request: HttpRequest, match: str) -> None:
        with pytest.raises(RecordingRefused, match=match) as caught:
            recording.send(request)
        assert SECRET not in str(caught.value) and ALICE not in str(caught.value)

    # A private value a caller put into the URL or the query, in any listed spelling: refused, unnamed.
    refused(
        HttpRequest(
            url="https://api.crossref.org/works/10.1000%2Fx", params=(("client_secret", SECRET),)
        ),
        "configured private value",
    )
    refused(HttpRequest(url="https://reader:pw@api.crossref.org/works/10.1000%2Fx"), "user info")
    refused(
        HttpRequest(url=f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={ALICE}"),
        "configured private value",
    )
    refused(
        HttpRequest(url=f"https://api.crossref.org/works/10.1000%2Fx?query.author={quote(ALICE)}"),
        "configured private value",
    )
    assert live.sent == []  # nothing refused was sent

    # The gateway attaches the values for the send; the key is the request without them.
    crossref = ALICES.attach(
        "crossref", HttpRequest(url="https://api.crossref.org/works/10.1000%2Fx")
    )
    ncbi = ALICES.attach(
        "pubmed",
        HttpRequest(
            url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", params=(("id", "1"),)
        ),
    )
    search = HttpRequest(
        url=f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={CONTACT_PLACEHOLDER}"
    )
    for request in (crossref, ncbi, search):
        answered = recording.send(request)
        assert ALICE.encode() not in answered.body and SECRET not in str(answered.headers)
    assert ("mailto", ALICE) in live.sent[0].on_the_wire().params
    assert {("email", ALICE), ("api_key", "ncbi-key-1234")} <= set(
        live.sent[1].on_the_wire().params
    )
    manifest = json.loads((directory / "manifest.json").read_text())
    assert sorted(manifest) == [
        "https://api.crossref.org/works/10.1000%2Fx",
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?id=1",
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=contact%40example.invalid",
    ]
    written = _written(directory).lower()
    assert SECRET.encode() not in written and b"alice" not in written and b"ncbi-key" not in written
    assert CONTACT_PLACEHOLDER.encode() in written and REDACTED.encode() in written

    # A replay needs no credentials and answers the same public request.
    replay = RecordedTransport(directory)
    assert replay.send(HttpRequest(url="https://api.crossref.org/works/10.1000%2Fx")).body == (
        answered.body
    )
    with pytest.raises(NotRecorded):
        replay.send(
            HttpRequest(
                url="https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=bob@example.org"
            )
        )


@pytest.mark.postgres
def test_a_replay_answers_in_the_recorded_order_with_the_bytes_the_live_run_saw(
    database: Database, tmp_path: Path
) -> None:
    """Unpaywall answers 503 twice, so the first lookup gives up; asked again, it answers, with
    a body that names the contact address. For another DOI the connection fails, then a body
    cannot be decoded, then the redirects never end, before it answers. The replay gives up, fails
    each of those ways, and answers in the same way, with the same bytes and the same hash
    as the live run."""
    directory = tmp_path / "sources"
    echo = json.dumps({"doi": "10.1186/s12916-026-04943-4", "email": ALICE}).encode()
    live = Sources(
        {
            UNPAYWALL: [
                response(503),
                response(503),
                response(200, echo, (("Content-Type", "application/json"),)),
            ],
            UNREACHABLE: [
                httpx2.ConnectError(f"connection refused for {UNREACHABLE}?email={ALICE}"),
                httpx2.DecodingError("Error -3 while decompressing data: incorrect header check"),
                httpx2.TooManyRedirects("Exceeded maximum allowed redirects."),
                response(200, b"{}", (("Content-Type", "application/json"),)),
            ],
        }
    )
    clock = FixedClock(datetime(2026, 10, 28, 9, tzinfo=UTC))
    policy = SourcePolicy(
        source="unpaywall", requests_per_second=100, concurrency=2, terms_url="https://x.test"
    )

    def gateway(
        transport: RecordingTransport | RecordedTransport, credentials: Credentials
    ) -> SourceGateway:
        return SourceGateway(
            database=database,
            transport=transport,
            artifacts=MemoryStore(),
            policies={"unpaywall": policy},
            credentials=credentials,
            clock=clock,
            buckets="memory",
            cache="memory",
            max_attempts=2,
            sleep=lambda _seconds: None,
        )

    def session(
        transport: RecordingTransport | RecordedTransport, credentials: Credentials
    ) -> HttpResponse:
        sources = gateway(transport, credentials)
        with pytest.raises(SourceUnavailable):
            sources.fetch("unpaywall", HttpRequest(url=UNPAYWALL))
        for failure in ("ConnectError", "DecodingError", "TooManyRedirects"):
            with pytest.raises(SourceUnavailable) as raised:
                sources.fetch("unpaywall", HttpRequest(url=UNREACHABLE))
            assert raised.value.reason is not None and f"({failure})" in raised.value.reason
            assert "alice" not in str(raised.value)
        assert sources.fetch("unpaywall", HttpRequest(url=UNREACHABLE)).body == b"{}"
        return sources.fetch("unpaywall", HttpRequest(url=UNPAYWALL))

    answered = session(RecordingTransport(live, directory, credentials=ALICES), ALICES)
    assert answered.status == 200 and len(live.sent) == 7
    # A failure is recorded by its kind alone: its message quoted the address.
    assert b"alice" not in _written(directory).lower()
    # The live run gets the bytes the recording holds: the address is the placeholder.
    assert json.loads(answered.body)["email"] == CONTACT_PLACEHOLDER
    assert answered.body_hash == sha256_hex(answered.body)

    replay = RecordedTransport(directory)
    replayed = session(replay, Credentials.of(contact_email="bob@example.org"))
    assert (replayed.status, replayed.body, replayed.body_hash) == (
        200,
        answered.body,
        answered.body_hash,
    )
    # The live run asked three times; a fourth has nothing left to answer it.
    with pytest.raises(NotRecorded, match="no recorded response 4"):
        replay.send(HttpRequest(url=UNPAYWALL))

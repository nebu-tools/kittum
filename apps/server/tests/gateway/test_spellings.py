"""Each spelling of a private value the matcher recognises, in a response's URL, a header and the
body; text is told from binary by its bytes, whatever the content type says.

The list is the one in `kittum.core.private`; these tests take it one spelling at a time.
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx2
import pytest

from kittum.core import FixedClock, PrivateValues, sha256_hex
from kittum.gateway import (
    CONTACT_PLACEHOLDER,
    REDACTED,
    Credentials,
    HttpRequest,
    HttpResponse,
    PrivateValueInResponse,
    SourceGateway,
    default_policies,
)
from kittum.storage import MemoryStore
from tests.gateway.test_redirects import Server

if TYPE_CHECKING:
    from kittum.db import Database

SECRET = 'pass word"/@x-1234'
ADDRESS = "librarian@example.org"
CREDENTIALS = Credentials.of((SECRET,), contact_email=ADDRESS)


def _percent(value: str, upper: bool) -> str:
    return "".join(f"%{b:02X}" if upper else f"%{b:02x}" for b in value.encode())


def _spellings(value: str) -> dict[str, str]:
    data = value.encode()
    found = {
        "literal": value,
        "percent on every byte, lower case": _percent(value, upper=False),
        "percent on every byte, upper case": _percent(value, upper=True),
        "percent on one character only": value.replace("@", "%40", 1),
        "json unicode escape": json.dumps(value, ensure_ascii=False)[1:-1].replace("@", "\\u0040"),
        "json unicode escape, upper case": "".join(f"\\u{ord(c):04X}" for c in value),
        "json backslash escape": json.dumps(value)[1:-1].replace("/", "\\/"),
        "plus for a space": value.replace(" ", "+"),
    }
    for offset in range(3):
        encoded = base64.b64encode(b"\x00" * offset + data + b"\x00").decode()
        found[f"base64 at alignment {offset}"] = encoded
        found[f"base64url at alignment {offset}"] = encoded.replace("+", "-").replace("/", "_")
    return found


SPELLINGS = [
    pytest.param(value, spelled, id=f"{name}: {how}")
    for name, value in (("secret", SECRET), ("address", ADDRESS))
    for how, spelled in _spellings(value).items()
]


def _response(url: str, headers: tuple[tuple[str, str], ...], body: bytes) -> HttpResponse:
    return HttpResponse(status=200, url=url, headers=headers, body=body, body_hash=sha256_hex(body))


@pytest.mark.parametrize(("value", "spelled"), SPELLINGS)
def test_each_spelling_is_found(value: str, spelled: str) -> None:
    matcher = PrivateValues([SECRET, ADDRESS])
    assert matcher.find(f"seen: {spelled}.") == value
    assert matcher.find(f"seen: {spelled}.".encode()) == value


@pytest.mark.parametrize(("value", "spelled"), SPELLINGS)
def test_each_spelling_is_scrubbed_from_the_url_a_header_and_the_body(
    value: str, spelled: str
) -> None:
    stand_in = CONTACT_PLACEHOLDER if value == ADDRESS else REDACTED
    got = CREDENTIALS.scrub(
        _response(
            f"https://api.example.org/v1?echo={spelled}",
            (("Content-Type", "application/json"), ("X-Echo", f"a {spelled} b")),
            f'{{"echo": "{spelled}"}}'.encode(),
        )
    )
    for part in (got.url or "", dict(got.headers)["X-Echo"], got.body.decode()):
        assert not CREDENTIALS.holds(part), part
        assert stand_in in part or quote(stand_in, safe="") in part, part
    assert got.body_hash == sha256_hex(got.body)


def test_what_is_not_on_the_list_is_not_recognised() -> None:
    matcher = PrivateValues([ADDRESS])
    assert not matcher.holds(ADDRESS.upper())
    assert not matcher.holds(ADDRESS[:10] + "\n" + ADDRESS[10:])
    assert not matcher.holds(base64.b32encode(ADDRESS.encode()).decode())


def test_text_labelled_as_binary_is_scrubbed() -> None:
    body = f"plain text: {SECRET} and {ADDRESS}".encode()
    got = CREDENTIALS.scrub(_response("", (("Content-Type", "application/octet-stream"),), body))
    assert got.body == f"plain text: {REDACTED} and {CONTACT_PLACEHOLDER}".encode()


def test_text_is_scrubbed_in_its_declared_charset() -> None:
    body = f"café {SECRET}".encode("latin-1")
    got = CREDENTIALS.scrub(_response("", (("Content-Type", "text/plain; charset=latin-1"),), body))
    assert got.body == f"café {REDACTED}".encode("latin-1")


def test_a_binary_body_is_kept_as_it_came_unless_it_holds_a_value() -> None:
    binary = b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01"
    kept = _response("", (("Content-Type", "text/plain"),), binary)
    assert CREDENTIALS.scrub(kept).body == binary
    with pytest.raises(PrivateValueInResponse):
        CREDENTIALS.scrub(_response("", (), binary + ADDRESS.encode()))
    with pytest.raises(PrivateValueInResponse):
        CREDENTIALS.scrub(_response("", (), binary + base64.b64encode(SECRET.encode())))


@pytest.mark.postgres
def test_a_binary_response_that_holds_a_value_is_an_error_and_nothing_is_kept(
    database: Database,
) -> None:
    def answer(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"\xff\xd8\xff\xe0" + ADDRESS.encode() + b"\xff")

    clock = FixedClock(datetime(2026, 11, 18, 9, 0, tzinfo=UTC))
    store = MemoryStore()
    gateway = SourceGateway(
        database=database,
        transport=Server(answer).transport,
        artifacts=store,
        policies=default_policies(CREDENTIALS),
        credentials=CREDENTIALS,
        clock=clock,
        buckets="memory",
        max_attempts=1,
        sleep=lambda seconds: clock.advance(timedelta(seconds=seconds)),
    )
    request = HttpRequest(url="https://files.example.org/figure.jpg")
    for _ in range(2):  # and not served from a cache the second time
        with pytest.raises(PrivateValueInResponse):
            gateway.fetch("openalex", request)
    with database.transaction() as tx:
        assert tx.execute("select count(*) as n from gateway_cache").fetchone()["n"] == 0
        errors = [r["error"] for r in tx.execute("select error from gateway_attempt")]
    assert len(errors) == 2 and all("PrivateValueInResponse" in e for e in errors)
    assert not store.exists(sha256_hex(b"\xff\xd8\xff\xe0" + ADDRESS.encode() + b"\xff"))

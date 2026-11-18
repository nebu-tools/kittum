"""Values and seams that cross the gateway boundary."""

import re
from typing import Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from kittum.core import Record, Sha256


class SourcePolicy(Record):
    """What one source allows: its rate, concurrency, daily budget, and terms."""

    source: str
    requests_per_second: float
    concurrency: int
    daily_budget: int | None = None
    terms_url: str


class HttpRequest(Record):
    method: str = "GET"
    url: str
    params: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None


class HttpResponse(Record):
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    body_hash: Sha256
    from_cache: bool = False


class Transport(Protocol):
    """Sends one request. The seam between the gateway and the network."""

    def send(self, request: HttpRequest) -> HttpResponse: ...


def effective_request(request: HttpRequest) -> HttpRequest:
    """Merge a query already in `url` into `params`, where httpx keeps it.

    httpx drops a URL's own query as soon as `params` is passed, even an empty list. Sending and
    hashing both use this form, so they always mean the same request.
    """
    split = urlsplit(request.url)
    if not split.query:
        return request
    params = tuple(parse_qsl(split.query, keep_blank_values=True)) + tuple(request.params)
    url = urlunsplit((split.scheme, split.netloc, split.path, "", split.fragment))
    return HttpRequest(
        method=request.method,
        url=url,
        params=params,
        headers=request.headers,
        body=request.body,
    )


CREDENTIAL_PARAMS = frozenset({"api_key", "apikey", "key", "token", "access_token"})
"""Query parameters that carry a credential (NCBI's `api_key`)."""

_CONTACT_PARAMS = frozenset({"email", "mailto"})
"""Query parameters that carry the contact address (NCBI and Unpaywall's `email`, Crossref and
OpenAlex's `mailto`)."""


def _private(name: str, value: str) -> bool:
    name = name.lower()
    return name in _CONTACT_PARAMS or name in CREDENTIAL_PARAMS or (name == "tool" and "@" in value)


def public_url(url: str) -> str:
    """`url` as Kittum stores and logs it: without the contact address (`email`, `mailto`, and a
    `tool` that holds an address), credentials, or user info. A URL with none of them comes back
    as it is."""
    split = urlsplit(url)
    pairs = parse_qsl(split.query, keep_blank_values=True)
    kept = [(n, v) for n, v in pairs if not _private(n, v)]
    netloc = split.netloc.rpartition("@")[2]
    if len(kept) == len(pairs) and netloc == split.netloc:
        return url
    query = urlencode(kept, quote_via=quote)
    return urlunsplit((split.scheme, netloc, split.path, query, split.fragment))


_URL = re.compile(r"https?://[^\s'\"<>]+")


def public_text(text: str) -> str:
    """`text` with every URL in it as `public_url` gives it: for messages and log lines."""
    return _URL.sub(lambda m: public_url(m.group()), text)


class Gateway(Protocol):
    def fetch(
        self, source: str, request: HttpRequest, *, use_cache: bool = True
    ) -> HttpResponse: ...


__all__ = [
    "CREDENTIAL_PARAMS",
    "Gateway",
    "HttpRequest",
    "HttpResponse",
    "SourcePolicy",
    "Transport",
    "public_text",
    "public_url",
]

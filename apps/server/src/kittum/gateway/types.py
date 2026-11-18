"""Values and seams that cross the gateway boundary."""

import importlib.metadata
from typing import Literal, Protocol
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from pydantic import Field

from kittum.core import Record, Sha256


class SourcePolicy(Record):
    """What one source allows: its rate, concurrency, daily budget, and terms."""

    source: str
    requests_per_second: float
    concurrency: int
    daily_budget: int | None = None
    terms_url: str


def _version() -> str:
    try:
        return importlib.metadata.version("kittum-server")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - installed in CI
        return "0.0.0"


def user_agent(contact_email: str | None = None) -> str:
    """The value sent in `User-Agent`, naming the version and, when one is configured, the
    contact address."""
    agent = f"Kittum/{_version()}"
    if contact_email:
        agent += f" (+mailto:{contact_email})"
    return agent


class Credential(Record):
    """One private value placed in a request for the send: a query parameter, a header, or the
    contact address in the User-Agent (`kittum.gateway.credentials`)."""

    kind: Literal["param", "header", "user_agent"]
    name: str
    value: str = Field(repr=False)


class HttpRequest(Record):
    """A request as a caller builds it, without a private value. `credentials` holds the values
    the gateway attaches for the send; the hash, the cache, recordings, and logs never read it."""

    method: str = "GET"
    url: str
    params: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None
    credentials: tuple[Credential, ...] = Field(default=(), exclude=True, repr=False)

    def on_the_wire(self) -> HttpRequest:
        """The request as a transport sends it: the query in `params` (`effective_request`), the
        credentials placed, and a User-Agent."""
        request = effective_request(self)
        params = (
            *request.params,
            *((c.name, c.value) for c in self.credentials if c.kind == "param"),
        )
        headers = [
            *request.headers,
            *((c.name, c.value) for c in self.credentials if c.kind == "header"),
        ]
        if not any(name.lower() == "user-agent" for name, _ in headers):
            contact = next((c.value for c in self.credentials if c.kind == "user_agent"), None)
            headers.append(("User-Agent", user_agent(contact)))
        return HttpRequest(
            method=request.method,
            url=request.url,
            params=params,
            headers=tuple(headers),
            body=request.body,
        )


class HttpResponse(Record):
    """A response. `url` is where it came from after redirects, when the transport knows."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    body_hash: Sha256
    from_cache: bool = False
    url: str | None = None


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
    return request.model_copy(update={"url": url, "params": params})


class Gateway(Protocol):
    def fetch(
        self, source: str, request: HttpRequest, *, use_cache: bool = True
    ) -> HttpResponse: ...

    def configured(self, source: str) -> bool:
        """Whether the deployment has every setting `source` requires (an API key, the contact
        address), so asking it can succeed."""
        ...


__all__ = [
    "Credential",
    "Gateway",
    "HttpRequest",
    "HttpResponse",
    "SourcePolicy",
    "Transport",
    "effective_request",
    "user_agent",
]

"""Following redirects one hop at a time, so each hop gets only its own host's private values.

The transport never follows a redirect. `send_following` sends a request, and while the answer
is a redirect it builds the next hop from the `Location` and the request as the adapter built it,
without private values, and `Credentials.attach` places the values for that hop's own host
(none over plain http). Every hop's response passes `Credentials.scrub`.

A `Location` query parameter that holds a private value is dropped from the next hop, since the
gateway places the values a host should get itself; a private value anywhere else in it, more
than `MAX_REDIRECTS` hops, a hop back to a URL already asked, or a target that is not http or
https raise `RedirectRefused`.
"""

from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from kittum.gateway.types import HttpRequest, HttpResponse, request_url

if TYPE_CHECKING:
    from kittum.gateway.credentials import Credentials
    from kittum.gateway.types import Transport

MAX_REDIRECTS = 10
"""Redirects followed for one request before the gateway gives up."""

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class RedirectRefused(Exception):
    """A redirect the gateway does not follow. `chain` is every hop asked so far, as built."""

    def __init__(self, reason: str, status: int, chain: tuple[str, ...]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.chain = chain


def send_following(
    transport: Transport, credentials: Credentials, source: str, request: HttpRequest
) -> tuple[HttpResponse, tuple[str, ...]]:
    """The final response to `request`, and the URL of every hop after the first, as built.

    The response's `url` is the last hop's URL as built. Both are scrubbed."""
    hop = request
    chain: tuple[str, ...] = ()
    asked = {(hop.method.upper(), request_url(hop))}
    while True:
        raw = transport.send(credentials.attach(source, hop))
        location = next((v for k, v in raw.headers if k.lower() == "location"), None)
        scrubbed = credentials.scrub(raw)
        if raw.status not in REDIRECT_STATUSES or location is None:
            url = credentials.scrub_text(request_url(hop))
            return scrubbed.model_copy(update={"url": url}), chain
        if len(chain) == MAX_REDIRECTS:
            raise RedirectRefused(f"more than {MAX_REDIRECTS} redirects", raw.status, chain)
        hop = _next_hop(hop, raw.status, location, credentials, chain)
        chain = (*chain, credentials.scrub_text(request_url(hop)))
        key = (hop.method.upper(), request_url(hop))
        if key in asked:
            raise RedirectRefused("the redirects loop", raw.status, chain)
        asked.add(key)


def _next_hop(
    hop: HttpRequest, status: int, location: str, credentials: Credentials, chain: tuple[str, ...]
) -> HttpRequest:
    target = urlsplit(urljoin(request_url(hop), location.strip()))
    if target.scheme not in {"http", "https"} or not target.hostname:
        raise RedirectRefused("a redirect to a URL that is not http or https", status, chain)
    base = urlunsplit((target.scheme, target.netloc, target.path, "", ""))
    if credentials.holds(base) or credentials.holds(target.fragment):
        raise RedirectRefused(
            "a redirect whose target holds a configured private value", status, chain
        )
    params = tuple(
        (name, value)
        for name, value in parse_qsl(target.query, keep_blank_values=True)
        if not credentials.holds(name) and not credentials.holds(value)
    )
    keep_body = status in {307, 308} or hop.method.upper() in {"GET", "HEAD"}
    method = hop.method if keep_body else "GET"
    headers = hop.headers
    if not keep_body:
        dropped = {"content-type", "content-length", "content-encoding"}
        headers = tuple((k, v) for k, v in headers if k.lower() not in dropped)
    if urlsplit(hop.url).hostname != target.hostname:
        # Headers the adapter set for one host (a cookie, its own authorisation) stay there.
        headers = tuple((k, v) for k, v in headers if k.lower() not in {"authorization", "cookie"})
    return HttpRequest(
        method=method,
        url=base,
        params=params,
        headers=headers,
        body=hop.body if keep_body else None,
    )


__all__ = ["MAX_REDIRECTS", "REDIRECT_STATUSES", "RedirectRefused", "send_following"]

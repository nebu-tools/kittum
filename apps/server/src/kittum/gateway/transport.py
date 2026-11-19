"""The real transport: httpx2 over HTTP/2, one connection pool, one request per call.

It never follows a redirect: it returns the 3xx as it came, and the gateway follows it one hop
at a time, placing each hop's private values itself (`kittum.gateway.redirects`).

HTTP/2 because some publishers' bot protection answers an HTTP/1.1 client with a challenge page
instead of the PDF it asked for (link.springer.com did in October 2026).

It sends `HttpRequest.on_the_wire()`, the only form that holds the credentials. httpx2 and
httpcore log each request with that URL at INFO and DEBUG, so both stay at WARNING; the gateway
logs the request as the caller built it.
"""

import logging

import httpx2

from kittum.core import sha256_hex
from kittum.gateway.types import HttpRequest, HttpResponse

for _name in ("httpx2", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class HttpxTransport:
    """Sends one request per call over a shared client with a fixed timeout."""

    def __init__(self, *, timeout: float = 30.0, client: httpx2.Client | None = None) -> None:
        self._client = client or httpx2.Client(
            timeout=httpx2.Timeout(timeout), follow_redirects=False, http2=True
        )

    def send(self, request: HttpRequest) -> HttpResponse:
        wire = request.on_the_wire()
        response = self._client.request(
            wire.method,
            wire.url,
            params=list(wire.params),
            headers=list(wire.headers),
            content=wire.body,
            follow_redirects=False,
        )
        body = response.content
        return HttpResponse(
            status=response.status_code,
            headers=tuple(response.headers.multi_items()),
            body=body,
            body_hash=sha256_hex(body),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpxTransport:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


__all__ = ["HttpxTransport"]

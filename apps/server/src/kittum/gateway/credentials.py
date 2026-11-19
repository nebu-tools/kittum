"""The deployment's private values, and the one place each goes into a request.

Callers never hold the contact address or an API key. A source adapter builds its request with
neither; `PLACEMENTS` says, per source, which setting goes into which query parameter or header,
and on which host. The gateway attaches them (`Credentials.attach`) only for the send, one hop
at a time (`kittum.gateway.redirects`): a hop gets the values placed for its own host, and a hop
over plain http gets none. Hashes, the cache, recordings, and logs use the request as the
adapter built it.

What comes back passes `Credentials.scrub` before the gateway stores, caches, or returns it, or
quotes it in an error. It replaces every configured private value, in each spelling
`kittum.core.PrivateValues` recognises, in the URL, every header, and a text body. A body is text
when it decodes in its declared charset or as UTF-8, whatever its `Content-Type`; any other body
is binary, is never rewritten, and is refused (`PrivateValueInResponse`) if it holds a value.
"""

import functools
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote, urlsplit

from kittum.core import PrivateValues, Record, sha256_hex
from kittum.gateway.errors import PrivateValueInResponse
from kittum.gateway.types import Credential, HttpRequest, HttpResponse

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

type Setting = Literal[
    "contact_email", "ncbi_api_key", "openalex_api_key", "semantic_scholar_api_key", "core_api_key"
]

CONTACT_PLACEHOLDER = "contact@example.invalid"
"""Stands for the contact address wherever a source echoed it back."""
REDACTED = "[redacted]"
"""Stands for an API key, a token, or a password wherever a source echoed it back."""


class Placement(Record):
    """Where one setting goes in a source's requests: a query parameter, a header (its value
    written by `template`), or the User-Agent. `host` limits it to the source's API: a PDF link
    the source found elsewhere gets none of its values. A `required` setting is needed before
    the gateway asks that host at all."""

    setting: Setting
    kind: Literal["param", "header", "user_agent"]
    name: str = "User-Agent"
    template: str = "{}"
    host: str | None = None
    required: bool = False


def _param(setting: Setting, name: str, host: str, *, required: bool = False) -> Placement:
    return Placement(setting=setting, kind="param", name=name, host=host, required=required)


def _header(
    setting: Setting, name: str, host: str, template: str = "{}", *, required: bool = False
) -> Placement:
    return Placement(
        setting=setting, kind="header", name=name, template=template, host=host, required=required
    )


EVERY_SOURCE = (Placement(setting="contact_email", kind="user_agent"),)
"""Every request names the contact address in its User-Agent, when one is configured."""

PLACEMENTS: Mapping[str, tuple[Placement, ...]] = {
    "pubmed": (
        _param("contact_email", "email", "eutils.ncbi.nlm.nih.gov"),
        _param("ncbi_api_key", "api_key", "eutils.ncbi.nlm.nih.gov"),
    ),
    "pmc": (_param("contact_email", "email", "pmc.ncbi.nlm.nih.gov"),),
    "crossref": (_param("contact_email", "mailto", "api.crossref.org"),),
    "unpaywall": (_param("contact_email", "email", "api.unpaywall.org", required=True),),
    "openalex": (_header("openalex_api_key", "Authorization", "api.openalex.org", "Bearer {}"),),
    "semanticscholar": (
        _header("semantic_scholar_api_key", "x-api-key", "api.semanticscholar.org", required=True),
    ),
    "core": (
        _header("core_api_key", "Authorization", "api.core.ac.uk", "Bearer {}", required=True),
    ),
}
"""Per source, which setting goes where. Sources not named here get only `EVERY_SOURCE`."""


def placements(source: str) -> tuple[Placement, ...]:
    return (*EVERY_SOURCE, *PLACEMENTS.get(source, ()))


@dataclass(frozen=True)
class Credentials:
    """The configured private values: the settings `PLACEMENTS` places, and `secrets`, the other
    values no stored response may hold (the database password, the model servers' keys)."""

    values: Mapping[Setting, str] = field(default_factory=dict)
    secrets: tuple[str, ...] = ()

    @classmethod
    def of(
        cls,
        secrets: Iterable[str | None] = (),
        *,
        contact_email: str | None = None,
        ncbi_api_key: str | None = None,
        openalex_api_key: str | None = None,
        semantic_scholar_api_key: str | None = None,
        core_api_key: str | None = None,
    ) -> Credentials:
        """The credentials from settings; unset and empty values are left out."""
        given: dict[Setting, str | None] = {
            "contact_email": contact_email,
            "ncbi_api_key": ncbi_api_key,
            "openalex_api_key": openalex_api_key,
            "semantic_scholar_api_key": semantic_scholar_api_key,
            "core_api_key": core_api_key,
        }
        return cls(
            values={k: v for k, v in given.items() if v},
            secrets=tuple(dict.fromkeys(s for s in secrets if s)),
        )

    @property
    def private(self) -> tuple[str, ...]:
        """Every configured private value."""
        return tuple(dict.fromkeys((*self.values.values(), *self.secrets)))

    def configured(self, source: str) -> bool:
        """Whether every setting `source` requires is configured."""
        return all(p.setting in self.values for p in placements(source) if p.required)

    def missing(self, source: str, request: HttpRequest) -> Setting | None:
        """The first setting `source` requires for `request`'s host that is not configured."""
        host = urlsplit(request.url).hostname or ""
        return next(
            (
                p.setting
                for p in placements(source)
                if p.required and p.host == host and p.setting not in self.values
            ),
            None,
        )

    def attach(self, source: str, request: HttpRequest) -> HttpRequest:
        """`request` with the source's configured settings for its own host attached for the
        send, and no others. A request over plain http gets none at all. Its URL, query, and
        headers stay as the adapter built them; the transport places `credentials`."""
        split = urlsplit(request.url)
        host = split.hostname or ""
        attached = tuple(
            Credential(kind=p.kind, name=p.name, value=p.template.format(self.values[p.setting]))
            for p in placements(source)
            if split.scheme == "https"
            and p.setting in self.values
            and (p.host is None or p.host == host)
        )
        return request.model_copy(update={"credentials": attached})

    # -- scrubbing ---------------------------------------------------------------------------

    @functools.cached_property
    def matcher(self) -> PrivateValues:
        """Recognises every configured private value (`kittum.core.PrivateValues`)."""
        return PrivateValues(self.private)

    def _stand_in(self, value: str, plain: bool) -> str:
        stand_in = CONTACT_PLACEHOLDER if value == self.values.get("contact_email") else REDACTED
        return stand_in if plain else quote(stand_in, safe="")

    def scrub_text(self, text: str) -> str:
        """`text` with every configured private value replaced, in any recognised spelling."""
        return self.matcher.replace(text, self._stand_in)

    def scrub(self, response: HttpResponse) -> HttpResponse:
        """`response` as the gateway may keep and return it: the URL, every header, and a text
        body without a configured private value. A binary body is kept as it came, unless it
        holds a private value: then `PrivateValueInResponse` is raised and nothing is kept."""
        if not self.matcher:
            return response
        decoded = _decoded(response)
        if decoded is None:
            if self.matcher.holds(response.body):
                raise PrivateValueInResponse()
            body = response.body
        else:
            text, encoding = decoded
            scrubbed = self.scrub_text(text)
            body = response.body if scrubbed == text else scrubbed.encode(encoding)
        return response.model_copy(
            update={
                "url": None if response.url is None else self.scrub_text(response.url),
                "headers": tuple(
                    (self.scrub_text(k), self.scrub_text(v)) for k, v in response.headers
                ),
                "body": body,
                "body_hash": response.body_hash if body == response.body else sha256_hex(body),
            }
        )

    def holds(self, data: str | bytes) -> bool:
        """Whether `data` holds a configured private value, in any recognised spelling."""
        return self.matcher.holds(data)


_CHARSET = re.compile(r"""charset\s*=\s*["']?([^"';\s]+)""", re.IGNORECASE)


def _decoded(response: HttpResponse) -> tuple[str, str] | None:
    """The body as text, with the encoding it decoded in: its declared charset, else UTF-8. None
    when it decodes in neither, which makes it binary."""
    content_type = next((v for k, v in response.headers if k.lower() == "content-type"), "")
    declared = _CHARSET.search(content_type)
    for encoding in (declared.group(1) if declared else None, "utf-8"):
        if encoding is None:
            continue
        try:
            return response.body.decode(encoding), encoding
        except LookupError, UnicodeDecodeError:
            continue
    return None


NO_CREDENTIALS = Credentials()


__all__ = [
    "CONTACT_PLACEHOLDER",
    "EVERY_SOURCE",
    "NO_CREDENTIALS",
    "PLACEMENTS",
    "REDACTED",
    "Credentials",
    "Placement",
    "Setting",
    "placements",
]

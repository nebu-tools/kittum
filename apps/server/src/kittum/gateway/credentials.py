"""The deployment's private values, and the one place each goes into a request.

Callers never hold the contact address or an API key. A source adapter builds its request with
neither; `PLACEMENTS` says, per source, which setting goes into which query parameter or header,
and the gateway attaches them (`Credentials.attach`) only for the send. Hashes, the cache,
recordings, and logs use the request as the adapter built it.

What comes back passes `Credentials.scrub` before the gateway stores, caches, or returns it, or
quotes it in an error: every configured private value, plain or percent-encoded, is replaced in
the post-redirect URL, every retained header, and a text body. A binary body, such as a PDF, is
left as it is.
"""

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote, quote_plus, urlsplit

from kittum.core import Record, sha256_hex
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


def spellings(value: str) -> tuple[str, ...]:
    """`value` as it may appear in a URL, a header, or a body: percent-encoded both ways, and
    plain. Longest first, so a replacement never leaves part of an encoded form behind."""
    found = dict.fromkeys((quote_plus(value), quote(value, safe=""), value))
    return tuple(sorted(found, key=len, reverse=True))


_BINARY = re.compile(
    r"^(application/(pdf|octet-stream|zip|gzip|x-gzip|x-tar|epub\+zip)|image/|audio/|video/|font/)",
    re.IGNORECASE,
)


def is_text(response: HttpResponse) -> bool:
    """Whether a body is text a source may have echoed a value into: anything but a PDF or
    another binary type, by its `Content-Type` or, without one, by its first bytes."""
    content_type = next((v for k, v in response.headers if k.lower() == "content-type"), "")
    if _BINARY.match(content_type.strip()):
        return False
    return response.body.lstrip()[:5] != b"%PDF-"


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
        """`request` with the source's configured settings attached for the send. Its URL, query,
        and headers stay as the adapter built them; the transport adds `credentials`."""
        host = urlsplit(request.url).hostname or ""
        attached = tuple(
            Credential(kind=p.kind, name=p.name, value=p.template.format(self.values[p.setting]))
            for p in placements(source)
            if p.setting in self.values and (p.host is None or p.host == host)
        )
        return request.model_copy(update={"credentials": attached}) if attached else request

    # -- scrubbing ---------------------------------------------------------------------------

    def _replacements(self) -> list[tuple[str, str]]:
        contact = self.values.get("contact_email")
        pairs: list[tuple[str, str]] = []
        for value in self.private:
            stand_in = CONTACT_PLACEHOLDER if value == contact else REDACTED
            pairs += [
                (s, quote_plus(stand_in) if s != value else stand_in) for s in spellings(value)
            ]
        return sorted(pairs, key=lambda p: len(p[0]), reverse=True)

    def scrub_text(self, text: str) -> str:
        """`text` with every configured private value replaced, in any spelling."""
        for value, stand_in in self._replacements():
            text = re.sub(re.escape(value), lambda _, s=stand_in: s, text, flags=re.IGNORECASE)
        return text

    def scrub_bytes(self, data: bytes) -> bytes:
        for value, stand_in in self._replacements():
            data = re.sub(re.escape(value.encode()), stand_in.encode(), data, flags=re.IGNORECASE)
        return data

    def scrub(self, response: HttpResponse) -> HttpResponse:
        """`response` as the gateway may keep and return it: the URL, every header, and a text
        body without a configured private value. A binary body is left alone."""
        if not self.private:
            return response
        body = self.scrub_bytes(response.body) if is_text(response) else response.body
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

    def holds(self, data: bytes) -> bool:
        """Whether `data` holds a configured private value, in any spelling."""
        folded = data.lower()
        return any(s.lower().encode() in folded for v in self.private for s in spellings(v))


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
    "is_text",
    "placements",
    "spellings",
]

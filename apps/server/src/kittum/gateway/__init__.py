"""All outbound HTTP (docs/sources.md, "How Kittum spreads load").

Per-source rate buckets persisted in Postgres, a safety margin under every published limit,
`Retry-After` handling, retries with jitter, daily budgets, and a content-addressed response
cache. A rerun of a frozen strategy makes no network calls. Callers never hold the contact
address or an API key: `PLACEMENTS` says where each goes, the gateway attaches them for the send
only, and scrubs them from all that comes back (`kittum.gateway.credentials`).
`RecordingTransport` records a live run's responses and `RecordedTransport` answers from them,
for a stack that must stay offline.

    credentials = Credentials.of(contact_email=settings.contact_email, ...)
    gateway = SourceGateway(
        database=db,
        transport=HttpxTransport(),
        artifacts=FilesystemStore(settings.artifact_dir),
        policies=default_policies(credentials),
        credentials=credentials,
        clock=clock,
    )
    response = gateway.fetch("pubmed", HttpRequest(url=..., params=...))
"""

from kittum.gateway.credentials import (
    CONTACT_PLACEHOLDER,
    NO_CREDENTIALS,
    PLACEMENTS,
    REDACTED,
    Credentials,
    Placement,
    placements,
)
from kittum.gateway.errors import (
    GatewayError,
    SourceBudgetExhausted,
    SourceNotConfigured,
    SourceUnavailable,
    UnknownSource,
    unreached,
)
from kittum.gateway.messages import MESSAGE_LENGTH, source_message
from kittum.gateway.policies import MARGIN, default_policies
from kittum.gateway.recorded import (
    NotRecorded,
    RecordedTransport,
    RecordingRefused,
    RecordingTransport,
    recording_key,
)
from kittum.gateway.service import MAX_ATTEMPTS, SourceGateway, request_hash
from kittum.gateway.transport import HttpxTransport
from kittum.gateway.types import (
    Credential,
    Gateway,
    HttpRequest,
    HttpResponse,
    SourcePolicy,
    Transport,
    user_agent,
)

__all__ = [
    "CONTACT_PLACEHOLDER",
    "MARGIN",
    "MAX_ATTEMPTS",
    "MESSAGE_LENGTH",
    "NO_CREDENTIALS",
    "PLACEMENTS",
    "REDACTED",
    "Credential",
    "Credentials",
    "Gateway",
    "GatewayError",
    "HttpRequest",
    "HttpResponse",
    "HttpxTransport",
    "NotRecorded",
    "Placement",
    "RecordedTransport",
    "RecordingRefused",
    "RecordingTransport",
    "SourceBudgetExhausted",
    "SourceGateway",
    "SourceNotConfigured",
    "SourcePolicy",
    "SourceUnavailable",
    "Transport",
    "UnknownSource",
    "default_policies",
    "placements",
    "recording_key",
    "request_hash",
    "source_message",
    "unreached",
    "user_agent",
]

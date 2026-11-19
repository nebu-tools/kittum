"""Errors a gateway caller is expected to handle."""

from typing import TYPE_CHECKING

import httpx2

from kittum.core import KittumError

if TYPE_CHECKING:
    from datetime import date


class GatewayError(KittumError):
    """Base for every gateway failure."""


class UnknownSource(GatewayError):
    """No policy is configured for the requested source."""

    def __init__(self, source: str) -> None:
        super().__init__(f"no source policy for {source!r}")
        self.source = source


class SourceBudgetExhausted(GatewayError):
    """The source's daily request budget is used up; try again on the next day."""

    def __init__(self, source: str, day: date, budget: int) -> None:
        super().__init__(f"{source} used its {budget} requests for {day.isoformat()}")
        self.source = source
        self.day = day
        self.budget = budget


class SourceNotConfigured(GatewayError):
    """The source needs a setting the deployment does not have (an API key, the contact address),
    so the gateway does not ask it."""

    def __init__(self, source: str, setting: str) -> None:
        super().__init__(f"{source} needs {setting}, which is not configured")
        self.source = source
        self.setting = setting


class PrivateValueInResponse(GatewayError):
    """A binary response holds one of the deployment's private values. A binary body is never
    rewritten, so the gateway keeps nothing of it: no cache entry, no recording, no result."""

    def __init__(self) -> None:
        super().__init__("a binary response holds a configured private value, so it was not kept")


class SourceUnavailable(GatewayError):
    """Every attempt failed with a status worth retrying, or (with `reason`, and no status) the
    source could not be reached at all, as offline with no recorded response. `message` is what
    the last response said about the failure, when it said something (`source_message`).

    The gateway builds both from what it scrubbed, so neither holds a private value."""

    def __init__(
        self,
        source: str,
        status: int | None,
        attempts: int,
        reason: str | None = None,
        message: str | None = None,
    ) -> None:
        super().__init__(
            f"{source} is unreachable: {reason}"
            if reason
            else f"{source} still failing after {attempts} attempts (last status {status})"
        )
        self.source = source
        self.status = status
        self.attempts = attempts
        self.reason = reason
        self.message = message


def unreached(source: str, error: BaseException, attempts: int = 1) -> SourceUnavailable:
    """A request that got no response at all, said in plain words and the error's kind. The
    error's own text, which can quote the request, is left out."""
    if isinstance(error, httpx2.TimeoutException | TimeoutError):
        reason = "it did not answer in time"
    elif isinstance(error, httpx2.ConnectError | ConnectionRefusedError):
        reason = "the connection failed"
    else:
        reason = "the connection broke off"
    return SourceUnavailable(source, None, attempts, f"{reason} ({type(error).__name__})")


__all__ = [
    "GatewayError",
    "PrivateValueInResponse",
    "SourceBudgetExhausted",
    "SourceNotConfigured",
    "SourceUnavailable",
    "UnknownSource",
    "unreached",
]

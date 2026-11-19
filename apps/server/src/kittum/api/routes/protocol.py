"""Protocol routes: parse and validate a source, preview its diff against the published version,
write the project's one open draft and publish it, and read versions back. The example protocols
of a checkout are served as starting points.

Saving a draft needs no role, as editing the protocol never has; publishing needs the project
lead. A save, discard, or publication that names an old revision of the draft is a 409 with the
draft as it is now (`draft` is null when it was published or discarded)."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import Field

from kittum.api import Context  # noqa: TC001 - FastAPI resolves it at runtime
from kittum.app import (
    ExampleProtocol,
    LifecycleError,
    NotLead,
    example_protocols,
    parse_source,
    protocol_source,
    require_project_lead,
    source_yaml,
)
from kittum.core import Id, NotFoundError, Record
from kittum.protocol import (
    DocumentationLevel,
    ProtocolDiff,
    ProtocolDraft,
    ProtocolError,
    ProtocolIssue,
    ProtocolStateError,
    ReviewProtocol,
    StaleDraft,
    StoredProtocol,
    compile,
    diff,
    discard_draft,
    get_draft,
    get_version,
    labelled,
    latest_published,
    list_versions,
    next_label,
    protocol_warnings,
    publish_draft,
    save_draft,
    start_draft,
)

router = APIRouter(prefix="/api", tags=["protocol"])

type ProtocolSource = dict[str, Any]


class ValidationReport(Record):
    ok: bool
    content_hash: str | None = None
    issues: tuple[ProtocolIssue, ...] = ()
    warnings: tuple[ProtocolIssue, ...] = ()
    """What may be wrong in a source that compiles, such as a field tag PubMed does not list."""
    documentation: dict[str, DocumentationLevel] = Field(default_factory=dict)
    """Each search topic's documentation level, by topic ID: `working` for mode S, `complete`
    otherwise. Only a `working` topic may show the AI's reading before the decision."""


class SourceText(Record):
    text: str
    """YAML or JSON."""


class ParsedSource(Record):
    """The source as data, or the syntax error as an issue with its line and column."""

    source: ProtocolSource | None
    issues: tuple[ProtocolIssue, ...] = ()


class DraftStart(Record):
    source: str | None = None
    """The text to start from; the published version as YAML when omitted."""
    base_revision_id: Id | None = None


class DraftSave(Record):
    revision: int
    """The revision the text was written on."""
    source: str


class DraftPublish(Record):
    revision: int
    approved_by: Id
    reason: str | None = None


class ProtocolVersionSummary(Record):
    id: Id
    parent_id: Id | None
    version_label: str
    content_hash: str
    created_at: datetime
    published_at: datetime


class DiffPreview(Record):
    """What publishing a source would change against the version every decision reads now;
    `previous` and `diff` are None before the first version."""

    content_hash: str
    version_label: str
    """The label the source would be published under."""
    previous: ProtocolVersionSummary | None
    diff: ProtocolDiff | None


class PublishedProtocol(Record):
    """The version every decision reads now."""

    stored: StoredProtocol
    documentation: dict[str, DocumentationLevel]
    """Each search topic's documentation level, by topic ID."""
    next_label: str
    """The label the next version is published under."""


def _documentation(protocol: ReviewProtocol) -> dict[str, DocumentationLevel]:
    return {t.id: t.documentation for t in protocol.search_topics}


def _invalid(e: ProtocolError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "message": "the protocol does not compile",
            "issues": [i.model_dump() for i in e.issues],
        },
    )


def _stale(e: StaleDraft) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        {"message": str(e), "draft": e.current.model_dump(mode="json") if e.current else None},
    )


def _summary(s: StoredProtocol) -> ProtocolVersionSummary:
    return ProtocolVersionSummary(
        id=s.id,
        parent_id=s.parent_id,
        version_label=s.version.protocol.version_label,
        content_hash=s.version.content_hash,
        created_at=s.created_at,
        published_at=s.published_at,
    )


@router.get("/protocol/examples", operation_id="listExampleProtocols")
def list_examples() -> list[ExampleProtocol]:
    """The example protocols in kittum/examples/protocols, to start a review from."""
    return example_protocols()


@router.post("/protocol/parse", operation_id="parseProtocol")
def parse_protocol(body: SourceText) -> ParsedSource:
    """Read YAML or JSON text as `kittum project publish-protocol` reads its file. A syntax error
    comes back as an issue; validate the source with `validateProtocol`."""
    try:
        data = parse_source(body.text, "the protocol")
    except LifecycleError as e:
        return ParsedSource(source=None, issues=(ProtocolIssue(path="", message=str(e)),))
    if not isinstance(data, dict):
        issue = ProtocolIssue(path="", message="the protocol must be a mapping of fields")
        return ParsedSource(source=None, issues=(issue,))
    return ParsedSource(source=data)


@router.post("/protocol/yaml", operation_id="protocolAsYaml")
def protocol_as_yaml(source: ProtocolSource) -> SourceText:
    """A source as YAML, compiling or not, for the editor to open what the form holds."""
    return SourceText(text=source_yaml(source))


@router.post("/protocol/validate", operation_id="validateProtocol")
def validate_protocol(source: ProtocolSource) -> ValidationReport:
    try:
        version = compile(source)
    except ProtocolError as e:
        return ValidationReport(ok=False, issues=e.issues)
    return ValidationReport(
        ok=True,
        content_hash=version.content_hash,
        warnings=protocol_warnings(version.protocol),
        documentation=_documentation(version.protocol),
    )


@router.get("/projects/{project_id}/protocol-draft", operation_id="getProtocolDraft")
def get_protocol_draft(project_id: Id, ctx: Context) -> ProtocolDraft:
    """The project's open draft; 404 when none is open."""
    with ctx.db.transaction() as tx:
        draft = get_draft(tx, project_id)
    if draft is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no draft is open")
    return draft


@router.post("/projects/{project_id}/protocol-draft", operation_id="startProtocolDraft")
def start_protocol_draft(project_id: Id, request: DraftStart, ctx: Context) -> ProtocolDraft:
    """Open the project's draft, or return the one already open, unchanged. Without a source
    it starts from the published version; 422 if none is published."""
    with ctx.db.transaction() as tx:
        source = request.source
        if source is None:
            try:
                source = protocol_source(tx, project_id)
            except LifecycleError as e:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from None
        return start_draft(tx, project_id, source, request.base_revision_id)


@router.put("/projects/{project_id}/protocol-draft", operation_id="saveProtocolDraft")
def save_protocol_draft(project_id: Id, request: DraftSave, ctx: Context) -> ProtocolDraft:
    try:
        with ctx.db.transaction() as tx:
            return save_draft(tx, project_id, request.revision, request.source)
    except StaleDraft as e:
        raise _stale(e) from None


@router.delete(
    "/projects/{project_id}/protocol-draft",
    operation_id="discardProtocolDraft",
    status_code=status.HTTP_204_NO_CONTENT,
)
def discard_protocol_draft(project_id: Id, revision: int, ctx: Context) -> None:
    try:
        with ctx.db.transaction() as tx:
            discard_draft(tx, project_id, revision)
    except StaleDraft as e:
        raise _stale(e) from None


@router.post("/projects/{project_id}/protocol-draft/publish", operation_id="publishProtocolDraft")
def publish_protocol_draft(project_id: Id, request: DraftPublish, ctx: Context) -> StoredProtocol:
    """Publish the draft with the lead's approval, under the label Kittum assigns, and delete it,
    in one transaction. 422 if it does not compile."""
    if ctx.ledger is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no ledger is configured")
    try:
        with ctx.db.transaction() as tx:
            require_project_lead(tx, project_id, request.approved_by)
            return publish_draft(
                tx,
                project_id,
                request.revision,
                request.approved_by,
                ledger=ctx.ledger,
                reason=request.reason,
            )
    except StaleDraft as e:
        raise _stale(e) from None
    except ProtocolError as e:
        raise _invalid(e) from None
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from None
    except NotLead as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from None
    except ProtocolStateError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None


@router.get("/projects/{project_id}/protocol", operation_id="getPublishedProtocol")
def get_published_protocol(project_id: Id, ctx: Context) -> PublishedProtocol:
    with ctx.db.transaction() as tx:
        stored = latest_published(tx, project_id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no protocol is published yet")
    return PublishedProtocol(
        stored=stored,
        documentation=_documentation(stored.version.protocol),
        next_label=next_label(stored.version.protocol.version_label),
    )


@router.get("/projects/{project_id}/protocol/source", operation_id="getPublishedProtocolSource")
def get_published_source(project_id: Id, ctx: Context) -> SourceText:
    """The published protocol as YAML, to edit into the next version."""
    try:
        with ctx.db.transaction() as tx:
            return SourceText(text=protocol_source(tx, project_id))
    except LifecycleError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from None


@router.post("/projects/{project_id}/protocol/diff", operation_id="previewProtocolDiff")
def preview_diff(project_id: Id, source: ProtocolSource, ctx: Context) -> DiffPreview:
    """The changes publishing this source would make to the published version, without saving
    anything. 422 if it does not compile."""
    try:
        version = compile(source)
    except ProtocolError as e:
        raise _invalid(e) from None
    with ctx.db.transaction() as tx:
        stored = latest_published(tx, project_id)
    version = labelled(version, stored)
    return DiffPreview(
        content_hash=version.content_hash,
        version_label=version.protocol.version_label,
        previous=_summary(stored) if stored else None,
        diff=diff(stored.version, version) if stored else None,
    )


@router.get("/projects/{project_id}/protocol-versions", operation_id="listProtocolVersions")
def list_protocol_versions(project_id: Id, ctx: Context) -> list[ProtocolVersionSummary]:
    with ctx.db.transaction() as tx:
        stored = list_versions(tx, project_id)
    return [_summary(s) for s in stored]


@router.get(
    "/projects/{project_id}/protocol-versions/{version_id}", operation_id="getProtocolVersion"
)
def get_protocol_version(project_id: Id, version_id: Id, ctx: Context) -> StoredProtocol:
    try:
        with ctx.db.transaction() as tx:
            return get_version(tx, project_id, version_id)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from None

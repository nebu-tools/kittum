"""Shared helpers for the protocol tests.

Tests use the real `kittum.ledger.PostgresLedger` for publish flows against Postgres.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from kittum.core import Id, new_id

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from kittum.db import Tx

EXAMPLES = Path(__file__).resolve().parents[4] / "examples" / "protocols"


def example_text(name: str = "nicotine-replacement") -> str:
    return (EXAMPLES / f"{name}.yaml").read_text()


def example_source(name: str = "nicotine-replacement") -> dict[str, Any]:
    """The example as JSON data: YAML dates become ISO strings, as in a request body."""
    return json.loads(json.dumps(yaml.safe_load(example_text(name)), default=str))


def seed_project(tx: Tx) -> tuple[Id, Id, Id]:
    """A project, a human approver who leads it, and a system actor."""
    project = tx.execute(
        "insert into project (name, policy_version) values ('example review', 'p1') returning id"
    ).fetchone()
    human = tx.execute(
        "insert into actor (identity_ref, kind) values (%s, 'human') returning id",
        (f"reviewer-{new_id()}",),
    ).fetchone()
    system = tx.execute(
        "insert into actor (identity_ref, kind) values (%s, 'system') returning id",
        (f"system-{new_id()}",),
    ).fetchone()
    assert project and human and system
    # The approver leads the project: only a lead publishes.
    tx.execute(
        "insert into project_member (project_id, actor_id, role, display_name, event_id)"
        " values (%s, %s, 'lead', 'Approver', uuidv7())",
        (project["id"], human["id"]),
    )
    return project["id"], human["id"], system["id"]


def publish_through_api(
    client: TestClient,
    project_id: str,
    source: str | dict[str, Any],
    approved_by: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Open the project's draft with `source`, YAML text or JSON data, and publish it, as the
    workbench does. Returns the published version."""
    text = source if isinstance(source, str) else json.dumps(source, default=str)
    draft_url = f"/api/projects/{project_id}/protocol-draft"
    draft = client.post(draft_url, json={"source": text})
    assert draft.status_code == 200, draft.text
    assert draft.json()["source"] == text, "a draft was already open"
    body = {"revision": draft.json()["revision"], "approved_by": approved_by, "reason": reason}
    published = client.post(f"{draft_url}/publish", json=body)
    assert published.status_code == 200, published.text
    return published.json()

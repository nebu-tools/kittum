"""The demo, from an empty database to a sealed bundle that `kittum verify` accepts.

The demo seed builds the project and opens title and abstract screening; the worker judges it
with the recorded answers; the screeners decide their queues over HTTP; the lead seals the
handover bundle over HTTP; and the bundle verifies offline. The database is created empty from
the schema file in this run, so every row the bundle carries was written by these steps.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.app.demo import SCOPE, demo_judge, run_judge_jobs, seed_demo
from kittum.cli import main
from kittum.core import new_id
from kittum.ledger import PostgresLedger
from kittum.models import ModelRegistry
from kittum.settings import Settings
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    from pathlib import Path

    from kittum.db import Database

pytestmark = pytest.mark.postgres

PEM = Ed25519PrivateKey.generate().private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)
INCLUDE = {
    "outcome": "include",
    "reason_criterion_id": None,
    "evidence_anchor_ids": [],
    "comment": None,
    "criteria": [],
}


def test_the_demo_runs_from_an_empty_database_to_a_bundle_that_verifies(
    empty_database: Database, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = tmp_path / "checkpoint.pem"
    key.write_bytes(PEM)
    ctx = AppContext(
        settings=Settings(
            database_url="postgresql://unused",
            artifact_dir=tmp_path / "artifacts",
            checkpoint_key_path=key,
        ),
        db=empty_database,
        artifacts=MemoryStore(),
        ledger=PostgresLedger(),
        judge=demo_judge(ModelRegistry.load()),
    )
    with empty_database.transaction() as tx:
        assert tx.execute("select count(*) as n from judge_config").fetchone()["n"] == 0

    demo = seed_demo(ctx)
    assert demo.judge_config_id == "demo-recorded@judge.v2"
    # The stage opened with its judge config, before any judging.
    with empty_database.transaction() as tx:
        (stage,) = tx.execute(
            "select s.judge_config_id, c.id as config from screening_stage s"
            " left join judge_config c on c.id = s.judge_config_id where s.project_id = %s",
            (demo.project_id,),
        ).fetchall()
    assert stage == {"judge_config_id": demo.judge_config_id, "config": demo.judge_config_id}

    assert run_judge_jobs(ctx) > 0
    with empty_database.transaction() as tx:
        judged = tx.execute(
            "select distinct judge_config_id from verdict where project_id = %s",
            (demo.project_id,),
        ).fetchall()
    assert [j["judge_config_id"] for j in judged] == [demo.judge_config_id]

    client = TestClient(create_app(ctx))
    base = f"/api/projects/{demo.project_id}"

    def ok(response: Any, status: int = 200) -> Any:
        assert response.status_code == status, response.text
        return response.json()

    decided = 0
    for person in ("Sam Okafor", "Jun Park"):
        headers = {"X-Kittum-Reviewer": str(demo.people[person])}
        params = {"scope_id": SCOPE, "stage": "title_abstract", "limit": 50}
        for item in ok(client.get(f"{base}/screening/queue", params=params, headers=headers)):
            now = datetime.now(UTC)
            timing = {
                "shown_at": now.isoformat(),
                "first_key_at": now.isoformat(),
                "decided_at": (now + timedelta(seconds=5)).isoformat(),
                "reveal_shown_at": None,
            }
            path = f"{base}/screening/assignments/{item['assignment_id']}/decide"
            body = {"decision": INCLUDE, "timing": timing, "expected_etag": item["etag"]}
            result = ok(
                client.post(
                    path,
                    json=body | {"command_id": str(new_id()), "phase": "provisional"},
                    headers=headers,
                )
            )
            if result["phase"] == "provisional":  # the AI's reading is shown; keep the decision
                shown = (now + timedelta(seconds=6)).isoformat()
                final = body | {
                    "expected_etag": result["etag"],
                    "timing": timing | {"reveal_shown_at": shown, "decided_at": shown},
                }
                ok(
                    client.post(
                        path,
                        json=final | {"command_id": str(new_id()), "phase": "final"},
                        headers=headers,
                    )
                )
            decided += 1
    assert decided > 0

    lead = str(demo.people["Morgan Lee"])
    bundle = ok(client.post(f"{base}/reports/bundle", json={"actor_id": lead}))
    capsys.readouterr()
    assert main(["verify", bundle["path"]]) == 0
    assert "OK" in capsys.readouterr().out

    db = sqlite3.connect(f"{bundle['path']}/kittum.sqlite")
    assert db.execute("select count(*) from screening_decision").fetchone()[0] >= decided
    configs = db.execute("select distinct judge_config_id from verdict").fetchall()
    assert configs == [(demo.judge_config_id,)]

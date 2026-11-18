import json
import os
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from kittum.api import AppContext, create_app
from kittum.core import new_id
from kittum.db import Database
from kittum.ledger import PostgresLedger
from kittum.protocol import PUBLISHED_EVENT, compile
from kittum.settings import Settings
from kittum.storage import FilesystemStore
from tests.protocol.support import example_source, example_text, seed_project

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

GERMAN = "tranexamic-acid"  # written in German, with two topics, one of them in mode M2


def _hash(name: str) -> str:
    return compile(example_text(name)).content_hash


def test_a_pubmed_strategy_may_use_every_field_tag_pubmed_lists() -> None:
    """Tags from PubMed's "Search Field Descriptions and Tags", short and long, proximity
    included: a strategy with them compiles. A tag PubMed does not list is a warning beside the
    strategy, as PubMed itself only warns; unbalanced syntax is a problem."""
    client = TestClient(create_app())
    source = example_source("nicotine-replacement")
    medline = source["search_topics"][0]["source_plan"]["sources"][1]
    assert medline["id"] == "medline"
    medline["strategy"] = (
        '(nicotine[mh] OR "nicotine patch"[tiab] OR NRT[Title/Abstract] OR "patch quit"[tiab:~3]'
        ' OR "Nicotinic Agonists"[pa] OR "Smoking Cessation"[majr] OR therapy[sh:noexp])'
        " AND (2024[epdat] OR 2023[ppdat] OR 2024/01/01[edat] OR 2024[Date - Entry]"
        " OR 2024[mhda] OR 2024[crdt] OR 2024[lr] OR 2020:2024[dp])"
        " AND (10.1000/xyz[aid] OR funding[cois] OR smith john[fir] OR e123[lid]"
        ' OR 0372354[jid] OR hughes j[ps] OR "randomized controlled trial"[Publication Type])'
        " AND english[la] AND medline[sb] AND humans[filter]"
        # PubMed's own Clinical Queries use these unexpanded and subheading forms.
        ' AND (review[pt:noexp] OR "clinical trial"[Publication Type:noexp]'
        " OR diagnosis[Subheading:noexp] OR therapy[subheading] OR therapy[MeSH Subheading:noexp]"
        ' OR "Smoking Cessation"[MeSH Major Topic:noexp] OR review[ptyp:noexp])'
    )
    report = client.post("/api/protocol/validate", json=source).json()
    assert (report["ok"], report["issues"], report["warnings"]) == (True, [], [])

    medline["strategy"] += " AND addiction[jt]"
    report = client.post("/api/protocol/validate", json=source).json()
    assert report["ok"] is True
    assert report["warnings"] == [
        {
            "path": "search_topics[0].source_plan.sources[1].strategy",
            "message": "[jt] is not a PubMed field tag; PubMed would search it as text",
        }
    ]
    medline["strategy"] = "(" + medline["strategy"]
    report = client.post("/api/protocol/validate", json=source).json()
    assert report["ok"] is False
    assert report["issues"][0]["message"] == "1 opening parenthesis is not closed"


def test_a_europe_pmc_strategy_is_checked_like_a_pubmed_one() -> None:
    """A source on Europe PMC uses Europe PMC's fields, in any case, and its date ranges; a field
    Europe PMC does not know is a warning, since Europe PMC finds nothing for it without saying
    so; unbalanced syntax is a problem."""
    client = TestClient(create_app())
    source = example_source("nicotine-replacement")
    sources = source["search_topics"][0]["source_plan"]["sources"]
    sources.append(
        {
            "id": "europepmc",
            "kind": "bibliographic",
            "name": "Europe PMC",
            "platform": "Europe PMC",
            "strategy": (
                '(TITLE_ABS:"nicotine replacement" OR title:NRT OR MESH:"Smoking Cessation")'
                ' AND PUB_TYPE:"Randomized Controlled Trial" AND (SRC:MED OR SRC:PPR)'
                " AND FIRST_PDATE:[2024-01-01 TO 2024-03-31] AND LANG:eng"
                # A colon inside a phrase or after a number is no field.
                ' AND NOT TITLE:"Erratum: correction" AND NOT covid-19:x'
            ),
        }
    )
    at = f"search_topics[0].source_plan.sources[{len(sources) - 1}].strategy"
    report = client.post("/api/protocol/validate", json=source).json()
    assert (report["ok"], report["issues"], report["warnings"]) == (True, [], [])

    sources[-1]["strategy"] += " AND JOURNAL_TITLE:Addiction AND Tiab:nicotine"
    report = client.post("/api/protocol/validate", json=source).json()
    assert report["ok"] is True
    assert report["warnings"] == [
        {
            "path": at,
            "message": "Europe PMC has no field JOURNAL_TITLE. It finds no records for a search"
            " in it.",
        },
        {
            "path": at,
            "message": "Europe PMC has no field Tiab. It finds no records for a search in it.",
        },
    ]
    sources[-1]["strategy"] = "(" + sources[-1]["strategy"]
    report = client.post("/api/protocol/validate", json=source).json()
    assert report["ok"] is False
    assert report["issues"] == [{"path": at, "message": "1 opening parenthesis is not closed"}]
    # A range written back to front has as many brackets of each kind, in the wrong order.
    sources[-1]["strategy"] = 'TITLE_ABS:"nicotine" AND FIRST_PDATE:]2020 TO 2024['
    report = client.post("/api/protocol/validate", json=source).json()
    assert report["ok"] is False
    assert [i["message"] for i in report["issues"]] == [
        "a closing square bracket has no opening one",
        "a square bracket is not closed",
    ]
    for broken, problem in (
        ("FIRST_PDATE:[2020 TO [2024]]", "square brackets are nested"),
        ("FIRST_PDATE:[2020 TO (2024]", "a parenthesis is inside square brackets"),
    ):
        sources[-1]["strategy"] = broken
        report = client.post("/api/protocol/validate", json=source).json()
        assert problem in [i["message"] for i in report["issues"]], broken


def test_validate_reports_the_hash_or_the_issues() -> None:
    client = TestClient(create_app())
    for name in (GERMAN, "nicotine-replacement"):
        ok = client.post("/api/protocol/validate", json=example_source(name))
        assert ok.status_code == 200
        assert ok.json() == {
            "ok": True,
            "content_hash": _hash(name),
            "issues": [],
            "warnings": [],
            "documentation": {t["id"]: "complete" for t in example_source(name)["search_topics"]},
        }

    broken = example_source(GERMAN)
    del broken["search_topics"][1]["validation_pack"]
    report = client.post("/api/protocol/validate", json=broken).json()
    assert report["ok"] is False
    assert report["issues"] == [
        {
            "path": "search_topics[1].validation_pack",
            "message": "mode M2 needs a validation pack that shows the AI's performance",
        }
    ]


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, dict[str, Any]]]:
    """An app on the test database. Rows commit, so each test makes its own project."""
    url = os.environ.get("KITTUM_DATABASE_URL")
    if not url:
        pytest.skip("KITTUM_DATABASE_URL is not set")
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        project, approver, system = seed_project(conn)
    context = AppContext(
        settings=Settings(database_url=url, artifact_dir=tmp_path),
        db=Database(url, max_size=2),
        artifacts=FilesystemStore(tmp_path),
        ledger=PostgresLedger(),
    )
    with TestClient(create_app(context)) as client:
        yield client, {"project": project, "approver": approver, "system": system, "url": url}
    context.db.close()


@pytest.mark.postgres
def test_api_writes_a_draft_and_publishes_it(api: tuple[TestClient, dict[str, Any]]) -> None:
    client, ids = api
    draft_url = f"/api/projects/{ids['project']}/protocol-draft"
    versions = f"/api/projects/{ids['project']}/protocol-versions"
    text = example_text(GERMAN)

    assert client.get(draft_url).status_code == 404
    # Nothing is published, so a draft needs a source to start from.
    assert client.post(draft_url, json={}).status_code == 422
    started = client.post(draft_url, json={"source": text})
    assert started.status_code == 200
    draft = started.json()
    assert (draft["revision"], draft["source"]) == (1, text)
    # Starting again returns the open draft, not a second one.
    assert client.post(draft_url, json={"source": "title: other"}).json() == draft

    # A save names the revision it was written on; an older one is refused with the draft.
    saved = client.put(draft_url, json={"revision": 1, "source": text + "\n"}).json()
    assert saved["revision"] == 2
    stale = client.put(draft_url, json={"revision": 1, "source": "mine"})
    assert stale.status_code == 409
    assert stale.json()["detail"]["draft"] == saved
    assert client.get(draft_url).json() == saved

    publish = f"{draft_url}/publish"
    denied = client.post(publish, json={"revision": 2, "approved_by": str(ids["system"])})
    assert denied.status_code == 403  # not the project's lead
    behind = client.post(publish, json={"revision": 1, "approved_by": str(ids["approver"])})
    assert behind.status_code == 409
    assert behind.json()["detail"]["draft"] == saved

    published = client.post(publish, json={"revision": 2, "approved_by": str(ids["approver"])})
    assert published.status_code == 200
    body = published.json()
    assert body["approved_by"] == str(ids["approver"])
    assert body["version"]["content_hash"] == _hash(GERMAN)
    assert client.get(draft_url).status_code == 404  # publishing removed the draft
    again = client.post(publish, json={"revision": 2, "approved_by": str(ids["approver"])})
    assert again.status_code == 409
    assert again.json()["detail"]["draft"] is None

    listed = client.get(versions).json()
    assert [(v["id"], v["version_label"]) for v in listed] == [(body["id"], "1.0")]
    fetched = client.get(f"{versions}/{body['id']}").json()
    assert fetched == body
    protocol = fetched["version"]["protocol"]
    assert protocol["default_language"] == "de"
    assert [q["id"] for q in protocol["questions"]] == ["a1", "a2", "b1", "b2"]
    assert [t["mode"] for t in protocol["search_topics"]] == ["M1", "M2"]
    assert client.get(f"{versions}/{new_id()}").status_code == 404
    in_force = client.get(f"/api/projects/{ids['project']}/protocol").json()
    assert in_force["next_label"] == "1.1"

    with psycopg.connect(ids["url"], row_factory=dict_row) as conn:
        event = conn.execute(
            "select type, actor_id, payload from audit_event where event_id = %s",
            (body["approved_event_id"],),
        ).fetchone()
        types = conn.execute(
            "select type from audit_event where project_id = %s", (ids["project"],)
        ).fetchall()
    assert event is not None
    assert (event["type"], str(event["actor_id"])) == (PUBLISHED_EVENT, str(ids["approver"]))
    assert event["payload"]["content_hash"] == body["version"]["content_hash"]
    assert types == [{"type": PUBLISHED_EVENT}]  # saving a draft is not a ledger event

    # The next draft starts from the published version, which it can only follow.
    following = client.post(draft_url, json={}).json()
    assert following["revision"] == 1
    assert compile(following["source"]).content_hash == _hash(GERMAN)
    unchanged = client.post(publish, json={"revision": 1, "approved_by": str(ids["approver"])})
    assert unchanged.status_code == 409
    assert "unchanged" in unchanged.json()["detail"]
    discarded = client.delete(draft_url, params={"revision": 1})
    assert discarded.status_code == 204
    assert client.get(draft_url).status_code == 404
    assert client.delete(draft_url, params={"revision": 1}).status_code == 409


@pytest.mark.postgres
def test_api_rejects_a_draft_that_does_not_compile(
    api: tuple[TestClient, dict[str, Any]],
) -> None:
    client, ids = api
    draft_url = f"/api/projects/{ids['project']}/protocol-draft"
    broken = example_source(GERMAN)
    broken["search_topics"][1]["scopes"].append("zz")
    # A draft holds what was typed, compiling or not.
    draft = client.post(draft_url, json={"source": json.dumps(broken)}).json()
    response = client.post(
        f"{draft_url}/publish",
        json={"revision": draft["revision"], "approved_by": str(ids["approver"])},
    )
    assert response.status_code == 422
    (issue,) = response.json()["detail"]["issues"]
    assert issue == {
        "path": "search_topics[1].scopes[2]",
        "message": "unknown scope 'zz'; a topic covers review questions (a1, a2, b1, b2)",
    }
    assert client.get(f"/api/projects/{ids['project']}/protocol-versions").json() == []
    assert client.get(draft_url).json() == draft


MALFORMED = {
    "questions is not a list": (
        lambda s: s.__setitem__("questions", 1),
        "questions",
        "Input should be a valid tuple, got 1",
    ),
    "default language is a list": (
        lambda s: s.__setitem__("default_language", ["de"]),
        "default_language",
        "Input should be a valid string",
    ),
    "a topic is not a mapping": (
        lambda s: s["search_topics"].append(7),
        "search_topics[1]",
        "Input should be a valid dictionary or instance of SearchTopic",
    ),
}


@pytest.mark.parametrize("case", MALFORMED.keys())
def test_validate_reports_malformed_sources_as_issues(case: str) -> None:
    edit, path, message = MALFORMED[case]
    source = example_source()
    edit(source)
    response = TestClient(create_app()).post("/api/protocol/validate", json=source)
    assert response.status_code == 200
    report = response.json()
    assert report["ok"] is False
    assert any(i["path"] == path and message in i["message"] for i in report["issues"]), report

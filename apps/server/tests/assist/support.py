"""What the assistant's recordings were made from, shared by the tests and `record_fixtures`."""

import json
from pathlib import Path
from typing import Any

import yaml

FIXTURES = Path(__file__).parent / "fixtures"
COUNTS = FIXTURES / "counts"
STACK = Path(__file__).parents[1] / "enrich" / "fixtures" / "stack"
CONTACT_EMAIL = "librarian@example.org"  # the gateway attaches it; no recording holds it

DESCRIPTION = (
    "Does nicotine replacement therapy (any form: patch, gum, lozenge, inhaler, nasal or mouth "
    "spray, sublingual tablet) help adults who smoke tobacco to stop smoking, compared with "
    "placebo or no NRT? A second question compares combination NRT (patch plus a fast-acting "
    "form) with a single form. Primary outcome: abstinence at six months or longer, "
    "biochemically validated where possible; secondary: serious adverse events and withdrawal "
    "due to adverse events. Randomized or quasi-randomized controlled trials only. Any language, "
    "no date limits."
)
# The nicotine replacement example, whose PubMed strategy gets improved.
EXAMPLE = Path(__file__).parents[4] / "examples" / "protocols" / "nicotine-replacement.yaml"
# Where `KITTUM_ASSIST_BACKEND=demo` finds the recorded answers. The three draft answers were
# edited by hand when the protocol schema changed: a top-level `null` field and the topic's `kind`
# were dropped, and the outcome categories GLM named became `primary` and `secondary`. Every
# answer was then re-keyed by replaying the recording run with those answers.
ANSWERS = (
    Path(__file__).parents[2] / "src" / "kittum" / "app" / "demo_data" / "assist" / "answers.json"
)
PUBMED = "search_topics[TRIALS].source_plan.sources[medline].strategy"
# A German protocol whose question b1 lists its comparators in German only, and the question the
# assistant improves in it.
# Its answer in `answers.json` is hand-written, not recorded from a model: it replaced the answer
# GLM gave for an earlier example when that example was removed, under the same model identity.
TRANEXAMIC = EXAMPLE.with_name("tranexamic-acid.yaml")
QUESTION = "questions[b1]"


def tranexamic_source() -> dict[str, Any]:
    """The tranexamic acid example as JSON data."""
    return json.loads(json.dumps(yaml.safe_load(TRANEXAMIC.read_text()), default=str))


def drafted_pubmed(document: dict) -> str:
    """The path of the PubMed strategy in a drafted protocol."""
    topic = document["search_topics"][0]
    source = next(s for s in topic["source_plan"]["sources"] if s.get("platform") == "PubMed")
    return f"search_topics[{topic['id']}].source_plan.sources[{source['id']}].strategy"


def accepted_subset(change_ids: list[str]) -> list[str]:
    """The changes the tests and the journey accept: all but the last."""
    return change_ids[:-1]


# A PubMed strategy with a mistyped word, a mistyped quoted phrase, and a word PubMed ignores:
# its recorded count carries all three warnings.
MISTYPED = 'by "nicotine replacment zzqx"[tiab] OR nicotinne[tiab] OR smoking cessation[tiab]'

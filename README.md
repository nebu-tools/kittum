# Kittum

<img align="right" src="assets/branding/kittum-logo.webp" alt="Kittum logo: a hen with spectacles sifting sheets of paper through a kitchen sieve" width="160" height="160">

Kittum is a workbench for systematic reviews that someone else must be able to audit. The AI
screens each record as a blinded third reader beside two human reviewers. Every decision goes into
a signed, hash-chained ledger, and anyone can verify that ledger offline.

<br clear="right">

## Status

Kittum runs a review from the protocol to the reports. Some stages are complete. Others work for
the common case only.

| Stage | What works | What is missing |
| --- | --- | --- |
| Protocol | PICO per scope, criteria, search plans, and screening modes, in a form or as YAML. Every save is a revision. A model can draft the protocol and suggest changes, and the methods lead accepts each one. Publishing freezes and signs a version. | |
| Search | PubMed and ClinicalTrials.gov run live. Imports of RIS, PubMed XML and MEDLINE, Scopus CSV, Web of Science, registry CSV, and your own CSV or XLSX list. A known pool shows which known papers a search misses. | Commercial databases have no live adapter; import their exports. |
| Enrichment | OpenAlex, Crossref, Europe PMC, Unpaywall, and PubTator fill identifiers and abstracts, list open-access copies, and flag retractions. | Local entity tagging. |
| Corpus | Deduplication, record-to-study links that a human confirms, and frozen identity revisions for the PRISMA counts. | Update searches: Kittum refuses a second corpus ingest. |
| Screening | Modes M1, M2, M3, and S at title and abstract and at full text. The server enforces blinding. The AI's reading appears after the human decides. Sentinels, audit samples, adjudication, and second opinions. | One AI judge per stage. M3 reports its stop test but does not stop the queues. |
| Full text | The open-access waterfall, uploads, PDF parsing, and quote anchors on the parsed text. | OCR adapters. |
| Extraction | A deterministic path (Janus) and an LLM path, validators, and reconciliation. Every accepted value is grounded in a quote on the PDF. | No second independent extraction when the paths disagree on a critical field. No grounding exceptions, such as a value read from a figure. |
| Appraisal | RoB 2 for individually randomised trials, with the published algorithms and quotes checked against the PDF. | All other instruments, including the RoB 2 variants for cluster and crossover trials, and ROBINS-I. |
| Reports | The PRISMA 2020 flow, RIS files, the AI-use disclosure, extraction values, the risk-of-bias table, and the sealed handover bundle with its offline check. | |

There is no sign-in yet. The workbench asks who you are, and the server accepts the answer. Anyone
who can reach the server can act as any reviewer, the methods lead included. Run Kittum only on a
machine or network where you trust every user.

## Try the demo

The demo is a seeded review: the nicotine replacement example protocol, a recorded PubMed search,
a CENTRAL export, two open-access trial PDFs, and the AI judge's recorded readings. It needs no
network and no model server. You need Docker, [uv](https://docs.astral.sh/uv/), and Node 26 with
pnpm. Run these commands from the repository root:

```sh
# Postgres 18, migrated
docker compose -f infra/compose.yaml up -d --wait postgres
docker compose -f infra/compose.yaml run --rm migrate

# Install, and build the workbench into apps/web/dist
(cd apps/server && uv sync)
pnpm install && pnpm build

# Settings. .env.example sets KITTUM_WEB_DIST=../web/dist, so `kittum serve` serves the
# workbench too. Load the settings in every shell you use below.
cp .env.example .env
cd apps/server
set -a && . ../../.env && set +a

# Seed the demo review with the recorded answers
uv run kittum demo seed

# The API and the workbench on http://127.0.0.1:8100, and a worker in a second shell
KITTUM_JUDGE_BACKEND=demo uv run kittum serve
KITTUM_JUDGE_BACKEND=demo uv run kittum worker
```

Open <http://127.0.0.1:8100>, choose the review, and choose who you are. To screen as two reviewers
side by side, use two browser tabs. Each tab remembers its own person.

1. Sam Okafor and Jun Park screen titles and abstracts. Press `i` to include, `e` and a number to
   exclude on a criterion, `u` for unclear, `Backspace` to undo, and `Space` for the next record.
   The AI shows its reading only after the reviewer decides, and only when it disagrees.
2. Rita Nunes, the auditor, screens the audit sample.
3. Elena Weiss adjudicates the conflicts between the two reviewers.
4. Morgan Lee, the methods lead, follows **Progress**, confirms study links on **Studies**, and
   renders the PRISMA flow on **Reports**. Sealing the bundle needs a checkpoint key in
   `KITTUM_CHECKPOINT_KEY`. `.env.example` shows how to make one.

`uv run kittum verify <bundle>` checks a sealed bundle offline, and
`uv run kittum ledger verify <project ID>` checks a project's chain in the database.

To judge live with a local model, install [Ollama](https://ollama.com), run
`ollama pull qwen3.5:4b`, and seed with `uv run kittum demo seed --live`. Then start `serve` and
`worker` without `KITTUM_JUDGE_BACKEND=demo`.

To start a review of your own, press `c` on the start page. **Setup** takes the methods lead through
the steps in order. `uv run kittum project --help` lists the same steps for the command line.

## Concepts

### Ledger

Every state change writes an event in the same transaction as its data. Each event
holds the hash of the event before it, so a changed or deleted event breaks the chain. Kittum signs
checkpoints of the chain with a separate key. A checkpoint also catches a chain that someone
rewrote from some point on, or cut short.

### Handover bundle

At the end, the methods lead seals a bundle: a SQLite copy of the ledger, the
artifacts that may leave, the reports, and a signed manifest. `kittum verify` checks the
signatures, the chain, and every file hash without a network or a server. Any cell of an exported
table traces to the accepted value, its place on the PDF, and the person who approved it.

### Screening modes

The protocol sets a mode for each search topic.

- M1: two humans screen every record, and the AI is a third reader. This is the default.
- M2: one human and the AI screen every record.
- M3: humans screen in the AI's order and stop when a statistical test passes.
- S: one human screens each record and can ask another for a second opinion. S is for targeted
  searches with working documentation. A review that others check for completeness uses M1, M2,
  or M3.

No mode lets the AI exclude a record that no human saw.

### Sentinels

Sentinels are records whose correct decision is known. Kittum puts them in the
normal queue, and the reviewers cannot tell them from other records. Missed sentinels show that a
reviewer's attention has drifted.

### Audit sample

In M1, M2, and M3, the protocol sets a random share of records that an auditor
screens again. From the sample, Kittum estimates how many relevant records the process missed, with
an upper bound.

## Configuration

Kittum reads its settings from environment variables. [.env.example](.env.example) lists every
variable and explains it. The most important ones:

| Variable | What it sets |
| --- | --- |
| `KITTUM_DATABASE_URL` | The Postgres database |
| `KITTUM_ARTIFACT_DIR` | The content-addressed store for PDFs, raw responses, and reports |
| `KITTUM_LLM_ENDPOINTS` | The model servers, as recipe=URL pairs |
| `KITTUM_JUDGE_MODEL`, `KITTUM_JUDGE_BACKEND` | The screening judge, and whether it calls a model or replays the demo |
| `KITTUM_CONTACT_EMAIL` | The address that Crossref, NCBI, and Unpaywall ask for |
| `KITTUM_WEB_DIST` | The built workbench that `kittum serve` serves |
| `KITTUM_CHECKPOINT_KEY` | The Ed25519 key that signs checkpoints and bundles |

[Models, recordings, and replay](docs/models-and-recordings.md) covers hosted model recipes, the
token budget for models that think, the protocol assistant, and how to record a live run and
replay it.

## Development

[AGENTS.md](AGENTS.md) has the layout and the conventions. These are the checks that CI runs:

```sh
# Server, in apps/server, with KITTUM_DATABASE_URL set for the Postgres tests
uv run ruff format --check src tests
uv run ruff check src tests
uv run ty check src
uv run pytest

# Workbench and contracts, from the repository root
pnpm exec playwright install chromium   # once: the browser tests need it
pnpm check                              # format, lint, typecheck, tests, knip, build
pnpm contracts:generate                 # after an API change; commit the result
pnpm --dir apps/web e2e                 # the whole review on the real stack
```

`pnpm dev:web` serves the workbench with hot reload on port 5180 against a running `kittum serve`.
`VITE_KITTUM_WORKBENCH=mock pnpm dev:web` shows recorded example data without a server.

## Documents

- [Design](DESIGN.md): how Kittum works and why. Read it before you change behaviour.
- [Sources](docs/sources.md): search and enrichment sources, their limits, and their licences.
- [Document and model tools](docs/document-tools.md): PDF parsers, OCR, extraction libraries, and
  local models.
- [Dependencies](docs/dependencies.md): the licence decision and an evaluation of each dependency.
- [Standards and regulation](docs/standards.md): what PRISMA, RAISE, the EU AI Act, and GDPR
  require of the system.
- [Models, recordings, and replay](docs/models-and-recordings.md): hosted models and recorded runs.

## Name and logo

Kittum (Akkadian *kittu*, "truth") was a Babylonian deity whom legal texts call on as a witness.
Say it KIT-toom. The logo is a stand-in sketch until a hand-painted one replaces it.

## Licence

Kittum is licensed under the [GNU Affero General Public License v3.0 or later](LICENSE).

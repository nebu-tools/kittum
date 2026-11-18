# Working on Kittum

Kittum runs auditable, AI-assisted systematic literature reviews, from the protocol to the final
reports, with every decision in a hash-chained ledger. Read [DESIGN.md](DESIGN.md) before changing
behaviour.

## Layout

```text
kittum/
├── apps/
│   ├── server/                 # Python 3.14, uv: every module below, the API, and the CLI
│   │   ├── src/kittum/<module>/   # one package per module from DESIGN.md
│   │   ├── tests/<module>/        # that module's tests
│   │   └── db/migrations/         # dbmate SQL migrations
│   └── web/                    # the reviewer workbench (React 19, TanStack Router, Vite+)
├── packages/contracts/         # TypeScript client generated from the server's OpenAPI
├── infra/compose.yaml          # Postgres 18, dbmate, GROBID
└── docs/                       # design reference
```

## Setup and checks

```sh
# Server
cd apps/server
uv sync
uv run ruff format src tests && uv run ruff check src tests && uv run ty check src
uv run pytest                          # Postgres tests skip without a database

# Postgres for the `postgres` tests
docker compose -f infra/compose.yaml up -d --wait postgres
docker compose -f infra/compose.yaml run --rm migrate
export KITTUM_DATABASE_URL=postgresql://kittum:kittum@127.0.0.1:55432/kittum

# Workbench and contracts (from kittum/)
pnpm install
pnpm check                             # format, lint, typecheck, tests, knip, build
pnpm contracts:generate                # after any API change; commit the result
pnpm dev:web                           # with `uv run kittum serve` running
pnpm --dir apps/web e2e                # the whole review on the real stack (KITTUM_DATABASE_URL)
```

CI runs all of this, including Postgres 18 and a check that the generated contracts match the
server.

## Conventions

- **Transactions:** a function that changes state takes `tx: kittum.db.Tx` from its caller and
  writes its rows and its ledger event through it. Modules never open transactions; entry points
  do (`with ctx.db.transaction() as tx:`). Rows come back as dicts.
- **Artifacts** (PDFs, raw responses, model outputs, reports) go to `kittum.storage.ArtifactStore`
  by SHA-256 before any row references them.
- **Import other modules only through their package**: `from kittum.judge import Verdict`, never
  `from kittum.judge.prompts import ...`. `tests/test_architecture.py` enforces this and the
  allowed dependencies. Tests may also import `kittum.<module>.testing` fakes.
- **Build against fakes.** Every module with a seam ships a fake in `<module>/testing.py`
  (`InMemoryLedger`, `RecordedBackend`, `FakeTransport`). A new seam ships its fake in the same
  change.
- **HTTP routes** for a module go in `src/kittum/api/routes/<module>.py` as a `router`, registered
  with one line in `api/routes/__init__.py`. Routes take `Context` and stay thin.
- **Migrations** are new files named `<UTC timestamp>_<module>_<what>.sql` with `migrate:up` and
  `migrate:down`. A module creates only its own tables and never alters another module's.
- Values that cross modules are `kittum.core.Record`s: frozen, unknown fields rejected.
- IDs come from `kittum.core.new_id()` (UUIDv7). Time comes from a `Clock`, never `datetime.now()`.
- Decimals stay exact strings with explicit units. Empty is not zero.
- A state change writes its ledger event in the same transaction as its rows. Deciders do no I/O;
  side effects run as jobs (see `kittum.runs`).
- HTTP to external sources (literature databases, registries, publishers) goes through
  `kittum.gateway`, which owns rate limits, retries, and the response cache. Model servers are
  reached only through `kittum.models.LlmBackend`.
- A quote counts only if `kittum.core.find_quote` finds it in the source.
- **Tests prove behaviour end to end.** Prefer a few tests that drive a realistic flow through
  your public interface against real Postgres and recorded real-world fixtures (a real PubMed
  response, a real trial PDF) over many tests of trivial pieces. No tests that only check a
  constructor, a getter, or that a function exists. Every test should be able to catch a bug a
  user would notice.
- Tests never touch the network. Use the fakes, recorded responses, or fixtures in the repo.
- **The core is domain-neutral.** Code and docs name no jurisdiction, agency, or agency template.
- Python and TypeScript match the surrounding code: comment density, naming, idiom.

## Dependencies and licences

Kittum is AGPL-3.0-or-later. A dependency's licence must be compatible with AGPL-3.0 for the way it
is combined (not GPL-2.0-only or CDDL code linked into Kittum). Model weights
must be on `kittum.models.ALLOWED_WEIGHTS_LICENCES`: no revenue caps, non-commercial terms,
field-of-use limits, or no-competition clauses. Add every new dependency or model to
[docs/dependencies.md](docs/dependencies.md) or [docs/document-tools.md](docs/document-tools.md)
with its licence and the reason, in the same pull request.

## Done means

- The change works for the cases it is for, with tests.
- Ruff, ty, pytest, and (if touched) `pnpm check` pass.
- Every fake still matches its interface.

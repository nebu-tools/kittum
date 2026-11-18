# Models, recordings, and replay

This page covers what the README leaves out: hosted model recipes, the token budget for models
that think, the protocol assistant's backend, and how to record a live run and replay it. Every
setting is an environment variable; [.env.example](../.env.example) lists them all. Commands run
in `apps/server` with the settings loaded, as in the [README](../README.md#try-the-demo), so the
compose file is `../../infra/compose.yaml`.

## A hosted model

Kittum judges with a local model by default. To judge with a hosted OpenAI-compatible API,
add the model as a recipe of your own in a file under `.kittum/` (git ignores it), point
`KITTUM_RECIPES_FILE` at it, and name the environment variable that holds the key in
`KITTUM_LLM_API_KEYS`. Kittum sends the key as a bearer token and never stores it. The AI
disclosure records the recipe, `serving: hosted`, and the endpoint. You decide which records may go
to a hosted model. For example, in `apps/server/.kittum/recipes.yaml`:

```yaml
recipes:
  - id: example-provider-model
    family: example
    repository: example-org/example-model # where the weights and their licence come from
    served_name: example-model # the model name the API expects
    revision: "2026-10"
    weights_licence: apache-2.0 # must be on ALLOWED_WEIGHTS_LICENCES
    licence_url: https://example.org/example-model/LICENSE
    serving: hosted # hosted recipes pin no weight files
    context_tokens: 131072
    reasoning: true
    extra_body: {"thinking": {"type": "disabled"}} # sent unchanged with every request
    reasoning_tokens: 0 # added to every call's max_tokens, for thinking that counts against it
    stream: true # false sends one non-streamed request
```

Kittum asks for every answer as a stream, so a model that thinks for minutes keeps sending and
never hits the read timeout. Some models count their thinking against `max_tokens`. GLM-5.3-Flash
on high reasoning does, and it can think through 7,600 of the draft's 12,000 tokens. For such a
model, set `reasoning_tokens` to the room it needs to think, for example 24000. Kittum adds it to
the `max_tokens` of every call, and the answer budgets stay the same. The ledger and the AI
disclosure record it. When the budget still cuts an answer off, Kittum does not ask again with the
same budget. The attempt fails and says how many tokens the model used, and how many of them went
to reasoning when the server reports it. If the reasoning used most of them, raise
`reasoning_tokens`. If the answer took most of them, ask again. GLM sometimes repeats search
terms until the budget ends.

```sh
export EXAMPLE_PROVIDER_API_KEY=...   # in your shell, not in .env
export KITTUM_RECIPES_FILE=.kittum/recipes.yaml
export KITTUM_LLM_ENDPOINTS=example-provider-model=https://api.example-provider.test/v1
export KITTUM_LLM_API_KEYS=example-provider-model=EXAMPLE_PROVIDER_API_KEY
export KITTUM_JUDGE_MODEL=example-provider-model
uv run kittum demo seed --live
```

## The protocol assistant

The lead writes the protocol in a form, or as YAML, and every save is a revision under their name.
A model can draft the protocol from a few sentences and suggest changes to one part at a time,
which the lead accepts change by change. `KITTUM_ASSIST_MODEL` names its recipe (by default the
judge's), served by one of `KITTUM_LLM_ENDPOINTS`. **Count hits** asks PubMed and
ClinicalTrials.gov through the gateway. `KITTUM_ASSIST_BACKEND=demo` replays the answers recorded
in `apps/server/src/kittum/app/demo_data/assist` instead, and
`uv run python -m tests.assist.record_fixtures <recipe>` records them again.

## Record a live run, replay it

A live run (real sources, a real model) can be captured once and replayed exactly, for a filmed
demo or a reproducible test. Recording writes what the sources and the models answered; replaying
answers every request from those recordings, without a network, a model server, or a key. The
source and model answers, the counts, the AI readings, the decisions, and the reports match the live
run. Random draws over record IDs (the auditor's samples, where sentinels are placed) can differ,
because every run gives its records new IDs: a stratum may sample two records live and three in the
replay, so the elusion upper bound and the full-text audit items differ too. They match only when
the replay reuses the live run's database IDs. What people do in the workbench is not recorded:
replay the same steps.

| Variable                | What it does                                                                                                                                                                                                                                         |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `KITTUM_GATEWAY_RECORD` | A directory. The gateway writes every source response into it (status, `Content-Type`, body), each attempt in order, with a `manifest.json`, the format `KITTUM_GATEWAY_RECORDINGS` reads. Live only: refused with `KITTUM_OFFLINE=1`.                 |
| `KITTUM_LLM_RECORD`     | A directory, or a file ending in `.json`. Every model completion is written under its request hash, in order, with the model identity the server reported, the serving, the endpoint, and the exact request and response bytes, and which recipe judged and which the protocol assistant used. |
| `KITTUM_LLM_REPLAY`     | A recording from `KITTUM_LLM_RECORD`. Replaces every model server: each request is answered from it, and one with no recorded answer left fails naming its request hash. The judge and the protocol assistant use the recipes the live run used, unless `KITTUM_JUDGE_MODEL` or `KITTUM_ASSIST_MODEL` names another. Refused with `KITTUM_LLM_RECORD`.     |

A replay answers in the order the live run was answered: a source that failed twice and then
answered fails twice and then answers, a connection that failed fails again, and a model asked
the same thing twice gives its two answers in turn.

Recordings hold no credentials and no personal data. Request headers (where API keys travel) are
never written, and credential query parameters such as NCBI's `api_key` are dropped. The contact
address becomes `contact@example.invalid` in the parameter each source takes it in (Crossref's and
OpenAlex's `mailto`, Unpaywall's and NCBI's `email`), on recording and on lookup alike, so a replay
finds the responses whatever `KITTUM_CONTACT_EMAIL` it runs with. A request with the address
anywhere else, a configured key or password in any parameter, or user info in its URL is refused
before it is sent, naming where, not what. A response body that holds the address or a key is
cleaned, and the live run gets the cleaned body too, so it and its replay store the same bytes. A
model completion whose request, answer, or raw bytes hold one is not recorded: the call fails,
naming the field.

A replay never passes itself off as a live call. The verdicts and transcripts keep the model
identity that really answered and its serving, and their endpoint, which every verdict, ledger
event, and AI disclosure carries, reads `replay of <recorded endpoint>`, for example `replay of
https://api.example-provider.test/v1`.

Recording uses the deployment's live rate limits but a response cache of its own process, so
every request of the run reaches its source and is written, even one the deployment had cached.
The demo judge (`KITTUM_JUDGE_BACKEND=demo`) is a replay already; record with a real judge. So is
the demo assistant (`KITTUM_ASSIST_BACKEND=demo`), which is refused beside `KITTUM_LLM_REPLAY`
rather than silently replaced by it or shadowing it: replay the assistant from one recording.

Recording checks every configured key and password before it starts: each must be at least 12
characters, not a common password, and not part of what Kittum itself sends (PubMed's
`tool=kittum`, the User-Agent, the placeholder). Otherwise ordinary requests and bodies would hold
the "secret" and be refused or redacted, so Kittum refuses to start, naming the setting. The dev
database password `kittum` is such a password: record with a role of its own that has a long one.
In the compose Postgres:

```sh
export KITTUM_RECORDER_PASSWORD=$(openssl rand -hex 16)
docker compose -f ../../infra/compose.yaml exec postgres psql -U kittum -d kittum \
  -c "create role kittum_recorder login password '$KITTUM_RECORDER_PASSWORD' in role kittum" \
  -c "alter role kittum_recorder set role kittum"   # what it creates belongs to kittum
export KITTUM_DATABASE_URL=postgresql://kittum_recorder:$KITTUM_RECORDER_PASSWORD@127.0.0.1:55432/kittum
```

For example, the demo with a hosted judge, recorded once into `.kittum/demo-run/` and replayed in
a fresh database:

```sh
# Record: live sources and the live model, as kittum_recorder.
export KITTUM_GATEWAY_RECORD=.kittum/demo-run/sources
export KITTUM_LLM_RECORD=.kittum/demo-run/models
uv run kittum demo seed --live
uv run kittum serve & uv run kittum worker   # click through the run; enrichment is recorded too

# Replay: a fresh database, no network, no key. The model comes from the recordings, so the
# endpoints and keys stay unset; KITTUM_RECIPES_FILE and KITTUM_JUDGE_MODEL stay as they were.
unset KITTUM_GATEWAY_RECORD KITTUM_LLM_RECORD KITTUM_LLM_ENDPOINTS KITTUM_LLM_API_KEYS
unset EXAMPLE_PROVIDER_API_KEY
docker compose -f ../../infra/compose.yaml exec postgres psql -U kittum -d kittum \
  -c "create database kittum_replay"
docker compose -f ../../infra/compose.yaml run --rm \
  -e DATABASE_URL='postgres://kittum:kittum@postgres:5432/kittum_replay?sslmode=disable' migrate
export KITTUM_DATABASE_URL=postgresql://kittum:kittum@127.0.0.1:55432/kittum_replay
export KITTUM_OFFLINE=1
export KITTUM_GATEWAY_RECORDINGS=.kittum/demo-run/sources
export KITTUM_LLM_REPLAY=.kittum/demo-run/models
uv run kittum demo seed --live
uv run kittum serve & uv run kittum worker
```

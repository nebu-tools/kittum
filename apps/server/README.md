# kittum-server

The Kittum backend. See [AGENTS.md](../../AGENTS.md) for how to work on it.

## Janus

Extraction uses [Janus](https://github.com/nebu-tools/janus)'s server package, pinned to a commit
in `pyproject.toml` under `[tool.uv.sources]`. `src/kittum/extraction/janus_path.py` is the only
module that imports it. To move to a newer Janus, change `rev` and run `uv lock`. To work on both
at once, point the source at a local checkout:
`janus-server = { path = "../../../janus/apps/server", editable = true }`, and do not commit that.

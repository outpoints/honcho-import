# Honcho Import

Utilities for backfilling historical conversation history into a [Honcho](https://honcho.dev) workspace.

When you adopt Honcho memory partway through a project, your past conversations aren't in it yet. These tools read your local conversation history and write it to Honcho so your memory reflects everything you've already done — not just what happens from now on.

The repo hosts two standalone importers, one per source:

| Importer | Source | Folder |
|---|---|---|
| **Claude Code** | `~/.claude/projects/**/*.jsonl` transcripts | [`claude-code-backfill/`](claude-code-backfill/) |
| **Hermes Agent** | `~/.hermes/state.db` (SQLite) | [`hermes-backfill/`](hermes-backfill/) |

Each importer is self-contained, has its own detailed README, and is **dry-run by default** — nothing is written until you pass `--execute`.

## Requirements

- Python 3.10+
- The `honcho-ai` Python package — `python -m pip install honcho-ai` (or `uv pip install honcho-ai`)
- A reachable Honcho server (self-hosted or hosted)

## Quick start — Claude Code

The Claude Code importer mirrors the official [Claude Code Honcho plugin](https://honcho.dev/docs/v3/guides/integrations/claude-code): it reads the **same `~/.honcho/config.json`** (peer name, AI peer, workspace, endpoint), names sessions per-directory exactly like the plugin, reconstructs native `[Tool]` call summaries, and preserves original timestamps — so a backfill merges seamlessly with the sessions the live plugin writes.

```bash
cd claude-code-backfill

# 1. Preview everything (reads ~/.honcho/config.json; writes nothing)
python honcho_backfill.py

# 2. Scope to one project and inspect detail
python honcho_backfill.py --project Scrobblie --verbose

# 3. Write to Honcho
python honcho_backfill.py --execute

# Optional: mask labeled secrets (api keys, session keys, passwords) before writing
python honcho_backfill.py --redact-secrets --execute
```

Identity and target default from your config, but everything is overridable:

```bash
python honcho_backfill.py \
  --peer-name "<your-name>" --ai-peer claude \
  --workspace claude_code \
  --honcho-url http://your-honcho-host:8000 \
  --execute
```

Full options and behavior (content scope, merge/`--fill-gap`, exclusions) are in [`claude-code-backfill/README.md`](claude-code-backfill/README.md).

## Quick start — Hermes Agent

The Hermes importer reads a Hermes Agent `state.db` and mirrors Hermes' live Honcho write path (sanitize, chunk, observation settings). It resolves config from a Hermes `honcho.json` or from CLI flags.

```bash
cd hermes-backfill

# 1. Preview 5 sessions (writes nothing)
python honcho_backfill.py \
  --state-db ~/.hermes/state.db \
  --honcho-url http://localhost:8000 \
  --target-workspace default \
  --target-user-peer user \
  --target-ai-peer assistant \
  --max-sessions 5

# 2. Execute once the preview looks right
python honcho_backfill.py \
  --state-db ~/.hermes/state.db \
  --honcho-url http://localhost:8000 \
  --target-workspace default \
  --target-user-peer user \
  --target-ai-peer assistant \
  --execute
```

For an authenticated server, prefer `export HONCHO_API_KEY=...` over passing the key on the command line. Full options (profiles, `--mode`, observation settings) are in [`hermes-backfill/README.md`](hermes-backfill/README.md).

## Safety

- **Dry-run by default** — both importers preview unless `--execute` is supplied.
- **No hardcoded identity** — peer names, workspace, URLs, and credentials come from config, env vars, or flags.
- **Secrets** — conversation history can contain real credentials. Use `--redact-secrets` (and review the output) if your history may include API keys or tokens. Prefer `HONCHO_API_KEY` over passing keys as flags. Never commit `state.db`, `honcho.json`, `.env`, or logs (see `.gitignore`).

## Repo layout

```text
honcho-import/
├── README.md              # this overview
├── LICENSE                # applies repo-wide
├── claude-code-backfill/  # Claude Code importer + README
└── hermes-backfill/       # Hermes Agent importer + README
```

## License

See [`LICENSE`](LICENSE).

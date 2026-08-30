# Honcho Import

Utilities for backfilling historical conversation history into a [Honcho](https://honcho.dev) workspace.

When you adopt Honcho memory partway through a project, your past conversations aren't in it yet. These tools read your local conversation history and write it to Honcho so your memory reflects everything you've already done — not just what happens from now on.

The repo hosts three standalone importers, one per source:

| Importer | Source | Folder |
|---|---|---|
| **Claude Code** | `~/.claude/projects/**/*.jsonl` transcripts | [`claude-code-backfill/`](claude-code-backfill/) |
| **Claude Cowork** | `~/Library/Application Support/Claude/local-agent-mode-sessions/**/audit.jsonl` | [`cowork-backfill/`](cowork-backfill/) |
| **Hermes Agent** | `~/.hermes/state.db` (SQLite) | [`hermes-backfill/`](hermes-backfill/) |

Each importer is self-contained, has its own detailed README, and is **dry-run by default** — nothing is written until you pass `--execute`.

## Requirements

- Python 3.10+
- `honcho-ai` 2.4+ — `python -m pip install -r requirements.txt` (or `uv pip install -r requirements.txt`)
- A reachable Honcho server (self-hosted or hosted)

### Which SDK version?

**Honcho server 3.x is driven by honcho-ai 2.x.** The server and the Python SDK
are versioned independently, so the numbers never line up — there is no
"honcho-ai 3.1.0". The 2.x line speaks the `/v3` API that Honcho 3.x serves;
honcho-ai 1.x is an older, differently shaped client and will not work.

Every importer prints both versions on startup and warns if they disagree:

```text
Server version: 3.1.0   SDK: honcho-ai 2.4.0 (speaks API v3)
```

This repo is verified end to end against **Honcho 3.1.0 with honcho-ai 2.4.0**.

## Re-running an import

The Claude Code and Cowork importers default to `--merge dedupe`: they read
what the target session already holds and write only the messages missing from
it. That makes an import **safe to re-run** and makes it pick up new
conversations in sessions that were imported before — including folders where
your live Honcho plugin is already writing.

A dry run reads the workspace too, so its counts are the real delta:

```text
  [10/26] alex-my-app  chats=14 have=2593 deduped=69 -> 774 new chunks
```

`--merge skip` restores the old all-or-nothing behavior, `--merge gap` writes
only history older than what is there, `--merge force` appends unconditionally,
and `--offline` previews the transcripts without contacting the server at all.
Details, including how dedupe matches, are in each importer's README.

## Quick start — Claude Code

The Claude Code importer mirrors the official [Claude Code Honcho plugin](https://honcho.dev/docs/v3/guides/integrations/claude-code): it reads the **same `~/.honcho/config.json`** (peer name, AI peer, workspace, endpoint), names sessions per-directory exactly like the plugin, reconstructs native `[Tool]` call summaries, and preserves original timestamps — so a backfill merges seamlessly with the sessions the live plugin writes.

```bash
cd claude-code-backfill

# 1. Preview everything (reads ~/.honcho/config.json; writes nothing)
python honcho_backfill.py

# 2. Scope to one project and inspect detail
python honcho_backfill.py --project my-app --verbose

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

Full options and behavior (content scope, `--merge` modes, exclusions) are in [`claude-code-backfill/README.md`](claude-code-backfill/README.md).

## Quick start — Claude Cowork

The Cowork importer reads the Claude **desktop app's** "local agent mode" sessions and writes them using the same conventions as the Claude Code plugin (there is no official Cowork → Honcho plugin to mirror). It groups chats **per working folder**, reconstructs `[Tool]` summaries (including `Dispatched task …`), extracts the ditto orchestrator's `SendUserMessage` replies as assistant turns, and preserves original timestamps. By default it imports into the same `claude_code` workspace, so a Cowork chat on a folder merges with that folder's Claude Code session.

```bash
cd cowork-backfill

# 1. Preview everything (discovers root / ditto / dispatch_child sessions; writes nothing)
python honcho_backfill.py

# 2. Scope to one project and inspect detail
python honcho_backfill.py --project my-app --verbose

# 3. Write to Honcho
python honcho_backfill.py --execute

# Keep Cowork memory separate from Claude Code instead of merging:
python honcho_backfill.py --workspace cowork --execute
```

`dispatch_child` subagent transcripts are excluded by default (the parent ditto conversation is still imported); add `--include-subagents` for the full worker transcripts. Full options are in [`cowork-backfill/README.md`](cowork-backfill/README.md).

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

## Running it on a schedule

Because `--merge dedupe` makes an import idempotent, it is worth running
periodically rather than once. Claude Code prunes its own transcripts on a
retention window (~30 days by default), so history that is never imported is
eventually gone for good.

`scripts/scheduled-import.sh` runs both Claude importers and is built for
unattended use: it gates on being on the right network, exits `0` and quietly
when it is not, logs each run, and prunes old logs. Configure it with
`~/.honcho/import-schedule.env` — keep personal values there, not in the repo:

```bash
HONCHO_IMPORT_REPO="$HOME/honcho-import"
HONCHO_IMPORT_PYTHON="$HOME/honcho-import/.venv/bin/python"
HONCHO_IMPORT_SSID_GLOB='my_network*'          # optional
HONCHO_IMPORT_GATEWAY_MACS="aa:bb:cc:dd:ee:ff" # optional; see below
HONCHO_IMPORT_REQUIRE_HONCHO=1
HONCHO_IMPORT_URL="http://your-honcho-host:18100"
```

Anything already in the environment overrides the file, so a one-off run can
be steered without editing config:

```bash
HONCHO_IMPORT_GATEWAY_MACS="" ./scripts/scheduled-import.sh
```

### Gating on the network

A self-hosted Honcho usually lives on one LAN, and running the import from
elsewhere just fills a log with connection errors. Two gates are available and
can be combined:

- **`HONCHO_IMPORT_SSID_GLOB`** — matched against the current Wi-Fi SSID.
- **`HONCHO_IMPORT_GATEWAY_MACS`** — an allowlist of default-gateway MAC
  addresses, used automatically whenever the SSID cannot be read.

The fallback exists because **macOS 15+ will not tell a background job the
SSID.** Reading it requires Location Services authorization, which a LaunchAgent
or cron job has no way to obtain; every unprivileged method (`ipconfig
getsummary`, `networksetup`, `system_profiler`) returns the literal string
`<redacted>`, and the old `airport` binary is gone. The gateway MAC is a stable
fingerprint for the same router and needs no permissions. Set both: the SSID
rule takes over on its own if the job is ever granted location access.

### Scheduling on macOS

Prefer a **LaunchAgent** over a crontab entry. `StartCalendarInterval` fires on
the next wake if the Mac was asleep at the scheduled time; cron simply skips
the run, which for a weekly job on a laptop usually means it never happens.

```bash
cp docs/com.example.honcho-import.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.honcho-import.plist

launchctl kickstart -p gui/$(id -u)/com.example.honcho-import  # run it now
launchctl print gui/$(id -u)/com.example.honcho-import | grep "last exit"
```

Logs land in `~/Library/Logs/honcho-import/`, newest last, capped at
`HONCHO_IMPORT_LOG_KEEP` files (default 12).

## Safety

- **Dry-run by default** — every importer previews unless `--execute` is supplied. A dry run reads the target workspace (the SDK's get-or-create workspace call is the only write it makes) so the preview is the real delta; `--offline` skips even that.
- **Re-runnable** — `--merge dedupe` (the default for the Claude Code and Cowork importers) will not write a message the session already has.
- **No hardcoded identity** — peer names, workspace, URLs, and credentials come from config, env vars, or flags.
- **Secrets** — conversation history can contain real credentials. Use `--redact-secrets` (and review the output) if your history may include API keys or tokens. Prefer `HONCHO_API_KEY` over passing keys as flags. Never commit `state.db`, `honcho.json`, `.env`, or logs (see `.gitignore`).

## Repo layout

```text
honcho-import/
├── README.md              # this overview
├── requirements.txt       # honcho-ai floor (Honcho 3.x <- honcho-ai 2.x)
├── LICENSE                # applies repo-wide
├── docs/                  # sample LaunchAgent
├── scripts/               # scheduled-import.sh (unattended re-runs)
├── claude-code-backfill/  # Claude Code importer + README
├── cowork-backfill/       # Claude Cowork importer + README
└── hermes-backfill/       # Hermes Agent importer + README
```

## License

See [`LICENSE`](LICENSE).

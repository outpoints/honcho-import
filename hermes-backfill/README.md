# Honcho Import for Hermes Agent

Import historical [Hermes Agent](https://github.com/NousResearch/hermes-agent) conversations from a local `state.db` SQLite database into a Honcho workspace.

This folder contains a standalone backfill utility (`honcho_backfill.py`), designed for one-time or occasional migrations/backfills from Hermes' local session history into a self-hosted or hosted Honcho instance.

> Part of the [honcho-import](../README.md) repo. For the Claude Code importer, see [`../claude-code-backfill/`](../claude-code-backfill/).

## What this does

The script reads Hermes Agent conversation history from SQLite and writes it to Honcho as normal session messages:

- one Honcho session per source Hermes session
- user messages attributed to a user peer
- assistant messages attributed to an assistant/AI peer
- messages written via `peer.message(...)` and `session.add_messages(...)`
- long messages chunked using Hermes-style `[continued]` continuation chunks
- original message timestamps preserved as Honcho `created_at` by default
- dry-run by default; writes only when `--execute` is supplied

It avoids hardcoded personal names, session IDs, hostnames, private URLs, and credentials. All environment-specific details are provided through CLI flags, environment variables, or an optional Hermes `honcho.json`.

## What this does not do

By default, the script does **not**:

- upload transcript files
- attach source session IDs/titles/models as metadata
- print target session IDs in logs
- redact message content
- delete or reset existing Honcho data
- purge Honcho queue/work units

Those behaviors are either explicit flags or out of scope.

## Requirements

- Python 3.10+
- A Hermes Agent `state.db` file
- `honcho-ai` 2.4+ — the SDK line that speaks Honcho 3.x's `/v3` API
- A reachable Honcho server

Install the SDK if needed:

```bash
python -m pip install -r ../requirements.txt
```

Or with `uv`:

```bash
uv pip install -r ../requirements.txt
```

The server and SDK version numbers do not line up: **Honcho server 3.x is
driven by honcho-ai 2.x.** The importer prints both on startup and warns if the
SDK is speaking a different API version than the server serves.

Unlike the Claude Code and Cowork importers, this one has no `--merge dedupe`
mode: it still skips any target session that already holds messages unless
`--force-reimport` is passed.

If you already run Hermes Agent, the SDK is often available in Hermes' virtual environment. In that case you can run the script with that Python interpreter instead of installing anything globally.

## Files

```text
honcho_backfill.py  # importer
README.md           # this guide
```

The project `LICENSE` lives at the repo root.

## Quick start: self-hosted Honcho

First run a preview. This does not write anything:

```bash
python honcho_backfill.py \
  --state-db ~/.hermes/state.db \
  --honcho-url http://localhost:8000 \
  --target-workspace default \
  --target-user-peer user \
  --target-ai-peer assistant \
  --max-sessions 5
```

If the preview looks correct, execute the import:

```bash
python honcho_backfill.py \
  --state-db ~/.hermes/state.db \
  --honcho-url http://localhost:8000 \
  --target-workspace default \
  --target-user-peer user \
  --target-ai-peer assistant \
  --execute
```

For authenticated Honcho servers, prefer an environment variable over passing secrets on the command line:

```bash
export HONCHO_API_KEY='your-api-key'
python honcho_backfill.py \
  --state-db ~/.hermes/state.db \
  --honcho-url http://localhost:8000 \
  --target-workspace default \
  --target-user-peer user \
  --target-ai-peer assistant \
  --execute
```

## Quick start: using a Hermes `honcho.json`

If your Hermes profile already has a `honcho.json`, the script can resolve the base URL, workspace, peers, message limit, and observation settings from it:

```bash
python honcho_backfill.py --honcho-config ~/.hermes/honcho.json
```

With multiple host configs, specify the host key:

```bash
python honcho_backfill.py \
  --honcho-config ~/.hermes/honcho.json \
  --host my-honcho-host
```

If `--host` is omitted, the script uses the first enabled host config when available, otherwise it falls back to top-level config fields.

## Hermes profiles

The default profile reads:

```text
~/.hermes/state.db
~/.hermes/honcho.json
```

A named profile reads:

```text
~/.hermes/profiles/<profile>/state.db
~/.hermes/profiles/<profile>/honcho.json
```

Example:

```bash
python honcho_backfill.py --profile research --execute
```

You can always override paths explicitly:

```bash
python honcho_backfill.py \
  --state-db /path/to/state.db \
  --honcho-config /path/to/honcho.json
```

## Important safety workflow

Recommended workflow:

1. Run a dry-run with a small number of sessions.
2. Confirm the resolved workspace, peers, observation settings, and message counts.
3. Run a slightly larger dry-run if needed.
4. Execute the import.
5. Check Honcho session/message counts and queue status.

Example:

```bash
# Preview 5 sessions
python honcho_backfill.py \
  --state-db ~/.hermes/state.db \
  --honcho-url http://localhost:8000 \
  --target-workspace default \
  --target-user-peer user \
  --target-ai-peer assistant \
  --max-sessions 5

# Execute once satisfied
python honcho_backfill.py \
  --state-db ~/.hermes/state.db \
  --honcho-url http://localhost:8000 \
  --target-workspace default \
  --target-user-peer user \
  --target-ai-peer assistant \
  --execute
```

## CLI reference

Common options:

```text
--profile, -p NAME          Source Hermes profile name. Default: default
--host NAME                 Host key inside honcho.json. Default: first enabled host or top-level config
--state-db PATH             Path to source Hermes state.db
--honcho-config PATH        Path to honcho.json
--honcho-url URL            Honcho base URL, for example http://localhost:8000
--honcho-api-key KEY        Honcho API key. Prefer HONCHO_API_KEY env var for real secrets
--target-workspace NAME     Honcho workspace to write to
--target-user-peer NAME     Peer for historical user messages
--target-ai-peer NAME       Peer for historical assistant messages
--session-prefix PREFIX     Prefix for created sessions. Default: hist-<profile>
--sources LIST              Comma-separated source filter. Default: cli,telegram,webui,tui,api_server
--mode live|safe            Import mode. Default: live
--execute                   Actually write to Honcho. Omit for dry-run
--force-reimport            Do not skip target sessions that already contain messages
--max-sessions N            Process at most N sessions. 0 means all
--batch-size N              Messages per add_messages call. Default: 10
--batch-delay SECONDS       Delay between sessions. Default: 0.05
--message-max-chars N       Override message chunk size
--include-files             Upload transcript text files too. Off by default
--redact-secrets            Redact obvious token patterns before import
--include-metadata          Store source IDs/titles/profile/model as Honcho metadata
--verbose                   Print target session IDs and detailed per-session errors
--no-preserve-created-at    Use import time instead of original message timestamps
```

See the full generated help:

```bash
python honcho_backfill.py --help
```

## Import modes

### `--mode live` \(default\)

`live` mode is intended to mirror Hermes' live Honcho write path as closely as possible:

- sanitize text with Hermes' `sanitize_context()` when Hermes is importable
- otherwise use whitespace stripping as a portable fallback
- chunk messages at `messageMaxChars`
- write `peer.message(content, created_at=...)`
- add messages through `session.add_messages(...)`
- use observation settings resolved from `honcho.json` or defaults

Live mode does **not** redact content by default because live Hermes generally does not redact content before writing to Honcho. If you want redaction while keeping live-like behavior, add `--redact-secrets`.

### `--mode safe`

`safe` mode intentionally diverges from live behavior:

- enables conservative token redaction
- stores provenance metadata
- preserves timestamps
- disables user-peer observation
- enables assistant-peer observation only

Use this when privacy/queue pressure is more important than faithful replay.

## Timestamp behavior

Hermes stores message timestamps as Unix epoch floats in `state.db`. The importer converts each timestamp to a timezone-aware UTC `datetime` and passes it to Honcho as `created_at`.

If a single source message is split into multiple Honcho messages, the chunks receive microsecond offsets:

```text
original timestamp
original timestamp + 1 microsecond
original timestamp + 2 microseconds
...
```

This preserves ordering without materially changing chronology.

Disable timestamp preservation with:

```bash
python honcho_backfill.py --no-preserve-created-at ...
```

## Session naming and idempotency

By default, target sessions are named:

```text
hist-<profile>-<source-session-id>
```

Examples:

```text
hist-default-<source-session-id>
hist-research-<source-session-id>
```

If a target session already contains messages, it is skipped unless `--force-reimport` is supplied. This prevents accidental duplicate imports.

To reimport cleanly, prefer one of these approaches:

1. delete the old target sessions from Honcho, then rerun the importer; or
2. use a new prefix:

```bash
python honcho_backfill.py --session-prefix hist-v2 --execute
```

Use `--force-reimport` only if you intentionally want to append duplicate messages to existing sessions.

## Source filtering

By default, the script imports these Hermes session sources:

```text
cli,telegram,webui,tui,api_server
```

Cron sessions are excluded by default because they may contain automated output, monitoring noise, or operational data.

Include all sources, including cron:

```bash
python honcho_backfill.py --sources all
```

Import only selected sources:

```bash
python honcho_backfill.py --sources webui,cli
```

## Privacy and PII considerations

Historical chat logs can contain private data. Review these defaults carefully:

- Dry-run by default: no writes unless `--execute` is passed.
- No personal defaults: no hardcoded user names, workspace names, private URLs, session IDs, or credentials.
- No metadata by default: source session IDs, titles, profile names, and models are not stored unless `--include-metadata` is passed.
- No transcript uploads by default: `--include-files` is off because files duplicate conversation content and may include more context than intended.
- No session IDs in logs by default: target session IDs are printed only with `--verbose`.
- Secret redaction is opt-in in live mode: use `--redact-secrets` if your history may include API keys or tokens.
- `--mode safe` is available for a more privacy-oriented, non-faithful import.

Suggested cautious command:

```bash
python honcho_backfill.py \
  --state-db ~/.hermes/state.db \
  --honcho-url http://localhost:8000 \
  --target-workspace default \
  --target-user-peer user \
  --target-ai-peer assistant \
  --redact-secrets \
  --max-sessions 5
```

Then rerun with `--execute` after reviewing the preview.

## Secret redaction

When `--redact-secrets` or `--mode safe` is used, the script redacts conservative patterns for common API tokens, including:

- GitHub tokens
- NVIDIA API tokens
- OpenAI-style keys
- Anthropic-style keys

This is not a complete DLP system. If your history may contain secrets, review or pre-process the source data before import.

## Observation settings

Observation settings control whether Honcho derives memory/representations from each peer.

The script mirrors Hermes' Honcho observation presets:

```text
observationMode: directional
user:      observe_me=True,  observe_others=True
assistant: observe_me=True,  observe_others=True

observationMode: unified
user:      observe_me=True,  observe_others=False
assistant: observe_me=False, observe_others=True
```

Aliases are handled like Hermes:

```text
shared   -> unified
separate -> directional
cross    -> directional
```

If `honcho.json` is present but does not specify `observationMode`, live mode uses Hermes' configured-host default of `unified`. If no config is available and you configure the server only through CLI flags, live mode defaults to `directional` full observation.

Granular overrides are read from Hermes-style nested config when present:

```json
{
  "observation": {
    "user": {"observeMe": true, "observeOthers": false},
    "ai": {"observeMe": false, "observeOthers": true}
  }
}
```

Safe mode uses a reduced, assistant-focused observation setup:

```text
user:      observe_me=False, observe_others=False
assistant: observe_me=True,  observe_others=False
```

If a large backfill creates too much derivation work, consider safe mode, smaller batches, or deriver-side tuning.

## Honcho queue notes

After import, Honcho may show pending work units while its deriver processes observations.

A nonzero queue is not automatically an importer failure. Check:

- whether `in_progress_work_units` changes over time
- whether the deriver/worker service is running
- whether small sessions are waiting for token batching thresholds
- whether your Honcho backend exposes stale/orphaned queue entries

Some Honcho deployments batch representation work by token count. If `pending_work_units` remains nonzero with `in_progress_work_units=0`, inspect the deriver configuration and logs before assuming imported messages are broken.

Useful API endpoint:

```text
GET /v3/workspaces/<workspace_id>/queue/status
```

## Validation after import

At minimum, verify:

1. expected number of sessions were created
2. expected number of messages were written
3. user messages belong to the user peer
4. assistant messages belong to the assistant peer
5. timestamps look correct
6. search/context endpoints return expected content
7. queue status behaves as expected for your Honcho deployment

Example SDK check:

```python
from honcho import Honcho

h = Honcho(base_url="http://localhost:8000", workspace_id="default")
s = h.session("hist-default-example")
print(len(list(s.messages())))
print(s.queue_status())
```

## Troubleshooting

### `ModuleNotFoundError: honcho`

Install the Honcho SDK in the environment you are using:

```bash
python -m pip install -r ../requirements.txt
```

### `404 Not Found` on every write

The installed SDK is speaking the wrong API version. Honcho server 3.x serves
`/v3`, which the honcho-ai **2.x** line speaks; honcho-ai 1.x does not. The
startup banner prints both versions — check it before digging further.

### `State DB not found`

Pass the source DB path explicitly:

```bash
python honcho_backfill.py --state-db /path/to/state.db
```

### `Honcho config not found`

Either provide a config file:

```bash
python honcho_backfill.py --honcho-config /path/to/honcho.json
```

or configure the server directly:

```bash
python honcho_backfill.py --honcho-url http://localhost:8000
```

### Existing sessions are skipped

This is expected. The importer skips target sessions that already contain messages. Use a new prefix or delete the old sessions before reimporting.

```bash
python honcho_backfill.py --session-prefix hist-v2 --execute
```

### Queue has pending work but sessions look empty in a UI

This may indicate stale/orphaned queue entries or internal queue IDs being shown by the UI. Confirm through the API:

- list the real sessions
- list messages for the specific session
- compare with workspace queue status

If the workspace queue references IDs that are not in the session list and have no messages/peers, that is likely backend queue bookkeeping rather than a source import filtering issue.

### Import creates too much queue work

Use one or more of:

```bash
--mode safe
--max-sessions 20
--batch-delay 0.5
--redact-secrets
```

Then let the deriver drain before continuing with more sessions.

## Publishing/security checklist

Before publishing a fork or edited version:

- run `python -m py_compile honcho_backfill.py`
- run `python honcho_backfill.py --help`
- run a dry-run against test data
- scan for real names, API keys, session IDs, private hostnames, and local paths
- do not commit `state.db`, `honcho.json`, `.env`, logs, or `__pycache__`
- prefer `HONCHO_API_KEY` over `--honcho-api-key` for real credentials

## License

See the repo-root [`LICENSE`](../LICENSE).

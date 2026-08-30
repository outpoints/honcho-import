# Honcho Import for Claude Cowork

Backfill historical [Claude Cowork](https://claude.com) conversations into a [Honcho](https://honcho.dev) workspace.

This folder contains a standalone backfill utility (`honcho_backfill.py`). It reads Cowork's local session store — the Claude **desktop app's** "local agent mode" sessions under `~/Library/Application Support/Claude/local-agent-mode-sessions/` — and writes the conversations to Honcho.

> Part of the [honcho-import](../README.md) repo. See also the [Claude Code importer](../claude-code-backfill/) and the [Hermes Agent importer](../hermes-backfill/).

## Design principle: mirror the Claude Code plugin's conventions

There is **no official Cowork → Honcho plugin** — Cowork doesn't ship one, and the only Honcho client integration is the Claude Code plugin. So "native 1:1" here means: reproduce the *conventions the Claude Code Honcho plugin established*, applied to Cowork's data, so the result looks like what Honcho would contain **if Cowork had written to it from day one with default settings**.

This importer is the sibling of [`../claude-code-backfill/honcho_backfill.py`](../claude-code-backfill/honcho_backfill.py) and deliberately shares its architecture: config resolution, session naming, message extraction, `[Tool]` summary reconstruction, chunking, redaction, merge policy, the direct-`Session` write path, and dry-run-by-default. The sections below describe only where Cowork's storage differs and how the importer adapts.

## How Cowork stores sessions (and how this maps)

Cowork keeps each session as a metadata file plus a transcript log:

```
~/Library/Application Support/Claude/local-agent-mode-sessions/<id>/<id>/
├── local_<uuid>.json          # metadata: title, model, folders, timestamps,
│                              #           sessionType, parentSessionId, account
├── local_<uuid>/audit.jsonl   # the transcript (Anthropic message records + _audit_timestamp)
└── agent/
    ├── local_ditto_<id>.json
    └── local_ditto_<id>/audit.jsonl   # the "ditto" orchestrator session
```

Three session kinds map onto the Claude Code model:

| Cowork session | `sessionType` | Claude Code analogue | Default |
|---|---|---|---|
| **Root chat** | `null` (parent `null`) | A regular session — your prompts; assistant prose in `text` blocks | ✅ imported |
| **ditto orchestrator** | `agent` (parent `null`) | A main session with `Task` calls — your conversation with the multi-agent orchestrator | ✅ imported |
| **dispatch_child** | `dispatch_child` | Subagents / sidechains — its "user" turn is a third-person orchestrator prompt, not you | ❌ excluded (opt-in) |

Key adaptations from the Claude Code importer:

- **Transcript source.** Conversation content is read from each session's `audit.jsonl` (not the metadata file). Record types `user`/`assistant` carry the conversation; `system`/`result`/`rate_limit_event`/hook records are operational and skipped.
- **`SendUserMessage` is assistant prose.** The ditto orchestrator delivers its user-facing replies via `SendUserMessage` tool calls rather than `text` blocks, so those are extracted as assistant turns. Regular sessions have no `SendUserMessage`, so this is a no-op there.
- **Timestamps** come from each record's `_audit_timestamp` (ISO 8601), preserved as Honcho `created_at` for every message (user, assistant, tool summaries).
- **Tool-name normalization.** Cowork namespaces some tools; they're normalized so the same native `[Tool] …` summaries reconstruct: `mcp__workspace__bash` → `Bash`, `Agent` → `Task`, `mcp__dispatch__start_task` → `Dispatch` (e.g. `[Tool] Dispatched task (my-app): Audit the my-app repo`).
- **`<uploaded_files>` wrappers** are stripped — your typed text is kept and the attachment is noted as `[Attached: <filename>]`.
- **Initial-message replay** (Cowork records the first user message twice) is de-duplicated.

## Grouping: per working folder

Like the Claude Code importer's per-directory model, Cowork chats are grouped into **one Honcho session per host folder** the chat operated on (`userSelectedFolders[0]`):

- A chat on `.../Documents/Research` → `alex-research`; subfolders (`.../Research/Drafts`) → `alex-drafts`.
- The **ditto** has no host folder of its own (its cwd is an in-VM path), so it **inherits the folder its dispatch children worked in** — e.g. `.../my-app`.
- `config.json` `sessions` overrides still apply, so a Cowork chat on `.../my-app` merges into the **same `alex-my-app` session your Claude Code history uses** (when importing into the same workspace).
- Chats with **no host folder** fall back to one Honcho session per conversation, named from the chat title (e.g. `alex-draft-launch-plan`).

## Identity / launch parameters

Peers and target default from `~/.honcho/config.json` and are fully overridable. By default it reads the **`claude_code` host block** so Cowork imports into the same workspace as the Claude Code importer (per-folder names then merge with live data).

| Concept | Flag | Default |
|---|---|---|
| Your name (human peer) | `--peer-name` | config `peerName` (e.g. `Alex`) |
| The client/AI name (assistant peer) | `--ai-peer` | config `aiPeer` (e.g. `claude`) |
| Workspace | `--workspace` | config `workspace` (e.g. `claude_code`) |
| Honcho URL | `--honcho-url` | config `endpoint.baseUrl` |
| API key | `--honcho-api-key` / `HONCHO_API_KEY` | config `apiKey` |

Nothing personal is hardcoded.

## What it imports (and what it skips)

Defaults reproduce the established native conventions:

- ✅ Your typed prompts (user turns), with `<uploaded_files>` wrappers replaced by `[Attached: …]`
- ✅ Assistant turns — `text` blocks **and** ditto `SendUserMessage` replies; meaningful prose and terse turns (drop terse with `--no-brief`)
- ✅ Tool-call summaries `[Tool] …` for `Write`/`Edit`/`Bash`/`Task`/`Dispatch` (disable with `--no-tool-summaries`)
- ❌ Assistant thinking blocks — beyond native; opt in with `--include-thinking`
- ❌ `dispatch_child` subagent transcripts — those are the orchestrator talking to its workers, not you. The dispatch **invocation** is still captured as a `[Tool] Dispatched task (…)` summary in the ditto session. Opt in to the full worker transcripts with `--include-subagents` (attribution is approximate).
- ❌ Slash-command turns, `[Request interrupted]` markers, `isMeta` wrappers, `<synthetic>` messages, and operational records (hooks, results, rate-limit events)

The importer **reports** the dispatch_child sessions it excludes (and the folders they worked in) so nothing disappears silently.

## Requirements

- Python 3.10+
- `honcho-ai` 2.4+ — the SDK line that speaks Honcho 3.x's `/v3` API
- A reachable Honcho server and a configured `~/.honcho/config.json` (or equivalent CLI flags)

```bash
python -m pip install -r ../requirements.txt
```

The server and SDK version numbers do not line up: **Honcho server 3.x is
driven by honcho-ai 2.x.** The importer prints both on startup and warns if the
SDK is speaking a different API version than the server serves.

## Quick start

Always preview first (dry-run writes nothing):

```bash
python honcho_backfill.py
```

This prints the resolved config, the discovered sessions (root / ditto / dispatch_child), the per-folder Honcho sessions it would create, message counts, and what it excludes. Scope it to one project while you sanity-check:

```bash
python honcho_backfill.py --project my-app --verbose
```

When the preview looks right, execute:

```bash
python honcho_backfill.py --execute
```

For an authenticated server, prefer the environment variable over a flag:

```bash
export HONCHO_API_KEY='hch-v2-...'
python honcho_backfill.py --execute
```

## Merging with live history (important)

Because sessions are keyed **per folder** and (by default) land in the same `claude_code` workspace, an import can target a session your live Claude Code plugin already uses (e.g. `alex-my-app`). Four policies control what happens:

| Policy | Flag | Behavior |
|---|---|---|
| Dedupe (default) | `--merge dedupe` | Read what the session already holds and write only the messages missing from it. Safe to re-run; picks up new Cowork chats in a folder that was imported before. |
| Skip | `--merge skip` | Leave any session that already has messages completely alone (the old default). |
| Fill the gap | `--merge gap` | Import only messages **older** than the earliest existing message — backfills the history from before the plugin started. |
| Force append | `--merge force` | Append regardless. Duplicates. Use deliberately. |

`--fill-gap` and `--force-reimport` still work as aliases for `--merge gap` and `--merge force`.

Dedupe is what makes a Cowork folder that collides with Claude Code history
importable at all: under the old skip-if-exists default, a Cowork chat on a
folder your Claude Code plugin already writes to was dropped entirely.

### How dedupe decides

A message counts as already imported when the **same peer**, the **same text**
(whitespace-normalized), and a creation time **within `--dedupe-window`
seconds** (default 3600) all match an existing message. Matches are consumed
one-for-one, so a line that genuinely repeats in a conversation is only
suppressed as many times as it already exists.

The clock check matters: a one-line summary like `[Tool] Ran: npm test
(success)` recurs verbatim over months, and matching on text alone would let a
message from May suppress an identical one from August. Widen the window with
`--dedupe-window`, demand exact timestamps with `0`, or match on text alone
with `-1`.

To keep Cowork and Claude Code memory **separate** instead, import into a dedicated workspace: `--workspace cowork`.

## CLI reference

```text
Sources
  --cowork-base PATH         Claude desktop app data dir (default: ~/Library/Application Support/Claude)
  --honcho-config PATH       Path to config.json (default: ~/.honcho/config.json)
  --host NAME                Host key in the config hosts block (default: claude_code)
  --project SUBSTR           Only import chats whose folder or title contains SUBSTR

Identity / target
  --peer-name NAME           Human peer (default: config peerName)
  --ai-peer NAME             Assistant/client peer (default: config aiPeer)
  --workspace NAME           Honcho workspace (default: config workspace)
  --honcho-url URL           Honcho base URL (default: config endpoint.baseUrl)
  --honcho-api-key KEY       API key (prefer HONCHO_API_KEY env var)

Session naming
  --no-session-prefix        Do not prefix session names with the peer name

Content scope (defaults are native; --no-* trims, --include-* goes beyond native)
  --no-tool-summaries        Do NOT reconstruct native '[Tool] ...' summaries
  --no-brief                 Drop terse/non-meaningful assistant turns
  --include-thinking         Include assistant thinking blocks (beyond native)
  --include-tools            Append '[Used tools: ...]' to every assistant turn
  --include-commands         Include slash-command turns
  --include-subagents        Include dispatch_child / sidechain transcripts
  --only-subagents           Import ONLY dispatch_child transcripts (add them after a base
                             import; implies --include-subagents; the default
                             --merge dedupe appends them without duplicating)
  --redact-secrets           Redact obvious API tokens and labeled secrets

Merge behavior
  --merge MODE               dedupe (default) | skip | gap | force
  --dedupe-window SECONDS    Clock slack when matching an existing message
                             (default: 3600; 0 = exact, -1 = text only)
  --offline                  Dry-run without reading Honcho (no delta)
  --force-reimport           Alias for --merge force
  --fill-gap                 Alias for --merge gap

Run control
  --execute                  Actually write to Honcho (otherwise dry-run)
  --max-sessions N           Process at most N sessions (0 = all)
  --batch-size N             Messages per add_messages call (default: 10)
  --batch-delay SECONDS      Delay between sessions (default: 0.05)
  --verbose                  Print per-session detail
```

See full generated help:

```bash
python honcho_backfill.py --help
```

## How it maps to the Claude Code plugin

| Claude Code plugin source | Importer |
|---|---|
| `config.ts` `loadConfig` / `resolveConfig` | `resolve_config()` |
| `config.ts` `getSessionName` / `sanitizeForSessionName` | `derive_session_name()` / `sanitize_for_session_name()` |
| `hooks/session-end.ts` `parseTranscript` | `parse_cowork_audit()` (adapted to `audit.jsonl` + `SendUserMessage`) |
| `hooks/session-end.ts` `isMeaningfulAssistantContent` | `is_meaningful_assistant_content()` |
| `hooks/post-tool-use.ts` `shouldLogTool` / `formatToolSummary` | `should_log_tool()` / `format_tool_summary()` (+ Cowork tool aliases) |
| `cache.ts` `chunkContent` | `chunk_content()` |
| `hooks/session-start.ts` `addPeers` | peer setup in `main()` |

If the Claude Code plugin's conventions change, re-check these mirrored pieces.

## Privacy and safety

- Dry-run by default; writes only with `--execute`.
- No hardcoded names, workspaces, URLs, or credentials — all from config or flags.
- Prefer `HONCHO_API_KEY` over `--honcho-api-key` for real secrets.
- `--redact-secrets` redacts prefixed tokens (GitHub/OpenAI/Anthropic/NVIDIA) and **labeled** secrets near cues like "session key", "api key", "password", "token". It is **not** a full DLP — unlabeled, signature-less secrets can still slip through. Cowork transcripts can contain personal data, documents, and credentials; review sensitive history before importing.
- Do not commit `config.json`, `.env`, transcripts, or logs.

## Validation after import

1. Expected per-folder sessions were created (`alex-research`, `alex-my-app`, …).
2. User messages belong to your peer, assistant messages (incl. ditto `SendUserMessage` replies) to the AI peer.
3. Timestamps reflect the original conversation chronology.
4. Open one session in the Honcho GUI and confirm content looks right.

```python
from honcho import Honcho

h = Honcho(base_url="http://your-honcho-host:8000", workspace_id="claude_code")
s = h.session("alex-research")  # a per-folder Cowork session name
print(len(list(s.messages())))
```

## License

See the repo-root [`LICENSE`](../LICENSE).

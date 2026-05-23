# Honcho Import for Claude Code

Backfill historical [Claude Code](https://claude.com/claude-code) conversations into a [Honcho](https://honcho.dev) workspace.

This folder contains a standalone backfill utility (`honcho_backfill.py`). It reads Claude Code's local session transcripts (`~/.claude/projects/<encoded-cwd>/*.jsonl`) and writes them to Honcho **the same way the live Claude Code Honcho plugin does**, so backfilled history is consistent with — and merges into — the sessions the live plugin writes going forward.

> Part of the [honcho-import](../README.md) repo. For the Hermes Agent importer, see [`../hermes-backfill/`](../hermes-backfill/).

## Design principle: mirror the live plugin

Rather than inventing its own conventions, the importer reads the **same `~/.honcho/config.json`** the live Honcho Claude Code plugin uses and reproduces its write path:

- **Config** is resolved from `~/.honcho/config.json` (host block `claude_code`), then environment variables, then CLI flags (CLI wins). So your peer name, AI peer, workspace, endpoint, and session-naming all match the live plugin by default.
- **One Honcho session per project directory** (the plugin's default `per-directory` strategy), named exactly like the plugin's `getSessionName()`: `"<peer>-<dir>"` (sanitized), or a manual `sessions[cwd]` override from your config. All transcripts in a directory merge into one session, so historical + live conversations live together.
- **Message extraction** mirrors the plugin's `parseTranscript()`: your typed prompts as user turns, assistant text as assistant turns, with a `[Used tools: …]` hint on terse turns. Assistant content is sliced to 3000/1500 chars by "meaningfulness", exactly like the plugin.
- **Tool calls** are reconstructed natively: for every significant tool call the plugin's `PostToolUse` hook would have logged, the importer emits the same `[Tool] …` summary (a port of `formatToolSummary`/`shouldLogTool`), correlating each `tool_use` block with its `tool_result`. On by default.
- **Chunking** at 24,000 chars with `[Part i/n]` prefixes (the plugin's `chunkContent()`).
- **Timestamps** from each transcript record are preserved as Honcho `created_at` for every message type (user, assistant, and tool summaries).
- **Peers** are added per the configured `observationMode` (unified → `[user, ai]`; directional → AI peer gets `observe_others`).

### Native fidelity

The goal is for the result to look like what Honcho would contain **if the plugin had been installed from day one with default settings**. Defaults therefore reproduce native behavior (tool summaries and brief turns included). Deliberate, documented divergences:

- Each turn is written **once** — the live plugin's `Stop` hook and `SessionEnd` flush re-save the same assistant text; a backfill must not duplicate.
- **No per-flush 40-message cap** — a backfill keeps the full history (the cap is a live performance guard).
- **No** operational `[Session ended]` markers or `[Git External]` observations (the latter can't be reconstructed from transcripts).

## Identity / launch parameters

"Your client name" and "my name" are just the two Honcho peers, and they default from your config (no Hermes-style predefined config required — Claude Code's plugin config supplies them):

| Concept | Flag | Default |
|---|---|---|
| Your name (human peer) | `--peer-name` | config `peerName` (e.g. `Alex`) |
| The client/AI name (assistant peer) | `--ai-peer` | config `aiPeer` (e.g. `claude`) |
| Workspace | `--workspace` | config `workspace` (e.g. `claude_code`) |
| Honcho URL | `--honcho-url` | config `endpoint.baseUrl` |
| API key | `--honcho-api-key` / `HONCHO_API_KEY` | config `apiKey` |

Everything personal comes from config or flags — nothing is hardcoded.

## What it imports (and what it skips)

Defaults reproduce **native** plugin behavior:

- ✅ Your typed prompts (user turns)
- ✅ Assistant turns — both meaningful prose and terse turns (drop terse with `--no-brief`)
- ✅ Tool-call summaries `[Tool] …` (disable with `--no-tool-summaries`)
- ❌ Assistant thinking blocks — beyond native; opt in with `--include-thinking`
- ❌ Slash-command turns, `[Request interrupted]` markers, `isMeta`/caveat wrappers, and `<synthetic>` messages — filtered because the live plugin never sends them either (it uploads the prompt you actually typed)
- ❌ Subagent/sidechain transcripts (`<session-id>/subagents/*.jsonl`) — those are Claude talking to its own subagents, not you. The Task **invocation** is still captured as a `[Tool] Agent task (…)` summary. Opt in to the full sidechains with `--include-subagents` (attribution is approximate).
- ❌ Internal tooling sessions (e.g. claude-mem's `~/.claude-mem/observer-sessions`) — these are background-agent sessions, not your conversations. Opt in with `--include-internal`.

The importer **reports** what it skips (orphaned subagent-only projects, excluded internal sessions) so nothing disappears silently.

## Requirements

- Python 3.10+
- The `honcho-ai` Python package
- A reachable Honcho server and a configured `~/.honcho/config.json` (or equivalent CLI flags)

```bash
python -m pip install honcho-ai
```

## Quick start

Always preview first (dry-run writes nothing):

```bash
python honcho_backfill.py
```

This prints the resolved config, the per-directory sessions it would create, message counts, and everything it is skipping. Scope it to one project while you sanity-check:

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

Because sessions are keyed **per directory**, an import lands in the *same* Honcho session your live plugin uses. If you've already used the plugin live in a directory, that session already has messages. Three options control what happens:

| Policy | Flag | Behavior |
|---|---|---|
| Skip (default) | — | Skip any session that already has messages. Safe. |
| Fill the gap | `--fill-gap` | Import only transcript messages **older** than the earliest existing message — backfills the history before the plugin started, no duplicates. |
| Force append | `--force-reimport` | Append regardless (may duplicate). Use deliberately. |

Recommended: import a directory's history **before** you start using the plugin there, or use `--fill-gap`.

## CLI reference

```text
Sources
  --projects-dir PATH        Claude Code projects dir (default: ~/.claude/projects)
  --honcho-config PATH       Path to config.json (default: ~/.honcho/config.json)
  --host NAME                Host key in the config hosts block (default: claude_code)
  --project SUBSTR           Only import sessions whose cwd contains SUBSTR

Identity / target
  --peer-name NAME           Human peer (default: config peerName)
  --ai-peer NAME             Assistant/client peer (default: config aiPeer)
  --workspace NAME           Honcho workspace (default: config workspace)
  --honcho-url URL           Honcho base URL (default: config endpoint.baseUrl)
  --honcho-api-key KEY       API key (prefer HONCHO_API_KEY env var)

Session naming
  --session-strategy S       per-directory | git-branch | per-session | chat-instance
  --no-session-prefix        Do not prefix session names with the peer name

Content scope (defaults are native; --no-* trims, --include-* goes beyond native)
  --no-tool-summaries        Do NOT reconstruct native '[Tool] ...' summaries
  --no-brief                 Drop terse/non-meaningful assistant turns
  --include-thinking         Include assistant thinking blocks (beyond native)
  --include-tools            Append '[Used tools: ...]' to every assistant turn
  --include-commands         Include slash-command turns
  --include-subagents        Include subagent/sidechain transcripts
  --include-internal         Include internal tooling sessions (claude-mem, etc.)
  --redact-secrets           Redact obvious API tokens before import

Merge behavior
  --force-reimport           Append even if the session already has messages
  --fill-gap                 Import only messages older than existing data

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

## How it maps to the live plugin

| Plugin source (`~/.claude/plugins/cache/honcho/honcho/<ver>/`) | Importer |
|---|---|
| `config.ts` `loadConfig` / `resolveConfig` | `resolve_config()` |
| `config.ts` `getSessionName` / `sanitizeForSessionName` | `derive_session_name()` / `sanitize_for_session_name()` |
| `hooks/session-end.ts` `parseTranscript` | `parse_transcript_file()` |
| `hooks/session-end.ts` `isMeaningfulAssistantContent` | `is_meaningful_assistant_content()` |
| `hooks/post-tool-use.ts` `shouldLogTool` / `formatToolSummary` / `inferContentPurpose` / `summarizeEdit` | `should_log_tool()` / `format_tool_summary()` / `_infer_content_purpose()` / `_summarize_edit()` |
| `cache.ts` `chunkContent` | `chunk_content()` |
| `hooks/session-end.ts` write path / `hooks/session-start.ts` `addPeers` | `build_honcho_messages()` / peer setup in `main()` |

If you upgrade the plugin, re-check these mirrored pieces.

## Privacy and safety

- Dry-run by default; writes only with `--execute`.
- No hardcoded names, workspaces, URLs, or credentials — all from config or flags.
- Prefer `HONCHO_API_KEY` over `--honcho-api-key` for real secrets.
- `--redact-secrets` redacts (a) prefixed tokens — GitHub/OpenAI/Anthropic/NVIDIA — and (b) **labeled** secrets: a token-like value following a cue word such as "session key", "api key", "secret", "password", or "token" (handles markdown-wrapped values, with a secret-likeness check to avoid masking ordinary prose). It is **not** a full DLP — unlabeled, signature-less secrets can still slip through. Transcripts can contain real credentials; review sensitive history before importing.
- Do not commit `config.json`, `.env`, transcripts, or logs.

## Validation after import

1. Expected per-directory sessions were created (names match the live plugin's).
2. User messages belong to your peer, assistant messages to the AI peer.
3. Timestamps reflect the original conversation chronology.
4. Open one session in the Honcho GUI and confirm content looks right.

```python
from honcho import Honcho

h = Honcho(base_url="http://your-honcho-host:8000", workspace_id="claude_code")
s = h.session("<your-name>-<project-dir>")  # e.g. the per-directory session name
print(len(list(s.messages())))
```

## License

See the repo-root [`LICENSE`](../LICENSE).

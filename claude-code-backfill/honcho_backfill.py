#!/usr/bin/env python3
"""
Import Claude Code session history into a Honcho workspace.

This is a one-time/occasional backfill utility. It reads Claude Code's local
transcript files (~/.claude/projects/<encoded-cwd>/*.jsonl) and writes them to
Honcho the same way the live Claude Code Honcho plugin does, so backfilled
history is consistent with - and merges into - the sessions the live plugin
writes.

Design principle: mirror the live plugin (the "honcho" Claude Code plugin) as
closely as possible.

  - config resolved from ~/.honcho/config.json (host block "claude_code"),
    then environment variables, then CLI flags (CLI wins). This is the same
    config the live plugin reads, so peer names, workspace, endpoint, and
    session naming all match by default.
  - one Honcho session per *project directory* by default (per-directory
    strategy), named exactly like the live plugin's getSessionName():
    "<peer>-<dir>" (sanitized). All transcripts in a directory merge into the
    same session, so historical + live conversations live together.
  - user turns attributed to the user peer (your name, e.g. "Alex"),
    assistant turns to the AI peer (the client name, e.g. "claude").
  - message extraction mirrors the plugin's parseTranscript(): user text or
    text-blocks; assistant text-blocks with a "[Used tools: ...]" hint when
    terse; assistant content sliced to 3000/1500 chars by meaningfulness.
  - tool calls are reconstructed natively: for every significant tool call the
    plugin's PostToolUse hook would have logged, we emit the same
    "[Tool] ..." summary (port of formatToolSummary/shouldLogTool), correlating
    tool_use blocks with their tool_result blocks. On by default.
  - long messages chunked at 24000 chars with "[Part i/n]" prefixes
    (matching the plugin's chunkContent()).
  - original transcript timestamps preserved as Honcho created_at for every
    message type (user, assistant, and tool summaries).
  - dry-run unless --execute is passed. A dry run still *reads* the target
    workspace (unless --offline) so the preview reflects what is genuinely
    missing, not just what the transcripts contain.

Targets Honcho server 3.x (the /v3 API) via the honcho-ai Python SDK 2.4+.
Those version numbers do not line up on purpose: it is the honcho-ai 2.x line
that speaks /v3, not a 3.x SDK. See requirements.txt.

The goal is fidelity: the result should look like what Honcho would contain if
the plugin had been installed from day one with default settings. Defaults
therefore match native behavior (tool summaries + brief turns included); flags
let you trim noise (--no-tool-summaries, --no-brief) or go beyond native
(--include-thinking).

Deliberate divergences from the live write path (documented, not bugs):
  - each turn is written once (the live plugin's Stop hook + SessionEnd flush
    re-save the same assistant text; a backfill must not duplicate).
  - re-running is safe: the default --merge dedupe mode indexes what the
    session already holds and writes only the messages missing from it.
  - no per-flush 40-message cap (a backfill keeps the full history).
  - no operational "[Session ended]" markers or "[Git External]" observations
    (the latter cannot be reconstructed from transcripts).

Nothing personal is hardcoded. "Your client name", "my name", workspace, host
URL, etc. all come from ~/.honcho/config.json or CLI flags. Use --help for
options.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# Constants mirrored from the live plugin (keep in sync with the plugin source)
# ---------------------------------------------------------------------------

# ~/.honcho/config.json + the Claude Code transcript store.
HONCHO_CONFIG_DEFAULT = Path.home() / ".honcho" / "config.json"
CLAUDE_PROJECTS_DEFAULT = Path.home() / ".claude" / "projects"

# Defaults the plugin falls back to for the claude_code host.
DEFAULT_HOST = "claude_code"
DEFAULT_WORKSPACE = "claude_code"
DEFAULT_AI_PEER = "claude"
DEFAULT_PEER_NAME = os.environ.get("USER") or os.environ.get("USERNAME") or "user"
DEFAULT_SESSION_STRATEGY = "per-directory"

# session-end.ts: assistant content is sliced by "meaningfulness".
ASSISTANT_MAX_MEANINGFUL = 3000
ASSISTANT_MAX_BRIEF = 1500
# cache.ts: chunkContent() splits at this size and prefixes "[Part i/n] ".
MAX_MESSAGE_SIZE = 24_000

# Conservative secret patterns, only applied with --redact-secrets.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]+")),
    ("nvidia_token", re.compile(r"nvapi-[A-Za-z0-9_-]{10,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
]

# Keyword redaction: catches *labeled* secrets that have no signature prefix
# (e.g. a service "session key"). Matches a cue word, an optional gap (incl.
# markdown wrappers / newlines), then a token-like value. The value is only
# redacted if it actually looks secret-like (see _looks_like_secret), to avoid
# masking ordinary prose like "the access token authentication mechanism".
_SECRET_LABEL = re.compile(
    r"(?i)\b(session\s*keys?|api[\s_-]*keys?|secret\w*|passwords?|passwd"
    r"|access[\s_-]*tokens?|auth[\s_-]*tokens?|client[\s_-]*secrets?|bearer|tokens?)"
    r"([\s:=\-—`*'\"\[\]()]{0,12})"
    r"([A-Za-z0-9_\-./+=]{16,})"
)


def _looks_like_secret(value: str) -> bool:
    if len(value) < 16:
        return False
    has_digit = any(c.isdigit() for c in value)
    has_underscore = "_" in value
    has_mixed_case = any(c.islower() for c in value) and any(c.isupper() for c in value)
    return has_digit or has_underscore or has_mixed_case

# User transcript turns that are NOT real prompts. The live plugin uploads user
# turns from its in-memory hook queue (the literal text you typed), never from
# the transcript, so these synthetic/injected turns never reach Honcho live.
# A backfill reads the transcript, so we filter them to match live behavior.
_USER_NOISE_MARKERS = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "caveat: the messages below were generated",
    "[request interrupted",  # synthetic interruption marker, not a real prompt
)

# Working directories that are internal tooling, not real projects you worked in.
# claude-mem runs its own observer-agent Claude sessions under ~/.claude-mem.
INTERNAL_CWD_MARKERS = ("/.claude-mem/",)


def _is_internal_cwd(cwd: str | None) -> bool:
    c = cwd or ""
    return any(marker in c for marker in INTERNAL_CWD_MARKERS)


# ---------------------------------------------------------------------------
# Resolved config
# ---------------------------------------------------------------------------


@dataclass
class CCImportConfig:
    """Resolved import config, mirroring the plugin's resolved config shape."""

    api_key: str = ""
    base_url: str | None = None
    workspace: str = DEFAULT_WORKSPACE
    peer_name: str = DEFAULT_PEER_NAME
    ai_peer: str = DEFAULT_AI_PEER
    session_strategy: str = DEFAULT_SESSION_STRATEGY
    session_peer_prefix: bool = True
    sessions: dict[str, str] = field(default_factory=dict)
    observation_mode: str = "unified"
    timeout: int = 30


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def resolve_config(raw_cfg: dict[str, Any], host: str) -> CCImportConfig:
    """Resolve config from a ~/.honcho/config.json shape, mirroring config.ts.

    Host-specific fields (workspace, aiPeer, sessionStrategy, ...) come from the
    hosts.<host> block first, then root-level fallbacks, then defaults.
    Environment variables override where the plugin honors them.
    """
    hosts = raw_cfg.get("hosts") if isinstance(raw_cfg.get("hosts"), dict) else {}
    # The plugin tries host, host with "_"->"-" and "-"->"_".
    host_block = (
        hosts.get(host)
        or hosts.get(host.replace("_", "-"))
        or hosts.get(host.replace("-", "_"))
        or {}
    )
    if not isinstance(host_block, dict):
        host_block = {}

    global_override = raw_cfg.get("globalOverride") is True

    if global_override:
        workspace = _coalesce(raw_cfg.get("workspace"), DEFAULT_WORKSPACE)
        ai_peer = _coalesce(raw_cfg.get("aiPeer"), host_block.get("aiPeer"), DEFAULT_AI_PEER)
    else:
        workspace = _coalesce(host_block.get("workspace"), raw_cfg.get("workspace"), DEFAULT_WORKSPACE)
        ai_peer = _coalesce(host_block.get("aiPeer"), raw_cfg.get("aiPeer"), DEFAULT_AI_PEER)

    peer_name = _coalesce(
        raw_cfg.get("peerName"),
        os.environ.get("HONCHO_PEER_NAME"),
        os.environ.get("USER"),
        os.environ.get("USERNAME"),
        "user",
    )

    endpoint = host_block.get("endpoint") or raw_cfg.get("endpoint") or {}
    base_url = _resolve_base_url(endpoint)

    return CCImportConfig(
        api_key=str(_coalesce(os.environ.get("HONCHO_API_KEY"), raw_cfg.get("apiKey"), "")),
        base_url=base_url,
        workspace=str(workspace),
        peer_name=str(peer_name),
        ai_peer=str(ai_peer),
        session_strategy=str(_coalesce(host_block.get("sessionStrategy"), raw_cfg.get("sessionStrategy"), DEFAULT_SESSION_STRATEGY)),
        session_peer_prefix=_coalesce(host_block.get("sessionPeerPrefix"), raw_cfg.get("sessionPeerPrefix"), True) is not False,
        sessions=raw_cfg.get("sessions") if isinstance(raw_cfg.get("sessions"), dict) else {},
        observation_mode=str(_coalesce(host_block.get("observationMode"), raw_cfg.get("observationMode"), "unified")),
        timeout=int(_coalesce(host_block.get("timeout"), raw_cfg.get("timeout"), 30)),
    )


def _resolve_base_url(endpoint: dict[str, Any]) -> str | None:
    """Mirror config.ts endpoint resolution: baseUrl > environment > production.

    The base URL is passed to the Honcho Python SDK as-is (the SDK manages its
    own version path), matching how this importer has always called the SDK.
    A custom self-hosted baseUrl such as "http://host:18100" is used verbatim.
    """
    if not isinstance(endpoint, dict):
        return None
    base = endpoint.get("baseUrl") or os.environ.get("HONCHO_URL") or os.environ.get("HONCHO_BASE_URL")
    if base:
        return str(base)
    env = endpoint.get("environment") or os.environ.get("HONCHO_ENDPOINT")
    if env == "local":
        return "http://localhost:8000"
    return None  # SDK default (production)


# ---------------------------------------------------------------------------
# Session naming (mirror config.ts getSessionName / sanitizeForSessionName)
# ---------------------------------------------------------------------------


def sanitize_for_session_name(s: str) -> str:
    return re.sub(r"[^a-z0-9-_]", "-", s.lower())


def derive_session_name(cfg: CCImportConfig, cwd: str, *, git_branch: str | None, instance_id: str | None) -> str:
    """Replicate the plugin's getSessionName() for the configured strategy."""
    strategy = cfg.session_strategy or DEFAULT_SESSION_STRATEGY

    # Manual overrides only apply to per-directory (matching the plugin).
    if strategy == "per-directory":
        override = cfg.sessions.get(cwd)
        if override:
            return override

    peer_part = sanitize_for_session_name(cfg.peer_name) if cfg.peer_name else "user"
    repo_part = sanitize_for_session_name(os.path.basename(cwd.rstrip("/")) or "root")
    base = f"{peer_part}-{repo_part}" if cfg.session_peer_prefix else repo_part

    if strategy == "git-branch":
        if git_branch:
            return f"{base}-{sanitize_for_session_name(git_branch)}"
        return base
    if strategy in ("chat-instance", "per-session"):
        if instance_id:
            suffix = sanitize_for_session_name(instance_id)
            return f"{peer_part}-chat-{suffix}" if cfg.session_peer_prefix else f"chat-{suffix}"
        return base
    # per-directory (default)
    return base


# ---------------------------------------------------------------------------
# Transcript discovery + parsing
# ---------------------------------------------------------------------------


@dataclass
class ParsedMessage:
    role: str  # "user" | "assistant"
    content: str
    timestamp: str | None  # ISO 8601 from the transcript
    meaningful: bool = False
    is_tool: bool = False  # a reconstructed "[Tool] ..." summary (AI peer)
    source_file: str = ""
    line_index: int = 0


@dataclass
class TranscriptGroup:
    """All transcripts that map to a single Honcho session (per-directory)."""

    session_name: str
    cwd: str
    git_branch: str | None
    files: list[Path] = field(default_factory=list)
    messages: list[ParsedMessage] = field(default_factory=list)


def iter_transcript_files(projects_dir: Path, *, include_subagents: bool) -> Iterable[Path]:
    """Yield Claude Code transcript .jsonl files.

    Top-level files in each project dir are main sessions. Files in nested
    subdirectories are subagent/sidechain transcripts, skipped unless asked for.
    """
    if not projects_dir.exists():
        return
    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        for path in sorted(project_dir.glob("*.jsonl")):
            yield path
        if include_subagents:
            for path in sorted(project_dir.rglob("*.jsonl")):
                if path.parent != project_dir:
                    yield path


def _record_text_user(content: Any) -> str:
    """Extract user text: string as-is, or join text blocks (drop tool_result)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _record_text_assistant(content: Any, *, include_thinking: bool, include_tools: bool) -> str:
    """Mirror parseTranscript() assistant extraction (with optional extras)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    text_blocks = "\n\n".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
    )
    out = text_blocks

    if include_thinking:
        thinking = "\n\n".join(
            b.get("thinking", "") for b in content if isinstance(b, dict) and b.get("type") == "thinking" and b.get("thinking")
        )
        if thinking:
            out = (f"[thinking]\n{thinking}\n\n" + out).strip()

    tool_uses = [
        b.get("name") for b in content if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name")
    ]
    if tool_uses:
        if include_tools:
            out = (out + ("\n" if out else "") + f"[Used tools: {', '.join(tool_uses)}]").strip()
        elif len(text_blocks) < 100:
            # Mirror the plugin: only annotate terse turns so a tool-only turn
            # isn't dropped entirely.
            out = (text_blocks + ("\n" if text_blocks else "") + f"[Used tools: {', '.join(tool_uses)}]").strip()

    return out


def is_meaningful_assistant_content(content: str) -> bool:
    """Port of session-end.ts isMeaningfulAssistantContent()."""
    if len(content) < 50:
        return False
    trimmed = content.strip()

    terse_announcements = [
        re.compile(r"^(I'll|Let me|I'm going to|I will|Now I'll|First,? I'll)\s+(run|use|execute|check|read|look at|search|edit|write|create)", re.I),
        re.compile(r"^Running\s+", re.I),
        re.compile(r"^Checking\s+", re.I),
        re.compile(r"^Looking at\s+", re.I),
    ]
    for pat in terse_announcements:
        if pat.search(trimmed) and len(content) < 200:
            return False
    if re.search(r"^(The command|The file|The output|This shows|Here's what)", trimmed, re.I) and len(content) < 150:
        return False

    meaningful = [
        re.compile(r"\b(because|since|therefore|however|although|this means|in summary|to summarize|the issue is|the problem is|I recommend|you should|we should|this approach|the solution|key point|important|note that)\b", re.I),
        re.compile(r"\b(implemented|fixed|resolved|completed|added|created|updated|changed|modified|refactored)\b", re.I),
        re.compile(r"\b(error|bug|issue|problem|solution|fix|improvement|optimization)\b", re.I),
    ]
    for pat in meaningful:
        if pat.search(content):
            return True
    return len(content) >= 200


# ---------------------------------------------------------------------------
# Tool-call summaries (port of hooks/post-tool-use.ts)
#
# The live plugin's PostToolUse hook writes a one-line "[Tool] ..." summary to
# Honcho (as the AI peer) for each significant tool call. A faithful backfill
# reconstructs the same summaries from each transcript's tool_use blocks and
# their matching tool_result blocks.
# ---------------------------------------------------------------------------

SIGNIFICANT_TOOLS = {"Write", "Edit", "Bash", "Task", "NotebookEdit"}
_TRIVIAL_BASH_PREFIXES = (
    "ls", "pwd", "echo", "cat", "head", "tail", "which", "type",
    "git status", "git log", "git diff",
)


def should_log_tool(tool_name: str, tool_input: dict[str, Any]) -> bool:
    """Port of post-tool-use.ts shouldLogTool()."""
    if tool_name not in SIGNIFICANT_TOOLS:
        return False
    if tool_name == "Bash":
        command = (tool_input.get("command") or "").strip()
        if any(command.startswith(c) for c in _TRIVIAL_BASH_PREFIXES):
            return False
    return True


def _infer_content_purpose(content: str, file_path: str) -> str:
    """Port of post-tool-use.ts inferContentPurpose()."""
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    if ext in ("ts", "tsx", "js", "jsx"):
        m = re.search(r"export\s+(default\s+)?(function|class|const|interface|type)\s+(\w+)", content)
        if m:
            return f"defines {m.group(2)} {m.group(3)}"
        m = re.search(r"(?:function|const)\s+(\w+).*(?:return|=>)\s*[(<]", content)
        if m:
            return f"component {m.group(1)}"
    if ext == "py":
        m = re.search(r"class\s+(\w+)", content)
        if m:
            return f"defines class {m.group(1)}"
        m = re.search(r"def\s+(\w+)", content)
        if m:
            return f"defines function {m.group(1)}"
    if ext in ("md", "mdx", "txt"):
        m = re.search(r"^#\s+(.+)$", content, re.M)
        if m:
            return f"doc: {m.group(1)[:50]}"
    if ext in ("json", "yaml", "yml", "toml"):
        return "config file"
    return f"{len(content.splitlines())} lines"


def _summarize_edit(old_str: str, new_str: str, file_path: str) -> str:
    """Port of post-tool-use.ts summarizeEdit()."""
    old_lines = len(old_str.split("\n"))
    new_lines = len(new_str.split("\n"))
    if old_str.strip() == "":
        return f"added {new_lines} lines ({_infer_content_purpose(new_str, file_path)})"
    if new_str.strip() == "":
        return f"removed {old_lines} lines"
    old_tokens = re.findall(r"\w+", old_str)
    new_tokens = re.findall(r"\w+", new_str)
    old_set, new_set = set(old_tokens), set(new_tokens)
    added = [t for t in new_tokens if t not in old_set and len(t) > 2]
    removed = [t for t in old_tokens if t not in new_set and len(t) > 2]
    if added and removed:
        return f"changed: {', '.join(removed[:2])} → {', '.join(added[:2])}"
    if added:
        return f"added: {', '.join(added[:3])}"
    if removed:
        return f"removed: {', '.join(removed[:3])}"
    diff = new_lines - old_lines
    if diff > 0:
        return f"expanded by {diff} lines"
    if diff < 0:
        return f"reduced by {-diff} lines"
    return f"modified {old_lines} lines"


def format_tool_summary(tool_name: str, tool_input: dict[str, Any], tool_response: dict[str, Any]) -> str:
    """Port of post-tool-use.ts formatToolSummary()."""
    ti = tool_input or {}
    tr = tool_response or {}
    if tool_name == "Write":
        file_path = ti.get("file_path") or "unknown"
        purpose = _infer_content_purpose(ti.get("content") or "", file_path)
        return f"Wrote {file_path.split('/')[-1] or file_path} ({purpose})"
    if tool_name == "Edit":
        file_path = ti.get("file_path") or "unknown"
        change = _summarize_edit(ti.get("old_string") or "", ti.get("new_string") or "", file_path)
        return f"Edited {file_path.split('/')[-1] or file_path}: {change}"
    if tool_name == "Bash":
        command = (ti.get("command") or "")[:100]
        success = not tr.get("error")
        cmd_parts = re.split(r"[;&|]", command)[0].strip()
        if any(pm in command for pm in ("npm", "pnpm", "yarn", "bun")):
            m = re.search(r"(install|build|test|run|dev|start)", command)
            return f"Package {m.group(0) if m else 'command'}: {'success' if success else 'failed'}"
        if "git commit" in command:
            m = re.search(r"-m\s*[\"']([^\"']+)[\"']", command)
            msg = m.group(1) if m else ""
            return f"Git commit: {msg[:50]}{'...' if len(msg) > 50 else ''}"
        if "git push" in command:
            return f"Git push: {'success' if success else 'failed'}"
        if any(c in command for c in ("curl", "wget", "fetch")):
            m = re.search(r"https?://[^\s\"']+", command)
            url = m.group(0) if m else ""
            parts = url.split("/")
            host = parts[2] if len(parts) > 2 else "API"
            return f"HTTP request to {host}: {'success' if success else 'failed'}"
        if "docker" in command or "flyctl" in command or "fly " in command:
            return f"Deploy: {cmd_parts[:60]} ({'success' if success else 'failed'})"
        return f"Ran: {cmd_parts[:60]} ({'success' if success else 'failed'})"
    if tool_name == "Task":
        return f"Agent task ({ti.get('subagent_type') or ''}): {ti.get('description') or 'unknown'}"
    if tool_name == "NotebookEdit":
        notebook_path = ti.get("notebook_path") or "unknown"
        return f"Notebook {ti.get('edit_mode') or 'replace'} {ti.get('cell_type') or 'code'} cell in {notebook_path.split('/')[-1] or notebook_path}"
    return f"Used {tool_name}"


def _is_user_noise(text: str, *, include_commands: bool) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return True
    for marker in _USER_NOISE_MARKERS:
        if marker in lowered:
            # Slash commands are command wrappers; keep only if explicitly asked.
            if marker == "<command-name>" and include_commands:
                continue
            return True
    return False


def parse_transcript_file(
    path: Path,
    *,
    include_thinking: bool,
    include_tools: bool,
    include_commands: bool,
    include_subagents: bool,
    include_brief: bool = True,
    tool_summaries: bool = True,
) -> tuple[list[ParsedMessage], str | None, str | None]:
    """Parse one transcript file into messages; return (messages, cwd, branch)."""
    messages: list[ParsedMessage] = []
    cwd: str | None = None
    branch: str | None = None
    # tool_use_id -> (tool_name, tool_input, timestamp); resolved when the
    # matching tool_result block arrives in a later user record.
    pending_tool_uses: dict[str, tuple[str, dict[str, Any], str | None]] = {}

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return messages, cwd, branch

    for idx, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue

        if cwd is None and entry.get("cwd"):
            cwd = str(entry["cwd"])
        if branch is None and entry.get("gitBranch"):
            branch = str(entry["gitBranch"])

        if entry.get("isSidechain") and not include_subagents:
            continue

        entry_type = entry.get("type") or entry.get("role")
        if entry_type not in ("user", "assistant"):
            continue

        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        content = message.get("content") if message else entry.get("content")
        timestamp = entry.get("timestamp")

        # Reconstruct PostToolUse "[Tool] ..." summaries: collect tool_use blocks
        # from assistant turns, emit when their tool_result arrives (user turn).
        if tool_summaries and isinstance(content, list):
            if entry_type == "assistant":
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name"):
                        pending_tool_uses[b.get("id")] = (b.get("name"), b.get("input") or {}, timestamp)
            else:  # user record carries tool_result blocks
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tu = pending_tool_uses.pop(b.get("tool_use_id"), None)
                        if not tu:
                            continue
                        name, tinput, use_ts = tu
                        if not should_log_tool(name, tinput):
                            continue
                        summary = format_tool_summary(name, tinput, {"error": bool(b.get("is_error"))})
                        messages.append(ParsedMessage("assistant", f"[Tool] {summary}", timestamp or use_ts, is_tool=True, source_file=path.name, line_index=idx))

        if entry_type == "user":
            if entry.get("isMeta"):
                continue
            user_text = _record_text_user(content)
            if not user_text or not user_text.strip():
                continue
            if _is_user_noise(user_text, include_commands=include_commands):
                continue
            messages.append(ParsedMessage("user", user_text, timestamp, source_file=path.name, line_index=idx))
        else:  # assistant
            if message.get("model") == "<synthetic>":
                continue
            asst_text = _record_text_assistant(content, include_thinking=include_thinking, include_tools=include_tools)
            if not asst_text or not asst_text.strip():
                continue
            meaningful = is_meaningful_assistant_content(asst_text)
            if not meaningful and not include_brief:
                continue
            max_len = ASSISTANT_MAX_MEANINGFUL if meaningful else ASSISTANT_MAX_BRIEF
            messages.append(
                ParsedMessage("assistant", asst_text[:max_len], timestamp, meaningful=meaningful, source_file=path.name, line_index=idx)
            )

    # Tool calls whose result never appeared (e.g. interrupted). Emit anyway so
    # the action is still recorded, with success unknown (treated as success,
    # matching the plugin's `!toolResponse.error` default).
    if tool_summaries:
        for name, tinput, use_ts in pending_tool_uses.values():
            if not should_log_tool(name, tinput):
                continue
            summary = format_tool_summary(name, tinput, {})
            messages.append(ParsedMessage("assistant", f"[Tool] {summary}", use_ts, is_tool=True, source_file=path.name, line_index=10**9))

    return messages, cwd, branch


def decode_project_dir(name: str) -> str:
    """Best-effort decode of an encoded project dir name into a cwd path.

    Claude Code encodes the cwd by replacing path separators with '-', which is
    lossy (real '-' in paths are indistinguishable). The cwd read from inside
    the transcript records is authoritative; this is only a fallback.
    """
    return "/" + name.lstrip("-").replace("-", "/")


def group_transcripts(
    cfg: CCImportConfig,
    files: Iterable[Path],
    *,
    include_thinking: bool,
    include_tools: bool,
    include_commands: bool,
    include_subagents: bool,
    include_brief: bool,
    tool_summaries: bool,
    project_filter: str | None,
) -> list[TranscriptGroup]:
    """Parse files and group them into Honcho sessions per the strategy."""
    groups: dict[str, TranscriptGroup] = {}

    for path in files:
        msgs, cwd, branch = parse_transcript_file(
            path,
            include_thinking=include_thinking,
            include_tools=include_tools,
            include_commands=include_commands,
            include_subagents=include_subagents,
            include_brief=include_brief,
            tool_summaries=tool_summaries,
        )
        if not msgs:
            continue
        if cwd is None:
            cwd = decode_project_dir(path.parent.name)

        if project_filter and project_filter.lower() not in cwd.lower():
            continue

        instance_id = path.stem  # the session UUID == chat instance
        session_name = derive_session_name(cfg, cwd, git_branch=branch, instance_id=instance_id)

        group = groups.get(session_name)
        if group is None:
            group = TranscriptGroup(session_name=session_name, cwd=cwd, git_branch=branch)
            groups[session_name] = group
        group.files.append(path)
        group.messages.extend(msgs)

    # Order messages chronologically. ISO-8601 UTC strings sort lexicographically;
    # fall back to file/line order when a timestamp is missing.
    for group in groups.values():
        group.messages.sort(key=lambda m: (m.timestamp or "", m.source_file, m.line_index))

    return sorted(groups.values(), key=lambda g: g.session_name)


# ---------------------------------------------------------------------------
# Message building (mirror chunkContent + the session-end write path)
# ---------------------------------------------------------------------------


def chunk_content(content: str, max_size: int = MAX_MESSAGE_SIZE) -> list[str]:
    """Port of cache.ts chunkContent()."""
    if len(content) <= max_size:
        return [content]

    chunks: list[str] = []
    remaining = content
    while remaining:
        if len(remaining) <= max_size:
            chunks.append(remaining)
            break
        split = remaining.rfind("\n", 0, max_size)
        if split <= 0 or split < max_size * 0.25:
            split = remaining.rfind(" ", 0, max_size)
        if split <= 0 or split < max_size * 0.25:
            split = max_size
        chunks.append(remaining[:split])
        remaining = remaining[split:].lstrip()

    if len(chunks) > 1:
        n = len(chunks)
        return [f"[Part {i + 1}/{n}] {c}" for i, c in enumerate(chunks)]
    return chunks


def redact_secrets(content: str) -> str:
    out = content
    for label, pattern in SECRET_PATTERNS:
        out = pattern.sub(f"[REDACTED_{label.upper()}]", out)

    def _kw_sub(m: re.Match[str]) -> str:
        if _looks_like_secret(m.group(3)):
            return f"{m.group(1)}{m.group(2)}[REDACTED_SECRET]"
        return m.group(0)

    return _SECRET_LABEL.sub(_kw_sub, out)


def created_at_from_iso(ts: str | None, *, offset_us: int = 0) -> datetime | None:
    if not ts:
        return None
    try:
        normalized = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    if offset_us:
        dt = dt + timedelta(microseconds=offset_us)
    return dt


def build_honcho_messages(
    group: TranscriptGroup,
    *,
    user_peer,
    ai_peer,
    redact: bool,
    session_name: str,
):
    """Build SDK message objects mirroring the live plugin's metadata shape.

    Returns (batch, kinds) where kinds[i] is the ("user"|"assistant"|"tool",
    source message index) of batch[i]. Carrying the kind per chunk lets the
    caller re-tally counts after dedupe has removed some of them.
    """
    batch = []
    kinds: list[tuple[str, int]] = []
    for msg_index, msg in enumerate(group.messages):
        peer = user_peer if msg.role == "user" else ai_peer
        kind = "tool" if msg.is_tool else msg.role
        content = redact_secrets(msg.content) if redact else msg.content
        for chunk_index, chunk in enumerate(chunk_content(content)):
            metadata: dict[str, Any] = {
                "imported": True,
                "import_source": "claude_code_transcript",
                "session_affinity": session_name,
            }
            if msg.is_tool:
                metadata["type"] = "tool"
            elif msg.role == "assistant":
                metadata["type"] = "assistant_prose" if msg.meaningful else "assistant_brief"
                metadata["meaningful"] = msg.meaningful
            kwargs: dict[str, Any] = {"metadata": metadata}
            created_at = created_at_from_iso(msg.timestamp, offset_us=chunk_index)
            if created_at is not None:
                kwargs["created_at"] = created_at
            batch.append(peer.message(chunk, **kwargs))
            kinds.append((kind, msg_index))
    return batch, kinds


# ---------------------------------------------------------------------------
# Honcho client
# ---------------------------------------------------------------------------


def init_honcho(cfg: CCImportConfig, workspace: str):
    from honcho import Honcho

    kwargs: dict[str, Any] = {
        "api_key": cfg.api_key,
        "workspace_id": workspace,
        "timeout": cfg.timeout,
    }
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    return Honcho(**kwargs)


# ---------------------------------------------------------------------------
# Server compatibility (Honcho 3.x / honcho-ai 2.4+)
# ---------------------------------------------------------------------------

# Limits declared by the Honcho 3.x OpenAPI schema. MessageCreate.content caps
# at 25,000 characters and MessageBatchCreate accepts at most 100 messages, so
# a chunk plus its "[Part i/n] " prefix must stay under the former and
# --batch-size under the latter.
SERVER_MAX_CONTENT = 25_000
SERVER_MAX_BATCH = 100
# Page size for reading a session back. Not a server limit, just the size that
# keeps the round trips down when indexing a few thousand existing messages.
EXISTING_PAGE_SIZE = 100


def read_server_version(base_url: str | None, timeout: float) -> str | None:
    """Best-effort read of the server's version from its OpenAPI document.

    Purely informational: a server that requires auth on /openapi.json, or an
    older build without one, simply yields None and the import proceeds.
    """
    if not base_url:
        return None
    url = base_url.rstrip("/") + "/openapi.json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return json.load(resp).get("info", {}).get("version")
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def sdk_versions() -> tuple[str, str | None]:
    """Return (API version the SDK speaks, installed honcho-ai version)."""
    try:
        from honcho.http import routes

        api_version = str(getattr(routes, "API_VERSION", "unknown"))
    except ImportError:
        api_version = "unknown"
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            pkg_version = version("honcho-ai")
        except PackageNotFoundError:
            pkg_version = None
    except ImportError:
        pkg_version = None
    return api_version, pkg_version


def print_version_banner(base_url: str | None, timeout: float) -> None:
    """Print server/SDK versions and warn when they cannot speak to each other.

    The version numbers are deliberately confusing: the Honcho *server* is on
    3.x and serves /v3, while the Python SDK that speaks /v3 is honcho-ai 2.4+.
    Printing both removes the guesswork.
    """
    api_version, pkg_version = sdk_versions()
    server = read_server_version(base_url, timeout)
    sdk_label = f"honcho-ai {pkg_version}" if pkg_version else "honcho-ai (version unknown)"
    print(f"Server version: {server or '<unknown>'}   SDK: {sdk_label} (speaks API {api_version})")
    if server and api_version != "unknown":
        server_major = server.split(".", 1)[0]
        if api_version != f"v{server_major}":
            print(
                f"WARNING: SDK speaks API {api_version} but the server reports {server}. "
                f"Install an SDK that targets /v{server_major} (Honcho 3.x needs honcho-ai>=2.4).",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Merge modes: what to do when the target session already holds messages
# ---------------------------------------------------------------------------

MERGE_MODES = ("dedupe", "skip", "gap", "force")


def _is_not_found(exc: BaseException) -> bool:
    return type(exc).__name__ == "NotFoundError" or "not found" in str(exc).lower()


def fetch_existing_messages(session) -> list:
    """Every message already in a session, or [] if it has not been created yet.

    Iterating a SyncPage transparently walks every page, so the page size is
    what decides how many round trips this costs.
    """
    try:
        return list(session.messages(size=EXISTING_PAGE_SIZE))
    except Exception as exc:  # noqa: BLE001
        if _is_not_found(exc):
            return []
        raise


def fingerprint(peer_id: str, content: str) -> str:
    """Identity of a message for dedupe: author plus whitespace-normalized text.

    Normalizing whitespace lets a turn written live by the plugin match the
    same turn reconstructed from the transcript when only trailing newlines or
    wrapping differ.
    """
    normalized = " ".join(str(content).split())
    return hashlib.sha1(f"{peer_id}\x00{normalized}".encode()).hexdigest()  # noqa: S324


# A duplicate is the *same text at about the same time*. Text alone is not
# enough: a one-line tool summary like "[Tool] Bash: npm test" recurs verbatim
# across months, and matching on content alone would let a May message suppress
# an identical August one. Imported messages carry the transcript's own
# timestamp and the live plugin writes within seconds of the turn, so an hour
# of slack separates "the same message" from "the same words again later".
DEFAULT_DEDUPE_WINDOW = 3600


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        value = created_at_from_iso(value)
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def build_existing_index(existing: Sequence[Any]) -> dict[str, list[datetime | None]]:
    """Map each fingerprint already in the session to its creation times."""
    index: dict[str, list[datetime | None]] = {}
    for m in existing:
        fp = fingerprint(getattr(m, "peer_id", ""), getattr(m, "content", ""))
        index.setdefault(fp, []).append(_as_utc(getattr(m, "created_at", None)))
    for times in index.values():
        times.sort(key=lambda d: (d is None, d or datetime.min.replace(tzinfo=timezone.utc)))
    return index


def _consume_match(times: list, incoming: datetime | None, tolerance: timedelta | None) -> bool:
    """Consume the closest existing timestamp within tolerance; True if matched.

    Consuming rather than merely testing keeps this a multiset check: a turn
    that genuinely occurs twice is only suppressed twice.
    """
    if not times:
        return False
    if tolerance is None or incoming is None:
        times.pop(0)
        return True
    best_index: int | None = None
    best_delta: timedelta | None = None
    for i, existing_at in enumerate(times):
        if existing_at is None:
            continue
        delta = abs(existing_at - incoming)
        if delta <= tolerance and (best_delta is None or delta < best_delta):
            best_index, best_delta = i, delta
    if best_index is None:
        return False
    times.pop(best_index)
    return True


def drop_already_present(
    batch: list,
    kinds: list,
    existing_index: dict[str, list[datetime | None]],
    window_seconds: int = DEFAULT_DEDUPE_WINDOW,
) -> tuple[list, list, int]:
    """Filter a built batch down to what the session does not already hold.

    A negative window_seconds drops the timestamp check and matches on text
    alone; zero demands the timestamps agree exactly.
    """
    remaining = {fp: list(times) for fp, times in existing_index.items()}
    tolerance = None if window_seconds < 0 else timedelta(seconds=window_seconds)
    kept_batch: list = []
    kept_kinds: list = []
    dropped = 0
    for params, kind in zip(batch, kinds):
        fp = fingerprint(getattr(params, "peer_id", ""), getattr(params, "content", ""))
        incoming = _as_utc(getattr(params, "created_at", None))
        if _consume_match(remaining.get(fp, []), incoming, tolerance):
            dropped += 1
            continue
        kept_batch.append(params)
        kept_kinds.append(kind)
    return kept_batch, kept_kinds, dropped


def tally_kinds(kinds: Sequence[tuple[str, int]]) -> tuple[int, int, int]:
    """Count distinct source messages per kind among a list of surviving chunks."""
    seen: dict[str, set[int]] = {"user": set(), "assistant": set(), "tool": set()}
    for kind, msg_index in kinds:
        seen[kind].add(msg_index)
    return len(seen["user"]), len(seen["assistant"]), len(seen["tool"])


def earliest_created_at(existing: Sequence[Any]) -> datetime | None:
    earliest: datetime | None = None
    for m in existing:
        created = getattr(m, "created_at", None)
        if isinstance(created, str):
            created = created_at_from_iso(created)
        if created is None:
            continue
        if earliest is None or created < earliest:
            earliest = created
    return earliest


class PreviewPeer:
    """Stand-in for an SDK Peer so a dry run can build the real batch offline.

    Produces objects with the same .peer_id / .content surface the dedupe and
    reporting code reads, without the get-or-create call a real client.peer()
    would make.
    """

    __slots__ = ("id",)

    def __init__(self, peer_id: str) -> None:
        self.id = peer_id

    def message(self, content: str, **kwargs: Any) -> "PreviewMessage":
        return PreviewMessage(self.id, content, kwargs.get("created_at"))


class PreviewMessage:
    __slots__ = ("peer_id", "content", "created_at")

    def __init__(self, peer_id: str, content: str, created_at: datetime | None) -> None:
        self.peer_id = peer_id
        self.content = content
        self.created_at = created_at


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill Claude Code session history into a Honcho workspace (mirrors the live honcho plugin).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # Sources
    parser.add_argument("--projects-dir", help="Claude Code projects dir", default=str(CLAUDE_PROJECTS_DEFAULT))
    parser.add_argument("--honcho-config", help="Path to ~/.honcho/config.json", default=str(HONCHO_CONFIG_DEFAULT))
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host key inside config.json hosts block")
    parser.add_argument("--project", help="Only import sessions whose cwd contains this substring")

    # Identity / target (the "my name" / "your client name" launch parameters).
    # Defaults come from ~/.honcho/config.json so they match the live plugin.
    parser.add_argument("--peer-name", help="Your name = the human peer (default: config peerName)")
    parser.add_argument("--ai-peer", help="The client/AI name = assistant peer (default: config aiPeer)")
    parser.add_argument("--workspace", help="Honcho workspace (default: config workspace)")
    parser.add_argument("--honcho-url", help="Honcho base URL (default: config endpoint.baseUrl)")
    parser.add_argument("--honcho-api-key", help="Honcho API key (prefer HONCHO_API_KEY env var)")

    # Session naming
    parser.add_argument("--session-strategy", choices=("per-directory", "git-branch", "per-session", "chat-instance"), help="Override session strategy (default: config sessionStrategy)")
    parser.add_argument("--no-session-prefix", action="store_true", help="Do not prefix session names with the peer name")

    # Content scope. Defaults reproduce native plugin behavior; --no-* trims,
    # --include-* goes beyond native.
    parser.add_argument("--no-tool-summaries", action="store_true", help="Do NOT reconstruct native '[Tool] ...' summaries (on by default)")
    parser.add_argument("--no-brief", action="store_true", help="Drop terse/non-meaningful assistant turns (native keeps them; on by default)")
    parser.add_argument("--include-thinking", action="store_true", help="Include assistant thinking blocks (beyond native; off by default)")
    parser.add_argument("--include-tools", action="store_true", help="Append '[Used tools: ...]' to every assistant turn (native only annotates terse turns; off by default)")
    parser.add_argument("--include-commands", action="store_true", help="Include slash-command turns (off by default)")
    parser.add_argument("--include-subagents", action="store_true", help="Include subagent/sidechain transcripts (off by default)")
    parser.add_argument("--include-internal", action="store_true", help="Include internal tooling sessions like claude-mem's ~/.claude-mem observer agents (off by default)")
    parser.add_argument("--redact-secrets", action="store_true", help="Redact secrets before import: prefixed tokens (ghp_/sk-/nvapi-) AND labeled secrets near cues like 'session key', 'api key', 'password', 'token'")

    # Idempotency / merge behavior
    parser.add_argument(
        "--merge",
        choices=MERGE_MODES,
        default="dedupe",
        help=(
            "What to do when the target session already holds messages. "
            "dedupe: write only the messages it does not already have (default). "
            "skip: leave the whole session alone. "
            "gap: write only messages older than its earliest existing message. "
            "force: append everything (may duplicate)"
        ),
    )
    parser.add_argument("--dedupe-window", type=int, default=DEFAULT_DEDUPE_WINDOW, help="Seconds of clock slack allowed when matching an incoming message against an existing one (--merge dedupe). 0 = timestamps must match exactly, -1 = match on text alone")
    parser.add_argument("--offline", action="store_true", help="Dry-run without contacting Honcho: previews the transcripts only, with no idea what is already imported")
    # Retained aliases for the pre-merge-mode flags.
    parser.add_argument("--force-reimport", action="store_true", help="Alias for --merge force")
    parser.add_argument("--fill-gap", action="store_true", help="Alias for --merge gap")

    # Run control
    parser.add_argument("--execute", action="store_true", help="Actually write to Honcho (otherwise dry-run)")
    parser.add_argument("--max-sessions", type=int, default=0, help="Process at most N sessions (0 = all)")
    parser.add_argument("--batch-size", type=int, default=10, help="Messages per add_messages call")
    parser.add_argument("--batch-delay", type=float, default=0.05, help="Delay between sessions in seconds")
    parser.add_argument("--verbose", action="store_true", help="Print per-session session names")
    args = parser.parse_args()

    projects_dir = Path(args.projects_dir).expanduser()
    honcho_config = Path(args.honcho_config).expanduser()

    print("=" * 72)
    print("Claude Code history -> Honcho importer")
    print("=" * 72)

    if not projects_dir.exists():
        print(f"ERROR: Claude Code projects dir not found at {projects_dir}", file=sys.stderr)
        return 1

    raw_cfg: dict[str, Any] = {}
    if honcho_config.exists():
        try:
            raw_cfg = load_json(honcho_config)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: Could not read {honcho_config}: {exc}", file=sys.stderr)
            return 1

    cfg = resolve_config(raw_cfg, args.host)

    # CLI overrides
    if args.peer_name:
        cfg.peer_name = args.peer_name
    if args.ai_peer:
        cfg.ai_peer = args.ai_peer
    if args.workspace:
        cfg.workspace = args.workspace
    if args.honcho_url:
        cfg.base_url = args.honcho_url
    if args.honcho_api_key:
        cfg.api_key = args.honcho_api_key
    if args.session_strategy:
        cfg.session_strategy = args.session_strategy
    if args.no_session_prefix:
        cfg.session_peer_prefix = False

    # Native by default; --no-* flags trim.
    brief_enabled = not args.no_brief
    tool_summaries_enabled = not args.no_tool_summaries

    # The old boolean flags still work; they simply select a merge mode.
    if args.force_reimport and args.fill_gap:
        print("ERROR: --force-reimport and --fill-gap are mutually exclusive.", file=sys.stderr)
        return 1
    if args.force_reimport:
        args.merge = "force"
    elif args.fill_gap:
        args.merge = "gap"
    if args.offline and args.execute:
        print("ERROR: --offline is a dry-run flag. Writing without reading the workspace first would duplicate messages.", file=sys.stderr)
        return 1

    if args.batch_size < 1:
        print("ERROR: --batch-size must be >= 1.", file=sys.stderr)
        return 1
    if args.batch_size > SERVER_MAX_BATCH:
        print(f"NOTE: --batch-size {args.batch_size} exceeds the server maximum of {SERVER_MAX_BATCH}; clamping.")
        args.batch_size = SERVER_MAX_BATCH
    if args.batch_delay < 0:
        print("ERROR: --batch-delay must be >= 0.", file=sys.stderr)
        return 1
    if cfg.peer_name == cfg.ai_peer:
        print(f"ERROR: peer name and AI peer are both '{cfg.peer_name}'. Set distinct --peer-name/--ai-peer.", file=sys.stderr)
        return 1
    if not args.execute and not cfg.api_key:
        print("NOTE: No API key resolved. A dry-run still works; --execute will need one (HONCHO_API_KEY or config apiKey).")
    if args.execute and not cfg.api_key:
        print("ERROR: --execute needs an API key. Set HONCHO_API_KEY or apiKey in config.json.", file=sys.stderr)
        return 1

    print(f"Projects dir: {projects_dir}")
    print(f"Honcho config: {honcho_config if honcho_config.exists() else '<not found>'} (host: {args.host})")
    print(f"Honcho base: {cfg.base_url or '<SDK default>'}")
    print(f"Workspace: {cfg.workspace}")
    print(f"Peers: you={cfg.peer_name!r} (user)  client={cfg.ai_peer!r} (assistant)")
    print(f"Session strategy: {cfg.session_strategy} (peer prefix: {cfg.session_peer_prefix})")
    print(f"Observation mode: {cfg.observation_mode}")
    print(f"Content (native default): tool-summaries={tool_summaries_enabled} brief={brief_enabled} | thinking={args.include_thinking} tools-annotate={args.include_tools} commands={args.include_commands} subagents={args.include_subagents}")
    print(f"Redact secrets: {args.redact_secrets}")
    policy = f"--merge {args.merge}"
    if args.merge == "dedupe":
        window = "text only" if args.dedupe_window < 0 else f"{args.dedupe_window}s clock slack"
        policy += f" ({window})"
    print(f"Existing-session policy: {policy}")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")

    files = iter_transcript_files(projects_dir, include_subagents=args.include_subagents)
    groups = group_transcripts(
        cfg,
        files,
        include_thinking=args.include_thinking,
        include_tools=args.include_tools,
        include_commands=args.include_commands,
        include_subagents=args.include_subagents,
        include_brief=brief_enabled,
        tool_summaries=tool_summaries_enabled,
        project_filter=args.project,
    )
    # Exclude internal tooling sessions (claude-mem observers, etc.) by default.
    if not args.include_internal:
        internal = [g for g in groups if _is_internal_cwd(g.cwd)]
        groups = [g for g in groups if not _is_internal_cwd(g.cwd)]
    else:
        internal = []

    if args.max_sessions > 0:
        groups = groups[: args.max_sessions]

    print(f"\nCandidate sessions (per {cfg.session_strategy}): {len(groups):,}")

    if internal:
        excluded_msgs = sum(len(g.messages) for g in internal)
        print(f"\nNote: excluded {len(internal)} internal tooling session(s) ({excluded_msgs:,} messages), e.g. claude-mem observers:")
        for g in internal:
            print(f"  - {g.session_name}  ({g.cwd})")
        print("  Use --include-internal to import them anyway.")

    # Transparency: report projects whose main transcripts are gone and only
    # subagent sidechains survive. These are skipped unless --include-subagents.
    if not args.include_subagents:
        orphans = []
        for project_dir in sorted(p for p in projects_dir.iterdir() if p.is_dir()):
            has_top = any(project_dir.glob("*.jsonl"))
            has_nested = any(p.parent != project_dir for p in project_dir.rglob("*.jsonl"))
            if not has_top and has_nested:
                orphans.append(project_dir.name)
        if orphans:
            print(f"\nNote: {len(orphans)} project(s) have only subagent transcripts (no main conversation), skipped:")
            for name in orphans:
                print(f"  - {name}")
            print("  Use --include-subagents to import subagent sidechains (attribution to your peers is approximate).")

    # A dry run reads the workspace so the preview reports what is actually
    # missing rather than what the transcripts happen to contain. --merge force
    # needs no reads, and --offline opts out of contacting the server at all.
    inspect_existing = args.merge != "force" and not args.offline
    need_client = args.execute or (inspect_existing and bool(cfg.api_key))

    client = None
    user_peer = ai_peer = None
    Session = SessionPeerConfig = None
    if need_client:
        print("\nPreparing Honcho client...")
        print_version_banner(cfg.base_url, cfg.timeout)
        client = init_honcho(cfg, cfg.workspace)
        # Session(id, client) and Peer(id, client) are local constructions: no
        # API call until something is read or written through them, which keeps
        # a dry run read-only.
        from honcho.session import Session, SessionPeerConfig  # noqa: F401

        # One cheap call up front so an unreachable host or a bad key fails
        # here with a clear message, instead of once per session further down.
        # It also performs the SDK's get-or-create workspace POST, which is the
        # only write a dry run makes.
        try:
            client.get_metadata()
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: cannot reach Honcho workspace '{cfg.workspace}' at {cfg.base_url or '<SDK default>'}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

        if args.execute:
            user_peer = client.peer(cfg.peer_name)
            ai_peer = client.peer(cfg.ai_peer)
            print(f"Honcho client ready for workspace '{cfg.workspace}'")
        else:
            print(f"Honcho client ready for workspace '{cfg.workspace}' (read-only: dry run)")
    elif inspect_existing:
        print("\nNOTE: no API key resolved, so this dry run cannot check what is already imported.")
        inspect_existing = False

    # In a dry run nothing is written, so the batch is built against local
    # stand-ins rather than real peers.
    if user_peer is None:
        user_peer = PreviewPeer(cfg.peer_name)
        ai_peer = PreviewPeer(cfg.ai_peer)

    processed = 0
    total_user = 0
    total_assistant = 0
    total_tool = 0
    total_written = 0
    total_deduped = 0
    skipped = 0
    errors = 0
    start = time.time()

    for i, group in enumerate(groups, start=1):
        try:
            session = Session(group.session_name, client) if client is not None else None

            existing: list = []
            if session is not None and inspect_existing:
                existing = fetch_existing_messages(session)

            # --merge skip / gap decide up front, on the session as a whole.
            if existing and args.merge == "skip":
                skipped += 1
                note = f"  SKIP (session already has {len(existing)} messages)"
                if args.verbose:
                    note += f": {group.session_name}"
                print(note)
                continue

            messages_to_use = group.messages
            if existing and args.merge == "gap":
                cutoff = earliest_created_at(existing)
                if cutoff is not None:
                    messages_to_use = [
                        m for m in group.messages
                        if (created_at_from_iso(m.timestamp) or datetime.max.replace(tzinfo=timezone.utc)) < cutoff
                    ]
                if not messages_to_use:
                    skipped += 1
                    print(f"  SKIP (no messages older than existing data){': ' + group.session_name if args.verbose else ''}")
                    continue

            preview_group = TranscriptGroup(group.session_name, group.cwd, group.git_branch, group.files, messages_to_use)
            batch, kinds = build_honcho_messages(
                preview_group,
                user_peer=user_peer,
                ai_peer=ai_peer,
                redact=args.redact_secrets,
                session_name=group.session_name,
            )

            # --merge dedupe decides per message, against what is already there.
            deduped = 0
            if existing and args.merge == "dedupe":
                batch, kinds, deduped = drop_already_present(
                    batch, kinds, build_existing_index(existing), args.dedupe_window
                )
                total_deduped += deduped

            if not batch:
                skipped += 1
                reason = "already fully imported" if deduped else "nothing to import"
                print(f"  SKIP ({reason}){': ' + group.session_name if args.verbose else ''}")
                continue

            n_user, n_asst, n_tool = tally_kinds(kinds)

            if args.execute:
                # Add peers exactly as the live plugin does for the observation mode.
                if cfg.observation_mode == "directional":
                    session.add_peers([user_peer, (ai_peer, SessionPeerConfig(observe_others=True))])
                else:
                    session.add_peers([user_peer, ai_peer])

                for j in range(0, len(batch), args.batch_size):
                    session.add_messages(batch[j : j + args.batch_size])

            processed += 1
            total_user += n_user
            total_assistant += n_asst
            total_tool += n_tool
            total_written += len(batch)

            have = f" have={len(existing)}" if existing else ""
            dedupe_note = f" deduped={deduped}" if deduped else ""
            if not args.execute:
                label = f" {group.session_name}" if args.verbose else ""
                print(f"  [{i}/{len(groups)}]{label}  files={len(group.files)}{have}{dedupe_note} user={n_user} assistant={n_asst} tool={n_tool} -> {len(batch)} new chunks  ({group.cwd})")
            elif args.verbose or i == 1 or i % 20 == 0:
                rate = (i / (time.time() - start) * 60) if time.time() > start else 0
                print(f"  [{i}/{len(groups)}] {group.session_name}{have}{dedupe_note}  user={n_user} assistant={n_asst} tool={n_tool} -> {len(batch)} chunks  ({rate:.0f} sess/min)")

            if args.execute and args.batch_delay:
                time.sleep(args.batch_delay)
        except Exception as exc:  # noqa: BLE001 - keep importing other sessions
            errors += 1
            if args.verbose:
                print(f"  ERROR {group.session_name}: {type(exc).__name__}: {exc}")
            else:
                print(f"  ERROR processing a session: {type(exc).__name__}: {exc}")

    elapsed = time.time() - start
    print(f"\n{'=' * 72}")
    print(f"Import {'complete' if args.execute else 'preview complete'} ({elapsed:.1f}s)")
    print(f"  Sessions: {processed:,}")
    print(f"  User messages: {total_user:,}")
    print(f"  Assistant messages: {total_assistant:,}")
    print(f"  Tool summaries: {total_tool:,}")
    print(f"  Honcho messages/chunks: {total_written:,}")
    print(f"  Already present (deduped): {total_deduped:,}")
    print(f"  Sessions skipped: {skipped:,}")
    print(f"  Errors: {errors:,}")
    print(f"  Workspace: {cfg.workspace}")
    if not args.execute:
        if inspect_existing and not errors:
            print("\nDry-run only. Counts above are the DELTA against what the workspace already holds.")
        elif inspect_existing:
            print("\nDry-run only. Some sessions could not be read, so the counts above are NOT a reliable delta.")
        else:
            print("\nDry-run only. The workspace was not read, so counts are the full transcript, not the delta.")
        print("Re-run with --execute to write to Honcho.")
    print("=" * 72)
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

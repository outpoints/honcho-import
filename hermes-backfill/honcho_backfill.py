#!/usr/bin/env python3
"""
Import Hermes Agent state.db conversations into a Honcho workspace.

The default import mode is designed to mirror Hermes' live Honcho memory writes
as closely as possible while remaining safe to run from a dry-run first:

  - source profile: default (~/.hermes/state.db) unless --profile/--state-db is passed
  - target workspace/user peer/AI peer: resolved from honcho.json, CLI flags, or sane defaults
  - one Honcho session per original Hermes session, prefixed by default to avoid collisions
  - add both peers with observation settings resolved from honcho.json or sensible defaults
  - sanitize with Hermes' sanitize_context() when available; otherwise strip whitespace only
  - chunk messages at messageMaxChars with the same [continued] behavior Hermes uses
  - preserve source message timestamps as Honcho created_at by default
  - do not upload transcript files, attach metadata, or redact content unless explicitly requested
  - dry-run unless --execute is passed

No user-specific hosts, peer names, workspace names, session IDs, API URLs, or
credentials are hardcoded in this script. Use --help for configuration options.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

HERMES_HOME_DEFAULT = Path.home() / ".hermes"
HERMES_AGENT_DIR = Path.home() / ".hermes" / "hermes-agent"
DEFAULT_SOURCES = "cli,telegram,webui,tui,api_server"
DEFAULT_MESSAGE_MAX_CHARS = 25_000
DEFAULT_WORKSPACE_ID = "default"
DEFAULT_USER_PEER = "user"
DEFAULT_AI_PEER = "assistant"
DEFAULT_SESSION_PREFIX_TEMPLATE = "hist-{profile}"

# These patterns are intentionally conservative and only used when
# --redact-secrets or --mode safe is explicitly enabled.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]+")),
    ("nvidia_token", re.compile(r"nvapi-[A-Za-z0-9_-]{10,}")),
    # Anthropic keys are a prefix-superset of the OpenAI pattern, so match them
    # first to keep the redaction label accurate.
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
]

if HERMES_AGENT_DIR.exists() and str(HERMES_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT_DIR))


@dataclass
class ImportConfig:
    """Resolved import/runtime config.

    This intentionally mirrors the subset of Hermes' Honcho config needed for
    historical imports, without requiring the full Hermes runtime to be present.
    """

    base_url: str | None = None
    api_key: str = ""
    environment: str = "production"
    workspace_id: str = DEFAULT_WORKSPACE_ID
    peer_name: str = DEFAULT_USER_PEER
    ai_peer: str = DEFAULT_AI_PEER
    message_max_chars: int = DEFAULT_MESSAGE_MAX_CHARS
    user_observe_me: bool = True
    user_observe_others: bool = True
    ai_observe_me: bool = True
    ai_observe_others: bool = True
    timeout: int = 60


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def resolve_profile_paths(profile_name: str | None) -> dict[str, Path]:
    """Return state/config paths for a Hermes profile.

    `default` means the root ~/.hermes profile. Non-default names resolve under
    ~/.hermes/profiles/<name>.
    """
    if profile_name and profile_name not in ("default", "custom"):
        hermes_home = HERMES_HOME_DEFAULT / "profiles" / profile_name
    else:
        hermes_home = HERMES_HOME_DEFAULT
    return {
        "state_db": hermes_home / "state.db",
        "honcho_config": hermes_home / "honcho.json",
    }


def _first_configured_host(raw_cfg: dict[str, Any]) -> str | None:
    hosts = raw_cfg.get("hosts")
    if isinstance(hosts, dict) and hosts:
        # Prefer an enabled host, otherwise fall back to the first declared host.
        for name, value in hosts.items():
            if isinstance(value, dict) and value.get("enabled", True):
                return str(name)
        return str(next(iter(hosts)))
    return None


def _host_dict(raw_cfg: dict[str, Any], host: str | None) -> dict[str, Any]:
    hosts = raw_cfg.get("hosts")
    if isinstance(hosts, dict) and hosts:
        chosen = host or _first_configured_host(raw_cfg)
        data = hosts.get(chosen) if chosen else None
        if isinstance(data, dict):
            return data
        if host:
            available = ", ".join(sorted(map(str, hosts.keys())))
            raise KeyError(f"Host {host!r} not found in honcho config. Available hosts: {available}")
    return raw_cfg


# Observation presets mirror Hermes' plugins/memory/honcho/client.py exactly so
# imported sessions derive memory the same way the live agent would. Each value
# is (user_observe_me, user_observe_others, ai_observe_me, ai_observe_others).
_OBSERVATION_PRESETS: dict[str, tuple[bool, bool, bool, bool]] = {
    "directional": (True, True, True, True),
    "unified": (True, False, False, True),
}
_OBSERVATION_MODE_ALIASES = {
    "shared": "unified",
    "separate": "directional",
    "cross": "directional",
}


def _normalize_observation_mode(mode: str | None) -> str:
    """Normalize an observationMode string to a known preset key.

    Mirrors Hermes: aliases collapse onto the two canonical modes and anything
    unrecognized falls back to ``directional`` (full observation).
    """
    val = (mode or "").strip().lower()
    val = _OBSERVATION_MODE_ALIASES.get(val, val)
    return val if val in _OBSERVATION_PRESETS else "directional"


def _observation_from_mode(mode: str | None) -> tuple[bool, bool, bool, bool]:
    """Map Hermes' observationMode to (user_me, user_others, ai_me, ai_others)."""
    return _OBSERVATION_PRESETS[_normalize_observation_mode(mode)]


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def resolve_import_config(raw_cfg: dict[str, Any], host: str | None) -> ImportConfig:
    """Resolve import settings directly from a Hermes ``honcho.json`` shape.

    This parses the public config layout (top-level fields plus an optional
    ``hosts`` map) so the importer stays portable and does not require the full
    Hermes runtime to be importable.

    Observation resolution mirrors Hermes' plugins/memory/honcho/client.py: a
    string ``observationMode`` selects a preset, an optional nested
    ``observation`` object overrides per peer, and a config that omits
    ``observationMode`` defaults to ``unified`` (the live Hermes default for an
    already-configured host) rather than full observation.
    """
    host_cfg = _host_dict(raw_cfg, host)

    # A loaded honcho.json counts as an explicit configuration. Hermes keeps such
    # installs on the conservative "unified" preset when observationMode is
    # absent; only fresh/unconfigured runs default to full "directional".
    explicitly_configured = bool(raw_cfg)
    raw_mode = _coalesce(host_cfg.get("observationMode"), raw_cfg.get("observationMode"))
    mode = raw_mode or ("unified" if explicitly_configured else "directional")
    user_me, user_others, ai_me, ai_others = _observation_from_mode(mode)

    # Optional granular override. Hermes nests these under user/ai sub-objects;
    # flat keys are also accepted for hand-written configs.
    obs = host_cfg.get("observation")
    obs = obs if isinstance(obs, dict) else {}
    user_block = obs.get("user") if isinstance(obs.get("user"), dict) else {}
    ai_block = obs.get("ai") if isinstance(obs.get("ai"), dict) else {}
    user_me = _coalesce(user_block.get("observeMe"), obs.get("userObserveMe"), host_cfg.get("userObserveMe"), user_me)
    user_others = _coalesce(user_block.get("observeOthers"), obs.get("userObserveOthers"), host_cfg.get("userObserveOthers"), user_others)
    ai_me = _coalesce(ai_block.get("observeMe"), obs.get("aiObserveMe"), host_cfg.get("aiObserveMe"), ai_me)
    ai_others = _coalesce(ai_block.get("observeOthers"), obs.get("aiObserveOthers"), host_cfg.get("aiObserveOthers"), ai_others)

    cfg = ImportConfig(
        base_url=_coalesce(host_cfg.get("baseUrl"), raw_cfg.get("baseUrl"), raw_cfg.get("base_url"), os.environ.get("HONCHO_URL"), os.environ.get("HONCHO_BASE_URL")),
        api_key=str(_coalesce(host_cfg.get("apiKey"), raw_cfg.get("apiKey"), os.environ.get("HONCHO_API_KEY"), "")),
        environment=str(_coalesce(host_cfg.get("environment"), raw_cfg.get("environment"), "production")),
        workspace_id=str(_coalesce(host_cfg.get("workspace"), host_cfg.get("workspace_id"), raw_cfg.get("workspace"), raw_cfg.get("workspace_id"), DEFAULT_WORKSPACE_ID)),
        peer_name=str(_coalesce(host_cfg.get("peerName"), host_cfg.get("peer_name"), DEFAULT_USER_PEER)),
        ai_peer=str(_coalesce(host_cfg.get("aiPeer"), host_cfg.get("ai_peer"), DEFAULT_AI_PEER)),
        message_max_chars=int(_coalesce(host_cfg.get("messageMaxChars"), raw_cfg.get("messageMaxChars"), DEFAULT_MESSAGE_MAX_CHARS)),
        user_observe_me=bool(_optional_bool(user_me)),
        user_observe_others=bool(_optional_bool(user_others)),
        ai_observe_me=bool(_optional_bool(ai_me)),
        ai_observe_others=bool(_optional_bool(ai_others)),
        timeout=int(_coalesce(host_cfg.get("timeout"), raw_cfg.get("timeout"), 60)),
    )
    return cfg


def init_honcho(resolved_cfg: ImportConfig, workspace_id: str):
    from honcho import Honcho

    kwargs: dict[str, Any] = {
        "api_key": resolved_cfg.api_key,
        "workspace_id": workspace_id,
        "timeout": resolved_cfg.timeout,
    }
    if resolved_cfg.base_url:
        # An explicit base_url takes precedence; environment is irrelevant for
        # self-hosted servers and the SDK only accepts 'local'/'production'.
        kwargs["base_url"] = resolved_cfg.base_url
    else:
        env = (resolved_cfg.environment or "production").strip().lower()
        kwargs["environment"] = env if env in {"local", "production"} else "production"
    return Honcho(**kwargs)


def get_sessions(db_path: Path, source_allow: list[str] | None, skip_cron: bool):
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row

    clauses: list[str] = []
    params: list[str] = []
    if source_allow:
        placeholders = ",".join("?" for _ in source_allow)
        clauses.append(f"source IN ({placeholders})")
        params.extend(source_allow)
    elif skip_cron:
        clauses.append("source != 'cron'")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sessions = db.execute(
        f"""
        SELECT id, title, started_at, ended_at, source, model, message_count
        FROM sessions {where}
        ORDER BY started_at ASC
        """,
        params,
    ).fetchall()
    return db, sessions


def get_session_messages(db: sqlite3.Connection, session_id: str):
    return db.execute(
        """
        SELECT role, content, timestamp
        FROM messages
        WHERE session_id = ? AND role IN ('user', 'assistant')
          AND content IS NOT NULL AND content != ''
        ORDER BY timestamp ASC, id ASC
        """,
        (session_id,),
    ).fetchall()


def iso_from_ts(ts) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def created_at_from_ts(ts, *, chunk_index: int = 0) -> datetime | None:
    """Return a timezone-aware created_at for Honcho messages.

    Hermes stores message timestamps in state.db as Unix epoch REAL values.
    Honcho's SDK accepts datetime objects and serializes them to ISO 8601.
    When a single source message is chunked into multiple Honcho messages, add
    a tiny microsecond offset per chunk so ordering remains deterministic.
    """
    if ts is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    if chunk_index:
        dt = dt + timedelta(microseconds=chunk_index)
    return dt


def safe_session_id(prefix: str, source_session_id: str, *, max_len: int = 100) -> str:
    """Create a Honcho-compatible id and enforce Hermes' 100-char limit."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", source_session_id).strip("-")
    raw = f"{prefix}-{cleaned}" if prefix else cleaned
    if len(raw) <= max_len:
        return raw

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{raw[: max_len - 9].rstrip('-')}-{digest}"


def redact_secrets(content: str) -> str:
    redacted = content
    for label, pattern in SECRET_PATTERNS:
        redacted = pattern.sub(f"[REDACTED_{label.upper()}]", redacted)
    return redacted


def sanitize_like_live_hermes(content: str) -> str:
    """Use the same sanitizer live Hermes calls before Honcho sync_turn()."""
    try:
        from agent.memory_manager import sanitize_context  # type: ignore
    except Exception:
        return (content or "").strip()
    return sanitize_context(content or "").strip()


def chunk_like_live_hermes(content: str, limit: int) -> list[str]:
    """Same algorithm as HonchoMemoryProvider._chunk_message()."""
    if limit <= 0:
        raise ValueError("message_max_chars must be positive")
    if len(content) <= limit:
        return [content]

    prefix = "[continued] "
    prefix_len = len(prefix)
    if limit <= prefix_len:
        raise ValueError("message_max_chars must be greater than len('[continued] ')")

    chunks: list[str] = []
    remaining = content
    first = True
    while remaining:
        effective = limit if first else limit - prefix_len
        if len(remaining) <= effective:
            chunks.append(remaining if first else prefix + remaining)
            break

        segment = remaining[:effective]
        cut = segment.rfind("\n\n")
        if cut < effective * 0.3:
            cut = segment.rfind(". ")
            if cut >= 0:
                cut += 2
        if cut < effective * 0.3:
            cut = segment.rfind(" ")
        if cut < effective * 0.3:
            cut = effective

        chunk = remaining[:cut].rstrip()
        remaining = remaining[cut:].lstrip()
        if not first:
            chunk = prefix + chunk
        chunks.append(chunk)
        first = False

    return chunks


def parse_sources(raw: str) -> tuple[list[str] | None, bool]:
    if raw.strip().lower() == "all":
        return None, False
    sources = [s.strip() for s in raw.split(",") if s.strip()]
    return sources, True


def format_transcript(session_row, messages: Iterable[sqlite3.Row], *, max_content_chars: int | None = None) -> str:
    sesh = dict(session_row)
    title = sesh.get("title") or "Untitled"
    sid = sesh["id"]
    source = sesh.get("source", "unknown")
    model = sesh.get("model", "unknown")
    started = iso_from_ts(sesh.get("started_at")) or "unknown"
    ended = iso_from_ts(sesh.get("ended_at")) or "unknown"

    lines = [
        f"# Session: {title}",
        f"ID: {sid}",
        f"Source: {source} | Model: {model}",
        f"Time: {started} → {ended}",
        "",
    ]
    for msg in messages:
        role = msg["role"]
        content = sanitize_like_live_hermes(msg["content"] or "")
        ts = iso_from_ts(msg["timestamp"]) or "?"
        body = content if max_content_chars is None else content[:max_content_chars]
        lines.append(f"[{ts}] {role.upper()}: {body}")
        lines.append("")
    return "\n".join(lines)


def build_honcho_messages(
    rows: Sequence[sqlite3.Row],
    *,
    user_peer,
    ai_peer,
    message_max_chars: int,
    mode: str,
    redact: bool,
    include_metadata: bool,
    preserve_created_at: bool,
    source_profile: str,
    source_session_id: str,
    session_source: str | None,
    title: str,
):
    batch = []
    raw_rows = 0
    written_chunks = 0
    for row in rows:
        role = row["role"]
        peer = user_peer if role == "user" else ai_peer
        content = sanitize_like_live_hermes(row["content"] or "")
        if not content:
            continue
        if redact:
            content = redact_secrets(content)
        for chunk_index, chunk in enumerate(chunk_like_live_hermes(content, message_max_chars)):
            kwargs: dict[str, Any] = {}
            if include_metadata:
                # Metadata is off by default because titles/session IDs can be
                # personally identifying. Enable only when you explicitly want
                # provenance stored in Honcho.
                kwargs["metadata"] = {
                    "imported": True,
                    "import_mode": mode,
                    "import_source": "hermes_state_db",
                    "source_profile": source_profile,
                    "source_session_id": source_session_id,
                    "source_role": role,
                    "source": session_source,
                    "title": title,
                }
            if preserve_created_at:
                created_at = created_at_from_ts(row["timestamp"], chunk_index=chunk_index)
                if created_at is not None:
                    kwargs["created_at"] = created_at
            batch.append(peer.message(chunk, **kwargs))
            written_chunks += 1
        raw_rows += 1
    return batch, raw_rows, written_chunks


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Hermes Agent history into a Honcho workspace")
    parser.add_argument("--profile", "-p", default="default", help="Source Hermes profile name (default: default)")
    parser.add_argument("--host", default=None, help="Honcho host config key to resolve (default: first enabled host or top-level config)")
    parser.add_argument("--state-db", help="Path to source Hermes state.db")
    parser.add_argument("--honcho-config", help="Path to honcho.json for baseUrl/apiKey/defaults")
    parser.add_argument("--honcho-url", help="Honcho base URL, e.g. http://localhost:8000 (overrides config/env)")
    parser.add_argument("--honcho-api-key", help="Honcho API key (prefer HONCHO_API_KEY env var for real secrets)")
    parser.add_argument("--target-workspace", help="Honcho workspace to write to (default: config workspace or 'default')")
    parser.add_argument("--target-user-peer", help="Peer for historical user messages (default: config peerName or 'user')")
    parser.add_argument("--target-ai-peer", help="Peer for historical assistant messages (default: config aiPeer or 'assistant')")
    parser.add_argument("--session-prefix", help="Prefix for created Honcho sessions (default: hist-<profile>; pass '' for raw source ids)")
    parser.add_argument("--sources", default=DEFAULT_SOURCES, help=f"Comma-separated session sources to import (default: {DEFAULT_SOURCES}); use 'all' to include cron")
    parser.add_argument("--mode", choices=("live", "safe"), default="live", help="live = mirror Hermes Honcho writes; safe = redacted AI-focused curated import")
    parser.add_argument("--execute", action="store_true", help="Actually write to Honcho; otherwise dry-run only")
    parser.add_argument("--force-reimport", action="store_true", help="Do not skip target sessions that already contain messages; can create duplicates")
    parser.add_argument("--max-sessions", type=int, default=0, help="Max sessions to process (0 = all)")
    parser.add_argument("--batch-size", type=int, default=10, help="Messages per add_messages call (default: 10)")
    parser.add_argument("--batch-delay", type=float, default=0.05, help="Delay between sessions in seconds")
    parser.add_argument("--message-max-chars", type=int, help=f"Override message chunk size (default: config or {DEFAULT_MESSAGE_MAX_CHARS})")
    parser.add_argument("--include-files", action="store_true", help="Also upload transcript files. Off by default and can duplicate/import more data than intended.")
    parser.add_argument("--redact-secrets", action="store_true", help="Redact obvious tokens before import. Off by default to mirror live Hermes behavior.")
    parser.add_argument("--include-metadata", action="store_true", help="Attach importer/source metadata. Off by default because IDs/titles can be identifying.")
    parser.add_argument("--verbose", action="store_true", help="Print target session IDs while processing. Off by default to avoid leaking session IDs into logs.")
    parser.add_argument(
        "--preserve-created-at",
        dest="preserve_created_at",
        action="store_true",
        default=True,
        help="Write historical message timestamps as Honcho created_at (default).",
    )
    parser.add_argument(
        "--no-preserve-created-at",
        dest="preserve_created_at",
        action="store_false",
        help="Do not pass historical timestamps; Honcho will use import arrival time.",
    )
    args = parser.parse_args()

    paths = resolve_profile_paths(args.profile)
    state_db = Path(args.state_db).expanduser() if args.state_db else paths["state_db"]
    honcho_config = Path(args.honcho_config).expanduser() if args.honcho_config else paths["honcho_config"]

    print("=" * 72)
    print("Hermes state.db → Honcho importer")
    print("=" * 72)

    if not state_db.exists():
        print(f"ERROR: State DB not found at {state_db}", file=sys.stderr)
        return 1

    raw_cfg: dict[str, Any] = {}
    if honcho_config.exists():
        raw_cfg = load_json(honcho_config)
    elif not args.honcho_url:
        print(
            f"ERROR: Honcho config not found at {honcho_config}. Pass --honcho-url or --honcho-config.",
            file=sys.stderr,
        )
        return 1

    try:
        resolved_cfg = resolve_import_config(raw_cfg, args.host)
    except KeyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.honcho_url:
        resolved_cfg.base_url = args.honcho_url
    if args.honcho_api_key:
        resolved_cfg.api_key = args.honcho_api_key
    if args.message_max_chars:
        resolved_cfg.message_max_chars = args.message_max_chars

    # Validate numeric options up front so a bad value fails cleanly here rather
    # than raising mid-import (chunking, range(), and sleep() would otherwise
    # throw partway through).
    min_chunk = len("[continued] ") + 1
    if resolved_cfg.message_max_chars < min_chunk:
        print(f"ERROR: message max chars must be >= {min_chunk}.", file=sys.stderr)
        return 1
    if args.batch_size < 1:
        print("ERROR: --batch-size must be >= 1.", file=sys.stderr)
        return 1
    if args.batch_delay < 0:
        print("ERROR: --batch-delay must be >= 0.", file=sys.stderr)
        return 1

    target_workspace = args.target_workspace or resolved_cfg.workspace_id
    target_user_peer = args.target_user_peer or resolved_cfg.peer_name or DEFAULT_USER_PEER
    target_ai_peer = args.target_ai_peer or resolved_cfg.ai_peer or DEFAULT_AI_PEER
    if target_user_peer == target_ai_peer:
        print(
            f"ERROR: user peer and assistant peer are both '{target_user_peer}'. "
            "Set distinct --target-user-peer/--target-ai-peer so roles are not merged.",
            file=sys.stderr,
        )
        return 1
    session_prefix = args.session_prefix if args.session_prefix is not None else DEFAULT_SESSION_PREFIX_TEMPLATE.format(profile=args.profile or "default")
    source_allow, skip_cron = parse_sources(args.sources)

    if args.mode == "live":
        redact = args.redact_secrets
        include_metadata = args.include_metadata
        preserve_created_at = args.preserve_created_at
        user_observe_me = resolved_cfg.user_observe_me
        user_observe_others = resolved_cfg.user_observe_others
        ai_observe_me = resolved_cfg.ai_observe_me
        ai_observe_others = resolved_cfg.ai_observe_others
    else:
        # Explicit curated mode: safer, intentionally non-faithful.
        redact = True
        include_metadata = True
        preserve_created_at = args.preserve_created_at
        user_observe_me = False
        user_observe_others = False
        ai_observe_me = True
        ai_observe_others = False

    print(f"Source profile: {args.profile}")
    print(f"State DB: {state_db}")
    print(f"Honcho config: {honcho_config if honcho_config.exists() else '<not used>'}")
    print(f"Honcho base: {resolved_cfg.base_url or '<SDK default>'}")
    print(f"Host config: {args.host or '<auto/top-level>'}")
    print(f"Target workspace: {target_workspace}")
    print(f"Target peers: user={target_user_peer}, ai={target_ai_peer}")
    print(f"Historical session prefix: {session_prefix!r}")
    print(f"Sources: {args.sources}")
    print(f"Mode: {args.mode.upper()} {'(mirrors live Hermes/Honcho writes)' if args.mode == 'live' else '(curated/safe, non-faithful)'}")
    print(f"Message max chars: {resolved_cfg.message_max_chars}")
    print(f"Observation: user(me={user_observe_me}, others={user_observe_others}) ai(me={ai_observe_me}, others={ai_observe_others})")
    print(f"Secret redaction: {'yes' if redact else 'no'}")
    print(f"Message metadata: {'yes' if include_metadata else 'no'}")
    print(f"Preserve created_at: {'yes' if preserve_created_at else 'no'}")
    print(f"Transcript uploads: {'yes' if args.include_files else 'no'}")
    print(f"Verbose session IDs: {'yes' if args.verbose else 'no'}")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")

    if args.mode == "live" and (args.redact_secrets or args.include_metadata or args.include_files):
        print("\nNOTE: You enabled options that intentionally diverge from live Hermes behavior.")
    if args.mode == "live" and args.preserve_created_at:
        print("\nNOTE: Historical timestamp preservation is enabled by default so imports retain original conversation chronology.")
        print("      Pass --no-preserve-created-at for strict arrival-time replay.")

    print("\nPreparing Honcho client...")
    client = init_honcho(resolved_cfg, target_workspace)
    user_peer = client.peer(target_user_peer)
    ai_peer = client.peer(target_ai_peer)
    # The SDK is lazy: no network call happens until the first write, so a
    # dry-run never contacts the server and never creates the workspace/peers.
    print(f"Honcho client ready for workspace '{client.workspace_id}' (connects on first write)")

    from honcho.session import SessionPeerConfig

    db, sessions = get_sessions(state_db, source_allow=source_allow, skip_cron=skip_cron)
    if args.max_sessions > 0:
        sessions = sessions[: args.max_sessions]

    print(f"\nCandidate sessions: {len(sessions):,}")

    processed_sessions = 0
    raw_message_rows = 0
    total_written_messages = 0
    total_file_uploads = 0
    skipped = 0
    errors = 0
    start_time = time.time()

    try:
        for i, session_row in enumerate(sessions, start=1):
            sesh = dict(session_row)
            sid = sesh["id"]
            title = sesh.get("title") or "Untitled"
            msg_count = sesh.get("message_count") or 0
            if msg_count < 2:
                skipped += 1
                continue

            messages = get_session_messages(db, sid)
            if len(messages) < 2:
                skipped += 1
                continue

            target_session_name = safe_session_id(session_prefix, sid)
            batch, raw_rows, written_chunks = build_honcho_messages(
                messages,
                user_peer=user_peer,
                ai_peer=ai_peer,
                message_max_chars=resolved_cfg.message_max_chars,
                mode=args.mode,
                redact=redact,
                include_metadata=include_metadata,
                preserve_created_at=preserve_created_at,
                source_profile=args.profile,
                source_session_id=sid,
                session_source=sesh.get("source"),
                title=title,
            )
            if not batch:
                skipped += 1
                continue

            if args.execute:
                try:
                    target_session = client.session(target_session_name)
                    if not args.force_reimport:
                        existing = list(target_session.messages())
                        if existing:
                            skipped += 1
                            msg = f"  SKIP existing session with {len(existing)} messages already present"
                            if args.verbose:
                                msg += f": {target_session_name}"
                            print(msg)
                            continue

                    if include_metadata:
                        target_session.set_metadata(
                            {
                                "imported": True,
                                "import_mode": args.mode,
                                "import_source": "hermes_state_db",
                                "source_profile": args.profile,
                                "source_session_id": sid,
                                "source": sesh.get("source"),
                                "title": title,
                                "started_at": iso_from_ts(sesh.get("started_at")),
                                "ended_at": iso_from_ts(sesh.get("ended_at")),
                                "model": sesh.get("model"),
                            }
                        )

                    target_session.add_peers(
                        [
                            (user_peer, SessionPeerConfig(observe_me=user_observe_me, observe_others=user_observe_others)),
                            (ai_peer, SessionPeerConfig(observe_me=ai_observe_me, observe_others=ai_observe_others)),
                        ]
                    )
                    for j in range(0, len(batch), args.batch_size):
                        target_session.add_messages(batch[j : j + args.batch_size])

                    if args.include_files:
                        transcript = format_transcript(session_row, messages)
                        target_session.upload_file(
                            file=(f"session_{hashlib.sha256(sid.encode()).hexdigest()[:16]}.txt", transcript.encode("utf-8"), "text/plain; charset=utf-8"),
                            peer=ai_peer,
                            metadata={
                                "imported": True,
                                "import_source": "hermes_state_db_transcript",
                                "source_profile": args.profile,
                                "source_session_id": sid,
                            },
                        )
                        total_file_uploads += 1
                except Exception as e:  # noqa: BLE001 - importer should continue
                    errors += 1
                    if args.verbose:
                        print(f"  ERROR session {sid} -> {target_session_name}: {type(e).__name__}: {e}")
                    else:
                        print(f"  ERROR processing a session: {type(e).__name__}: {e}")
                    continue

            processed_sessions += 1
            raw_message_rows += raw_rows
            total_written_messages += written_chunks

            if i == 1 or i % 20 == 0:
                elapsed = time.time() - start_time
                rate = (i / elapsed * 60) if elapsed > 0 else 0
                pct = (i / len(sessions) * 100) if sessions else 100
                session_part = f"session={target_session_name} | " if args.verbose else ""
                print(
                    f"  [{i}/{len(sessions)}] {pct:.0f}% | "
                    f"{session_part}rows={raw_rows} chunks={written_chunks} | "
                    f"{total_written_messages:,} total chunks | {rate:.0f} sess/min"
                )

            if args.execute and args.batch_delay:
                time.sleep(args.batch_delay)
    finally:
        db.close()

    elapsed = time.time() - start_time
    print(f"\n{'=' * 72}")
    print(f"Import {'complete' if args.execute else 'preview complete'} ({elapsed:.1f}s)")
    print(f"  Sessions processed: {processed_sessions:,}")
    print(f"  Raw user/assistant rows: {raw_message_rows:,}")
    print(f"  Honcho messages/chunks: {total_written_messages:,}")
    print(f"  File transcripts: {total_file_uploads:,}")
    print(f"  Skipped: {skipped:,}")
    print(f"  Errors: {errors:,}")
    print(f"  Target workspace: {target_workspace}")
    print(f"  Historical sessions prefix: {session_prefix}-*" if session_prefix else "  Historical sessions prefix: <none>")
    if not args.execute:
        print("\nDry-run only. Re-run with --execute to write to Honcho.")
    if args.mode == "live":
        print("\nLive mode mirrors Hermes' Honcho write path: sanitize, chunk, peer.message(...), add_messages().")
    else:
        print("\nSafe mode is intentionally non-faithful: redaction + metadata + AI-peer-only observation.")
    print("=" * 72)
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

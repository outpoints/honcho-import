#!/bin/bash
#
# Re-run the Claude Code and Cowork importers on a schedule.
#
# Safe to run repeatedly: both importers default to --merge dedupe, so a run
# writes only the messages the target workspace does not already hold.
#
# The point of the gates below is that this is a LAN-only job. The Honcho
# server usually lives on one network, and running the import from a coffee
# shop just produces connection errors in a log nobody reads. So the script
# exits 0 (quietly, "not now") when it is off-network, and only reports real
# failures.
#
# Configure by placing overrides in ~/.honcho/import-schedule.env — keep the
# personal values (network fingerprints, paths) there, not in this file.
#
# Exit codes: 0 = imported, or skipped because conditions were not met.
#             1 = configuration error.  2 = an importer failed.

set -uo pipefail

CONFIG_FILE="${HONCHO_IMPORT_CONFIG:-$HOME/.honcho/import-schedule.env}"

# The config file supplies the baseline, but anything already exported wins --
# otherwise sourcing would silently clobber a deliberate one-off override such
# as HONCHO_IMPORT_GATEWAY_MACS=... ./scheduled-import.sh. Snapshot first,
# source, then put the environment's values back.
_env_snapshot=$(export -p | grep -E '^(export |declare -x )HONCHO_IMPORT_' || true)
# shellcheck source=/dev/null
[[ -f "$CONFIG_FILE" ]] && source "$CONFIG_FILE"
[[ -n "$_env_snapshot" ]] && eval "$_env_snapshot"
unset _env_snapshot

REPO_DIR="${HONCHO_IMPORT_REPO:-$HOME/honcho-import}"
PYTHON_BIN="${HONCHO_IMPORT_PYTHON:-$REPO_DIR/.venv/bin/python}"
LOG_DIR="${HONCHO_IMPORT_LOG_DIR:-$HOME/Library/Logs/honcho-import}"
LOG_KEEP="${HONCHO_IMPORT_LOG_KEEP:-12}"

# Network gates. Leave a variable empty to disable that gate.
#   SSID_GLOB        - shell glob the Wi-Fi SSID must match, e.g. 'my_network*'
#   GATEWAY_MACS     - space-separated router MACs, used when the SSID is
#                      unreadable (macOS 15+ redacts it without Location
#                      Services, which a background job cannot obtain)
#   REQUIRE_HONCHO   - 1 to also require the Honcho server to answer
SSID_GLOB="${HONCHO_IMPORT_SSID_GLOB:-}"
GATEWAY_MACS="${HONCHO_IMPORT_GATEWAY_MACS:-}"
REQUIRE_HONCHO="${HONCHO_IMPORT_REQUIRE_HONCHO:-1}"
HONCHO_URL="${HONCHO_IMPORT_URL:-}"

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/import-$(date +%Y%m%d-%H%M%S).log"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"; }

skip() { log "SKIP: $*"; log "Nothing to do. Exiting cleanly."; exit 0; }

# --- current Wi-Fi SSID, or empty when it cannot be read -------------------
# macOS 15+ returns the literal string "<redacted>" to any caller without
# Location Services authorization, so treat that as "unknown", not as a name.
current_ssid() {
    local ssid=""
    if command -v ipconfig >/dev/null 2>&1; then
        ssid=$(ipconfig getsummary en0 2>/dev/null \
               | awk -F ' SSID : ' '/ SSID : /{print $2; exit}')
    fi
    if [[ -z "$ssid" || "$ssid" == "<redacted>" ]]; then
        ssid=$(networksetup -getairportnetwork en0 2>/dev/null \
               | sed -n 's/^Current Wi-Fi Network: //p')
    fi
    [[ "$ssid" == "<redacted>" ]] && ssid=""
    printf '%s' "$ssid"
}

# --- MAC of the default gateway: a permission-free network fingerprint -----
gateway_mac() {
    local gw
    gw=$(route -n get default 2>/dev/null | awk '/gateway:/{print $2; exit}')
    [[ -z "$gw" ]] && return 0
    ping -c1 -W1000 "$gw" >/dev/null 2>&1
    arp -n "$gw" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i ~ /^([0-9a-f]{1,2}:){5}[0-9a-f]{1,2}$/) {print tolower($i); exit}}'
}

log "=== honcho-import scheduled run ==="

[[ -x "$PYTHON_BIN" ]] || { log "ERROR: python not found at $PYTHON_BIN"; exit 1; }
[[ -d "$REPO_DIR" ]]   || { log "ERROR: repo not found at $REPO_DIR"; exit 1; }

# --- gate 1: are we on the right network? ----------------------------------
if [[ -n "$SSID_GLOB" || -n "$GATEWAY_MACS" ]]; then
    ssid=$(current_ssid)
    if [[ -n "$ssid" ]]; then
        # shellcheck disable=SC2053
        if [[ -n "$SSID_GLOB" && $ssid == $SSID_GLOB ]]; then
            log "Network OK: SSID '$ssid' matches '$SSID_GLOB'."
        else
            skip "SSID '$ssid' does not match '$SSID_GLOB'."
        fi
    elif [[ -n "$GATEWAY_MACS" ]]; then
        # SSID unreadable (the normal case for a background job on macOS 15+).
        gw_mac=$(gateway_mac)
        if [[ -n "$gw_mac" && " $GATEWAY_MACS " == *" $gw_mac "* ]]; then
            log "Network OK: SSID unreadable, gateway $gw_mac is a known router."
        else
            skip "SSID unreadable and gateway '${gw_mac:-none}' is not in the allowlist."
        fi
    else
        skip "SSID unreadable and no gateway allowlist configured."
    fi
fi

# --- gate 2: is the server actually there? ---------------------------------
if [[ "$REQUIRE_HONCHO" == "1" && -n "$HONCHO_URL" ]]; then
    if curl -fsS -m 10 -o /dev/null "${HONCHO_URL%/}/health" 2>/dev/null; then
        log "Honcho reachable at $HONCHO_URL."
    else
        skip "Honcho not reachable at $HONCHO_URL."
    fi
fi

# --- run both importers ----------------------------------------------------
status=0
for importer in claude-code-backfill cowork-backfill; do
    log "--- $importer ---"
    if "$PYTHON_BIN" "$REPO_DIR/$importer/honcho_backfill.py" --execute >>"$LOG_FILE" 2>&1; then
        summary=$(grep -E "Honcho messages/chunks:|Already present" "$LOG_FILE" | tail -2 | tr -s ' ')
        log "$importer OK.${summary:+ $summary}"
    else
        log "ERROR: $importer exited non-zero. See $LOG_FILE"
        status=2
    fi
done

# Keep the log directory from growing without bound.
ls -1t "$LOG_DIR"/import-*.log 2>/dev/null | tail -n +$((LOG_KEEP + 1)) | while read -r old; do rm -f "$old"; done

log "=== done (status $status) ==="
exit $status

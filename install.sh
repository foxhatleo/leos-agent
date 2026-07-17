#!/usr/bin/env bash
# v3 is a Claude Code PLUGIN, installed via `claude plugin` — this script only
# handles what a plugin manifest can't: v2 cleanup, personal settings, drift.
# Anything mutated is first backed up to ~/.claude/backups/leos-agent-<ts>/.
#   migrate [--install]  one-time v2->v3 cleanup, idempotent (default mode);
#                         --install also registers+installs the plugin
#   settings              merge install/personal-settings.json into
#                          ~/.claude/settings.json (repo keys win)
#   check                  report drift, change nothing (exit 1 if any)
#   update                 pull the marketplace + update the plugin
#   mcp                    deprecated no-op (MCP now ships in-plugin)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
BACKUP_DIR="$CLAUDE_DIR/backups/leos-agent-$(date +%Y%m%d-%H%M%S)"
MODE="${1:-migrate}"
drift=0
backup() { mkdir -p "$BACKUP_DIR"; mv "$1" "$BACKUP_DIR/"; echo "  moved existing $(basename "$1") -> $BACKUP_DIR/"; }
# v2 symlinked repo items (per-item, or old-old whole-dir) into a layout the
# plugin doesn't use. Remove them; leave real files/foreign symlinks alone.
scan_symlinks() {
  local mode="$1" d dst item base target
  for d in agents skills hooks workflows; do
    dst="$CLAUDE_DIR/$d"
    if [[ -L "$dst" ]]; then
      if [[ "$(readlink "$dst")" != *"/leos-agent/claude/"* ]]; then
        echo "  ok    $d (foreign symlink, left alone)"
      elif [[ "$mode" == "check" ]]; then
        echo "  DRIFT $d (whole-dir v2 symlink)"; drift=1
      else
        backup "$dst"; mkdir -p "$dst"; echo "  fix   $d converted from v2 symlink to real dir"
      fi
      continue
    fi
    [[ -d "$dst" ]] || { echo "  ok    $d (not present)"; continue; }
    for item in "$dst"/*; do
      # -L only: -e follows symlinks, and the v2 links are DANGLING once the
      # old layout is deleted — exactly the entries this scan must remove.
      # A literal unmatched glob is not a symlink, so -L also covers that.
      [[ -L "$item" ]] || continue
      base="$(basename "$item")"; target="$(readlink "$item")"
      [[ "$target" == *"/leos-agent/claude/"* ]] || continue
      if [[ "$mode" == "check" ]]; then
        echo "  DRIFT $d/$base (v2 symlink into deleted layout)"; drift=1
      else
        backup "$item"; echo "  fix   removed $d/$base (v2 symlink)"
      fi
    done
  done
}
# check/migrate the stale v2 `@.../leos-agent/claude/CLAUDE.md` import line.
claude_md() {
  local mode="$1" md="$CLAUDE_DIR/CLAUDE.md" pat='^@.*leos-agent/claude/CLAUDE\.md'
  [[ -e "$md" ]] || { echo "  ok    CLAUDE.md (not present)"; return; }
  if [[ -L "$md" ]]; then
    echo "  WARN  CLAUDE.md is a symlink; not touching it — remove the v2 @import line at its target yourself"
  elif ! grep -qE "$pat" "$md"; then
    echo "  ok    CLAUDE.md (no v2 import line)"
  elif [[ "$mode" == "check" ]]; then
    echo "  DRIFT CLAUDE.md (stale v2 @import line)"; drift=1
  else
    backup "$md"
    grep -vE "$pat" "$BACKUP_DIR/CLAUDE.md" >"$md"
    echo "  fix   removed stale v2 @import line from CLAUDE.md"
  fi
}
# claude plugin list --json is the source of truth for installed + version.
check_plugin_version() {
  command -v claude >/dev/null || { echo "  skip  plugin install/version (claude CLI not found)"; return; }
  local result
  result=$(python3 - "$(claude plugin list --json 2>/dev/null || echo '[]')" "$REPO_DIR/.claude-plugin/plugin.json" <<'PY'
import json, sys
try:
    installed = json.loads(sys.argv[1])
    # Tolerate both output shapes: a top-level array, or {"plugins": [...]}.
    if isinstance(installed, dict):
        installed = installed.get("plugins", [])
    match = next(p for p in installed if isinstance(p, dict) and p.get("name") in ("leo", "leo@leos-agent"))
except StopIteration:
    print("not-found"); sys.exit()
except Exception:
    print("skip"); sys.exit()
v, repo_v = match.get("version", ""), json.load(open(sys.argv[2])).get("version", "")
print(f"ok:{v}" if v == repo_v else f"drift:{v}:{repo_v}")
PY
  )
  case "$result" in
    skip)      echo "  skip  plugin version (could not parse claude plugin list --json)" ;;
    not-found) echo "  DRIFT plugin leo not installed (claude plugin install leo@leos-agent)"; drift=1 ;;
    ok:*)      echo "  ok    plugin leo@${result#ok:}" ;;
    drift:*)   IFS=: read -r _ inst repo <<<"$result"
               echo "  DRIFT version drift: installed=$inst repo=$repo (pull + ./install.sh update)"; drift=1 ;;
    *)         echo "  skip  plugin version (unexpected output)" ;;
  esac
}
# settings.json is populated, not symlinked: repo keys are canonical, every
# machine-local key survives. Also drops any PreToolUse entry mentioning
# bash-guard.py — that hook now ships in the plugin; a leftover entry errors.
merge_settings() {
  local mode="$1" src="$REPO_DIR/install/personal-settings.json" dst="$CLAUDE_DIR/settings.json" result hook settings
  [[ -f "$src" ]] || { echo "  skip  settings.json (install/personal-settings.json not in repo)"; return; }
  command -v python3 >/dev/null || { echo "  WARN  settings.json needs python3 to merge"; drift=1; return; }
  if [[ "$mode" == "write" && -f "$dst" ]]; then
    mkdir -p "$BACKUP_DIR"; cp "$dst" "$BACKUP_DIR/settings.json"
    echo "  backed up settings.json -> $BACKUP_DIR/settings.json"
  fi
  result=$(python3 - "$src" "$dst" "$mode" <<'PY'
import json, os, sys, tempfile
src, dst, mode = sys.argv[1:4]
def merge(b, p):
    if not isinstance(b, dict) or not isinstance(p, dict):
        return p
    out = dict(b)
    for k, v in p.items():
        out[k] = merge(b[k], v) if k in b else v
    return out
def has_bash_guard(pre):
    return isinstance(pre, list) and any("bash-guard.py" in str(h.get("command", ""))
        for e in pre if isinstance(e, dict) for h in e.get("hooks", []) if isinstance(h, dict))
repo, local = json.load(open(src)), {}
if os.path.exists(dst):
    try:
        local = json.load(open(dst))
    except json.JSONDecodeError:
        print("skip:unreadable"); sys.exit()
hook = "stale" if has_bash_guard((local.get("hooks") or {}).get("PreToolUse")) else "clean"
merged = merge(local, repo)
pre = merged.get("hooks", {}).get("PreToolUse")
if isinstance(pre, list):
    merged["hooks"]["PreToolUse"] = [e for e in pre if not (isinstance(e, dict) and has_bash_guard([e]))]
if merged == local:
    state = "ok"
elif mode == "check":
    state = "drift"
else:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst) or ".", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(merged, fh, indent=2); fh.write("\n")
    os.replace(tmp, dst)
    state = "merged"
print(f"{hook}:{state}")
PY
  )
  hook="${result%%:*}"; settings="${result#*:}"
  if [[ "$mode" == "check" ]]; then
    case "$hook" in
      stale) echo "  DRIFT stale bash-guard hook (would double-fire / dangling)"; drift=1 ;;
      clean) echo "  ok    no stale bash-guard hook" ;;
      skip)  echo "  skip  bash-guard hook check (settings.json unreadable)" ;;
    esac
  fi
  case "$settings" in
    ok)         echo "  ok    settings.json (personal-settings keys present)" ;;
    merged)     echo "  merge settings.json (personal-settings applied, machine-local keys kept)" ;;
    drift)      echo "  DRIFT settings.json (personal-settings keys missing or changed)"; drift=1 ;;
    unreadable) echo "  WARN  settings.json is not valid JSON — fix it by hand; not overwriting"; drift=1 ;;
    *)          echo "  WARN  settings.json merge failed"; drift=1 ;;
  esac
}
# LEOS_AGENT_PATH is an optional override for this repo's local/ state dir;
# a value pointing elsewhere silently sends skill state to the wrong place.
leos_agent_path_check() {
  if [[ -n "${LEOS_AGENT_PATH:-}" ]]; then
    [[ "$(cd "$LEOS_AGENT_PATH" 2>/dev/null && pwd)" == "$REPO_DIR" ]] || {
      echo "WARN: LEOS_AGENT_PATH=$LEOS_AGENT_PATH does not point at this repo ($REPO_DIR)"
      echo "      Skills keep state under \$LEOS_AGENT_PATH/local — unset it or point it here."
    }
  elif [[ "$REPO_DIR" != "$HOME/.leos-agent" ]]; then
    echo "WARN: this clone is at $REPO_DIR but LEOS_AGENT_PATH is unset."
    echo "      Skills resolve state.py via \${LEOS_AGENT_PATH:-\$HOME/.leos-agent}, which is not this clone."
    echo "      Add 'export LEOS_AGENT_PATH=$REPO_DIR' to your shell profile."
  fi
}
run_migrate() {
  echo "leos-agent migrate  (repo: $REPO_DIR, target: $CLAUDE_DIR)"
  scan_symlinks migrate
  claude_md migrate
  mkdir -p "${LEOS_AGENT_PATH:-$HOME/.leos-agent}/local"
  echo "  ok    local/ dir"
  leos_agent_path_check
  if [[ "${1:-}" != "--install" ]]; then
    echo; echo "Next (not run automatically — pass --install to run them):"
    echo "  claude plugin marketplace add $REPO_DIR"
    echo "  claude plugin install leo@leos-agent"
  elif command -v claude >/dev/null; then
    echo "  run   claude plugin marketplace add $REPO_DIR"; claude plugin marketplace add "$REPO_DIR"
    echo "  run   claude plugin install leo@leos-agent"; claude plugin install leo@leos-agent
  else
    echo "WARN: claude CLI not found on PATH; run these yourself:"
    echo "  claude plugin marketplace add $REPO_DIR"
    echo "  claude plugin install leo@leos-agent"
  fi
}
run_check() {
  echo "leos-agent check  (repo: $REPO_DIR, target: $CLAUDE_DIR)"
  scan_symlinks check
  claude_md check
  check_plugin_version
  merge_settings check
  leos_agent_path_check
  exit "$drift"
}
run_update() {
  command -v claude >/dev/null || { echo "claude CLI not found on PATH — install it first, then re-run: ./install.sh update" >&2; exit 1; }
  claude plugin marketplace update leos-agent
  claude plugin update leo
}
run_mcp() {
  cat <<'EOF'
mcp: deprecated no-op — MCP servers now ship in the plugin's .mcp.json.
  1. Enable the leo plugin.
  2. Run /mcp once per machine to complete OAuth for servers that need it.
  3. export SLACK_MCP_TOKEN in your shell profile for the Slack server.
EOF
}
case "$MODE" in
  migrate)  run_migrate "${2:-}" ;;
  settings) echo "leos-agent settings  (repo: $REPO_DIR, target: $CLAUDE_DIR)"; merge_settings write ;;
  check)    run_check ;;
  update)   run_update ;;
  mcp)      run_mcp ;;
  *) echo "usage: install.sh [migrate [--install]|settings|check|update|mcp]" >&2; exit 2 ;;
esac

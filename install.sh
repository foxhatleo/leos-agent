#!/usr/bin/env bash
# Bootstrap Leo's portable Claude config: symlink ~/.claude into this repo.
#
#   ./install.sh          install or repair links (idempotent, safe to re-run)
#   ./install.sh check    report drift, change nothing (exit 1 on drift)
#   ./install.sh mcp      register MCP servers from claude/mcp.list (opt-in)
#
# Anything replaced by a link is moved to ~/.claude/backups/leos-agent-<ts>/.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
BACKUP_DIR="$CLAUDE_DIR/backups/leos-agent-$(date +%Y%m%d-%H%M%S)"
MODE="${1:-install}"

case "$MODE" in
  install|check|mcp) ;;
  *) echo "usage: install.sh [install|check|mcp]" >&2; exit 2 ;;
esac

# Symlinked wholesale. CLAUDE.md is handled via @import instead, so the local
# file keeps room for machine-specific notes.
LINKS=(settings.json agents skills hooks workflows)
IMPORT_LINE="@${REPO_DIR}/claude/CLAUDE.md"

drift=0

backup() {
  mkdir -p "$BACKUP_DIR"
  mv "$1" "$BACKUP_DIR/"
  echo "  moved existing $(basename "$1") -> $BACKUP_DIR/"
}

link_one() {
  local name="$1" src="$REPO_DIR/claude/$1" dst="$CLAUDE_DIR/$1"
  if [[ ! -e "$src" ]]; then
    echo "  skip  $name (not in repo)"
    return
  fi
  if [[ -L "$dst" && "$(readlink "$dst")" == "$src" ]]; then
    echo "  ok    $name"
    return
  fi
  if [[ "$MODE" == "check" ]]; then
    echo "  DRIFT $name (not linked to repo)"
    drift=1
    return
  fi
  [[ -e "$dst" || -L "$dst" ]] && backup "$dst"
  ln -s "$src" "$dst"
  echo "  link  $name -> $src"
}

ensure_import() {
  local md="$CLAUDE_DIR/CLAUDE.md"
  # A leos-agent import that doesn't match this clone's path is stale (repo
  # moved or was re-cloned elsewhere) and would break every session silently.
  if [[ -f "$md" ]] && grep -E '^@.*leos-agent/claude/CLAUDE\.md' "$md" | grep -qvF "$IMPORT_LINE"; then
    echo "  WARN  CLAUDE.md has a stale leos-agent import pointing elsewhere — remove it manually"
    drift=1
  fi
  if [[ -f "$md" ]] && grep -qF "$IMPORT_LINE" "$md"; then
    echo "  ok    CLAUDE.md import"
    return
  fi
  if [[ "$MODE" == "check" ]]; then
    echo "  DRIFT CLAUDE.md (missing $IMPORT_LINE)"
    drift=1
    return
  fi
  if [[ -s "$md" ]]; then
    printf '\n%s\n' "$IMPORT_LINE" >>"$md"
    echo "  add   import appended to existing CLAUDE.md"
  else
    cat >"$md" <<EOF
# Global Claude config — canonical content lives in the leos-agent repo.
$IMPORT_LINE

<!-- Machine-local notes go below this line; this file is not synced. -->
EOF
    echo "  write CLAUDE.md stub with import"
  fi
}

# Kept opt-in (not part of default install): work machines may not want
# personal MCP servers registered.
install_mcp() {
  command -v claude >/dev/null || { echo "claude CLI not found on PATH" >&2; exit 1; }
  local manifest="$REPO_DIR/claude/mcp.list"
  [[ -f "$manifest" ]] || { echo "no claude/mcp.list in repo"; return; }
  local name transport target header
  while read -r name transport target header; do
    [[ -z "$name" || "$name" == \#* ]] && continue
    if claude mcp get "$name" >/dev/null 2>&1; then
      echo "  ok    mcp:$name"
    elif [[ -n "$header" ]]; then
      claude mcp add --scope user --transport "$transport" --header "$header" "$name" "$target"
      echo "  add   mcp:$name"
    else
      claude mcp add --scope user --transport "$transport" "$name" "$target"
      echo "  add   mcp:$name"
    fi
  done <"$manifest"
  echo "OAuth servers need one-time interactive auth per machine: run /mcp inside a session."
  echo "Slack needs SLACK_MCP_TOKEN in the environment (see README > MCP servers)."
}

if [[ "$MODE" == "mcp" ]]; then
  install_mcp
  exit 0
fi

[[ "$MODE" == "check" ]] || mkdir -p "$CLAUDE_DIR"
echo "leos-agent $MODE  (repo: $REPO_DIR, target: $CLAUDE_DIR)"
for l in "${LINKS[@]}"; do link_one "$l"; done
ensure_import

if [[ "$MODE" == "check" ]]; then
  exit "$drift"
fi
echo "Done. Updating is just: git -C $REPO_DIR pull"

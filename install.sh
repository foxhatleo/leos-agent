#!/usr/bin/env bash
# Bootstrap Leo's portable Claude config: the *items* inside each content dir
# (agents, skills, hooks, workflows) are symlinked into this repo one by one,
# leaving ~/.claude/<dir> a real directory so machine-local items (e.g. a
# local-only skill) live alongside the repo's; CLAUDE.md is wired via @import;
# settings.json is populated (merged) as a real file.
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

# Wiring principle: symlinks are only for ADDITIONS, and per-item — each entry
# inside a content dir is linked individually so a local-only sibling in the
# same dir is never clobbered. Config files use an import directive where the
# format supports it (CLAUDE.md via @import) and are populated — written as
# real, merged files — where it doesn't (settings.json).
LINKS=(agents skills hooks workflows)
IMPORT_LINE="@${REPO_DIR}/claude/CLAUDE.md"

drift=0

backup() {
  mkdir -p "$BACKUP_DIR"
  mv "$1" "$BACKUP_DIR/"
  echo "  moved existing $(basename "$1") -> $BACKUP_DIR/"
}

# Link every item inside repo/claude/<name> into ~/.claude/<name>, keeping the
# destination a real directory. This never touches items the repo doesn't own,
# so a machine-local skill/agent/hook sitting in the same dir survives.
link_dir() {
  local name="$1" src="$REPO_DIR/claude/$1" dst="$CLAUDE_DIR/$1"
  if [[ ! -d "$src" ]]; then
    echo "  skip  $name (not in repo)"
    return
  fi
  # Old layout linked the whole dir. Convert that link into a real directory so
  # per-item links can live beside local items. A link into this repo carries
  # nothing of its own, so drop it; a link elsewhere is backed up.
  if [[ -L "$dst" ]]; then
    if [[ "$MODE" == "check" ]]; then
      echo "  DRIFT $name (whole-dir symlink from the old layout; re-run install.sh to convert)"
      drift=1
      return
    fi
    if [[ "$(readlink "$dst")" == "$src" ]]; then
      rm "$dst"
    else
      backup "$dst"
    fi
  fi
  [[ "$MODE" == "check" ]] || mkdir -p "$dst"
  # Glob skips dotfiles (.gitkeep, .DS_Store) — exactly the entries we don't link.
  local item base idst
  for item in "$src"/*; do
    [[ -e "$item" ]] || continue          # empty dir: glob stayed literal
    base="$(basename "$item")"
    idst="$dst/$base"
    if [[ -L "$idst" && "$(readlink "$idst")" == "$item" ]]; then
      echo "  ok    $name/$base"
      continue
    fi
    if [[ "$MODE" == "check" ]]; then
      echo "  DRIFT $name/$base (not linked to repo)"
      drift=1
      continue
    fi
    [[ -e "$idst" || -L "$idst" ]] && backup "$idst"
    ln -s "$item" "$idst"
    echo "  link  $name/$base -> $item"
  done
}

# settings.json is populated, not symlinked: repo-defined keys are canonical
# (a pull + re-run propagates them), every other machine-local key survives.
merge_settings() {
  local src="$REPO_DIR/claude/settings.json" dst="$CLAUDE_DIR/settings.json"
  [[ -f "$src" ]] || { echo "  skip  settings.json (not in repo)"; return; }
  command -v python3 >/dev/null || { echo "  WARN  settings.json needs python3 to merge"; drift=1; return; }
  if [[ -L "$dst" ]]; then
    if [[ "$MODE" == "check" ]]; then
      echo "  DRIFT settings.json (symlink from the old layout; re-run install.sh to convert)"
      drift=1
      return
    fi
    # Old layout linked this file into the repo; replace the link with a real
    # merged file. A link we didn't create is backed up rather than removed.
    if [[ "$(readlink "$dst")" == "$src" ]]; then
      rm "$dst"
    else
      backup "$dst"
    fi
  fi
  local result
  result=$(python3 - "$src" "$dst" "$MODE" <<'PY'
import json, os, sys, tempfile
src, dst, mode = sys.argv[1:4]

def deep_merge(base, patch):
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            out[k] = deep_merge(base[k], v) if k in base else v
        return out
    return patch

with open(src) as fh:
    repo = json.load(fh)
local = {}
if os.path.exists(dst):
    try:
        with open(dst) as fh:
            local = json.load(fh)
    except json.JSONDecodeError:
        print("unreadable")
        sys.exit(0)
merged = deep_merge(local, repo)  # repo keys win; machine-local extras survive
if merged == local:
    print("ok")
elif mode == "check":
    print("drift")
else:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(merged, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, dst)
    print("merged")
PY
  )
  case "$result" in
    ok)         echo "  ok    settings.json (repo keys present)" ;;
    merged)     echo "  merge settings.json (repo keys applied, machine-local keys kept)" ;;
    drift)      echo "  DRIFT settings.json (repo keys missing or changed)"; drift=1 ;;
    unreadable) echo "  WARN  settings.json is not valid JSON — fix it by hand; not overwriting"; drift=1 ;;
    *)          echo "  WARN  settings.json merge failed"; drift=1 ;;
  esac
}

ensure_import() {
  local md="$CLAUDE_DIR/CLAUDE.md"
  # A symlinked CLAUDE.md (stow/chezmoi setups, or pointing back into this
  # repo) must not be appended to — the redirect would write into the target.
  if [[ -L "$md" ]]; then
    if grep -qF "$IMPORT_LINE" "$md" 2>/dev/null; then
      echo "  ok    CLAUDE.md import (via symlink)"
    else
      echo "  WARN  CLAUDE.md is a symlink; not modifying its target — add the import there yourself: $IMPORT_LINE"
      drift=1
    fi
    return
  fi
  # A leos-agent import that doesn't match this clone's path is stale (repo
  # moved or was re-cloned elsewhere) and would break every session silently.
  # install mode repairs it in place; check mode only reports.
  if [[ -f "$md" ]] && grep -E '^@.*leos-agent/claude/CLAUDE\.md' "$md" | grep -qvF "$IMPORT_LINE"; then
    if [[ "$MODE" == "check" ]]; then
      echo "  DRIFT CLAUDE.md (stale leos-agent import pointing elsewhere)"
      drift=1
    else
      awk -v good="$IMPORT_LINE" \
        '!($0 ~ /^@.*leos-agent\/claude\/CLAUDE\.md/ && $0 != good)' \
        "$md" >"$md.tmp" && mv "$md.tmp" "$md"
      echo "  fix   removed stale leos-agent import from CLAUDE.md"
    fi
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

# LEOS_AGENT_PATH is an optional override skills use to find this repo (and
# its local/ state dir). install.sh never sets it — but a value pointing at a
# different clone would silently send all skill state elsewhere.
if [[ -n "${LEOS_AGENT_PATH:-}" ]]; then
  if [[ "$(cd "$LEOS_AGENT_PATH" 2>/dev/null && pwd)" != "$REPO_DIR" ]]; then
    echo "WARN: LEOS_AGENT_PATH=$LEOS_AGENT_PATH does not point at this repo ($REPO_DIR)"
    echo "      Skills keep state under \$LEOS_AGENT_PATH/local — unset it or point it here."
  fi
elif [[ "$REPO_DIR" != "$HOME/.leos-agent" ]]; then
  echo "WARN: this clone is at $REPO_DIR but LEOS_AGENT_PATH is unset."
  echo "      Skills resolve state.py via \${LEOS_AGENT_PATH:-\$HOME/.leos-agent}, which is not this clone."
  echo "      Add 'export LEOS_AGENT_PATH=$REPO_DIR' to your shell profile."
fi

[[ "$MODE" == "check" ]] || mkdir -p "$CLAUDE_DIR" "$REPO_DIR/local"
echo "leos-agent $MODE  (repo: $REPO_DIR, target: $CLAUDE_DIR)"
for l in "${LINKS[@]}"; do link_dir "$l"; done
merge_settings
ensure_import

if [[ "$MODE" == "check" ]]; then
  exit "$drift"
fi
echo "Done. Updating is just: git -C $REPO_DIR pull"

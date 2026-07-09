#!/usr/bin/env python3
"""PostToolUse hook: auto-detect the edited file's project toolchain, then
(1) FORMAT/FIX silently (write-mode), and (2) report remaining LINT errors back
to the agent so they get fixed in the same turn.

Tool-neutral: one script serves Claude Code, Codex, OpenCode, and Cursor. It reads
both Claude-style payloads (tool_input.file_path) and Codex apply_patch envelopes
(tool_input.command), so a single hook covers every host.

Supported (all config-gated except gofmt/rustfmt, which are universal conventions):
  JS/TS : oxfmt > biome > eslint --fix   (first config found wins; lint feedback from biome/eslint)
  Python: ruff format + ruff check --fix ; pylint -E feedback
  Go    : gofmt -w ; golangci-lint feedback
  Rust  : rustfmt ; cargo clippy feedback (incremental cache makes repeat runs fast)

Exit 0 = silent. Exit 44 = lint feedback for the agent — the host's hook wrapper maps
44 -> 2 (stderr shown to the model) and everything else -> 0, so interpreter/script
failures can never inject noise. Every tool is timeout-bounded; missing binaries
and missing configs are silent no-ops. This hook must NEVER break a turn.
"""

import json
import os
import re
import subprocess
import sys

HOME = os.path.realpath(os.path.expanduser("~"))
MAX_FEEDBACK_LINES = 30

JS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
PY_EXTS = {".py"}
GO_EXTS = {".go"}
RS_EXTS = {".rs"}

ESLINT_CONFIGS = ("eslint.config.js", "eslint.config.mjs", "eslint.config.cjs",
                  "eslint.config.ts", ".eslintrc.js", ".eslintrc.cjs",
                  ".eslintrc.json", ".eslintrc.yml", ".eslintrc.yaml", ".eslintrc")
BIOME_CONFIGS = ("biome.json", "biome.jsonc")
OXFMT_CONFIGS = (".oxfmtrc.json", ".oxfmtrc", ".oxfmtrc.jsonc")
RUFF_CONFIGS = ("ruff.toml", ".ruff.toml")
PYLINT_CONFIGS = (".pylintrc", "pylintrc")
GOLANGCI_CONFIGS = (".golangci.yml", ".golangci.yaml", ".golangci.toml", ".golangci.json")


def walk_up(start_dir):
    d = start_dir
    while True:
        yield d
        parent = os.path.dirname(d)
        if parent == d or not d.startswith(HOME):
            return
        d = parent


def find_config(start_dir, names, pyproject_key=None):
    """Nearest dir containing any of `names` (or pyproject.toml with `pyproject_key`)."""
    for d in walk_up(start_dir):
        for n in names:
            if os.path.isfile(os.path.join(d, n)):
                return d
        if pyproject_key:
            pp = os.path.join(d, "pyproject.toml")
            if os.path.isfile(pp):
                try:
                    with open(pp, errors="replace") as f:
                        if pyproject_key in f.read():
                            return d
                except Exception:
                    pass
    return None


def find_bin(start_dir, name):
    """node_modules/.bin and .venv/bin walking up, then PATH."""
    for d in walk_up(start_dir):
        for sub in (os.path.join("node_modules", ".bin"), os.path.join(".venv", "bin")):
            cand = os.path.join(d, sub, name)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    from shutil import which
    return which(name)


def run(cmd, cwd, timeout):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def condense(text, header):
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return None
    out = lines[:MAX_FEEDBACK_LINES]
    if len(lines) > MAX_FEEDBACK_LINES:
        out.append(f"... ({len(lines) - MAX_FEEDBACK_LINES} more lines)")
    return f"[format-on-edit] {header}\n" + "\n".join(out)


def handle_js(fp, fdir):
    # Nearest config wins ACROSS tool families: walk up once, checking every JS
    # tool's configs per directory (a child package's eslint config must beat a
    # monorepo-root oxfmt config). Priority within the same dir: oxfmt > biome > eslint.
    for d in walk_up(fdir):
        if any(os.path.isfile(os.path.join(d, n)) for n in OXFMT_CONFIGS):
            b = find_bin(fdir, "oxfmt")
            if b:
                run([b, fp], d, 15)
            return None
        if any(os.path.isfile(os.path.join(d, n)) for n in BIOME_CONFIGS):
            b = find_bin(fdir, "biome")
            if b:
                r = run([b, "check", "--write", fp], d, 20)
                if r and r.returncode != 0:
                    return condense(r.stdout or r.stderr, f"biome found remaining issues in {os.path.basename(fp)}:")
            return None
        if any(os.path.isfile(os.path.join(d, n)) for n in ESLINT_CONFIGS):
            b = find_bin(fdir, "eslint")
            if b:
                r = run([b, "--fix", fp], d, 30)
                if r and r.returncode != 0:
                    return condense(r.stdout or r.stderr, f"eslint found remaining issues in {os.path.basename(fp)}:")
            return None
    return None


def handle_py(fp, fdir):
    feedback = None
    d = find_config(fdir, RUFF_CONFIGS, pyproject_key="[tool.ruff")
    if d:
        b = find_bin(fdir, "ruff")
        if b:
            run([b, "format", fp], d, 15)
            r = run([b, "check", "--fix", fp], d, 15)
            if r and r.returncode != 0:
                feedback = condense(r.stdout or r.stderr, f"ruff found remaining issues in {os.path.basename(fp)}:")
    d = find_config(fdir, PYLINT_CONFIGS, pyproject_key="[tool.pylint")
    if d and not feedback:
        b = find_bin(fdir, "pylint")
        if b:
            r = run([b, "-E", "--output-format=text", fp], d, 30)
            if r and r.returncode != 0:
                feedback = condense(r.stdout, f"pylint errors in {os.path.basename(fp)}:")
    return feedback


def handle_go(fp, fdir):
    b = find_bin(fdir, "gofmt")
    if b:
        run([b, "-w", fp], fdir, 15)
    d = find_config(fdir, GOLANGCI_CONFIGS)
    if d:
        b = find_bin(fdir, "golangci-lint")
        if b:
            r = run([b, "run", fp], d, 30)
            if r and r.returncode != 0:
                return condense(r.stdout or r.stderr, f"golangci-lint issues in {os.path.basename(fp)}:")
    return None


def handle_rs(fp, fdir):
    b = find_bin(fdir, "rustfmt")
    if b:
        run([b, fp], fdir, 15)  # edition read from the crate's rustfmt.toml/Cargo.toml
    crate = find_config(fdir, ("Cargo.toml",))
    if crate:
        b = find_bin(fdir, "cargo")
        if b:
            r = run([b, "clippy", "--message-format", "short"], crate, 25)
            if r and r.returncode != 0:
                rel = os.path.relpath(fp, crate)
                mine = [l for l in (r.stderr or "").splitlines() if rel in l]
                if mine:
                    return condense("\n".join(mine), f"clippy issues in {rel}:")
    return None


# Codex sends tool_name "apply_patch" with the patch envelope in tool_input.command
# (there is no file_path field); Claude-style alias payloads keep file_path.
PATCH_PATH_RE = re.compile(r"^\*\*\* (?:Add|Update) File: (.+)$|^\*\*\* Move to: (.+)$",
                           re.MULTILINE)


def edited_paths(payload):
    ti = payload.get("tool_input") or {}
    fp = ti.get("file_path", "")
    if fp:
        return [fp]
    if payload.get("tool_name") != "apply_patch":
        return []
    cmd = ti.get("command", "")
    if isinstance(cmd, list):
        cmd = "\n".join(str(c) for c in cmd)
    if not isinstance(cmd, str):
        return []
    cwd = payload.get("cwd") or os.getcwd()
    paths = []
    for m in PATCH_PATH_RE.finditer(cmd):
        p = (m.group(1) or m.group(2)).strip()
        if not os.path.isabs(p):
            p = os.path.join(cwd, p)
        paths.append(p)
    return list(dict.fromkeys(paths))


def main():
    try:
        payload = json.load(sys.stdin)
        if payload.get("tool_name") not in ("apply_patch", "Edit", "Write",
                                            "MultiEdit", "NotebookEdit"):
            return 0
        parts = []
        for fp in edited_paths(payload):
            if not fp or not os.path.isfile(fp):
                continue
            fp = os.path.realpath(fp)  # symlinks: operate on the real target's project
            if not (fp == HOME or fp.startswith(HOME + os.sep)):
                continue  # never format outside the home tree
            ext = os.path.splitext(fp)[1].lower()
            fdir = os.path.dirname(fp)

            if ext in JS_EXTS:
                feedback = handle_js(fp, fdir)
            elif ext in PY_EXTS:
                feedback = handle_py(fp, fdir)
            elif ext in GO_EXTS:
                feedback = handle_go(fp, fdir)
            elif ext in RS_EXTS:
                feedback = handle_rs(fp, fdir)
            else:
                continue

            if feedback:
                parts.append(feedback)
        if parts:
            sys.stderr.write("\n".join(parts) + "\n")
            return 44
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())

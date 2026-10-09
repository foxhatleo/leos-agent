#!/usr/bin/env python3
"""Show or change Claude Code's `advisorModel` setting: opt-in, one key only.

  claude_advisor.py show
  claude_advisor.py set <model> [--scope user|local] [--apply]
  claude_advisor.py off [--scope user|local] [--apply]
  claude_advisor.py restore <backup>

Without --apply, `set` and `off` print the exact change and write nothing. With
it, the one key is edited in place: other keys, their order and formatting, the
newline style and the file mode are kept; a symlinked file is written through;
a concurrent edit aborts the write; and a backup lands under
${LEOS_AGENT_LOCAL_PATH:-~/.leos-agent-local}/backups first. The settings
directory is never created, and no other key is read into the output.

The user scope is the file `/advisor` itself writes; `local` is this project's
.claude/settings.local.json. The preference lives only in Claude's settings,
not in routing.json.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import install_transaction  # noqa: E402
import jsonc_edit  # noqa: E402
import settings_probe  # noqa: E402
from state import _data_root  # noqa: E402

KEY = "advisorModel"
# The aliases Claude Code accepts for an advisor, or a full Claude model ID.
MODEL_RE = re.compile(r"fable|opus|sonnet|claude-[a-z0-9][a-z0-9.-]{0,80}", re.ASCII)


class Refused(Exception):
    pass


def target(scope):
    if scope == "user":
        return Path(settings_probe.config_dir("claude")) / "settings.json"
    return Path(settings_probe.project_roots()[-1]) / ".claude" / "settings.local.json"


def edited(text, model):
    """`text` with only KEY set to `model`, or removed when model is None."""
    newline = "\r\n" if "\r\n" in text else "\n"
    if not text.strip():
        text = "{}" + newline
    try:
        before = json.loads(text)
    except ValueError as exc:
        raise Refused(f"not valid JSON ({exc}); fix the file first") from None
    if not isinstance(before, dict):
        raise Refused("top level is not a JSON object")
    try:
        _, spans, close = jsonc_edit.properties(text)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    expected = dict(before)
    if model is None:
        expected.pop(KEY, None)
    else:
        expected[KEY] = model
    if KEY in spans and not isinstance(spans[KEY][2], str):
        raise Refused(f"{KEY} holds a {type(spans[KEY][2]).__name__}, not a model name; edit it by hand")
    ordered = sorted(spans.values(), key=lambda span: span[3])
    if model is not None and KEY in spans:
        start, end = spans[KEY][:2]
        result = text[:start] + json.dumps(model) + text[end:]
    elif model is not None:
        prop = json.dumps(KEY) + ": " + json.dumps(model)
        last_end = max(span[1] for span in ordered) if ordered else None
        if ordered and "\n" not in text[text.index("{"):close]:
            result = text[:last_end] + ", " + prop + text[last_end:]  # a one-line object stays on one line
        elif ordered:
            first = ordered[0][3]
            indent = text[text.rfind("\n", 0, first) + 1:first]
            indent = indent if not indent.strip() else "  "
            result = text[:last_end] + "," + newline + indent + prop + text[last_end:]
        else:
            result = text[:close] + newline + "  " + prop + newline + text[close:]
    elif KEY in spans:
        index = [span[3] for span in ordered].index(spans[KEY][3])
        key_start, end = spans[KEY][3], spans[KEY][1]
        if index + 1 < len(ordered):
            result = text[:key_start] + text[ordered[index + 1][3]:]
        elif index:
            result = text[:ordered[index - 1][1]] + text[end:]
        else:
            result = text[:text.index("{") + 1] + text[close:]
    else:
        return text
    if json.loads(result) != expected:
        raise Refused("the edit would change more than one key; nothing was written")
    return result


def plan(scope, model):
    path = target(scope)
    if not path.parent.is_dir():
        raise Refused(f"{path.parent} does not exist; this helper never creates a settings directory")
    try:
        original = path.read_bytes() if path.exists() else b""
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused(f"{path} is not UTF-8") from None
    result = edited(text, model)
    current = json.loads(text).get(KEY) if text.strip() else None
    return path, original, result, current


def backup_path():
    root = _data_root()
    os.makedirs(root, mode=0o700, exist_ok=True)
    directory = os.path.join(root, "backups")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return Path(directory) / f"claude-advisor-{stamp}-{os.getpid()}.json"


def apply(path, original, text):
    tx = install_transaction.Transaction(backup_path())
    tx.stage(path, text.encode("utf-8"))
    for staged, (before, _, _) in tx.changes.items():
        if before != original:
            raise Refused(f"{staged} changed while it was being edited; nothing was written")
    tx.commit()
    return tx.backup if tx.changes else None


def change(args, model):
    path, original, text, current = plan(args.scope, model)
    report = {"path": str(path), "scope": args.scope, "key": KEY,
              "before": settings_probe.clean(current) if isinstance(current, str) else current,
              "after": model, "changed": text.encode("utf-8") != original, "applied": False}
    if model is not None:
        report["settings_entry"] = json.dumps(KEY) + ": " + json.dumps(model)
    if args.apply and report["changed"]:
        backup = apply(path, original, text)
        report.update(applied=True, backup=str(backup) if backup else None,
                      takes_effect="new sessions, or after /clear or /compact in a running one")
    elif report["changed"]:
        report["next"] = "rerun with --apply to write this one key"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="claude_advisor.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    for name in ("set", "off"):
        command = sub.add_parser(name)
        if name == "set":
            command.add_argument("model")
        command.add_argument("--scope", choices=("user", "local"), default="user")
        command.add_argument("--apply", action="store_true", help="write the change; without it nothing is written")
    restore = sub.add_parser("restore")
    restore.add_argument("backup")
    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            import doctor
            loaded, _ = settings_probe.load_settings(settings_probe.claude_settings_files())
            report = doctor.advisor(loaded)
        elif args.command == "restore":
            report = {"restored_files": install_transaction.rollback(Path(args.backup))}
        else:
            model = args.model.strip() if args.command == "set" else None
            if model is not None and not MODEL_RE.fullmatch(model):
                raise Refused("advisor model must be fable, opus, sonnet, or a full claude-* model ID")
            report = change(args, model)
    except (Refused, ValueError, OSError) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, indent=2))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

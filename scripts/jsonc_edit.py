"""Small positional JSONC edits: preserve unrelated keys, formatting and comments.

Only top-level string-array settings are managed. Invalid/duplicate keys or a
different setting shape are refused, never normalized into a guessed config.

Edits are minimal: an insertion goes after the last property or element in the
container's own style, and a removal deletes the element, one separating comma
and the whitespace it leaves behind, so adding then removing a value restores
the original bytes. Comment characters are never deleted or moved across a
line break.
"""
import json


def scan(text):
    """Return (cleaned, in_comment): comments blanked, plus a per-character mask."""
    out = list(text)
    mask = [False] * len(text)
    i = 0
    while i < len(text):
        if text[i] == '"':
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                elif text[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            out[i:end] = " " * (end - i)
            mask[i:end] = [True] * (end - i)
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError("unterminated JSONC comment")
            end += 2
            out[i:end] = ["\n" if c == "\n" else " " for c in text[i:end]]
            mask[i:end] = [True] * (end - i)
            i = end
        else:
            i += 1
    return "".join(out), mask


def clean(text, trailing=True):
    """`text` as strict JSON: comments blanked and, by default, trailing commas too."""
    source, _ = scan(text)
    if not trailing:
        return source
    out = list(source)
    i = 0
    while i < len(source):
        if source[i] == '"':
            i += 1
            while i < len(source) and source[i] != '"':
                i += 2 if source[i] == "\\" else 1
            i += 1
            continue
        if source[i] == ",":
            end = i + 1
            while end < len(source) and source[end].isspace():
                end += 1
            if end < len(source) and source[end] in "]}":
                out[i] = " "
        i += 1
    return "".join(out)


def properties(text):
    source = clean(text)
    decoder = json.JSONDecoder()
    root = json.loads(source)
    if not isinstance(root, dict):
        raise ValueError("configuration must be an object")
    i = source.index("{") + 1
    spans = {}
    while True:
        while source[i].isspace() or source[i] == ",":
            i += 1
        if source[i] == "}":
            return source, spans, i
        key_start = i
        key, end = decoder.raw_decode(source, i)
        if not isinstance(key, str) or key in spans:
            raise ValueError("configuration has a duplicate or invalid key")
        i = end
        while source[i].isspace():
            i += 1
        if source[i] != ":":
            raise ValueError("missing property colon")
        i += 1
        while source[i].isspace():
            i += 1
        value, end = decoder.raw_decode(source, i)
        # A fourth element, appended rather than inserted: existing callers index
        # positions 0..2 and keep working.
        spans[key] = (i, end, value, key_start)
        i = end


def _items(source, start, end):
    """(begin, stop, value) for each element of the array spanning [start, end)."""
    decoder = json.JSONDecoder()
    items = []
    i = start + 1
    while i < end - 1:
        while i < end - 1 and (source[i].isspace() or source[i] == ","):
            i += 1
        if i >= end - 1:
            break
        value, stop = decoder.raw_decode(source, i)
        items.append((i, stop, value))
        i = stop
    return items


def _next_token(source, i, limit):
    while i < limit and source[i].isspace():
        i += 1
    return i


def _previous_token(source, i, floor):
    while i > floor and source[i - 1].isspace():
        i -= 1
    return i - 1


def _blank(text, mask, lo, hi):
    """True when text[lo:hi] holds only whitespace that is not inside a comment."""
    return all(text[i].isspace() and not mask[i] for i in range(lo, hi))


def _delete(text, removed):
    """Delete `removed` indices, then drop any line the deletion left blank."""
    if not removed:
        return text
    starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
    out = []
    for n, start in enumerate(starts):
        stop = starts[n + 1] if n + 1 < len(starts) else len(text)
        line = text[start:stop]
        kept = "".join(c for i, c in enumerate(line, start) if i not in removed)
        touched = len(kept) != len(line)
        if touched and line.strip() and not kept.strip() and kept.endswith("\n"):
            continue  # the removal emptied this line; take its newline too
        out.append(kept)
    return "".join(out)


def _remove_element(text, begin, stop, open_index, close_index):
    """Remove the element spanning [begin, stop) and one separating comma.

    `open_index` and `close_index` locate the enclosing bracket or brace. A
    following comma goes with the element, with the spaces after it; for the
    last element the preceding comma goes instead, with the spaces between
    them. Only spaces and tabs are deleted explicitly: a line the removal
    empties is dropped whole, so no line break is ever taken from a // comment
    and no comment character is deleted.
    """
    punctuation, mask = scan(text)
    removed = {i for i in range(begin, stop) if not mask[i] and text[i] != "\n"}

    def spaces(lo, hi):
        if _blank(text, mask, lo, hi):
            removed.update(i for i in range(lo, hi) if text[i] in " \t")

    after = _next_token(punctuation, stop, close_index)
    before = _previous_token(punctuation, begin, open_index)
    if after < close_index and punctuation[after] == ",":
        removed.add(after)
        spaces(stop, after)
        follow = after + 1
        while follow < close_index and text[follow] in " \t":
            follow += 1
        if _next_token(punctuation, after + 1, close_index) >= close_index:
            # A trailing comma: the element was last, so close up behind it.
            spaces(before + 1, begin)
        else:
            removed.update(range(after + 1, follow))
        return _delete(text, removed)
    if before > open_index and punctuation[before] == ",":
        removed.add(before)
        spaces(before + 1, begin)
        return _delete(text, removed)
    # The only element: collapse the container when no comment is inside it.
    if not any(mask[open_index + 1:close_index]):
        removed.update(range(open_index + 1, close_index))
    return _delete(text, removed)


def _line_start(text, i):
    return text.rfind("\n", 0, i) + 1


def _indent(text, i):
    start = _line_start(text, i)
    end = start
    while end < len(text) and text[end] in " \t":
        end += 1
    return text[start:end]


def _append(text, open_index, close_index, last_stop, rendered, default_indent):
    """Insert `rendered` (a property or element) after the container's last entry.

    Multi-line containers get a new line in the last entry's indentation, with
    the comma placed right after the previous entry so a trailing // comment
    stays with it. Single-line containers stay on one line.
    """
    punctuation, mask = scan(text)
    if last_stop is None:
        if _blank(text, mask, open_index + 1, close_index):
            if not default_indent:
                return text[:open_index + 1] + rendered + text[close_index:]
            return text[:open_index + 1] + "\n" + default_indent + rendered + "\n" + text[close_index:]
        line = _line_start(text, close_index)
        if not text[line:close_index].strip():
            return text[:line] + default_indent + rendered + "\n" + text[line:]
        return text[:close_index] + " " + rendered + text[close_index:]
    anchor = _next_token(punctuation, last_stop, close_index)
    trailing = anchor < close_index and punctuation[anchor] == ","
    after = anchor + 1 if trailing else last_stop
    # The end of this line, ignoring line breaks inside a /* */ comment.
    eol = next((i for i in range(after, close_index) if text[i] == "\n" and not mask[i]), -1)
    if eol >= 0 and not punctuation[after:eol].strip():
        indent = _indent(text, last_stop)
        insert = "\n" + indent + rendered + ("," if trailing else "")
        if trailing:
            return text[:eol] + insert + text[eol:]
        return text[:last_stop] + "," + text[last_stop:eol] + insert + text[eol:]
    if trailing:
        return text[:after] + " " + rendered + "," + text[after:]
    return text[:last_stop] + ", " + rendered + text[last_stop:]


def update_array(text, key, additions=(), removals=()):
    text = text or "{}\n"
    source, spans, close = properties(text)
    additions = list(dict.fromkeys(additions))
    if key not in spans:
        if not additions:
            return text
        open_index = source.index("{")
        last = max((span[1] for span in spans.values()), default=None)
        rendered = json.dumps(key) + ": " + json.dumps(additions)
        result = _append(text, open_index, close, last, rendered, "  ")
        properties(result)
        return result
    values = spans[key][2]
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ValueError(f"{key} must be an array of strings")
    wanted = [v for v in values if v not in removals]
    additions = [v for v in additions if v not in wanted]
    if wanted == values and not additions:
        return text
    body = text
    # One element at a time, re-reading positions after each removal.
    while True:
        source, spans, _ = properties(body)
        start, end = spans[key][:2]
        doomed = [item for item in _items(source, start, end) if item[2] in removals]
        if not doomed:
            break
        begin, stop, _ = doomed[0]
        body = _remove_element(body, begin, stop, start, end - 1)
    for value in additions:
        source, spans, _ = properties(body)
        start, end = spans[key][:2]
        items = _items(source, start, end)
        body = _append(body, start, end - 1, items[-1][1] if items else None, json.dumps(value), "")
    properties(body)
    return body


def drop_empty_array(text, key):
    """Remove `key` entirely when its array has no values left.

    The caller must establish that it created this key. Nonempty arrays are
    untouched, and comments survive removing the property syntax.
    """
    source, spans, close = properties(text)
    if key not in spans or spans[key][2] != []:
        return text
    _, end, _, key_start = spans[key]
    result = _remove_element(text, key_start, end, source.index("{"), close)
    properties(result)
    return result


def is_empty(text):
    """True for an object with no properties and no comments: nothing a user wrote."""
    _, spans, _ = properties(text)
    _, mask = scan(text)
    return not spans and not any(mask)

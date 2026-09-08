"""Small positional JSONC edits: preserve unrelated keys, formatting and comments.

Only top-level string-array settings are managed. Invalid/duplicate keys or a
different setting shape are refused, never normalized into a guessed config.
"""
import json


def clean(text, trailing=True):
    out = list(text)
    commas = []
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
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError("unterminated JSONC comment")
            end += 2
            out[i:end] = ["\n" if c == "\n" else " " for c in text[i:end]]
            i = end
        else:
            if text[i] == ",":
                commas.append(i)
            i += 1
    for i in commas if trailing else []:
        end = i + 1
        while end < len(out) and out[end].isspace():
            end += 1
        if end < len(out) and out[end] in "]}":
            out[i] = " "
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


def update_array(text, key, additions=(), removals=()):
    text = text or "{}\n"
    source, spans, close = properties(text)
    additions = list(dict.fromkeys(additions))
    if key not in spans:
        if not additions:
            return text
        prefix = "," if spans and source[:close].rstrip()[-1] != "," else ""
        # A trailing comma is whitespace in clean(); inspect the original
        # punctuation while excluding comments, via a harmless sentinel value.
        if spans:
            last_end = max(span[1] for span in spans.values())
            if "," in source[last_end:close] or "," in clean(text[last_end:close] + "0"):
                prefix = ""
        inserted = prefix + "\n  " + json.dumps(key) + ": " + json.dumps(additions) + "\n"
        result = text[:close] + inserted + text[close:]
        properties(result)
        return result
    start, end, values = spans[key][:3]
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ValueError(f"{key} must be an array of strings")
    wanted = [v for v in values if v not in removals]
    additions = [v for v in additions if v not in wanted]
    if wanted == values and not additions:
        return text
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
    erase = set()
    punctuation = clean(text, trailing=False)
    previous = start + 1
    kept = []
    for begin, stop, value in items:
        erase.update(i for i in range(previous, begin) if punctuation[i] == ",")
        if value in removals:
            erase.update(range(begin, stop))
        else:
            kept.append((begin, stop))
        previous = stop
    erase.update(i for i in range(previous, end - 1) if punctuation[i] == ",")
    separators = {stop for _, stop in kept[:-1]}
    # Rebuild only commas between retained strings, keeping all comments.
    body = "".join(("," if i in separators else "") + (" " if i in erase else c)
                   for i, c in enumerate(text))
    if additions:
        _, new_spans, _ = properties(body)
        array_start, array_end, remaining = new_spans[key][:3]
        interior = clean(body[array_start + 1:array_end - 1] + "0")[:-1].rstrip()
        prefix = "," if remaining and not interior.endswith(",") else ""
        body = body[:array_end - 1] + prefix + "\n    " + ", ".join(json.dumps(v) for v in additions) + "\n  " + body[array_end - 1:]
    properties(body)
    return body


def drop_empty_array(text, key):
    """Remove `key` entirely when its array has no values left.

    Uninstall should not leave behind a key the installer invented. A key that
    still holds anything is never touched, so a list the user already had --
    emptied by them, or holding entries we never wrote -- keeps its place.
    """
    source, spans, close = properties(text)
    if key not in spans or spans[key][2] != []:
        return text
    _, end, _, key_start = spans[key]
    # Take one adjacent comma with the property, preferring the one before it so
    # a trailing comment after the previous value is not orphaned.
    punctuation = clean(text, trailing=False)
    start = key_start
    while start > 0 and punctuation[start - 1].isspace():
        start -= 1
    if start > 0 and punctuation[start - 1] == ",":
        start -= 1
    else:
        after = end
        while after < close and punctuation[after].isspace():
            after += 1
        if after < close and punctuation[after] == ",":
            end = after + 1
    result = text[:start] + text[end:]
    properties(result)
    return result

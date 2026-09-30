"""
title: Leo LaTeX
author: leo
version: 1.0.0
description: Rewrites math delimiters into the one canonical style that KaTeX renders reliably (outlet + stream).
"""

# Self-contained on purpose: Open WebUI stores this file as a function, and the
# ingest job imports normalize_math() from it for book chapters (§8, §9.3).

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

INLINE_STYLES = ("dollar", "paren", "double")
_ENV = re.compile(r"\\begin\{(align\*?|equation\*?|gather\*?)\}")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LABEL = re.compile(r"[ \t]*\\label\{[^{}]*\}")
_MAX_DISPLAY_LINES = 60


# ── inline pieces ───────────────────────────────────────────────────────────


def _inline(body: str, style: str) -> str:
    b = body.strip()
    if not b:
        return body
    if style == "paren":
        return f"\\({b}\\)"
    if style == "double":
        return f"$${b}$$"
    return f"${b}$"


def _display(body: str) -> str:
    b = _LABEL.sub("", body)
    b = "\n".join(line.rstrip() for line in b.strip().splitlines() if line.strip())
    return f"\n\n$$\n{b}\n$$\n\n" if b else ""


def _map_env(env: str, body: str) -> str:
    base = env.rstrip("*")
    if base == "align":
        return f"\\begin{{aligned}}{body}\\end{{aligned}}"
    if base == "gather":
        return f"\\begin{{gathered}}{body}\\end{{gathered}}"
    return body  # equation: the display block is enough


def _inline_or_display(line: str, start: int, end: int, body: str, style: str) -> str:
    """In 'double' style a formula alone on its line would read as display later; make it display now."""
    if style == "double" and not line[:start].strip() and not line[end:].strip():
        return _display(body)
    return _inline(body, style)


_UNKNOWN = -2  # "cannot decide yet" (incomplete line)


def _inline_close(line: str, i: int, complete: bool) -> int | None:
    """Closing `$` index for a single-`$` opener at i (pandoc rules); None = not math; _UNKNOWN = wait."""
    n = len(line)
    if i + 1 >= n:
        return None if complete else _UNKNOWN
    if line[i + 1].isspace() or line[i + 1] == "$":
        return None
    k = i + 1
    tick = line.find("`", k)  # math never spans into a code span
    while True:
        k = line.find("$", k)
        if tick != -1 and (k == -1 or tick < k):
            return None
        if k == -1:
            return None if complete else _UNKNOWN
        if line[k - 1] == "\\":
            k += 1
            continue
        if line.startswith("$$", k):
            k += 2
            continue
        if not complete and k + 1 >= n:
            return _UNKNOWN  # the next char decides (a digit would make it invalid)
        if not line[k - 1].isspace() and not (k + 1 < n and line[k + 1].isdigit()):
            body = line[i + 1 : k]
            # Currency guard: "$5 and the total $x$" is prose, not math.
            if line[i + 1].isdigit() and "\\" not in body and re.search(r"[A-Za-z]{3,}", body):
                return None
            return k
        k += 1


def _scan(line: str, style: str, complete: bool) -> str:
    """Normalize a line (no trailing newline) left to right.

    With complete=False (a line still streaming in) it stops before the first
    token whose output could still change, so its result is always a prefix of
    the result for the finished line.
    """
    out: list[str] = []

    def put_math(tok: str) -> None:
        # "$a$$b$" would re-read as a $$ delimiter: keep adjacent formulas apart.
        if tok.startswith("$") and out and out[-1].endswith("$") and not out[-1].endswith("\\$"):
            out.append(" ")
        out.append(tok)

    i, n = 0, len(line)
    if not complete and line.lstrip().startswith("<"):
        return ""  # may become a protected <details> block
    while i < n:
        c = line[i]
        if c == "\\":
            if i + 1 >= n and not complete:
                break
            if line.startswith("\\$", i):
                out.append("\\$")
                i += 2
                continue
            if line.startswith("\\(", i):
                j = line.find("\\)", i + 2)
                if j != -1:
                    if style == "double" and not line[:i].strip():
                        if not complete:
                            break  # alone on the line → display (see _inline_or_display)
                        put_math(_inline_or_display(line, i, j + 2, line[i + 2 : j], style))
                    else:
                        put_math(_inline(line[i + 2 : j], style))
                    i = j + 2
                    continue
                if not complete:
                    break
            if line.startswith("\\[", i):
                if not complete:
                    break  # display output depends on the rest of the line
                j = line.find("\\]", i + 2)
                if j != -1:
                    out.append(_display(line[i + 2 : j]))
                    i = j + 2
                    continue
            m = _ENV.match(line, i)
            if m or (not complete and "\\begin{".startswith(line[i : i + 7]) and n - i < 17):
                if not complete:
                    break
                assert m is not None
                end = f"\\end{{{m.group(1)}}}"
                j = line.find(end, m.end())
                if j != -1:
                    out.append(_display(_map_env(m.group(1), line[m.end() : j])))
                    i = j + len(end)
                    continue
            out.append(line[i : i + 2])
            i += 2
            continue
        if c == "`":
            run = len(line[i:]) - len(line[i:].lstrip("`"))
            if not complete and i + run >= n:
                break  # the run may still grow
            j = line.find("`" * run, i + run)
            if j == -1:
                if not complete:
                    break
                out.append(line[i:])
                break
            if not complete and j + run >= n:
                break  # closing run may still grow
            out.append(line[i : j + run])
            i = j + run
            continue
        if c == "$":
            if not complete and i + 1 >= n:
                break
            if line.startswith("$$", i):
                j = line.find("$$", i + 2)
                if j != -1:
                    body = line[i + 2 : j]
                    before = bool(line[:i].strip())
                    if not complete and not before:
                        break  # display vs inline depends on the rest of the line
                    alone = not before and not line[j + 2 :].strip()
                    out.append(_display(body) if alone else _inline(body, style))
                    i = j + 2
                    continue
                if not complete:
                    break
                out.append("$$")
                i += 2
                continue
            j = _inline_close(line, i, complete)
            if j == _UNKNOWN:
                break
            if j is not None:
                if style == "double" and not line[:i].strip():
                    if not complete:
                        break
                    put_math(_inline_or_display(line, i, j + 1, line[i + 1 : j], style))
                else:
                    put_math(_inline(line[i + 1 : j], style))
                i = j + 1
                continue
            out.append("\\$" if i + 1 < n and line[i + 1].isdigit() else "$")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def normalize_line(line: str, style: str) -> str:
    return _scan(line, style, complete=True)


# ── block units ─────────────────────────────────────────────────────────────


def _lines(text: str, pos: int) -> list[tuple[int, int, bool]]:
    """(start, end_without_newline, has_newline) for each line from pos."""
    res = []
    while pos < len(text):
        nl = text.find("\n", pos)
        if nl == -1:
            res.append((pos, len(text), False))
            break
        res.append((pos, nl, True))
        pos = nl + 1
    return res


def _display_opener(line: str) -> tuple[str, int] | None:
    """First unclosed display opener on the line, using exactly _scan's tokenization: (closer, index)."""
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == "\\":
            if line.startswith("\\$", i):
                i += 2
                continue
            if line.startswith("\\(", i):
                j = line.find("\\)", i + 2)
                i = j + 2 if j != -1 else i + 2
                continue
            if line.startswith("\\[", i):
                j = line.find("\\]", i + 2)
                if j == -1:
                    return "\\]", i
                i = j + 2
                continue
            m = _ENV.match(line, i)
            if m:
                end = f"\\end{{{m.group(1)}}}"
                j = line.find(end, m.end())
                if j == -1:
                    return end, i
                i = j + len(end)
                continue
            i += 2
            continue
        if c == "`":
            run = len(line[i:]) - len(line[i:].lstrip("`"))
            j = line.find("`" * run, i + run)
            if j == -1:
                return None  # rest of the line is literal
            i = j + run
            continue
        if c == "$":
            if line.startswith("$$", i):
                j = line.find("$$", i + 2)
                if j == -1:
                    return "$$", i
                i = j + 2
                continue
            j = _inline_close(line, i, True)
            i = j + 1 if isinstance(j, int) and j >= 0 else i + 1
            continue
        i += 1
    return None


def next_unit(text: str, final: bool) -> tuple[str, int] | None:
    """Split off the next unit from the start of text: (kind, end). None = need more text."""
    lines = _lines(text, 0)
    if not lines:
        return None
    s0, e0, nl0 = lines[0]
    if not nl0 and not final:
        return None
    first = text[s0:e0]

    def end_of(idx: int) -> int:
        s, e, nl = lines[idx]
        return e + 1 if nl else e

    m = _FENCE.match(first)
    if m:
        ch, ln = m.group(1)[0], len(m.group(1))
        close = re.compile(rf"^ {{0,3}}{re.escape(ch)}{{{ln},}}\s*$")
        for idx in range(1, len(lines)):
            s, e, nl = lines[idx]
            if close.match(text[s:e]) and (nl or final):
                return "code", end_of(idx)
        return ("code", len(text)) if final else None

    if first.lstrip().startswith("<details"):
        for idx in range(len(lines)):
            s, e, nl = lines[idx]
            if "</details>" in text[s:e] and (nl or final):
                return "html", end_of(idx)
        return ("html", len(text)) if final else None

    op = _display_opener(first)
    if op:
        closer, _ = op
        for idx in range(1, min(len(lines), _MAX_DISPLAY_LINES)):
            s, e, nl = lines[idx]
            ln = text[s:e]
            if not nl and not final and closer not in ln:
                return None  # the line is still arriving
            if not ln.strip() or _FENCE.match(ln):
                return "line", end_of(0)  # abort: blank line / fence before the closer
            if closer in ln:
                if not nl and not final:
                    return None
                return "display", end_of(idx)
        if len(lines) >= _MAX_DISPLAY_LINES or final:
            return "line", end_of(0)
        return None
    return "line", end_of(0)


def normalize_unit(kind: str, unit: str, style: str) -> str:
    if kind in ("code", "html"):
        return unit
    nl = "\n" if unit.endswith("\n") else ""
    body = unit[:-1] if nl else unit
    if kind == "line":
        return normalize_line(body, style) + nl
    # display: prefix + opener … closer + suffix
    first, _, rest = body.partition("\n")
    op = _display_opener(first)
    if not op:
        return normalize_line(body, style) + nl
    closer, k = op
    opener_len = 2 if closer in ("$$", "\\]") else len(re.match(_ENV, first[k:]).group(0))  # type: ignore[union-attr]
    env = _ENV.match(first, k)
    inner_all = first[k + opener_len :] + "\n" + rest
    c = inner_all.find(closer, inner_all.rfind("\n") + 1)  # first closer on the last line
    inner, suffix = inner_all[:c], inner_all[c + len(closer) :]
    if env:
        inner = _map_env(env.group(1), inner)
    return normalize_line(first[:k], style) + _display(inner) + normalize_line(suffix, style) + nl


def _partial(buf: str, style: str) -> str:
    """Normalized text of an incomplete unit that can already be shown (never changes later)."""
    if not buf:
        return ""
    first_nl = buf.find("\n")
    first = buf if first_nl == -1 else buf[:first_nl]
    if first_nl != -1 and _FENCE.match(first):
        return buf  # inside a code block nothing is rewritten
    if first_nl == -1 and (_FENCE.match(first) or (first.lstrip(" ").startswith(("`", "~")) and len(first) < 7)):
        return ""  # is, or could still become, a fence line
    return _scan(first, style, complete=False)


class StreamNormalizer:
    """Incremental normalize_math: feed(chunk) → text safe to show now; flush() → the rest.

    Holds back only the current unit from its first delimiter onwards, so the
    concatenated output equals normalize_math() of the whole text.
    """

    def __init__(self, style: str = "dollar") -> None:
        self.style = style if style in INLINE_STYLES else "dollar"
        self.buf = ""
        self.emitted = 0  # normalized chars of the current (incomplete) unit already emitted
        self.tail = ""  # last two emitted chars, for newline collapsing
        self.started = False

    def _join(self, piece: str, kind: str) -> str:
        if kind not in ("code", "html"):
            piece = re.sub(r"\n{3,}", "\n\n", piece)
        lead = len(piece) - len(piece.lstrip("\n"))
        if lead:
            have = len(self.tail) - len(self.tail.rstrip("\n"))
            allowed = max(0, 2 - have) if self.started else 0
            piece = piece[lead - min(lead, allowed) :]
        return piece

    def _emit(self, s: str) -> str:
        if s:
            self.started = True
            self.tail = (self.tail + s)[-2:]
        return s

    def _drain(self, final: bool) -> str:
        out = []
        while self.buf:
            r = next_unit(self.buf, final)
            if r is None:
                break
            kind, end = r
            if end <= 0:
                break
            piece = self._join(normalize_unit(kind, self.buf[:end], self.style), kind)
            out.append(self._emit(piece[self.emitted :]))
            self.emitted = 0
            self.buf = self.buf[end:]
        if not final:
            part = _partial(self.buf, self.style)
            if len(part) > self.emitted:
                out.append(self._emit(part[self.emitted :]))
                self.emitted = len(part)
        return "".join(out)

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        return self._drain(final=False)

    def flush(self) -> str:
        return self._drain(final=True)


def normalize_math(text: str, inline_style: str = "dollar") -> str:
    """Canonicalize math delimiters for KaTeX. Idempotent; code blocks and inline code untouched."""
    sn = StreamNormalizer(inline_style)
    return sn.feed(text) + sn.flush()


# ── Open WebUI filter ───────────────────────────────────────────────────────


class Filter:
    class Valves(BaseModel):
        enabled: bool = True
        inline_style: str = Field(default="dollar", description="dollar | paren | double")
        stream_normalize: bool = True
        priority: int = Field(default=100, description="High = runs after other filters")

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._streams: dict[str, StreamNormalizer] = {}

    def _style(self) -> str:
        return self.valves.inline_style if self.valves.inline_style in INLINE_STYLES else "dollar"

    def stream(self, event: dict) -> dict:
        try:
            if not (self.valves.enabled and self.valves.stream_normalize):
                return event
            choices = event.get("choices") or []
            if not choices:
                return event
            key = str(event.get("id") or "default")
            choice = choices[0]
            delta = choice.get("delta") or {}
            sn = self._streams.get(key)
            if sn is None:
                if len(self._streams) > 64:  # abandoned streams
                    self._streams.clear()
                sn = self._streams[key] = StreamNormalizer(self._style())
            content = delta.get("content")
            text = sn.feed(content) if isinstance(content, str) else ""
            if choice.get("finish_reason"):
                text += sn.flush()
                self._streams.pop(key, None)
            if isinstance(content, str) or text:
                delta["content"] = text
                choice["delta"] = delta
            return event
        except Exception:  # never break a response
            log.exception("leo latex stream normalization failed")
            return event

    def outlet(self, body: dict, __user__: dict | None = None) -> dict:
        try:
            if not self.valves.enabled:
                return body
            for m in reversed(body.get("messages") or []):
                if m.get("role") == "assistant":
                    if isinstance(m.get("content"), str) and m["content"]:
                        m["content"] = normalize_math(m["content"], self._style())
                    break
        except Exception:
            log.exception("leo latex outlet normalization failed")
        return body


def _selftest(sample: Any = None) -> str:  # handy for manual checks: python -c "..."
    return normalize_math(sample or "The sum \\(a+b\\) and \\[x^2\\] costs $5.")

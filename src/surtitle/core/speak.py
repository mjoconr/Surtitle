"""The speak layer.

The agent writes plain text sprinkled with two tags:

* ``<say>...</say>`` — spoken aloud, and also shown in the transcript.
* ``<display>...</display>`` — shown in the transcript only. Use it for
  tables, code, long lists and file listings that would be tedious to hear.

Everything outside a tag is display text. Spoken text is emitted in *chunks*
as soon as a sentence boundary is complete, so text-to-speech can start before
the model has finished the paragraph. This is what keeps the conversation from
stalling on a slow generation.

The parser is incremental: it is fed arbitrary token deltas, and tags may be
split across deltas. It also repairs output from models that forget the tags,
by falling back to speaking the prose text (stripping fenced code blocks,
tables and paths), so the user is never met with silence.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["Chunk", "ChunkKind", "SpeakParser", "repair_fallback", "strip_for_speech"]


class ChunkKind(StrEnum):
    """What the parser decided a piece of text is."""

    SAY = "say"
    DISPLAY = "display"


@dataclass(slots=True, frozen=True)
class Chunk:
    """A unit of parsed output.

    ``final`` marks the last chunk for a given ``kind`` in this call, which lets
    the synthesizer flush a trailing fragment without waiting for punctuation.
    """

    kind: ChunkKind
    text: str
    final: bool = False


# Tags the model is allowed to emit.
_OPEN_SAY = "<say>"
_CLOSE_SAY = "</say>"
_OPEN_DISPLAY = "<display>"
_CLOSE_DISPLAY = "</display>"
_KNOWN_TAGS = (_OPEN_SAY, _CLOSE_SAY, _OPEN_DISPLAY, _CLOSE_DISPLAY)

# Characters that end a speakable sentence. A closing quote/bracket after the
# terminator belongs to the same sentence.
# The quote characters are deliberate: they preserve sentence-final punctuation.
_SENTENCE_END = re.compile(
    r"""[.!?…](?:["'”’)\]]*)(?=\s|$)"""  # noqa: RUF001
)
_CLAUSE_END = re.compile(r"[,;:](?=\s|$)")

# If a spoken sentence is getting long, cut it at a clause rather than making the
# user wait for a full stop. Tuned by ear; both are character counts.
_CLAUSE_SPLIT_THRESHOLD = 120
_HARD_SPLIT_THRESHOLD = 300
# Do not speak tiny fragments; they sound like stutter.
_MIN_SPEAKABLE = 2
# Keep no more than this much trailing text buffered while waiting for a boundary.
_TAIL_KEEP = 8

_FENCED_CODE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MARKDOWN_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_MARKDOWN_RULE = re.compile(r"^\s*([-*_])\s*(?:\1\s*){2,}$", re.MULTILINE)
_HEADING_PREFIX = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET_PREFIX = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_MULTI_NEWLINE = re.compile(r"\n{2,}")
_WHITESPACE = re.compile(r"[ \t]+")
# A line whose only content is a short heading, as in "## Summary".
_HEADING_LINE = re.compile(r"^\s{0,3}#{1,6}\s+(\S[^\n]*?)\s*$")
_URLISH = re.compile(r"https?://\S+")
# File paths read terribly aloud ("output slash summary dot xlsx"). These
# patterns must never swallow trailing punctuation, or the sentence loses its
# full stop; trailing punctuation is re-attached by _drop_paths.
_PATH_PATTERNS = (
    # Windows drive paths, including UNC.
    re.compile(r"[A-Za-z]:[\\/][^\s,;:)\]]+"),
    # Absolute POSIX paths: exactly one or more leading slashes, then segments.
    re.compile(r"/{1,2}[\w.@+-]+(?:/[\w.@+-]+)+/?"),
    # Directory-relative paths such as output/summary.xlsx.
    re.compile(r"\w[\w.@+-]*(?:/[\w.@+-]+)+/?(?=[\s.,;:!?)\]]|$)"),
    # Bare filenames that read badly: report.xlsx, main.py
    re.compile(
        r"\b[\w@+-]+\.(?:py|js|ts|json|md|txt|csv|tsv|xlsx|xls|pdf|docx|html|css|toml|yaml|yml|sh|ps1|bat|ini|log|zip|png|jpg|svg)\b"
    ),
)


def _drop_paths(text: str) -> str:
    """Remove file paths and URLs, preserving the punctuation around them."""
    text = _URLISH.sub("", text)
    for pattern in _PATH_PATTERNS:
        text = pattern.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


def strip_for_speech(text: str) -> str:
    """Reduce formatted text to something that sounds natural when read aloud.

    Drops code blocks, tables and rules, unwraps links and inline code, and
    removes markdown emphasis. Whitespace is collapsed to single spaces because
    every TTS engine reads newlines as pauses at best and garbage at worst.
    """
    if not text:
        return ""

    out = _FENCED_CODE.sub(" ", text)
    out = _MARKDOWN_LINK.sub(r"\1", out)
    out = _MARKDOWN_TABLE_ROW.sub(" ", out)
    out = _MARKDOWN_RULE.sub(" ", out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _BULLET_PREFIX.sub("", out)
    # Handle headings line-wise: a bare heading line would otherwise run into
    # the following paragraph with no pause, so give it a full stop of its own.
    out = "\n".join(
        f"{heading_match.group(1)}." if (heading_match := _HEADING_LINE.match(line)) else line
        for line in out.splitlines()
    )
    # Repeatedly unwrap nesting such as **`x`**.
    for _ in range(3):
        unwrapped = _EMPHASIS.sub(r"\2", out)
        if unwrapped == out:
            break
        out = unwrapped
    out = _drop_paths(out)
    out = _MULTI_NEWLINE.sub(" ", out)
    out = out.replace("\n", " ")
    out = _WHITESPACE.sub(" ", out)
    return out.strip()


@dataclass
class SpeakParser:
    """Incrementally split a token stream into spoken and displayed chunks.

    Feed deltas to :meth:`feed`, which yields zero or more :class:`Chunk`
    objects. Call :meth:`finish` when the stream ends to flush anything left and
    guarantee a spoken statement for the turn.

    ``spoken_anything`` and ``displayed_anything`` report what the model
    actually produced, which :func:`repair_fallback` uses to decide whether the
    agent honoured the tagging contract.
    """

    _pending: str = ""
    _mode: ChunkKind = ChunkKind.DISPLAY
    _say_buffer: str = ""
    _display_buffer: str = ""
    _saw_say_tag: bool = False
    _spoken_chars: int = 0
    _displayed_chars: int = 0
    _finished: bool = False

    # Extra: the raw text the model produced, kept for repair decisions.
    raw_parts: list[str] = field(default_factory=list)

    @property
    def saw_say_tag(self) -> bool:
        """True when the model explicitly used ``<say>`` at least once."""
        return self._saw_say_tag

    @property
    def spoken_chars(self) -> int:
        """Count of characters emitted for speech (post-stripping)."""
        return self._spoken_chars

    @property
    def displayed_chars(self) -> int:
        """Count of characters emitted for display."""
        return self._displayed_chars

    # --- feeding ---------------------------------------------------------
    def feed(self, delta: str) -> list[Chunk]:
        """Consume a token delta and return any completed chunks."""
        if not delta or self._finished:
            return []
        self.raw_parts.append(delta)
        self._pending += delta
        chunks = list(self._drain_tags())
        chunks.extend(self._emit_pending(final=False))
        return chunks

    def finish(self) -> list[Chunk]:
        """Flush everything left, guaranteeing the turn has been handled."""
        if self._finished:
            return []
        self._finished = True
        chunks: list[Chunk] = []

        # A tag that never closed is treated as literal text.
        leftover = self._pending
        self._pending = ""
        if leftover:
            if self._mode is ChunkKind.SAY:
                self._say_buffer += leftover
            else:
                self._display_buffer += leftover

        if self._say_buffer.strip():
            chunks.append(Chunk(ChunkKind.SAY, strip_for_speech(self._say_buffer), final=True))
            self._say_buffer = ""

        if self._display_buffer.strip():
            chunks.append(Chunk(ChunkKind.DISPLAY, self._display_buffer, final=True))
            self._display_buffer = ""

        return self._record(chunks)

    # --- internals -------------------------------------------------------
    def _drain_tags(self) -> Iterator[Chunk]:
        """Process any complete tags present in the pending buffer."""
        while True:
            index = self._pending.find("<")
            if index == -1:
                # No tag left: the whole remainder is plain text.
                if self._pending:
                    self._append_text(self._pending)
                    self._pending = ""
                yield from self._emit_pending(final=False)
                return

            # Emit plain text preceding the tag.
            if index > 0:
                self._append_text(self._pending[:index])
                self._pending = self._pending[index:]
                yield from self._emit_pending(final=False)

            matched = next((tag for tag in _KNOWN_TAGS if self._pending.startswith(tag)), None)
            if matched is not None:
                self._pending = self._pending[len(matched) :]
                yield from self._apply_tag(matched)
                continue

            if self._is_partial_tag(self._pending):
                # Might complete on the next delta; wait.
                return

            # A bare '<' that cannot become a known tag: literal text.
            self._append_text("<")
            self._pending = self._pending[1:]
            yield from self._emit_pending(final=False)

    @staticmethod
    def _is_partial_tag(buffer: str) -> bool:
        """True when ``buffer`` is a prefix of some tag, so we should wait."""
        if not buffer.startswith("<"):
            return False
        if len(buffer) > 10:  # len("</display>")
            return False
        return any(tag.startswith(buffer) for tag in _KNOWN_TAGS)

    def _apply_tag(self, tag: str) -> Iterator[Chunk]:
        if tag == _OPEN_SAY:
            # Flush pending display before switching modes.
            yield from self._emit_pending(final=True)
            self._mode = ChunkKind.SAY
            self._saw_say_tag = True
        elif tag in (_OPEN_DISPLAY, _CLOSE_SAY, _CLOSE_DISPLAY):
            # Every other tag returns us to display mode.
            yield from self._emit_pending(final=True)
            self._mode = ChunkKind.DISPLAY

    def _append_text(self, text: str) -> None:
        if self._mode is ChunkKind.SAY:
            self._say_buffer += text
        else:
            self._display_buffer += text

    def _emit_pending(self, *, final: bool) -> list[Chunk]:
        """Emit whatever is speakable or displayable right now."""
        if self._mode is ChunkKind.SAY:
            return self._emit_say(final=final)
        return self._emit_display(final=final)

    def _emit_display(self, *, final: bool) -> list[Chunk]:
        buffer = self._display_buffer
        if not buffer:
            return []
        if final:
            self._display_buffer = ""
            return self._record([Chunk(ChunkKind.DISPLAY, buffer, final=True)]) if buffer else []

        # Stream display text promptly so the UI feels live, but hold back a
        # short tail so a tag split across deltas is not shown as literal text.
        safe_length = len(buffer) - _TAIL_KEEP
        if safe_length <= 0:
            return []
        cut = buffer.rfind(" ", 0, safe_length)
        if cut <= 0:
            cut = safe_length
        emit, self._display_buffer = buffer[:cut], buffer[cut:]
        return self._record([Chunk(ChunkKind.DISPLAY, emit, final=False)])

    def _emit_say(self, *, final: bool) -> list[Chunk]:
        buffer = self._say_buffer
        if not buffer.strip():
            self._say_buffer = buffer
            return []

        if final:
            self._say_buffer = ""
            spoken = strip_for_speech(buffer)
            if not spoken:
                return []
            return self._record([Chunk(ChunkKind.SAY, spoken, final=True)])

        # A whole block can arrive in one delta when the model emits few tokens
        # or the stream buffers, so drain every complete sentence rather than
        # only the first. The trailing incomplete fragment stays buffered.
        chunks: list[Chunk] = []
        while True:
            cut = self._find_boundary(self._say_buffer)
            if cut is None:
                break
            head, self._say_buffer = self._say_buffer[:cut], self._say_buffer[cut:]
            spoken = strip_for_speech(head)
            if not spoken:
                continue
            if len(spoken) < _MIN_SPEAKABLE:
                # Too short to be worth a separate utterance; put it back.
                self._say_buffer = head + self._say_buffer
                break
            chunks.append(Chunk(ChunkKind.SAY, spoken, final=False))
        return self._record(chunks)

    @staticmethod
    def _find_boundary(buffer: str) -> int | None:
        """Return an index to cut the spoken buffer at, or ``None`` to wait.

        A complete sentence is spoken as soon as it is visible, even when it
        ends exactly at the end of the buffer: waiting would add latency for no
        benefit, since a later delta simply starts a fresh utterance. The
        *first* boundary is returned, so callers that loop drain one sentence
        per pass and speech begins on the earliest complete thought.
        """
        for match in _SENTENCE_END.finditer(buffer):
            return match.end()

        # No sentence end yet: only split early if the user is waiting.
        if len(buffer) >= _HARD_SPLIT_THRESHOLD:
            clause = None
            for match in _CLAUSE_END.finditer(buffer):
                if match.end() <= len(buffer) - _TAIL_KEEP:
                    clause = match.end()
            if clause is not None:
                return clause
            return len(buffer) - _TAIL_KEEP
        if len(buffer) >= _CLAUSE_SPLIT_THRESHOLD:
            clause = None
            for match in _CLAUSE_END.finditer(buffer):
                clause = match.end()
            if clause is not None:
                return clause
        return None

    def _record(self, chunks: list[Chunk]) -> list[Chunk]:
        for chunk in chunks:
            if chunk.kind is ChunkKind.SAY:
                self._spoken_chars += len(chunk.text)
            else:
                self._displayed_chars += len(chunk.text)
        return chunks

    # --- convenience -----------------------------------------------------
    def raw_text(self) -> str:
        """The full text the model produced, tags included."""
        return "".join(self.raw_parts)

    def clean_text(self) -> str:
        """The model's output with all tags removed, for transcript storage."""
        text = "".join(self.raw_parts)
        for tag in _KNOWN_TAGS:
            text = text.replace(tag, " ")
        return text.strip()


def repair_fallback(text: str, *, max_chars: int = 240) -> str:
    """Build something worth saying from an untagged model response.

    Used when the model ignored the tagging contract. We prefer the opening
    prose, which tends to contain the actual answer, rather than a trailing
    fragment.
    """
    cleaned = strip_for_speech(text)
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned

    # Take whole sentences up to the budget.
    out = ""
    for match in _SENTENCE_END.finditer(cleaned):
        candidate = cleaned[: match.end()]
        if len(candidate) > max_chars:
            break
        out = candidate
    if out:
        return out.strip()
    return cleaned[:max_chars].rsplit(" ", 1)[0].strip()

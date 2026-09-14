"""Tests for the incremental speak layer."""

from __future__ import annotations

from collections.abc import Iterable

from surtitle.core.speak import (
    Chunk,
    ChunkKind,
    SpeakParser,
    repair_fallback,
    strip_for_speech,
)


def collect(parser: SpeakParser, deltas: Iterable[str]) -> list[Chunk]:
    """Feed deltas then finish, returning every chunk in order."""
    chunks: list[Chunk] = []
    for delta in deltas:
        chunks.extend(parser.feed(delta))
    chunks.extend(parser.finish())
    return chunks


def spoken(chunks: Iterable[Chunk]) -> str:
    return " ".join(c.text for c in chunks if c.kind is ChunkKind.SAY)


def displayed(chunks: Iterable[Chunk]) -> str:
    return "".join(c.text for c in chunks if c.kind is ChunkKind.DISPLAY)


def by_char(text: str) -> list[str]:
    """Worst-case tokenisation: one character per delta."""
    return list(text)


class TestBasicTagging:
    def test_say_produces_speech(self):
        chunks = collect(SpeakParser(), ["<say>Hello there.</say>"])
        assert "Hello there." in spoken(chunks)

    def test_say_text_is_also_shown_in_the_transcript(self):
        chunks = collect(SpeakParser(), ["<say>Hello there.</say>"])
        # The UI shows a live "what is being said" line, so say text must be
        # reported at least once as a spoken chunk.
        assert any(c.kind is ChunkKind.SAY for c in chunks)

    def test_display_is_never_spoken(self):
        chunks = collect(SpeakParser(), ["<display>| a | b |\n| 1 | 2 |</display>"])
        assert spoken(chunks) == ""
        assert "1" in displayed(chunks)

    def test_untagged_text_is_display_only(self):
        chunks = collect(SpeakParser(), ["Just some prose."])
        assert spoken(chunks) == ""
        assert "Just some prose." in displayed(chunks)

    def test_say_and_display_interleave(self):
        raw = (
            "<say>Let me read that file.</say>"
            "<display>Read 42 pages.</display>"
            "<say>Revenue is up eight percent.</say>"
        )
        chunks = collect(SpeakParser(), [raw])
        speech = spoken(chunks)
        assert "Let me read that file." in speech
        assert "Revenue is up eight percent." in speech
        assert "42 pages" not in speech
        assert "42 pages" in displayed(chunks)

    def test_say_is_emitted_before_a_trailing_display_block(self):
        """The first spoken chunk must not wait for the whole turn to finish."""
        parser = SpeakParser()
        early = parser.feed("<say>Let me look that up.</say>")
        assert "Let me look that up." in spoken(early)


class TestIncrementalStreaming:
    def test_sentence_is_spoken_before_the_turn_ends(self):
        parser = SpeakParser()
        out = parser.feed("<say>The first sentence. And the sec")
        assert "The first sentence." in spoken(out)

    def test_two_sentences_are_split_for_tts(self):
        chunks = collect(SpeakParser(), ["<say>One. Two. Three.</say>"])
        say_chunks = [c for c in chunks if c.kind is ChunkKind.SAY]
        # Each sentence should be its own utterance so speech starts early.
        assert len(say_chunks) >= 2
        assert say_chunks[0].text == "One."
        assert say_chunks[-1].text == "Three."

    def test_tag_split_across_deltas_is_not_spoken_literally(self):
        chunks = collect(SpeakParser(), ["<sa", "y>H", "i.</sa", "y>"])
        speech = spoken(chunks)
        assert "Hi." in speech
        assert "<sa" not in speech
        assert "y>" not in speech

    def test_character_by_character_streaming_matches_single_shot(self):
        raw = "<say>Hello there, friend. How are you?</say><display>meta</display>"
        one_shot = spoken(collect(SpeakParser(), [raw]))
        drip = spoken(collect(SpeakParser(), by_char(raw)))
        assert drip == one_shot
        assert "Hello there, friend." in drip

    def test_display_tail_is_not_shown_as_literal_tag_text(self):
        chunks = collect(SpeakParser(), ["<display>data</displ", "ay>"])
        text = displayed(chunks)
        assert "data" in text
        assert "displ" not in text

    def test_lone_angle_bracket_is_literal(self):
        chunks = collect(SpeakParser(), ["<say>two < three</say>"])
        assert "two < three" in spoken(chunks)

    def test_incomplete_tag_at_end_of_stream_is_literal(self):
        chunks = collect(SpeakParser(), ["<say>done</say><disp"])
        assert "done" in spoken(chunks)

    def test_unclosed_say_still_speaks(self):
        chunks = collect(SpeakParser(), ["<say>I was cut off mid"])
        assert "I was cut off mid" in spoken(chunks)

    def test_finish_is_idempotent(self):
        parser = SpeakParser()
        parser.feed("<say>Hello.</say>")
        first = parser.finish()
        second = parser.finish()
        assert second == []
        assert first is not None


class TestSpeechMarkupCleanup:
    def test_fenced_code_is_not_spoken(self):
        chunks = collect(
            SpeakParser(), ["<say>Here you go:\n```python\nprint(1)\n```\nDone.</say>"]
        )
        speech = spoken(chunks)
        assert "print" not in speech
        assert "Done." in speech

    def test_markdown_links_speak_their_label(self):
        assert strip_for_speech("See [the report](reports/q3.pdf) now.") == ("See the report now.")

    def test_paths_are_dropped_from_speech(self):
        result = strip_for_speech("I wrote output/summary.xlsx for you.")
        assert "output/summary.xlsx" not in result
        assert "wrote" in result

    def test_urls_are_dropped_from_speech(self):
        result = strip_for_speech("Fetched from https://example.com/a/b today.")
        assert "example.com" not in result

    def test_emphasis_and_bullets_are_unwrapped(self):
        assert strip_for_speech("- **Revenue** grew") == "Revenue grew"

    def test_headings_are_unwrapped(self):
        assert strip_for_speech("## Summary\nAll good.") == "Summary. All good."

    def test_no_speakable_text_returns_empty(self):
        assert strip_for_speech("```\ncode only\n```") == ""


class TestBoundaries:
    def test_short_fragment_is_not_spoken_alone(self):
        parser = SpeakParser()
        # "O" then "K." should not produce a one-letter utterance first.
        out = parser.feed("<say>O")
        assert spoken(out) == ""
        out = parser.feed("K.")
        assert "OK." in spoken(out)

    def test_long_unpunctuated_run_is_split_for_responsiveness(self):
        parser = SpeakParser()
        long_clause = "word, " * 60
        out = parser.feed(f"<say>{long_clause}")
        assert spoken(out) != ""

    def test_sentence_ending_with_quote_closes_correctly(self):
        chunks = collect(SpeakParser(), ['<say>He said "go." Then he left.</say>'])
        assert 'He said "go."' in spoken(chunks)

    def test_ellipsis_terminates_a_sentence(self):
        parser = SpeakParser()
        out = parser.feed("<say>Well then…")
        assert "Well then…" in spoken(out)


class TestCounters:
    def test_counters_track_speech_and_display(self):
        parser = SpeakParser()
        collect(parser, ["<say>Spoken words.</say><display>Shown words.</display>"])
        assert parser.spoken_chars >= len("Spoken words.")
        assert parser.displayed_chars >= len("Shown words.")

    def test_saw_say_tag_flag(self):
        assert SpeakParser().saw_say_tag is False
        parser = SpeakParser()
        collect(parser, ["no tags here"])
        assert parser.saw_say_tag is False
        parser2 = SpeakParser()
        collect(parser2, ["<say>hi</say>"])
        assert parser2.saw_say_tag is True

    def test_clean_text_strips_tags(self):
        parser = SpeakParser()
        collect(parser, ["<say>Hello.</say><display>Table</display>"])
        cleaned = parser.clean_text()
        assert "<say>" not in cleaned
        assert "Hello." in cleaned
        assert "Table" in cleaned


class TestRepairFallback:
    def test_repair_prefers_leading_sentences(self):
        text = "Revenue rose eight percent. Costs fell. " + "padding " * 100
        result = repair_fallback(text)
        assert result.startswith("Revenue rose eight percent.")
        assert len(result) <= 240

    def test_repair_short_text_passes_through(self):
        assert repair_fallback("All done.") == "All done."

    def test_repair_of_empty_is_empty(self):
        assert repair_fallback("") == ""

    def test_repair_drops_code(self):
        result = repair_fallback("```python\nx=1\n```")
        assert result == ""

    def test_repair_hard_truncates_single_long_sentence(self):
        result = repair_fallback("word " * 200)
        assert len(result) <= 240
        assert not result.endswith(" ")

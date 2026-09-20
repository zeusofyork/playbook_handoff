import pytest

from clipper.brief import CaptionStyle, HookStyle
from clipper.captions import Cue, ass_escape, ass_time, build_ass, chunk_words, clip_words


def words(*pairs):
    return [{"start": s, "end": e, "word": w} for s, e, w in pairs]


def test_ass_time_formats_centiseconds():
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(61.239) == "0:01:01.24"
    assert ass_time(3661.5) == "1:01:01.50"


def test_ass_time_clamps_negatives():
    assert ass_time(-5) == "0:00:00.00"


def test_ass_escape_neutralises_markup_and_newlines():
    assert ass_escape("{an override}") == "\\{an override\\}"
    assert ass_escape("two\nlines") == "two\\Nlines"


def test_clip_words_rebases_onto_the_clip_timeline():
    source = words((9.0, 9.5, "before"), (10.5, 11.0, "inside"), (40.0, 40.5, "after"))
    out = clip_words(source, 10.0, 20.0)
    assert [w["word"] for w in out] == ["inside"]
    assert out[0]["start"] == pytest.approx(0.5)


def test_clip_words_clamps_a_word_straddling_the_boundary():
    source = words((9.5, 10.5, "straddle"))
    out = clip_words(source, 10.0, 20.0)
    assert out[0]["start"] == 0.0


def test_chunk_words_respects_the_word_budget():
    style = CaptionStyle(max_words_per_cue=2, max_chars_per_cue=100, max_cue_seconds=100)
    cues = chunk_words(words(*[(i * 0.5, i * 0.5 + 0.4, f"w{i} ") for i in range(6)]), style)
    assert all(len(c.text.split()) <= 2 for c in cues)


def test_chunk_words_breaks_on_sentence_endings():
    style = CaptionStyle(max_words_per_cue=10, max_chars_per_cue=100, max_cue_seconds=100)
    cues = chunk_words(words((0, 0.4, "done. "), (0.5, 0.9, "next ")), style)
    assert cues[0].text == "done."


def test_chunk_words_breaks_on_duration():
    style = CaptionStyle(max_words_per_cue=99, max_chars_per_cue=999, max_cue_seconds=1.0)
    cues = chunk_words(words((0, 0.4, "a "), (0.5, 0.9, "b "), (1.5, 1.9, "c ")), style)
    assert len(cues) > 1


def test_chunk_words_never_emits_overlapping_cues():
    style = CaptionStyle(max_words_per_cue=1)
    cues = chunk_words(words((0.0, 5.0, "long "), (1.0, 2.0, "overlap ")), style)
    for earlier, later in zip(cues, cues[1:]):
        assert later.start >= earlier.end
        assert later.end > later.start


def test_chunk_words_on_empty_input():
    assert chunk_words([], CaptionStyle()) == []


def test_build_ass_emits_both_styles_and_a_hook_line():
    doc = build_ass(
        [Cue(0.0, 1.0, "hello")],
        width=1080,
        height=1920,
        caption_style=CaptionStyle(),
        hook_style=HookStyle(),
        hook_text="watch this",
    )
    assert "PlayResX: 1080" in doc
    assert "Style: Caption," in doc and "Style: Hook," in doc
    assert "WATCH THIS" in doc          # hook uppercase
    assert "HELLO" in doc               # caption uppercase
    assert doc.count("Dialogue:") == 2


def test_build_ass_omits_captions_when_disabled():
    doc = build_ass(
        [Cue(0.0, 1.0, "hello")],
        width=1080, height=1920,
        caption_style=CaptionStyle(enabled=False),
        hook_style=HookStyle(enabled=False),
        hook_text="nope",
    )
    assert doc.count("Dialogue:") == 0


def test_build_ass_preserves_case_when_uppercase_is_off():
    doc = build_ass(
        [Cue(0.0, 1.0, "Hello There")],
        width=1080, height=1920,
        caption_style=CaptionStyle(uppercase=False),
        hook_style=HookStyle(enabled=False),
    )
    assert "Hello There" in doc

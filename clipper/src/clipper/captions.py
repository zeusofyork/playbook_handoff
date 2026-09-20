"""Burned-in captions and hook cards, emitted as an ASS subtitle file.

Word timings come from the transcript and are rebased to the clip's own
timeline. ffmpeg burns the result in during render, so the text survives
re-encoding by the platform and is visible with the sound off - which is how
most of these clips are actually watched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from .brief import CaptionStyle, HookStyle

ALIGN_BOTTOM_CENTER = 2
ALIGN_TOP_CENTER = 8


@dataclass
class Cue:
    start: float
    end: float
    text: str


def ass_time(seconds: float) -> str:
    """ASS timestamps are H:MM:SS.cc (centiseconds, single-digit hour)."""
    seconds = max(0.0, seconds)
    centis = int(round(seconds * 100))
    hours, rem = divmod(centis, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    """Escape characters ASS treats as markup."""
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\r\n", "\\N")
        .replace("\n", "\\N")
    )


def clip_words(
    words: Sequence[dict[str, Any]], start: float, end: float
) -> list[dict[str, Any]]:
    """Words overlapping [start, end), rebased so the clip begins at t=0."""
    out = []
    for word in words:
        if word["end"] <= start or word["start"] >= end:
            continue
        out.append(
            {
                "word": word["word"],
                "start": max(0.0, word["start"] - start),
                "end": min(end - start, word["end"] - start),
            }
        )
    return out


def chunk_words(words: Sequence[dict[str, Any]], style: CaptionStyle) -> list[Cue]:
    """Group words into short cues bounded by word count, line length and duration."""
    cues: list[Cue] = []
    buffer: list[dict[str, Any]] = []

    def flush() -> None:
        if not buffer:
            return
        text = "".join(w["word"] for w in buffer).strip()
        text = re.sub(r"\s+", " ", text)
        if text:
            cues.append(Cue(start=buffer[0]["start"], end=buffer[-1]["end"], text=text))
        buffer.clear()

    for word in words:
        candidate = buffer + [word]
        text_len = len("".join(w["word"] for w in candidate).strip())
        span = candidate[-1]["end"] - candidate[0]["start"]
        too_long = text_len > style.max_chars_per_cue
        too_many = len(candidate) > style.max_words_per_cue
        too_slow = span > style.max_cue_seconds

        if buffer and (too_long or too_many or too_slow):
            flush()
        buffer.append(word)

        # A sentence boundary is a natural cut even mid-budget.
        if re.search(r"[.!?]\s*$", word["word"]):
            flush()

    flush()
    return _dedupe_overlaps(cues)


def _dedupe_overlaps(cues: list[Cue]) -> list[Cue]:
    """Keep cues strictly ordered; renderers show garbage when two cues overlap."""
    fixed: list[Cue] = []
    for cue in cues:
        if fixed and cue.start < fixed[-1].end:
            cue = Cue(start=fixed[-1].end, end=max(cue.end, fixed[-1].end + 0.05), text=cue.text)
        if cue.end <= cue.start:
            cue = Cue(start=cue.start, end=cue.start + 0.4, text=cue.text)
        fixed.append(cue)
    return fixed


def _style_line(
    name: str,
    *,
    font: str,
    size: int,
    bold: bool,
    primary: str,
    outline_color: str,
    outline: int,
    shadow: int,
    alignment: int,
    margin_v: int,
) -> str:
    return (
        f"Style: {name},{font},{size},{primary},&H000000FF,{outline_color},&H64000000,"
        f"{-1 if bold else 0},0,0,0,100,100,0,0,1,{outline},{shadow},{alignment},60,60,{margin_v},1"
    )


def build_ass(
    cues: Sequence[Cue],
    *,
    width: int,
    height: int,
    caption_style: CaptionStyle,
    hook_style: HookStyle,
    hook_text: str = "",
) -> str:
    """Render cues plus an optional hook card into a complete ASS document."""
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        _style_line(
            "Caption",
            font=caption_style.font,
            size=caption_style.font_size,
            bold=caption_style.bold,
            primary=caption_style.primary_color,
            outline_color=caption_style.outline_color,
            outline=caption_style.outline,
            shadow=caption_style.shadow,
            alignment=ALIGN_BOTTOM_CENTER,
            margin_v=caption_style.margin_v,
        ),
        _style_line(
            "Hook",
            font=hook_style.font,
            size=hook_style.font_size,
            bold=hook_style.bold,
            primary=hook_style.primary_color,
            outline_color=hook_style.outline_color,
            outline=hook_style.outline,
            shadow=hook_style.shadow,
            alignment=ALIGN_TOP_CENTER,
            margin_v=hook_style.margin_v,
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    if hook_style.enabled and hook_text.strip():
        text = hook_text.upper() if hook_style.uppercase else hook_text
        lines.append(
            f"Dialogue: 0,{ass_time(0.0)},{ass_time(hook_style.seconds)},Hook,,0,0,0,,"
            f"{ass_escape(text)}"
        )

    if caption_style.enabled:
        for cue in cues:
            text = cue.text.upper() if caption_style.uppercase else cue.text
            lines.append(
                f"Dialogue: 0,{ass_time(cue.start)},{ass_time(cue.end)},Caption,,0,0,0,,"
                f"{ass_escape(text)}"
            )

    return "\n".join(lines) + "\n"

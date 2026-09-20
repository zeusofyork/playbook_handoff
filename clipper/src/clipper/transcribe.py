"""Stage 2: word-level transcription via faster-whisper.

Word timings drive both moment selection and burned-in captions, so ask for
them explicitly. The model import is lazy: the rest of the pipeline works
(audio-energy scoring only, no captions) on a machine without it installed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .fetch import Source
from .util import read_json, write_json

log = logging.getLogger("clipper.transcribe")

TRANSCRIPT_FILE = "transcript.json"


def transcript_path(source: Source) -> Path:
    return source.dir / TRANSCRIPT_FILE


def load_transcript(source: Source) -> dict[str, Any] | None:
    path = transcript_path(source)
    return read_json(path) if path.exists() else None


def transcribe(
    source: Source,
    *,
    model: str = "small.en",
    device: str = "auto",
    compute_type: str = "int8",
    language: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Transcribe the source with word timestamps. Idempotent via transcript.json."""
    out = transcript_path(source)
    if out.exists() and not force:
        log.info("transcript already present for %s", source.source_id)
        return read_json(out)

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "faster-whisper is not installed. Install it with:\n"
            "  pip install 'clipper[whisper]'\n"
            "or run `clipper score --no-transcript` for audio-energy-only selection."
        ) from exc

    log.info("transcribing %s with %s (%s/%s)", source.title, model, device, compute_type)
    whisper = WhisperModel(model, device=device, compute_type=compute_type)
    segments, info = whisper.transcribe(
        str(source.media),
        word_timestamps=True,
        vad_filter=True,
        language=language,
    )

    payload: dict[str, Any] = {
        "language": getattr(info, "language", language or "en"),
        "duration": float(getattr(info, "duration", source.duration)),
        "model": model,
        "segments": [],
    }
    for seg in segments:
        payload["segments"].append(
            {
                "start": float(seg.start),
                "end": float(seg.end),
                "text": (seg.text or "").strip(),
                "words": [
                    {
                        "start": float(w.start),
                        "end": float(w.end),
                        "word": w.word,
                        "prob": float(getattr(w, "probability", 1.0)),
                    }
                    for w in (seg.words or [])
                    if w.start is not None and w.end is not None
                ],
            }
        )

    write_json(out, payload)
    log.info("transcribed %d segments", len(payload["segments"]))
    return payload


def all_words(transcript: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten a transcript into a single ordered word list."""
    if not transcript:
        return []
    words: list[dict[str, Any]] = []
    for seg in transcript.get("segments", []):
        words.extend(seg.get("words", []))
    words.sort(key=lambda w: w["start"])
    return words


def text_between(transcript: dict[str, Any] | None, start: float, end: float) -> str:
    """The spoken text overlapping [start, end)."""
    parts = [w["word"] for w in all_words(transcript) if start <= w["start"] < end]
    return "".join(parts).strip()

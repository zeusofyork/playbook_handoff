"""Stage 3: find the moments worth cutting.

Signals, all resampled onto one fixed-hop timeline:

  energy         loudness of the source audio (shouting, laughter, music swell)
  hook_energy    loudness of the window's FIRST seconds, because a clip that
                 opens flat is scrolled past regardless of how good it gets
  speech_density words per second (dead air ranks low)
  keyword        campaign keywords spoken inside the window

Candidate windows slide across the timeline, get scored, then go through
non-maximum suppression so you get N spread-out moments instead of N
off-by-one views of the same ten seconds.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .brief import Brief, ScoreWeights
from .fetch import Source
from .transcribe import all_words, text_between
from .util import fmt_timestamp, read_json, run_binary, short_hash, write_json

log = logging.getLogger("clipper.score")

MOMENTS_FILE = "moments.json"
HOP_SECONDS = 0.5
HOOK_SECONDS = 3.0
SAMPLE_RATE = 16000


@dataclass
class Moment:
    start: float
    end: float
    score: float
    components: dict[str, float] = field(default_factory=dict)
    text: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start

    def label(self) -> str:
        return f"{fmt_timestamp(self.start)}-{fmt_timestamp(self.end)} score={self.score:.3f}"


def moments_path(source: Source) -> Path:
    return source.dir / MOMENTS_FILE


def brief_fingerprint(brief: Brief) -> str:
    """Hash of every brief field that changes which moments get picked.

    Without this, tuning thresholds and re-running would silently reuse the old
    selection - the worst kind of bug, because it looks like the knobs do nothing.
    """
    parts = [
        f"{brief.min_seconds}/{brief.target_seconds}/{brief.max_seconds}",
        f"{brief.max_clips_per_source}/{brief.min_gap_seconds}/{brief.max_overlap}",
        f"{brief.skip_intro_seconds}/{brief.skip_outro_seconds}",
        ",".join(sorted(k.lower() for k in brief.keywords)),
        f"{brief.weights.energy}/{brief.weights.hook_energy}"
        f"/{brief.weights.speech_density}/{brief.weights.keyword}",
    ]
    return short_hash(*parts)


def load_moments(source: Source, brief: Brief | None = None) -> list[Moment] | None:
    """Cached moments for this source, or None if absent or stale for this brief."""
    path = moments_path(source)
    if not path.exists():
        return None
    data = read_json(path)
    if brief is not None and data.get("fingerprint") != brief_fingerprint(brief):
        log.info("brief changed since the last scoring run; rescoring %s", source.source_id)
        return None
    return [Moment(**m) for m in data["moments"]]


def audio_energy(media: Path, hop: float = HOP_SECONDS) -> np.ndarray:
    """Per-hop RMS of the source audio, as float32 in arbitrary units.

    Decodes to 16 kHz mono s16le on stdout rather than writing a temp wav.
    """
    from .util import require

    require("ffmpeg", "Install ffmpeg.")
    raw = run_binary(
        [
            "ffmpeg", "-nostdin", "-v", "error",
            "-i", str(media),
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-f", "s16le", "-",
        ]
    )
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return rms_envelope(samples, hop=hop, sample_rate=SAMPLE_RATE)


def rms_envelope(samples: np.ndarray, *, hop: float, sample_rate: int) -> np.ndarray:
    """Root-mean-square per hop window. Pure function so it is testable offline."""
    frame = max(1, int(round(hop * sample_rate)))
    n_frames = len(samples) // frame
    if n_frames == 0:
        return np.zeros(0, dtype=np.float32)
    trimmed = samples[: n_frames * frame].reshape(n_frames, frame)
    return np.sqrt((trimmed.astype(np.float64) ** 2).mean(axis=1)).astype(np.float32)


def normalize(series: np.ndarray) -> np.ndarray:
    """Scale to 0..1 using robust percentiles so one clipped peak can't flatten everything."""
    if series.size == 0:
        return series
    lo = float(np.percentile(series, 5))
    hi = float(np.percentile(series, 95))
    if hi - lo < 1e-9:
        return np.zeros_like(series)
    return np.clip((series - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)


def speech_density(transcript: dict[str, Any] | None, n_hops: int, hop: float) -> np.ndarray:
    """Words per hop, smoothed over a few hops."""
    series = np.zeros(n_hops, dtype=np.float32)
    for word in all_words(transcript):
        idx = int(word["start"] / hop)
        if 0 <= idx < n_hops:
            series[idx] += 1.0
    return _smooth(series, width=max(1, int(round(2.0 / hop))))


def keyword_series(
    transcript: dict[str, Any] | None, n_hops: int, hop: float, keywords: Sequence[str]
) -> np.ndarray:
    """1.0 at each hop where a campaign keyword is spoken."""
    series = np.zeros(n_hops, dtype=np.float32)
    if not keywords or not transcript:
        return series
    patterns = [re.compile(r"\b" + re.escape(k.lower()) + r"\b") for k in keywords if k.strip()]
    for word in all_words(transcript):
        token = word["word"].lower().strip()
        if not token:
            continue
        if any(p.search(token) for p in patterns):
            idx = int(word["start"] / hop)
            if 0 <= idx < n_hops:
                series[idx] = 1.0
    return series


def _smooth(series: np.ndarray, width: int) -> np.ndarray:
    if width <= 1 or series.size == 0:
        return series
    kernel = np.ones(width, dtype=np.float32) / width
    return np.convolve(series, kernel, mode="same").astype(np.float32)


def window_scores(
    energy: np.ndarray,
    density: np.ndarray,
    keywords: np.ndarray,
    *,
    hop: float,
    window_seconds: float,
    stride_seconds: float,
    weights: ScoreWeights,
    skip_start_hops: int = 0,
    skip_end_hops: int = 0,
) -> list[Moment]:
    """Score every candidate window of `window_seconds` across the timeline."""
    n = min(len(energy), len(density), len(keywords))
    win = max(1, int(round(window_seconds / hop)))
    stride = max(1, int(round(stride_seconds / hop)))
    hook_hops = max(1, int(round(HOOK_SECONDS / hop)))

    first = max(0, skip_start_hops)
    last = n - win - max(0, skip_end_hops)
    if last < first:
        return []

    e_norm = normalize(energy[:n])
    d_norm = normalize(density[:n])
    kw = keywords[:n]

    moments: list[Moment] = []
    for i in range(first, last + 1, stride):
        window = slice(i, i + win)
        mean_energy = float(e_norm[window].mean())
        hook_energy = float(e_norm[i : i + min(hook_hops, win)].mean())
        mean_density = float(d_norm[window].mean())
        # Diminishing returns: three keyword mentions is already a strong signal.
        kw_score = min(1.0, float(kw[window].sum()) / 3.0)

        components = {
            "energy": mean_energy,
            "hook_energy": hook_energy,
            "speech_density": mean_density,
            "keyword": kw_score,
        }
        total = (
            weights.energy * mean_energy
            + weights.hook_energy * hook_energy
            + weights.speech_density * mean_density
            + weights.keyword * kw_score
        )
        divisor = weights.energy + weights.hook_energy + weights.speech_density + weights.keyword
        moments.append(
            Moment(
                start=i * hop,
                end=(i + win) * hop,
                score=total / divisor if divisor else 0.0,
                components=components,
            )
        )
    return moments


def overlap_fraction(a: Moment, b: Moment) -> float:
    """Intersection over the shorter of the two windows."""
    intersection = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    shortest = min(a.duration, b.duration)
    return intersection / shortest if shortest > 0 else 0.0


def suppress(
    moments: Sequence[Moment], *, max_overlap: float, min_gap: float, limit: int
) -> list[Moment]:
    """Greedy non-maximum suppression: keep the best, drop anything too close to it.

    A candidate conflicts with an already-kept moment when it overlaps it by more
    than `max_overlap`, or when the gap between them is under `min_gap`. Note that
    a positive `min_gap` already forbids any overlap, so `max_overlap` only has an
    effect when `min_gap` is 0 - set `min_gap_seconds: 0` in the brief if you want
    clips allowed to sit shoulder to shoulder.
    """
    kept: list[Moment] = []
    for candidate in sorted(moments, key=lambda m: m.score, reverse=True):
        if len(kept) >= limit:
            break
        conflict = False
        for chosen in kept:
            if overlap_fraction(candidate, chosen) > max_overlap:
                conflict = True
                break
            gap = max(candidate.start, chosen.start) - min(candidate.end, chosen.end)
            if gap < min_gap:
                conflict = True
                break
        if not conflict:
            kept.append(candidate)
    return sorted(kept, key=lambda m: m.start)


def snap_to_speech(
    moment: Moment,
    transcript: dict[str, Any] | None,
    *,
    brief: Brief,
    tolerance: float = 2.0,
) -> Moment:
    """Nudge boundaries onto nearby word edges so clips don't start mid-syllable."""
    words = all_words(transcript)
    if not words:
        return moment

    starts = [w["start"] for w in words]
    ends = [w["end"] for w in words]

    start = _nearest(starts, moment.start, tolerance)
    end = _nearest(ends, moment.end, tolerance)
    if end - start < brief.min_seconds:
        return moment
    if end - start > brief.max_seconds:
        end = start + brief.max_seconds
    return Moment(start=start, end=end, score=moment.score, components=moment.components)


def _nearest(values: Sequence[float], target: float, tolerance: float) -> float:
    best = min(values, key=lambda v: abs(v - target), default=target)
    return best if abs(best - target) <= tolerance else target


def select_moments(
    source: Source,
    brief: Brief,
    transcript: dict[str, Any] | None,
    *,
    use_audio: bool = True,
    force: bool = False,
) -> list[Moment]:
    """Run the full selection for one source and cache the result."""
    if not force:
        cached = load_moments(source, brief)
        if cached:
            log.info("reusing %d cached moments for %s", len(cached), source.source_id)
            return cached

    hop = HOP_SECONDS
    if use_audio:
        energy = audio_energy(source.media, hop=hop)
    else:
        energy = np.zeros(int(source.duration / hop), dtype=np.float32)

    n_hops = len(energy)
    if n_hops == 0:
        log.warning("no audio hops for %s; nothing to score", source.title)
        return []

    density = speech_density(transcript, n_hops, hop)
    kw = keyword_series(transcript, n_hops, hop, brief.keywords)

    candidates = window_scores(
        energy,
        density,
        kw,
        hop=hop,
        window_seconds=brief.target_seconds,
        stride_seconds=max(hop, brief.target_seconds / 6.0),
        weights=brief.weights,
        skip_start_hops=int(brief.skip_intro_seconds / hop),
        skip_end_hops=int(brief.skip_outro_seconds / hop),
    )
    log.info("scored %d candidate windows over %s", len(candidates), fmt_timestamp(source.duration))

    kept = suppress(
        candidates,
        max_overlap=brief.max_overlap,
        min_gap=brief.min_gap_seconds,
        limit=brief.max_clips_per_source,
    )
    snapped = [snap_to_speech(m, transcript, brief=brief) for m in kept]
    for moment in snapped:
        moment.text = text_between(transcript, moment.start, moment.end)

    write_json(
        moments_path(source),
        {
            "source_id": source.source_id,
            "brief": brief.name,
            "fingerprint": brief_fingerprint(brief),
            "hop": hop,
            "moments": [asdict(m) for m in snapped],
        },
    )
    for moment in snapped:
        log.info("  %s  %s", moment.label(), (moment.text[:70] or "<no speech>"))
    return snapped


def pick_hook(moment: Moment, brief: Brief) -> str:
    """Fill a hook template from the moment's own transcript."""
    if not brief.hook_style.templates:
        return ""
    words = [w for w in re.split(r"\s+", moment.text) if w]
    first_line = " ".join(words[:8])
    keyword = next(
        (k for k in brief.keywords if k and k.lower() in moment.text.lower()),
        brief.keywords[0] if brief.keywords else brief.name,
    )
    index = int(math.floor(moment.start)) % len(brief.hook_style.templates)
    template = brief.hook_style.templates[index]
    values = {"keyword": keyword, "quote": first_line, "campaign": brief.name}
    return re.sub(
        r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", lambda m: str(values.get(m.group(1), "")), template
    ).strip()

"""The campaign brief: a validated spec that every later stage obeys.

A clipping campaign's rules (duration bounds, aspect, captions, banned words,
attribution) are the contract you get paid against. Encoding them here means a
violation is a loud failure at render time instead of an unpaid clip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_origin, get_type_hints

import yaml

KNOWN_PLATFORMS = {"tiktok", "instagram", "youtube", "x", "facebook", "snapchat"}
REFRAME_MODES = {"crop", "blur_pad", "none"}


class BriefError(ValueError):
    """The brief is malformed or self-contradictory."""


@dataclass
class CaptionStyle:
    """Burned-in subtitle appearance. Colors are ASS &HAABBGGRR literals."""

    enabled: bool = True
    font: str = "DejaVu Sans"
    font_size: int = 76
    bold: bool = True
    primary_color: str = "&H00FFFFFF"
    outline_color: str = "&H00000000"
    outline: int = 5
    shadow: int = 2
    margin_v: int = 420
    max_chars_per_cue: int = 28
    max_words_per_cue: int = 4
    max_cue_seconds: float = 2.5
    uppercase: bool = True


@dataclass
class HookStyle:
    """The static text card burned over the opening seconds."""

    enabled: bool = True
    seconds: float = 3.0
    font: str = "DejaVu Sans"
    font_size: int = 88
    bold: bool = True
    primary_color: str = "&H0000F0FF"
    outline_color: str = "&H00000000"
    outline: int = 6
    shadow: int = 2
    margin_v: int = 260
    uppercase: bool = True
    templates: list[str] = field(default_factory=lambda: ["{keyword} changed everything"])


@dataclass
class ScoreWeights:
    """Relative pull of each signal when ranking candidate windows."""

    energy: float = 1.0
    hook_energy: float = 0.8
    speech_density: float = 0.6
    keyword: float = 1.2


@dataclass
class Economics:
    """Campaign payout terms, used only for queue estimates. Never for decisions."""

    rate_per_1k_views_usd: float = 1.0
    per_clip_cap_usd: float | None = 1000.0
    min_views_to_qualify: int = 0


@dataclass
class Brief:
    name: str
    source_urls: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=lambda: ["tiktok"])

    min_seconds: float = 20.0
    target_seconds: float = 34.0
    max_seconds: float = 60.0

    width: int = 1080
    height: int = 1920
    fps: int = 30
    reframe: str = "crop"
    crop_anchor: float = 0.5
    loudness_lufs: float = -14.0
    video_crf: int = 20
    video_preset: str = "veryfast"
    audio_bitrate: str = "192k"

    keywords: list[str] = field(default_factory=list)
    banned_words: list[str] = field(default_factory=list)
    max_clips_per_source: int = 8
    min_gap_seconds: float = 30.0
    max_overlap: float = 0.25
    skip_intro_seconds: float = 0.0
    skip_outro_seconds: float = 0.0

    caption_template: str = "{hook}\n\n{hashtags}"
    hashtags: list[str] = field(default_factory=list)
    attribution: str = ""
    notes: str = ""

    whisper_model: str = "small.en"
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"

    caption_style: CaptionStyle = field(default_factory=CaptionStyle)
    hook_style: HookStyle = field(default_factory=HookStyle)
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    economics: Economics = field(default_factory=Economics)

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-") or "campaign"

    @classmethod
    def from_yaml(cls, path: Path) -> "Brief":
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise BriefError(f"{path}: not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise BriefError(f"{path}: expected a mapping at the top level")
        brief = cls.from_dict(raw, origin=str(path))
        brief.validate()
        return brief

    @classmethod
    def from_dict(cls, raw: dict[str, Any], origin: str = "<dict>") -> "Brief":
        return _build(cls, raw, origin)

    def validate(self) -> None:
        errors: list[str] = []

        if not self.name.strip():
            errors.append("name must not be empty")
        if not (0 < self.min_seconds <= self.target_seconds <= self.max_seconds):
            errors.append(
                "durations must satisfy 0 < min_seconds <= target_seconds <= max_seconds "
                f"(got {self.min_seconds}/{self.target_seconds}/{self.max_seconds})"
            )
        unknown = sorted(set(p.lower() for p in self.platforms) - KNOWN_PLATFORMS)
        if unknown:
            errors.append(f"unknown platforms: {', '.join(unknown)}")
        if not self.platforms:
            errors.append("at least one platform is required")
        if self.reframe not in REFRAME_MODES:
            errors.append(f"reframe must be one of {sorted(REFRAME_MODES)}, got {self.reframe!r}")
        if not 0.0 <= self.crop_anchor <= 1.0:
            errors.append("crop_anchor must be between 0.0 and 1.0")
        if self.width % 2 or self.height % 2:
            errors.append("width and height must both be even (h264 yuv420p requirement)")
        if self.width <= 0 or self.height <= 0:
            errors.append("width and height must be positive")
        if not 0 <= self.video_crf <= 51:
            errors.append("video_crf must be between 0 and 51")
        if self.fps <= 0:
            errors.append("fps must be positive")
        if not 0.0 <= self.max_overlap < 1.0:
            errors.append("max_overlap must be in [0.0, 1.0)")
        if self.max_clips_per_source <= 0:
            errors.append("max_clips_per_source must be at least 1")
        if self.skip_intro_seconds < 0 or self.skip_outro_seconds < 0:
            errors.append("skip_intro_seconds and skip_outro_seconds must not be negative")
        if self.economics.rate_per_1k_views_usd < 0:
            errors.append("economics.rate_per_1k_views_usd must not be negative")
        if self.hook_style.enabled and not self.hook_style.templates:
            errors.append("hook_style.enabled is true but hook_style.templates is empty")
        if self.hook_style.seconds <= 0 and self.hook_style.enabled:
            errors.append("hook_style.seconds must be positive when hooks are enabled")

        bad_tags = [t for t in self.hashtags if not t.startswith("#")]
        if bad_tags:
            errors.append(f"hashtags must start with '#': {', '.join(bad_tags)}")

        for label, color in self._colors():
            if not re.fullmatch(r"&H[0-9A-Fa-f]{8}", color):
                errors.append(f"{label} must be an ASS color like &H00FFFFFF, got {color!r}")

        if errors:
            raise BriefError("invalid brief:\n  - " + "\n  - ".join(errors))

    def _colors(self) -> list[tuple[str, str]]:
        return [
            ("caption_style.primary_color", self.caption_style.primary_color),
            ("caption_style.outline_color", self.caption_style.outline_color),
            ("hook_style.primary_color", self.hook_style.primary_color),
            ("hook_style.outline_color", self.hook_style.outline_color),
        ]

    def banned_hits(self, text: str) -> list[str]:
        """Which banned words appear in text, matched on word boundaries."""
        lowered = text.lower()
        hits = []
        for word in self.banned_words:
            pattern = r"\b" + re.escape(word.lower()) + r"\b"
            if re.search(pattern, lowered):
                hits.append(word)
        return hits

    def render_caption(self, hook: str, transcript_preview: str = "") -> str:
        """Build the post caption from the template. Unknown fields render empty."""
        values = {
            "hook": hook,
            "hashtags": " ".join(self.hashtags),
            "attribution": self.attribution,
            "campaign": self.name,
            "preview": transcript_preview,
        }
        rendered = _SafeFormat(values).format(self.caption_template)
        # An empty {attribution} or {hashtags} otherwise leaves a run of blank lines.
        return re.sub(r"\n{3,}", "\n\n", rendered).strip()


class _SafeFormat:
    """str.format with unknown placeholders rendered as empty instead of KeyError."""

    def __init__(self, values: dict[str, str]):
        self.values = values

    def format(self, template: str) -> str:
        def sub(match: re.Match) -> str:
            return str(self.values.get(match.group(1), ""))

        return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", sub, template)


def _build(cls: type, raw: dict[str, Any], origin: str, prefix: str = "") -> Any:
    """Recursively construct a dataclass from a mapping, rejecting unknown keys.

    `from __future__ import annotations` makes dataclass field types strings, so
    resolve them with get_type_hints before deciding whether to recurse.
    """
    known = {f.name for f in fields(cls)}
    hints = get_type_hints(cls)
    where = f"{origin}:{prefix}" if prefix else origin

    unknown = sorted(set(raw) - known)
    if unknown:
        raise BriefError(
            f"{where}: unknown key(s): {', '.join(unknown)}. "
            f"Known keys: {', '.join(sorted(known))}"
        )

    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        hint = hints.get(key)
        child_prefix = f"{prefix}.{key}" if prefix else key

        if is_dataclass(hint):
            if not isinstance(value, dict):
                raise BriefError(f"{where}: {key} must be a mapping, got {type(value).__name__}")
            kwargs[key] = _build(hint, value, origin, prefix=child_prefix)
            continue

        if get_origin(hint) is list and isinstance(value, (str, bytes)):
            raise BriefError(
                f"{where}: {key} must be a list, got a string. "
                f"Use a YAML list, e.g. [{value!r}]"
            )

        kwargs[key] = value
    return cls(**kwargs)

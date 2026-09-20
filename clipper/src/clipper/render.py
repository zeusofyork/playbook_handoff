"""Stage 4: cut, reframe, caption and encode one clip per selected moment.

Every clip is checked against the brief before a single frame is encoded:
duration bounds, banned words, required attribution. Failing loudly here costs
seconds; failing at submission costs the whole clip.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .brief import Brief
from .captions import build_ass, chunk_words, clip_words
from .fetch import Source
from .score import Moment, pick_hook
from .transcribe import all_words
from .util import ensure_dir, require, run, short_hash, write_json

log = logging.getLogger("clipper.render")


class BriefViolation(RuntimeError):
    """The clip would break the campaign's rules, so it was not rendered."""


@dataclass
class Clip:
    clip_id: str
    source_id: str
    source_origin: str
    campaign: str
    start: float
    end: float
    duration: float
    score: float
    components: dict[str, float]
    hook: str
    transcript: str
    caption: str
    platforms: list[str]
    path: str
    created_at: float = field(default_factory=time.time)
    width: int = 1080
    height: int = 1920
    fps: int = 30


def clip_id_for(source: Source, moment: Moment) -> str:
    return short_hash(source.source_id, f"{moment.start:.3f}", f"{moment.end:.3f}", length=12)


def clip_dir(work: Path, clip_id: str) -> Path:
    return work / "clips" / clip_id


def check_brief(moment: Moment, hook: str, caption: str, brief: Brief) -> None:
    """Raise BriefViolation if this clip would break the campaign rules."""
    problems: list[str] = []

    if moment.duration < brief.min_seconds:
        problems.append(f"duration {moment.duration:.1f}s is under min_seconds {brief.min_seconds}")
    if moment.duration > brief.max_seconds:
        problems.append(f"duration {moment.duration:.1f}s is over max_seconds {brief.max_seconds}")

    hits = sorted(set(brief.banned_hits(moment.text) + brief.banned_hits(hook) + brief.banned_hits(caption)))
    if hits:
        problems.append(f"banned word(s) present: {', '.join(hits)}")

    if brief.attribution and brief.attribution not in caption:
        problems.append(
            f"required attribution {brief.attribution!r} is missing from the caption; "
            "add {attribution} to caption_template"
        )

    if problems:
        raise BriefViolation("; ".join(problems))


def build_filter_complex(
    brief: Brief, *, subtitle_file: str | None, audio_input: str
) -> tuple[str, str, str]:
    """Return (filter_complex, video_label, audio_label) for the render."""
    w, h = brief.width, brief.height
    target_ar = w / h
    chains: list[str] = []

    if brief.reframe == "crop":
        anchor = brief.crop_anchor
        crop = (
            f"crop='min(iw,ih*{target_ar:.6f})':'min(ih,iw/{target_ar:.6f})'"
            f":'(iw-min(iw,ih*{target_ar:.6f}))*{anchor:.4f}'"
            f":'(ih-min(ih,iw/{target_ar:.6f}))/2'"
        )
        chains.append(f"[0:v]{crop},scale={w}:{h}:flags=lanczos,setsar=1[base]")
    elif brief.reframe == "blur_pad":
        chains.append(
            f"[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},boxblur=24:2,eq=brightness=-0.08[bg];"
            f"[fgsrc]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]"
        )
    else:  # "none" - letterbox onto the target canvas, never crop
        chains.append(
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[base]"
        )

    video_tail = f"[base]fps={brief.fps},format=yuv420p"
    if subtitle_file:
        video_tail += f",subtitles=filename='{_escape_filter_arg(subtitle_file)}'"
    chains.append(f"{video_tail}[v]")

    chains.append(
        f"[{audio_input}]loudnorm=I={brief.loudness_lufs}:TP=-1.5:LRA=11,"
        f"aresample=async=1:osr=48000[a]"
    )
    return ";".join(chains), "[v]", "[a]"


def _escape_filter_arg(value: str) -> str:
    """Escape a value for use inside a single-quoted ffmpeg filter argument."""
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_ffmpeg_cmd(
    source: Source,
    moment: Moment,
    brief: Brief,
    out_path: Path,
    *,
    subtitle_file: str | None,
) -> list[str]:
    """Assemble the single ffmpeg invocation that produces the finished clip."""
    duration = moment.duration
    inputs: list[str] = ["-ss", f"{moment.start:.3f}", "-i", str(source.media)]
    audio_input = "0:a"

    # A source with no audio stream still needs one: most platforms reject
    # video-only uploads, and loudnorm has nothing to map without it.
    if not source.has_audio:
        inputs += [
            "-f", "lavfi",
            "-t", f"{duration:.3f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        ]
        audio_input = "1:a"

    filter_complex, vlabel, alabel = build_filter_complex(
        brief, subtitle_file=subtitle_file, audio_input=audio_input
    )

    return [
        "ffmpeg", "-nostdin", "-y", "-v", "error", "-stats",
        *inputs,
        "-t", f"{duration:.3f}",
        "-filter_complex", filter_complex,
        "-map", vlabel,
        "-map", alabel,
        "-c:v", "libx264",
        "-preset", brief.video_preset,
        "-crf", str(brief.video_crf),
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-g", str(brief.fps * 2),
        "-c:a", "aac",
        "-b:a", brief.audio_bitrate,
        "-ar", "48000",
        "-ac", "2",
        "-movflags", "+faststart",
        str(out_path),
    ]


def render_clip(
    source: Source,
    moment: Moment,
    brief: Brief,
    work: Path,
    transcript: dict[str, Any] | None,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> Clip:
    """Render one moment into a finished, brief-compliant clip."""
    clip_id = clip_id_for(source, moment)
    out_dir = ensure_dir(clip_dir(work, clip_id))
    out_path = out_dir / "clip.mp4"

    hook = pick_hook(moment, brief) if brief.hook_style.enabled else ""
    caption = brief.render_caption(hook=hook or moment.text[:80], transcript_preview=moment.text[:200])
    check_brief(moment, hook, caption, brief)

    subtitle_name: str | None = None
    if brief.caption_style.enabled or (brief.hook_style.enabled and hook):
        cues = chunk_words(
            clip_words(all_words(transcript), moment.start, moment.end), brief.caption_style
        )
        ass = build_ass(
            cues,
            width=brief.width,
            height=brief.height,
            caption_style=brief.caption_style,
            hook_style=brief.hook_style,
            hook_text=hook,
        )
        (out_dir / "captions.ass").write_text(ass, encoding="utf-8")
        subtitle_name = "captions.ass"
        if brief.caption_style.enabled and not cues:
            log.warning("clip %s has no transcript words; rendering without captions", clip_id)

    cmd = build_ffmpeg_cmd(source, moment, brief, out_path, subtitle_file=subtitle_name)

    clip = Clip(
        clip_id=clip_id,
        source_id=source.source_id,
        source_origin=source.origin,
        campaign=brief.name,
        start=moment.start,
        end=moment.end,
        duration=moment.duration,
        score=moment.score,
        components=moment.components,
        hook=hook,
        transcript=moment.text,
        caption=caption,
        platforms=list(brief.platforms),
        path=str(out_path),
        width=brief.width,
        height=brief.height,
        fps=brief.fps,
    )

    if dry_run:
        log.info("[dry-run] %s\n  %s", clip_id, " ".join(cmd))
        return clip

    if out_path.exists() and not force:
        log.info("clip %s already rendered", clip_id)
    else:
        require("ffmpeg", "Install ffmpeg.")
        # cwd is the clip dir so the subtitles filter takes a bare filename and
        # we never have to escape an absolute path into the filter graph.
        run(cmd, cwd=out_dir)
        log.info("rendered %s (%.1fs)", out_path, clip.duration)

    write_json(out_dir / "meta.json", asdict(clip))
    (out_dir / "caption.txt").write_text(caption + "\n", encoding="utf-8")
    return clip

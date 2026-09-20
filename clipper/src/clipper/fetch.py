"""Stage 1: pull source video into the work directory.

Only ever point this at content a campaign has explicitly licensed you to clip,
or at your own footage. Downloading someone's stream without that permission is
a copyright strike, not a side hustle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from pathlib import Path

from .util import MediaInfo, probe, read_json, require, run, short_hash, write_json

log = logging.getLogger("clipper.fetch")

STATE_FILE = "source.json"


@dataclass
class Source:
    source_id: str
    origin: str
    path: str
    work_dir: str
    title: str
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool = True

    @property
    def dir(self) -> Path:
        """Where later stages cache their output.

        Distinct from the media file's own directory: a local source stays where
        the user put it, and nothing this pipeline derives should be written
        next to it.
        """
        return Path(self.work_dir)

    @property
    def media(self) -> Path:
        return Path(self.path)


def source_id_for(origin: str) -> str:
    return short_hash(origin)


def source_dir(work: Path, origin: str) -> Path:
    return work / "sources" / source_id_for(origin)


def load_source(work: Path, origin: str) -> Source | None:
    state = source_dir(work, origin) / STATE_FILE
    if not state.exists():
        return None
    src = Source(**read_json(state))
    if not Path(src.path).exists():
        log.warning("cached source %s is missing its media file, refetching", src.source_id)
        return None
    return src


def fetch(
    origin: str,
    work: Path,
    *,
    max_height: int = 1080,
    cookies: Path | None = None,
    force: bool = False,
) -> Source:
    """Resolve `origin` (a URL or a local path) into a cached Source. Idempotent."""
    if not force:
        cached = load_source(work, origin)
        if cached:
            log.info("source %s already fetched (%s)", cached.source_id, cached.title)
            return cached

    out_dir = source_dir(work, origin)
    out_dir.mkdir(parents=True, exist_ok=True)

    local = Path(origin).expanduser()
    if local.exists() and local.is_file():
        media = local.resolve()
        title = media.stem
        log.info("using local source %s", media)
    else:
        media, title = _download(origin, out_dir, max_height=max_height, cookies=cookies)

    info: MediaInfo = probe(media)
    if not info.has_audio:
        log.warning("source %s has no audio track; scoring will fall back to transcript only", title)

    src = Source(
        source_id=source_id_for(origin),
        origin=origin,
        path=str(media),
        work_dir=str(out_dir),
        title=title,
        duration=info.duration,
        width=info.width,
        height=info.height,
        fps=info.fps,
        has_audio=info.has_audio,
    )
    write_json(out_dir / STATE_FILE, asdict(src))
    log.info("fetched %s (%.0fs, %dx%d)", title, info.duration, info.width, info.height)
    return src


def _download(
    url: str, out_dir: Path, *, max_height: int, cookies: Path | None
) -> tuple[Path, str]:
    require("yt-dlp", "pip install yt-dlp")
    template = str(out_dir / "source.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-progress",
        "--write-info-json",
        "--merge-output-format", "mp4",
        "-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b",
        "-o", template,
        url,
    ]
    if cookies:
        cmd[1:1] = ["--cookies", str(cookies)]
    run(cmd)

    candidates = sorted(
        p for p in out_dir.glob("source.*") if p.suffix.lower() not in {".json", ".tmp", ".part"}
    )
    if not candidates:
        raise FileNotFoundError(f"yt-dlp produced no media file in {out_dir}")
    media = max(candidates, key=lambda p: p.stat().st_size)

    title = url
    info_json = out_dir / "source.info.json"
    if info_json.exists():
        title = read_json(info_json).get("title") or url
    return media, title

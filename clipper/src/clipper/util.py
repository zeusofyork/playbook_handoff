"""Shared helpers: subprocess wrapping, ffprobe, paths, logging."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

log = logging.getLogger("clipper")


class ToolMissing(RuntimeError):
    """A required external binary is not on PATH."""


class CommandFailed(RuntimeError):
    """An external command exited non-zero."""


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def require(binary: str, hint: str = "") -> str:
    path = shutil.which(binary)
    if not path:
        raise ToolMissing(f"{binary!r} not found on PATH. {hint}".strip())
    return path


def run(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    capture: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a command, raising CommandFailed with a useful stderr tail."""
    log.debug("exec: %s", " ".join(str(c) for c in cmd))
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        capture_output=capture,
        text=True,
    )
    if check and proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-25:]
        raise CommandFailed(
            f"{cmd[0]} exited {proc.returncode}\n  cmd: "
            + " ".join(str(c) for c in cmd)
            + "\n  stderr:\n    "
            + "\n    ".join(tail)
        )
    return proc


def run_binary(cmd: Sequence[str], *, cwd: Path | None = None) -> bytes:
    """Run a command and return raw stdout bytes (for piping PCM out of ffmpeg)."""
    log.debug("exec(binary): %s", " ".join(str(c) for c in cmd))
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-25:]
        raise CommandFailed(
            f"{cmd[0]} exited {proc.returncode}\n  stderr:\n    " + "\n    ".join(tail)
        )
    return proc.stdout


@dataclass
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


def _parse_fps(rate: str) -> float:
    if not rate or rate == "0/0":
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        den_f = float(den)
        return float(num) / den_f if den_f else 0.0
    return float(rate)


def probe(path: Path) -> MediaInfo:
    """ffprobe a media file into a MediaInfo."""
    require("ffprobe", "Install ffmpeg (which ships ffprobe).")
    out = run(
        [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    ).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise CommandFailed(f"no video stream in {path}")

    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0.0)
    fps = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
    return MediaInfo(
        path=path,
        duration=duration,
        width=int(video["width"]),
        height=int(video["height"]),
        fps=fps or 30.0,
        has_audio=audio is not None,
    )


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def short_hash(*parts: str, length: int = 10) -> str:
    digest = hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


def write_json(path: Path, payload: object) -> None:
    """Write JSON atomically so a killed run never leaves a half-written cache."""
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def fmt_timestamp(seconds: float) -> str:
    """Seconds -> H:MM:SS for human-readable logs."""
    seconds = max(0.0, seconds)
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"

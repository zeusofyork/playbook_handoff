import shutil
import subprocess

import pytest

from clipper.fetch import fetch, load_source, source_id_for

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


@pytest.fixture
def tiny_video(tmp_path):
    """A two-second silent-ish clip, generated rather than committed."""
    path = tmp_path / "media" / "source.mp4"
    path.parent.mkdir()
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=200:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_source_id_is_stable_for_the_same_origin():
    assert source_id_for("https://example/v") == source_id_for("https://example/v")
    assert source_id_for("https://example/v") != source_id_for("https://example/w")


@needs_ffmpeg
def test_local_source_is_probed_without_being_copied(tmp_path, tiny_video):
    work = tmp_path / "work"
    src = fetch(str(tiny_video), work)

    assert src.media == tiny_video.resolve()
    assert src.duration == pytest.approx(2.0, abs=0.3)
    assert src.width == 320 and src.height == 180
    assert src.has_audio is True


@needs_ffmpeg
def test_derived_files_land_in_the_work_dir_not_beside_the_media(tmp_path, tiny_video):
    """Regression: a local source must not get transcript/moments dumped next to it."""
    work = tmp_path / "work"
    src = fetch(str(tiny_video), work)

    assert src.dir == work / "sources" / src.source_id
    assert src.dir != tiny_video.parent
    assert sorted(p.name for p in tiny_video.parent.iterdir()) == ["source.mp4"]


@needs_ffmpeg
def test_fetch_is_idempotent_and_reloadable(tmp_path, tiny_video):
    work = tmp_path / "work"
    first = fetch(str(tiny_video), work)
    second = fetch(str(tiny_video), work)
    assert first == second
    assert load_source(work, str(tiny_video)) == first


@needs_ffmpeg
def test_a_missing_cached_media_file_forces_a_refetch(tmp_path, tiny_video):
    work = tmp_path / "work"
    fetch(str(tiny_video), work)
    tiny_video.unlink()
    assert load_source(work, str(tiny_video)) is None

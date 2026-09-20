import pytest

from clipper.brief import Brief
from clipper.render import BriefViolation, build_filter_complex, check_brief, _escape_filter_arg
from clipper.score import Moment


def fc(brief, subtitle_file="captions.ass"):
    graph, vlabel, alabel = build_filter_complex(
        brief, subtitle_file=subtitle_file, audio_input="0:a"
    )
    assert (vlabel, alabel) == ("[v]", "[a]")
    return graph


def test_crop_mode_is_aspect_driven_not_hardcoded():
    graph = fc(Brief(name="d", width=1080, height=1920))
    assert "crop='min(iw,ih*0.562500)'" in graph
    assert "scale=1080:1920" in graph


def test_crop_anchor_reaches_the_crop_expression():
    assert "*0.0000'" in fc(Brief(name="d", crop_anchor=0.0))


def test_blur_pad_builds_a_background_and_foreground_chain():
    graph = fc(Brief(name="d", reframe="blur_pad"))
    assert "split=2[bgsrc][fgsrc]" in graph
    assert "boxblur" in graph
    assert "overlay=(W-w)/2:(H-h)/2" in graph


def test_none_mode_letterboxes_instead_of_cropping():
    graph = fc(Brief(name="d", reframe="none"))
    assert "pad=1080:1920" in graph
    assert "crop=" not in graph


@pytest.mark.parametrize("mode", ["crop", "blur_pad", "none"])
def test_every_mode_produces_exactly_one_video_and_audio_output(mode):
    graph = fc(Brief(name="d", reframe=mode))
    assert graph.count("[v]") == 1
    assert graph.count("[a]") == 1
    assert "loudnorm=I=-14.0" in graph


def test_subtitles_filter_is_omitted_when_there_is_no_subtitle_file():
    assert "subtitles=" not in fc(Brief(name="d"), subtitle_file=None)


def test_filter_arg_escaping_protects_colons_and_quotes():
    assert _escape_filter_arg("C:/a'b") == "C\\:/a\\'b"


def test_check_brief_passes_a_compliant_clip():
    brief = Brief(name="d", min_seconds=20, max_seconds=60)
    check_brief(Moment(0, 30, 1.0, text="all good"), "hook", "caption", brief)


@pytest.mark.parametrize(
    "moment, hook, caption, message",
    [
        (Moment(0, 10, 1.0), "", "", "under min_seconds"),
        (Moment(0, 90, 1.0), "", "", "over max_seconds"),
        (Moment(0, 30, 1.0, text="total scam"), "", "", "banned word"),
        (Moment(0, 30, 1.0), "a scam hook", "", "banned word"),
    ],
)
def test_check_brief_rejects_violations(moment, hook, caption, message):
    brief = Brief(name="d", min_seconds=20, max_seconds=60, banned_words=["scam"])
    with pytest.raises(BriefViolation, match=message):
        check_brief(moment, hook, caption, brief)


def test_check_brief_requires_attribution_to_appear_in_the_caption():
    brief = Brief(name="d", attribution="via @creator")
    with pytest.raises(BriefViolation, match="attribution"):
        check_brief(Moment(0, 30, 1.0), "", "no credit here", brief)
    check_brief(Moment(0, 30, 1.0), "", "clip via @creator", brief)


def _source(**kw):
    from clipper.fetch import Source

    defaults = dict(
        source_id="src", origin="https://example/v", path="/tmp/source.mp4",
        work_dir="/tmp/work/sources/src", title="t", duration=600.0,
        width=1920, height=1080, fps=30.0, has_audio=True,
    )
    defaults.update(kw)
    return Source(**defaults)


def test_ffmpeg_cmd_seeks_before_the_input_and_bounds_the_duration():
    from clipper.render import build_ffmpeg_cmd
    from pathlib import Path

    cmd = build_ffmpeg_cmd(
        _source(), Moment(61.5, 91.5, 1.0), Brief(name="d"), Path("/tmp/out.mp4"),
        subtitle_file=None,
    )
    assert cmd[cmd.index("-ss") + 1] == "61.500"
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-t") + 1] == "30.000"
    assert cmd[-1] == "/tmp/out.mp4"


def test_a_silent_source_gets_a_generated_audio_track():
    from clipper.render import build_ffmpeg_cmd
    from pathlib import Path

    cmd = build_ffmpeg_cmd(
        _source(has_audio=False), Moment(0, 30, 1.0), Brief(name="d"), Path("/tmp/out.mp4"),
        subtitle_file=None,
    )
    joined = " ".join(cmd)
    assert "anullsrc" in joined
    assert "[1:a]loudnorm" in joined


def test_a_source_with_audio_maps_the_real_track():
    from clipper.render import build_ffmpeg_cmd
    from pathlib import Path

    cmd = build_ffmpeg_cmd(
        _source(), Moment(0, 30, 1.0), Brief(name="d"), Path("/tmp/out.mp4"), subtitle_file=None
    )
    joined = " ".join(cmd)
    assert "anullsrc" not in joined
    assert "[0:a]loudnorm" in joined

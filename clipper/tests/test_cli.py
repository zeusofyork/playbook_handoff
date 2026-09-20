import argparse

import pytest

from clipper.brief import Brief
from clipper.cli import build_parser, main, origins


def ns(**kw):
    return argparse.Namespace(**kw)


def test_parser_exposes_every_stage():
    parser = build_parser()
    for command in ["doctor", "init", "fetch", "transcribe", "score", "render", "run", "queue"]:
        assert parser.parse_args([command, *_min_args(command)]).command == command


def _min_args(command):
    if command in {"fetch", "transcribe", "score", "render", "run"}:
        return ["--brief", "b.yml"]
    if command == "init":
        return ["somewhere"]
    if command == "queue":
        return ["list"]
    return []


def test_cli_flags_override_the_briefs_sources():
    brief = Brief(name="d", source_urls=["https://from-brief"])
    assert origins(brief, ns(url=["https://from-flag"])) == ["https://from-flag"]
    assert origins(brief, ns(url=None)) == ["https://from-brief"]


def test_a_brief_with_no_sources_is_a_clear_exit():
    with pytest.raises(SystemExit, match="no sources"):
        origins(Brief(name="d"), ns(url=None))


def test_scaffold_placeholders_are_caught_before_yt_dlp_sees_them():
    brief = Brief(name="d", source_urls=["https://www.youtube.com/watch?v=REPLACE_ME"])
    with pytest.raises(SystemExit, match="placeholders"):
        origins(brief, ns(url=None))


def test_a_missing_brief_exits_with_a_pointer_to_init():
    with pytest.raises(SystemExit, match="clipper init"):
        main(["score", "--brief", "/nonexistent/campaign.yml"])


def test_an_invalid_brief_exits_2(tmp_path, capsys):
    bad = tmp_path / "campaign.yml"
    bad.write_text("name: d\nplatforms: [myspace]\n", encoding="utf-8")
    assert main(["score", "--brief", str(bad)]) == 2
    assert "unknown platforms" in capsys.readouterr().err


def test_queue_list_on_an_empty_ledger_succeeds(tmp_path, capsys):
    assert main(["--work", str(tmp_path), "queue", "list"]) == 0
    assert "queue is empty" in capsys.readouterr().out

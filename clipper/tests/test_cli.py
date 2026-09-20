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


# ------------------------------------------------------------------ campaigns

FIXTURE = __import__("pathlib").Path(__file__).parent / "fixtures" / "discover_sample.html"


@pytest.fixture
def seeded_work(tmp_path):
    """A work dir whose board cache is the offline fixture, so nothing hits the network."""
    cache = tmp_path / "campaigns" / "board.html"
    cache.parent.mkdir(parents=True)
    cache.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def test_campaigns_list_ranks_and_annotates(seeded_work, capsys):
    assert main(["--work", str(seeded_work), "campaigns", "list"]) == 0
    out = capsys.readouterr().out
    assert "RUNWAY" in out and "CLIPPERS" in out
    assert "Boxabl Official Clipping" in out
    assert "application required" in out


def test_campaigns_list_json_is_machine_readable(seeded_work, capsys):
    assert main(["--work", str(seeded_work), "campaigns", "list", "--json"]) == 0
    data = __import__("json").loads(capsys.readouterr().out)
    assert len(data) == 4
    assert {"days_left", "burn_per_day", "score", "budget_per_creator"} <= set(data[0])


def test_campaigns_list_filters_narrow_the_board(seeded_work, capsys):
    assert main(["--work", str(seeded_work), "campaigns", "list", "--min-cpm", "2.5"]) == 0
    out = capsys.readouterr().out
    assert "Syberjet" in out
    assert "Boxabl" not in out


def test_campaigns_show_prints_the_brief(seeded_work, capsys):
    assert main(["--work", str(seeded_work), "campaigns", "show", "boxabl"]) == 0
    out = capsys.readouterr().out
    assert "per 1k views" in out
    assert "left per clipper already in" in out


def test_campaigns_watch_then_check_exits_nonzero_when_runway_is_short(seeded_work, capsys):
    assert main(["--work", str(seeded_work), "campaigns", "watch", "ForgeGUI"]) == 0
    capsys.readouterr()
    assert main(["--work", str(seeded_work), "campaigns", "check", "--min-days", "7"]) == 1
    assert "under 7 days" in capsys.readouterr().out


def test_campaigns_check_is_quiet_when_every_watched_pool_is_healthy(seeded_work, capsys):
    main(["--work", str(seeded_work), "campaigns", "watch", "boxabl"])
    capsys.readouterr()
    assert main(["--work", str(seeded_work), "campaigns", "check", "--min-days", "7"]) == 0
    assert "above 7 days" in capsys.readouterr().out


def test_campaigns_check_with_an_empty_watchlist_is_a_no_op(seeded_work, capsys):
    assert main(["--work", str(seeded_work), "campaigns", "check"]) == 0
    assert "nothing on the watchlist" in capsys.readouterr().out


def test_campaigns_unwatch(seeded_work, capsys):
    main(["--work", str(seeded_work), "campaigns", "watch", "boxabl"])
    capsys.readouterr()
    assert main(["--work", str(seeded_work), "campaigns", "unwatch", "188c3e39"]) == 0
    assert "stopped watching" in capsys.readouterr().out
    assert main(["--work", str(seeded_work), "campaigns", "unwatch", "nope"]) == 1


def test_a_broken_board_exits_4_rather_than_printing_an_empty_table(tmp_path, capsys):
    cache = tmp_path / "campaigns" / "board.html"
    cache.parent.mkdir(parents=True)
    cache.write_text("<html>site redesigned</html>", encoding="utf-8")
    assert main(["--work", str(tmp_path), "campaigns", "list"]) == 4
    assert "flight payload" in capsys.readouterr().err

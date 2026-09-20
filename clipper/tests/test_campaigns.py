import json
import time
import urllib.error
from pathlib import Path

import pytest

from clipper.campaigns import (
    BoardError,
    BoardParseError,
    Campaign,
    History,
    Watchlist,
    campaign_from_raw,
    enrich,
    fetch_board,
    filter_campaigns,
    parse_board,
    percentile_ranks,
    rank,
    resolve,
)

FIXTURE = Path(__file__).parent / "fixtures" / "discover_sample.html"
DAY = 86_400.0


@pytest.fixture
def board():
    return parse_board(FIXTURE.read_text(encoding="utf-8"))


def make(**kw) -> Campaign:
    defaults = dict(
        id="aaaaaaaa-1111-2222-3333-444444444444", title="Demo", brand="Brand", cpm=1.0,
        budget_total=10_000.0, budget_spent=2_000.0, budget_available=8_000.0,
        creators=100, submissions=500, platforms=["tiktok"], category="",
        created_at_ms=int((time.time() - 10 * DAY) * 1000), requires_application=False,
        verified=True,
    )
    defaults.update(kw)
    return Campaign(**defaults)


# ------------------------------------------------------------------- parsing


def test_parse_board_reads_every_campaign(board):
    assert len(board) == 4
    assert {c.title for c in board} >= {"Boxabl Official Clipping", "Syberjet Speed Record"}


def test_parse_board_maps_the_fields_we_rank_on(board):
    boxabl = next(c for c in board if "Boxabl" in c.title)
    assert boxabl.cpm == 0.5
    assert boxabl.budget_total == 85_000
    assert boxabl.budget_available == pytest.approx(67_386.81)
    assert boxabl.creators == 757
    assert boxabl.brand == "Clip Farm"
    assert "tiktok" in boxabl.platforms


def test_parse_board_keeps_the_application_flag(board):
    gated = [c for c in board if c.requires_application]
    assert len(gated) == 1


@pytest.mark.parametrize(
    "html", ["", "<html><body>nothing here</body></html>", "<script>var x = 1;</script>"]
)
def test_parse_board_fails_loudly_when_the_payload_is_missing(html):
    with pytest.raises(BoardParseError, match="flight payload"):
        parse_board(html)


def test_parse_board_fails_loudly_when_the_payload_has_no_campaigns():
    page = '<script>self.__next_f.push([1,' + json.dumps('3:{"other":[]}\n') + '])</script>'
    with pytest.raises(BoardParseError, match="field names"):
        parse_board(page)


def test_campaign_from_raw_tolerates_a_missing_description():
    campaign = campaign_from_raw({"id": "x", "title": "T", "availableBudgetRaw": 1.0})
    assert campaign.description == ""
    assert campaign.budget_available == 1.0


def test_campaign_from_raw_tolerates_explicit_nulls():
    campaign = campaign_from_raw(
        {"id": "x", "title": "T", "availableBudgetRaw": 1.0, "platforms": None, "category": None}
    )
    assert campaign.platforms == []
    assert campaign.category == ""


# ------------------------------------------------------------------- scoring


def test_percentile_ranks_are_monotonic_and_bounded():
    ranks = percentile_ranks([10.0, 50.0, 20.0, 90.0])
    assert ranks[3] == 1.0 and ranks[0] == 0.0
    assert ranks[2] < ranks[1] < ranks[3]


def test_percentile_ranks_share_a_rank_across_ties():
    ranks = percentile_ranks([5.0, 5.0, 5.0])
    assert ranks == [0.5, 0.5, 0.5]


def test_percentile_ranks_edge_cases():
    assert percentile_ranks([]) == []
    assert percentile_ranks([42.0]) == [1.0]


def test_lifetime_burn_is_spend_over_age():
    campaign = make(budget_spent=1_000.0, created_at_ms=int((time.time() - 10 * DAY) * 1000))
    enrich([campaign])
    assert campaign.burn_per_day == pytest.approx(100.0, rel=0.02)
    assert campaign.burn_source == "lifetime"
    assert campaign.days_left == pytest.approx(80.0, rel=0.02)


def test_an_unspent_campaign_gets_a_sentinel_runway_not_a_divide_by_zero():
    campaign = make(budget_spent=0.0)
    enrich([campaign])
    assert campaign.burn_source == "none"
    assert campaign.days_left == 999.0
    assert campaign.runway_label() == ">999d"


def test_a_brand_new_campaign_does_not_divide_by_a_tiny_age():
    campaign = make(budget_spent=100.0, created_at_ms=int(time.time() * 1000))
    enrich([campaign])
    assert campaign.age_days == pytest.approx(0.5)
    assert campaign.burn_per_day == pytest.approx(200.0, rel=0.05)


def test_a_dead_pool_ranks_below_a_live_one_despite_a_far_better_rate():
    dying = make(id="dying", cpm=10.0, budget_available=200.0, budget_spent=49_800.0,
                 budget_total=50_000.0, creators=2_000)
    healthy = make(id="healthy", cpm=1.0, budget_available=40_000.0, budget_spent=10_000.0,
                   budget_total=50_000.0, creators=300)
    ordered = rank(enrich([dying, healthy]))
    assert ordered[0].id == "healthy"


def test_headroom_rewards_the_same_pool_split_fewer_ways():
    crowded = make(id="crowded", creators=2_000)
    quiet = make(id="quiet", creators=20)
    enrich([crowded, quiet])
    assert quiet.components["headroom"] > crowded.components["headroom"]
    assert quiet.score > crowded.score


def test_score_components_sum_to_the_score():
    campaign = make()
    enrich([campaign, make(id="other", cpm=4.0)])
    parts = campaign.components
    expected = 0.45 * parts["runway"] + 0.25 * parts["rate"] + 0.30 * parts["headroom"]
    assert campaign.score == pytest.approx(expected, abs=0.002)


# ------------------------------------------------------------------- history


def test_observed_burn_needs_two_snapshots(tmp_path):
    history = History.for_work(tmp_path)
    campaign = make()
    history.record([campaign], now=1000.0)
    assert history.observed_burn(campaign.id, now=1000.0) is None


def test_observed_burn_measures_the_drop_between_snapshots(tmp_path):
    history = History.for_work(tmp_path)
    start = 1_000_000.0
    history.record([make(budget_available=10_000.0)], now=start)
    history.record([make(budget_available=9_000.0)], now=start + DAY)
    burn = history.observed_burn(make().id, now=start + DAY)
    assert burn == pytest.approx(1_000.0)


def test_observed_burn_ignores_snapshots_under_an_hour_apart(tmp_path):
    history = History.for_work(tmp_path)
    start = 1_000_000.0
    history.record([make(budget_available=10_000.0)], now=start)
    history.record([make(budget_available=9_999.0)], now=start + 1_800)
    assert history.observed_burn(make().id, now=start + 1_800) is None


def test_a_topped_up_budget_reads_as_zero_burn_not_negative(tmp_path):
    history = History.for_work(tmp_path)
    start = 1_000_000.0
    history.record([make(budget_available=5_000.0)], now=start)
    history.record([make(budget_available=25_000.0)], now=start + DAY)
    assert history.observed_burn(make().id, now=start + DAY) == 0.0


def test_observed_burn_overrides_the_lifetime_average(tmp_path):
    history = History.for_work(tmp_path)
    now = time.time()
    campaign = make(budget_available=10_000.0, budget_spent=1_000.0)
    history.record([make(budget_available=12_000.0)], now=now - DAY)
    history.record([campaign], now=now)

    enrich([campaign], history, now=now)
    assert campaign.burn_source == "observed"
    assert campaign.burn_per_day == pytest.approx(2_000.0, rel=0.02)
    assert campaign.days_left == pytest.approx(5.0, rel=0.02)


def test_repeat_runs_inside_a_minute_do_not_pile_up_snapshots(tmp_path):
    history = History.for_work(tmp_path)
    for offset in range(5):
        history.record([make()], now=1000.0 + offset)
    assert len(history.data[make().id]) == 1


def test_history_is_trimmed_to_a_bounded_length(tmp_path):
    history = History.for_work(tmp_path)
    for i in range(History.MAX_SNAPSHOTS + 50):
        history.record([make()], now=1000.0 + i * 120)
    assert len(history.data[make().id]) == History.MAX_SNAPSHOTS


def test_history_survives_a_round_trip(tmp_path):
    history = History.for_work(tmp_path)
    history.record([make()], now=1000.0)
    history.save()
    assert History.for_work(tmp_path).data == history.data


def test_corrupt_history_starts_fresh_instead_of_crashing(tmp_path):
    path = tmp_path / "campaigns" / "history.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert History.for_work(tmp_path).data == {}


# ----------------------------------------------------------------- watchlist


def test_watchlist_add_is_idempotent_and_persists(tmp_path):
    watchlist = Watchlist.for_work(tmp_path)
    assert watchlist.add("abc") is True
    assert watchlist.add("abc") is False
    watchlist.save()
    assert Watchlist.for_work(tmp_path).ids == ["abc"]


def test_watchlist_remove_reports_whether_it_did_anything(tmp_path):
    watchlist = Watchlist.for_work(tmp_path)
    watchlist.add("abc")
    assert watchlist.remove("abc") is True
    assert watchlist.remove("abc") is False


# ------------------------------------------------------------------ filters


def test_platform_filter_is_case_insensitive(board):
    enrich(board)
    assert filter_campaigns(board, platform="TikTok")
    assert not filter_campaigns(board, platform="myspace")


def test_runway_filter_drops_the_nearly_dry_pools(board):
    enrich(board)
    survivors = filter_campaigns(board, min_days=20)
    assert all((c.days_left or 0) >= 20 for c in survivors)
    assert not any("ForgeGUI" in c.title for c in survivors)


def test_application_filter(board):
    enrich(board)
    assert all(not c.requires_application for c in filter_campaigns(board, include_application=False))


def test_query_matches_title_and_brand(board):
    enrich(board)
    assert len(filter_campaigns(board, query="boxabl")) == 1
    assert len(filter_campaigns(board, query="clip farm")) == 1


def test_numeric_filters_compose(board):
    enrich(board)
    assert filter_campaigns(board, min_cpm=2.0, max_creators=200, min_budget=1_000)


# ------------------------------------------------------------------ resolve


def test_resolve_by_id_prefix_and_title(board):
    boxabl = next(c for c in board if "Boxabl" in c.title)
    assert resolve(board, boxabl.id) is boxabl
    assert resolve(board, boxabl.id[:8]) is boxabl
    assert resolve(board, "boxabl") is boxabl


def test_resolve_rejects_an_ambiguous_token(board):
    with pytest.raises(KeyError, match="ambiguous"):
        resolve(board, "Clipping")


def test_resolve_rejects_an_unknown_token(board):
    with pytest.raises(KeyError, match="no campaign"):
        resolve(board, "nothing-like-this")


# ------------------------------------------------------------------ fetching


def test_fetch_refuses_paths_robots_disallows(tmp_path):
    with pytest.raises(BoardError, match="robots.txt"):
        fetch_board(tmp_path, url="https://contentrewards.com/api/campaigns")


def test_a_fresh_cache_is_reused_without_a_request(tmp_path, monkeypatch):
    cache = tmp_path / "campaigns" / "board.html"
    cache.parent.mkdir(parents=True)
    cache.write_text("cached board", encoding="utf-8")

    def explode(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("network was used despite a fresh cache")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    assert fetch_board(tmp_path, max_age=3600) == "cached board"


def test_a_stale_cache_is_served_when_the_network_fails(tmp_path, monkeypatch):
    cache = tmp_path / "campaigns" / "board.html"
    cache.parent.mkdir(parents=True)
    cache.write_text("stale board", encoding="utf-8")

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down")),
    )
    assert fetch_board(tmp_path, max_age=0) == "stale board"


def test_a_network_failure_with_no_cache_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down")),
    )
    with pytest.raises(BoardError, match="could not fetch"):
        fetch_board(tmp_path, max_age=0)

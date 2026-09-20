import pytest

from clipper.brief import Brief, Economics
from clipper.queue import Ledger, STATUS_PENDING, STATUS_POSTED, estimate_payout, write_checklist
from clipper.render import Clip


def make_clip(clip_id="abc123def456", **kw):
    defaults = dict(
        clip_id=clip_id, source_id="src", source_origin="https://example/v", campaign="demo",
        start=0.0, end=30.0, duration=30.0, score=0.5, components={}, hook="hook",
        transcript="words", caption="caption", platforms=["tiktok"], path=f"/tmp/{clip_id}.mp4",
    )
    defaults.update(kw)
    return Clip(**defaults)


@pytest.fixture
def brief():
    return Brief(
        name="demo",
        economics=Economics(rate_per_1k_views_usd=2.0, per_clip_cap_usd=10.0, min_views_to_qualify=1000),
    )


def test_estimate_payout_applies_rate_cap_and_qualifying_floor(brief):
    assert estimate_payout(5000, brief) == 10.0     # capped
    assert estimate_payout(2000, brief) == 4.0
    assert estimate_payout(500, brief) == 0.0       # under the floor


def test_estimate_payout_without_a_cap():
    brief = Brief(name="d", economics=Economics(rate_per_1k_views_usd=1.0, per_clip_cap_usd=None))
    assert estimate_payout(1_000_000, brief) == 1000.0


def test_ledger_round_trips_through_disk(tmp_path, brief):
    ledger = Ledger.for_work(tmp_path)
    ledger.add(make_clip())
    ledger.mark_posted("abc123def456", "tiktok", "https://tiktok/x")
    ledger.record_views("abc123def456", "tiktok", 3000, brief)
    ledger.save()

    reloaded = Ledger.for_work(tmp_path)
    entry = reloaded.get("abc123def456")
    assert entry.status == STATUS_POSTED
    assert entry.total_views() == 3000
    assert entry.total_payout() == 6.0


def test_clip_ids_can_be_addressed_by_unique_prefix(tmp_path):
    ledger = Ledger.for_work(tmp_path)
    ledger.add(make_clip())
    assert ledger.get("abc1").clip_id == "abc123def456"


def test_ambiguous_prefix_is_an_error(tmp_path):
    ledger = Ledger.for_work(tmp_path)
    ledger.add(make_clip("aaa111"))
    ledger.add(make_clip("aaa222"))
    with pytest.raises(KeyError, match="ambiguous"):
        ledger.get("aaa")


def test_unknown_clip_id_is_an_error(tmp_path):
    with pytest.raises(KeyError, match="no clip"):
        Ledger.for_work(tmp_path).get("nope")


def test_rerendering_a_clip_keeps_its_posting_history(tmp_path):
    ledger = Ledger.for_work(tmp_path)
    ledger.add(make_clip())
    ledger.mark_posted("abc123def456", "tiktok", "https://tiktok/x")
    ledger.add(make_clip(caption="new caption"))

    entry = ledger.get("abc123def456")
    assert entry.status == STATUS_POSTED
    assert len(entry.postings) == 1
    assert entry.caption == "new caption"


def test_marking_the_same_posting_twice_is_idempotent(tmp_path):
    ledger = Ledger.for_work(tmp_path)
    ledger.add(make_clip())
    ledger.mark_posted("abc123def456", "tiktok", "https://tiktok/x")
    ledger.mark_posted("abc123def456", "tiktok", "https://tiktok/x")
    assert len(ledger.get("abc123def456").postings) == 1


def test_recording_views_for_an_unposted_platform_is_an_error(tmp_path, brief):
    ledger = Ledger.for_work(tmp_path)
    ledger.add(make_clip())
    with pytest.raises(KeyError, match="no posting"):
        ledger.record_views("abc123def456", "instagram", 100, brief)


def test_stats_reports_the_effective_cpm_not_the_headline_rate(tmp_path, brief):
    ledger = Ledger.for_work(tmp_path)
    for i in range(3):
        cid = f"clip{i:08d}"
        ledger.add(make_clip(cid))
        ledger.mark_posted(cid, "tiktok", f"https://tiktok/{i}")
        ledger.record_views(cid, "tiktok", 10_000, brief)  # each capped at $10

    stats = ledger.stats()
    assert stats["clips"] == 3
    assert stats["total_views"] == 30_000
    assert stats["estimated_payout_usd"] == 30.0
    # Headline rate is $2.00/1k; the per-clip cap drags the real number down.
    assert stats["effective_cpm_usd"] == 1.0


def test_stats_on_an_empty_ledger(tmp_path):
    stats = Ledger.for_work(tmp_path).stats()
    assert stats["clips"] == 0
    assert stats["effective_cpm_usd"] == 0.0


def test_new_clips_start_pending(tmp_path):
    ledger = Ledger.for_work(tmp_path)
    ledger.add(make_clip())
    assert ledger.pending()[0].status == STATUS_PENDING


def test_checklist_names_the_platforms_and_the_manual_posting_rule(tmp_path):
    ledger = Ledger.for_work(tmp_path)
    entry = ledger.add(make_clip())
    brief = Brief(name="demo", platforms=["tiktok", "instagram"])
    body = write_checklist(entry, brief, tmp_path).read_text()
    assert "- [ ] tiktok" in body and "- [ ] instagram" in body
    assert "never a browser bot" in body

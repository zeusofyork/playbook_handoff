import numpy as np
import pytest

from clipper.brief import Brief, ScoreWeights
from clipper.score import (
    Moment,
    keyword_series,
    normalize,
    overlap_fraction,
    pick_hook,
    rms_envelope,
    snap_to_speech,
    speech_density,
    suppress,
    window_scores,
)

HOP = 0.5


def transcript(words):
    return {"segments": [{"start": w[0], "end": w[1], "text": w[2], "words": [
        {"start": w[0], "end": w[1], "word": w[2]}]} for w in words]}


def test_rms_envelope_tracks_amplitude():
    quiet = np.zeros(16000, dtype=np.float32)
    loud = np.full(16000, 0.5, dtype=np.float32)
    env = rms_envelope(np.concatenate([quiet, loud]), hop=0.5, sample_rate=16000)
    assert env.tolist() == pytest.approx([0.0, 0.0, 0.5, 0.5], abs=1e-4)


def test_rms_envelope_on_short_input_is_empty_not_an_error():
    assert rms_envelope(np.zeros(10, dtype=np.float32), hop=0.5, sample_rate=16000).size == 0


def test_normalize_is_robust_to_a_single_spike():
    series = np.array([0.0, 0.1, 0.2, 0.1, 100.0], dtype=np.float32)
    out = normalize(series)
    assert out.max() <= 1.0 and out.min() >= 0.0
    # The spike must not flatten the ordinary variation to zero.
    assert out[2] > out[0]


def test_normalize_flat_series_is_all_zero():
    assert normalize(np.full(10, 3.0, dtype=np.float32)).tolist() == [0.0] * 10


def test_window_scores_prefers_the_loud_region():
    energy = np.array([0, 0, 0, 0, 1, 1, 1, 1], dtype=np.float32)
    zeros = np.zeros(8, dtype=np.float32)
    moments = window_scores(
        energy, zeros, zeros, hop=HOP, window_seconds=2.0, stride_seconds=0.5,
        weights=ScoreWeights(),
    )
    best = max(moments, key=lambda m: m.score)
    assert best.start >= 2.0


def test_window_scores_honours_skip_regions():
    energy = np.ones(20, dtype=np.float32)
    zeros = np.zeros(20, dtype=np.float32)
    moments = window_scores(
        energy, zeros, zeros, hop=HOP, window_seconds=2.0, stride_seconds=0.5,
        weights=ScoreWeights(), skip_start_hops=4, skip_end_hops=4,
    )
    assert min(m.start for m in moments) == pytest.approx(2.0)
    assert max(m.end for m in moments) <= 8.0


def test_window_scores_returns_nothing_when_source_is_shorter_than_the_window():
    short = np.ones(2, dtype=np.float32)
    assert window_scores(
        short, short, short, hop=HOP, window_seconds=30.0, stride_seconds=1.0,
        weights=ScoreWeights(),
    ) == []


def test_speech_density_counts_words_per_hop():
    series = speech_density(transcript([(0.1, 0.4, "a"), (0.2, 0.5, "b")]), n_hops=6, hop=HOP)
    assert series.sum() > 0


def test_keyword_series_marks_only_keyword_hops():
    data = transcript([(0.0, 0.4, " azure"), (1.0, 1.4, " weather")])
    series = keyword_series(data, n_hops=6, hop=HOP, keywords=["azure"])
    assert series[0] == 1.0
    assert series[2] == 0.0


def test_keyword_series_without_keywords_is_all_zero():
    data = transcript([(0.0, 0.4, " azure")])
    assert keyword_series(data, 6, HOP, []).sum() == 0.0


def test_overlap_fraction_uses_the_shorter_window():
    assert overlap_fraction(Moment(0, 10, 1.0), Moment(5, 15, 1.0)) == pytest.approx(0.5)
    assert overlap_fraction(Moment(0, 10, 1.0), Moment(20, 30, 1.0)) == 0.0


def test_suppress_keeps_the_best_and_drops_neighbours():
    candidates = [
        Moment(0, 30, 0.9),
        Moment(5, 35, 0.95),   # overlaps the winner
        Moment(200, 230, 0.4),  # far away, survives
    ]
    kept = suppress(candidates, max_overlap=0.25, min_gap=30.0, limit=5)
    assert [m.start for m in kept] == [5, 200]


def test_suppress_respects_the_limit():
    candidates = [Moment(i * 100, i * 100 + 30, 1.0 - i / 100) for i in range(10)]
    assert len(suppress(candidates, max_overlap=0.25, min_gap=30.0, limit=3)) == 3


def test_snap_to_speech_moves_boundaries_onto_word_edges():
    brief = Brief(name="demo", min_seconds=5, target_seconds=10, max_seconds=60)
    data = transcript([(10.2, 10.6, "start"), (29.4, 29.9, "end")])
    snapped = snap_to_speech(Moment(10.0, 30.0, 1.0), data, brief=brief, tolerance=2.0)
    assert snapped.start == pytest.approx(10.2)
    assert snapped.end == pytest.approx(29.9)


def test_snap_to_speech_refuses_to_shrink_below_min_seconds():
    brief = Brief(name="demo", min_seconds=20, target_seconds=30, max_seconds=60)
    data = transcript([(10.0, 10.4, "a"), (20.0, 20.5, "b")])
    moment = Moment(9.0, 21.0, 1.0)
    assert snap_to_speech(moment, data, brief=brief, tolerance=2.0).start == 9.0


def test_snap_to_speech_without_a_transcript_is_a_no_op():
    brief = Brief(name="demo")
    moment = Moment(1.0, 40.0, 1.0)
    assert snap_to_speech(moment, None, brief=brief) is moment


def test_pick_hook_fills_the_template_from_the_moment():
    brief = Brief(name="demo", keywords=["azure"])
    brief.hook_style.templates = ["what about {keyword}?", "\"{quote}\""]
    moment = Moment(0, 30, 1.0, text="they said azure was cheap")
    assert pick_hook(moment, brief) == "what about azure?"


def test_pick_hook_with_no_templates_is_empty():
    brief = Brief(name="demo")
    brief.hook_style.templates = []
    assert pick_hook(Moment(0, 30, 1.0, text="hi"), brief) == ""


def test_brief_fingerprint_changes_with_selection_knobs():
    from clipper.score import brief_fingerprint

    base = Brief(name="demo", keywords=["azure"])
    assert brief_fingerprint(base) == brief_fingerprint(Brief(name="demo", keywords=["AZURE"]))
    assert brief_fingerprint(base) != brief_fingerprint(Brief(name="demo", keywords=["aws"]))
    assert brief_fingerprint(base) != brief_fingerprint(
        Brief(name="demo", keywords=["azure"], target_seconds=50, max_seconds=60)
    )


def test_brief_fingerprint_ignores_fields_that_do_not_affect_selection():
    from clipper.score import brief_fingerprint

    a = Brief(name="demo", keywords=["azure"])
    b = Brief(name="demo", keywords=["azure"], video_crf=30, hashtags=["#x"])
    assert brief_fingerprint(a) == brief_fingerprint(b)

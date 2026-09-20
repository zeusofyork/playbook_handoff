import pytest

from clipper.brief import Brief, BriefError, CaptionStyle


def test_defaults_validate():
    Brief(name="demo").validate()


def test_nested_dataclasses_are_constructed_not_left_as_dicts():
    brief = Brief.from_dict({"name": "demo", "caption_style": {"font_size": 40}})
    assert isinstance(brief.caption_style, CaptionStyle)
    assert brief.caption_style.font_size == 40


def test_unknown_key_is_rejected_with_the_key_named():
    with pytest.raises(BriefError, match="hook_stile"):
        Brief.from_dict({"name": "demo", "hook_stile": {}})


def test_string_where_list_expected_is_rejected():
    with pytest.raises(BriefError, match="must be a list"):
        Brief.from_dict({"name": "demo", "platforms": "tiktok"})


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"min_seconds": 40, "target_seconds": 30}, "durations must satisfy"),
        ({"platforms": ["myspace"]}, "unknown platforms"),
        ({"reframe": "zoom"}, "reframe must be one of"),
        ({"width": 1081}, "must both be even"),
        ({"hashtags": ["clips"]}, "must start with"),
        ({"crop_anchor": 1.5}, "crop_anchor"),
        ({"max_overlap": 1.0}, "max_overlap"),
    ],
)
def test_validation_rejects_contradictions(overrides, message):
    brief = Brief(name="demo", **overrides)
    with pytest.raises(BriefError, match=message):
        brief.validate()


def test_bad_ass_color_is_rejected():
    brief = Brief(name="demo")
    brief.caption_style.primary_color = "white"
    with pytest.raises(BriefError, match="ASS color"):
        brief.validate()


def test_banned_hits_match_on_word_boundaries():
    brief = Brief(name="demo", banned_words=["scam", "free"])
    assert brief.banned_hits("this is a SCAM") == ["scam"]
    assert brief.banned_hits("scamper freedom") == []


def test_caption_template_drops_unknown_fields_and_collapses_blank_lines():
    brief = Brief(
        name="demo",
        hashtags=["#a", "#b"],
        caption_template="{hook}\n\n{attribution}\n\n{nope}\n\n{hashtags}",
    )
    assert brief.render_caption("HOOK") == "HOOK\n\n#a #b"


def test_slug_is_filesystem_safe():
    assert Brief(name="Lacy's Clips -- Q4!").slug == "lacy-s-clips-q4"


def test_packaged_template_parses():
    from pathlib import Path

    import clipper

    template = Path(clipper.__file__).parent / "templates" / "campaign.yml"
    brief = Brief.from_yaml(template)
    assert brief.name == "example-campaign"

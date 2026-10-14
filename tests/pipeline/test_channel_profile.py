"""Contract of a Content Engine channel profile."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from src.pipeline.channel_profile import ChannelProfile


def _profile(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "slug": "mesa-em-ordem",
        "name": "Mesa em Ordem",
        "language": "pt-BR",
        "niche": "organização e produtividade para estudantes",
        "persona": "Fala direto, com exemplos do dia a dia de quem estuda em casa.",
        "formats": "both",
        "visual_style": "photo",
        "caption_templates": {"short": "word_pop", "long": "phrase_blocks"},
        "voice_reference_id": "owner-2026-10",
        "videos_per_day": 2,
    }
    data.update(changes)
    return data


def test_a_complete_profile_is_accepted_as_draft() -> None:
    profile = ChannelProfile.model_validate(_profile())

    assert profile.status == "draft"
    assert profile.published_formats() == ("short", "long")


def test_a_single_format_channel_publishes_only_that_format() -> None:
    profile = ChannelProfile.model_validate(
        _profile(formats="short", caption_templates={"short": "word_pop"})
    )

    assert profile.published_formats() == ("short",)


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"slug": "Mesa em Ordem"}, "slug"),
        ({"language": "en"}, "language"),
        ({"niche": "  "}, "niche"),
        ({"formats": "short"}, "exactly the published formats"),
        ({"caption_templates": {"short": "word_pop"}}, "exactly the published formats"),
        ({"caption_templates": {"short": "word_pop", "long": "karaoke"}}, "caption_templates"),
        ({"visual_style": "anime"}, "visual_style"),
        ({"videos_per_day": 5}, "videos_per_day"),
        ({"voice_reference_id": "C:\\voz\\ref.wav"}, "voice_reference_id"),
        ({"oauth_refresh_token": "x"}, "oauth_refresh_token"),
    ],
)
def test_an_invalid_profile_is_refused(changes: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        ChannelProfile.model_validate(_profile(**changes))

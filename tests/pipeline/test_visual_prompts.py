"""Scene descriptions for generated A3 visuals, with a fake script model."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.pipeline.visual_prompts import (
    SCENE_PROMPT_ATTEMPTS,
    VISUAL_STYLE_LABELS,
    VISUAL_STYLES,
    ScenePromptError,
    scene_prompt_problem,
    scene_prompt_request,
    styled_prompt,
    write_scene_prompts,
)

_GOOD = "A wooden desk seen from above with only a pencil, scissors and blank paper in the center"
_OTHER = "A small ceramic tray on the edge of a desk holding pens and paper clips"
_NARRATION = "Pega só o que vai usar. Põe os objetos soltos num pote."
_STRETCHES = ("Pega só o que vai usar.", "Põe os objetos soltos num pote.")


def test_scene_prompt_problem_accepts_a_plain_english_scene() -> None:
    assert scene_prompt_problem(_GOOD) is None


@pytest.mark.parametrize(
    ("prompt", "fragment"),
    [
        ("A desk", "has 2 words"),
        (" ".join(["desk"] * 61), "has 61 words"),
        ("Uma mesa de madeira vista de cima com lápis, tesoura e papéis no centro", "English"),
        ("A notebook page with a chart and a pencil resting on a wooden desk", "'chart'"),
        ("A hand lifts a pen above an open notebook on a desk without visible text", "'text'"),
        ("A cluttered desk with pens and papers while a laptop sits in the background", "'laptop'"),
        ("A wooden desk with pens drawn in the style of a famous animation studio", "'style of'"),
    ],
)
def test_scene_prompt_problem_explains_the_refusal(prompt: str, fragment: str) -> None:
    problem = scene_prompt_problem(prompt)
    assert problem is not None and fragment in problem


def test_write_scene_prompts_returns_one_clean_prompt_per_stretch() -> None:
    call = MagicMock(side_effect=[f'"{_GOOD}"', f"  {_OTHER}\n"])
    prompts = write_scene_prompts(
        call, topic="Mesa", narration=_NARRATION, stretches=_STRETCHES
    )
    assert prompts == (_GOOD, _OTHER)
    second_user = call.call_args_list[1].args[1]
    assert f"- {_GOOD}" in second_user and "number 2" in second_user


def test_write_scene_prompts_retries_with_the_refusal_reason() -> None:
    refused = "A desk with a logo on a mug and many pens around it on the table"
    call = MagicMock(side_effect=[refused, _GOOD])
    assert write_scene_prompts(
        call, topic="Mesa", narration=_NARRATION, stretches=_STRETCHES[:1]
    ) == (_GOOD,)
    retry_user = call.call_args_list[1].args[1]
    assert "refused because it mentions 'logo'" in retry_user and refused in retry_user


def test_write_scene_prompts_fails_closed_after_the_attempt_ceiling() -> None:
    call = MagicMock(return_value="Uma mesa de madeira com lápis, tesoura e papéis no centro")
    with pytest.raises(ScenePromptError, match="stretch 1"):
        write_scene_prompts(call, topic="Mesa", narration=_NARRATION, stretches=_STRETCHES)
    assert call.call_count == SCENE_PROMPT_ATTEMPTS


def test_scene_prompt_request_keeps_style_readable_content_and_people_out() -> None:
    system, user = scene_prompt_request(
        topic="Mesa", narration=_NARRATION, index=1, stretch=_STRETCHES[0], previous=()
    )
    assert "visual style is added separately" in system
    assert "Never say what is absent" in system
    assert "No real person" in system
    assert "(none yet)" in user and _NARRATION in user


def test_every_style_forbids_text_and_has_an_operator_label() -> None:
    assert set(VISUAL_STYLES) == set(VISUAL_STYLE_LABELS)
    assert all("no text" in style and "no logos" in style for style in VISUAL_STYLES.values())
    assert all("style of" not in style for style in VISUAL_STYLES.values())
    assert styled_prompt(_GOOD, "dark") == f"{_GOOD}, {VISUAL_STYLES['dark']}"


def test_revised_description_carries_the_note_the_old_scene_and_the_kept_ones() -> None:
    from src.pipeline.visual_prompts import revise_scene_prompt, revise_scene_prompt_request

    system, user = revise_scene_prompt_request(
        topic="Mesa",
        narration=_NARRATION,
        stretch=_STRETCHES[0],
        previous_scene=_OTHER,
        note="Tire a pessoa, só a mesa",
        other_scenes=(_GOOD,),
    )
    assert "visual style is added separately" in system
    assert f"«{_OTHER}»" in user and "«Tire a pessoa, só a mesa»" in user
    assert f"- {_GOOD}" in user and _STRETCHES[0] in user

    call = MagicMock(side_effect=["A desk with a laptop on it and a few pens nearby here", _GOOD])
    assert (
        revise_scene_prompt(
            call,
            topic="Mesa",
            narration=_NARRATION,
            stretch=_STRETCHES[0],
            previous_scene=_OTHER,
            note="só a mesa",
            other_scenes=(),
        )
        == _GOOD
    )
    assert "refused because it mentions 'laptop'" in call.call_args_list[1].args[1]
    with pytest.raises(ScenePromptError):
        revise_scene_prompt(
            MagicMock(return_value="Uma mesa de madeira com lápis e papéis no centro dela"),
            topic="Mesa",
            narration=_NARRATION,
            stretch=_STRETCHES[0],
            previous_scene=_OTHER,
            note="só a mesa",
            other_scenes=(),
        )


def test_scene_is_recovered_from_a_styled_prompt() -> None:
    from src.pipeline.visual_prompts import scene_from_prompt

    assert scene_from_prompt(styled_prompt(_GOOD, "cartoon"), "cartoon") == _GOOD

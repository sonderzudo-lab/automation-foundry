"""One real ``codex exec`` call; it uses the owner's ChatGPT plan quota.

Opt-in only, with the owner's authorization for each run:

    AUTOMATION_FOUNDRY_TEST_CODEX_EXEC=1 pytest tests/integration/test_codex_exec_text_model.py

It confirms in a single call that the CLI answers through the plan and that
``--output-schema`` accepts the scene plan schema as it is.
"""

from __future__ import annotations

import os

import pytest

from src.pipeline.scene_plan import parse_scene_plan, scene_plan_response_schema
from src.pipeline.text_model import TextModelRequest
from src.pipeline.text_model_codex import CodexExecTextModel


@pytest.mark.skipif(
    os.environ.get("AUTOMATION_FOUNDRY_TEST_CODEX_EXEC") != "1",
    reason="consumes the ChatGPT plan quota; set AUTOMATION_FOUNDRY_TEST_CODEX_EXEC=1",
)
def test_codex_exec_answers_a_scene_plan_through_the_chatgpt_plan() -> None:
    model = CodexExecTextModel(
        executable=os.environ.get("CONTENT_TEXT_MODEL_CODEX_EXECUTABLE", "codex"),
        model=os.environ.get("CONTENT_TEXT_MODEL_CODEX_MODEL") or None,
    )
    request = TextModelRequest(
        purpose="scene_plan",
        system=(
            "You plan the images of a short video. Answer a JSON object with the given "
            "schema: schema_version 1, recurring characters and settings described by "
            "appearance only, and exactly 2 scenes numbered 1 and 2. Every description is "
            "plain English, 10 to 30 words, with no text, screens, logos or real people."
        ),
        user=(
            "Stretch 1: Pegue só o que vai usar e guarde o resto numa caixa.\n"
            "Stretch 2: Coloque as canetas num pote para a mesa ficar livre."
        ),
        response_schema=scene_plan_response_schema(),
    )

    result = model.complete(request)

    assert result.provider == "codex_exec"
    plan = parse_scene_plan(result.text, scene_count=2)
    assert [scene.index for scene in plan.scenes] == [1, 2]

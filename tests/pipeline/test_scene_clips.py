"""Contract of the animated scenes manifest (A5a), read by the montage."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from src.pipeline.scene_clips import (
    MAX_SCENE_CLIP_FRAMES,
    SceneClipsManifest,
    scene_clip_frames,
    scene_clip_ms,
    scene_clip_window,
)
from src.pipeline.scene_motion import NEUTRAL_MOTION
from src.pipeline.timeline_builder import SceneSpan, scene_cuts_sha256

_MOTION = "the adult slowly folds the shirt on the bed while the camera pushes in"
_SPANS = [(0, 4_000), (4_000, 9_000), (9_000, 12_000)]


def _cuts(spans: list[tuple[int, int]]) -> str:
    return scene_cuts_sha256(
        [SceneSpan(index=i, start_ms=s, end_ms=e) for i, (s, e) in enumerate(spans, start=1)]
    )


def _clip(index: int, frames: int, **changes: Any) -> dict[str, Any]:
    start, end = _SPANS[index - 1]
    scene = {
        "kind": "clip", "index": index, "image": f"visual-{index:03d}.png",
        "image_sha256": f"{index:x}" * 64, "start_ms": start, "end_ms": end,
        "motion": _MOTION, "seed": 190_000 + index, "frames": frames,
        "video": f"scene-{index:03d}.mp4", "video_sha256": f"{index + 5:x}" * 64,
    }
    scene.update(changes)
    return scene


def _manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "format": "short",
        "width": 1080,
        "height": 1920,
        "duration_ms": 12_000,
        "transition_ms": 400,
        "fps": 25,
        "cuts_sha256": _cuts(_SPANS),
        "provider": "local-ltx23",
        "model_id": "Lightricks/LTX-2.3-fp8@1d756cd2/ltx-2.3-22b-distilled-fp8",
        "model_inventory_sha256": "e" * 64,
        "graph_version": "ltx23-i2v-two-stage-v1",
        "license_id": "LTX-2-Community-License-2026-01-05",
        "usage_scope": "quality-test-only",
        # Windows: 0-4200 ms, 3800-9200 ms and 8800-12000 ms.
        "scenes": [
            _clip(1, 105),
            {"kind": "still", "index": 2, "image": "visual-002.png", "image_sha256": "2" * 64,
             "start_ms": 4_000, "end_ms": 9_000, "reason": "generation_failed"},
            _clip(3, 81, motion=NEUTRAL_MOTION),
        ],
    }


def _changed(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(_manifest())
    data.update(changes)
    return data


def test_a_consistent_manifest_is_accepted() -> None:
    manifest = SceneClipsManifest.model_validate(_manifest())

    assert [scene.kind for scene in manifest.scenes] == ["clip", "still", "clip"]
    assert SceneClipsManifest.model_validate_json(manifest.model_dump_json()) == manifest


@pytest.mark.parametrize(
    ("window_ms", "frames"),
    [
        (100, 9),
        (4_200, 105),
        (5_400, 137),
        (8_040, 201),
        # Run 19 scene 2: 8.76 s plus both half crossfades, played 1.14x slower.
        (9_160, 201),
        (10_050, 201),
        (10_051, None),
    ],
)
def test_clip_frames_follow_the_8k_plus_1_rule_and_the_ram_limit(
    window_ms: int, frames: int | None
) -> None:
    assert scene_clip_frames(window_ms) == frames
    if frames is not None:
        assert frames % 8 == 1
        assert frames <= MAX_SCENE_CLIP_FRAMES


def test_a_clip_covers_half_of_each_crossfade_inside_the_audio() -> None:
    span = SceneSpan(index=2, start_ms=4_000, end_ms=9_000)

    assert scene_clip_window(span, duration_ms=12_000, transition_ms=400) == (3_800, 9_200)
    assert scene_clip_window(
        SceneSpan(index=1, start_ms=0, end_ms=4_000), duration_ms=12_000, transition_ms=400
    ) == (0, 4_200)
    assert scene_clip_ms(201) == 8_040


def test_a_scene_too_long_for_a_clip_stays_a_still_image() -> None:
    spans = [(0, 1_000), (1_000, 12_000)]
    data = _changed(
        duration_ms=12_000,
        cuts_sha256=_cuts(spans),
        scenes=[
            {"kind": "still", "index": 1, "image": "visual-001.png", "image_sha256": "1" * 64,
             "start_ms": 0, "end_ms": 1_000, "reason": "owner_choice"},
            {"kind": "still", "index": 2, "image": "visual-002.png", "image_sha256": "2" * 64,
             "start_ms": 1_000, "end_ms": 12_000, "reason": "too_long"},
        ],
    )
    SceneClipsManifest.model_validate(data)

    data["scenes"][1] = {
        **_clip(2, MAX_SCENE_CLIP_FRAMES), "start_ms": 1_000, "end_ms": 12_000,
    }
    with pytest.raises(ValidationError, match="too long for a clip"):
        SceneClipsManifest.model_validate(data)


def _with_scene(position: int, scene: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(_manifest())
    data["scenes"][position] = scene
    return data


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (_changed(width=1920, height=1080), "dimensions"),
        (_changed(cuts_sha256="0" * 64), "cuts_sha256"),
        (_changed(fps=30), "fps"),
        (_changed(usage_scope="commercial"), "usage_scope"),
        (_with_scene(0, _clip(1, 113)), "must have 105 frames"),
        (_with_scene(0, _clip(1, 104)), "must have 105 frames"),
        (_with_scene(0, _clip(1, 105, motion="the adult talks to the camera while folding")),
         "motion that would be refused"),
        (_with_scene(2, _clip(3, 81, video="scene-001.mp4")), "reuses the video"),
        (_with_scene(1, {**_manifest()["scenes"][1], "reason": "too_long"}),
         "marked too long but fits"),
        (_with_scene(1, {**_manifest()["scenes"][1], "start_ms": 4_100}), "does not start where"),
        (_with_scene(0, _clip(1, 105, video="scene-001.mov")), "video"),
        (_with_scene(0, {**_clip(1, 105), "kind": "gif"}), "kind"),
    ],
)
def test_inconsistent_manifests_are_refused(data: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        SceneClipsManifest.model_validate(data)


def test_scenes_must_cover_the_audio_from_zero() -> None:
    data = copy.deepcopy(_manifest())
    data["scenes"][2]["end_ms"] = 11_000

    with pytest.raises(ValidationError, match="does not end with the audio"):
        SceneClipsManifest.model_validate(data)

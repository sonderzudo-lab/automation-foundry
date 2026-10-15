"""The pinned ComfyUI runtime of A5a, against a fake ComfyUI started as a real process."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

import psutil
import pytest

from src.pipeline.comfyui_runtime import (
    LTX23_CHECKPOINT,
    LTX23_FPS,
    LTX23_TEXT_ENCODER,
    LTX23_UPSCALER,
    ClipRenderRequest,
    ComfyUIConfig,
    ComfyUILtx23Renderer,
    SceneClipFailedError,
    SceneClipTimeoutError,
    SceneVideoMisconfiguredError,
    SceneVideoUnavailableError,
    ltx23_graph,
    ltx23_prompt,
    model_inventory_sha256,
)
from src.pipeline.scene_clips import SCENE_CLIP_FPS

_FAKE_MAIN = Path(__file__).with_name("fake_comfyui_main.py")
_WEIGHTS = {
    f"checkpoints/{LTX23_CHECKPOINT}": b"checkpoint",
    f"latent_upscale_models/{LTX23_UPSCALER}": b"upscaler",
    f"text_encoders/{LTX23_TEXT_ENCODER}": b"gemma",
}
_MOTION = "The adult slowly lifts the folded shirt from the bed while the camera pushes in"


@pytest.fixture
def record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "record"
    monkeypatch.setenv("FAKE_COMFYUI_RECORD", str(folder))
    monkeypatch.setenv("FAKE_COMFYUI_MODE", "ok")
    return folder


def _install(tmp_path: Path, *, version: str = "0.40.0", **options: object) -> ComfyUIConfig:
    comfyui = tmp_path / "ComfyUI"
    comfyui.mkdir()
    shutil.copyfile(_FAKE_MAIN, comfyui / "main.py")
    (comfyui / "comfyui_version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    models = tmp_path / "models"
    for relative, content in _WEIGHTS.items():
        (models / relative).parent.mkdir(parents=True, exist_ok=True)
        (models / relative).write_bytes(content)
    values: dict[str, object] = {
        "comfyui_dir": str(comfyui),
        "python_path": sys.executable,
        "models_dir": str(models),
        "ffmpeg_path": "ffmpeg-not-used",
        "ffmpeg_sha256": None,
        "min_free_ram_bytes": 1024,
        "inventory": {
            relative: hashlib.sha256(content).hexdigest() for relative, content in _WEIGHTS.items()
        },
        "poll_interval_seconds": 0.05,
    }
    values.update(options)
    return ComfyUIConfig(**values)  # type: ignore[arg-type]


def _encoder(calls: list[tuple[int, int, int]]):  # type: ignore[no-untyped-def]
    def encode(frames: list[Path], output: Path, width: int, height: int, timeout: float) -> None:
        calls.append((len(frames), width, height))
        output.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32)

    return encode


def _renderer(
    config: ComfyUIConfig,
    tmp_path: Path,
    *,
    encoded: list[tuple[int, int, int]] | None = None,
    released: list[str] | None = None,
    ram: int = 64 * 1024**3,
) -> ComfyUILtx23Renderer:
    return ComfyUILtx23Renderer(
        config,
        workspace=tmp_path / "workspace",
        release_text_model=(lambda: released.append("released")) if released is not None else None,
        encoder=_encoder(encoded if encoded is not None else []),
        available_ram_bytes=lambda: ram,
        sample_memory=lambda: (18_000, 40 * 1024**3),
    )


def _request(tmp_path: Path, *, frames: int = 17) -> ClipRenderRequest:
    image = tmp_path / "visual-001.png"
    image.write_bytes(b"approved image")
    return ClipRenderRequest(
        index=1,
        image_path=image,
        image_sha256=hashlib.sha256(b"approved image").hexdigest(),
        motion=_MOTION,
        seed=1234,
        frames=frames,
        format="short",
        width=1080,
        height=1920,
        output_path=tmp_path / "clip.mp4",
    )


def _gone(pid: int) -> bool:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.1)
    return False


def test_the_clip_is_rendered_by_the_lab_graph_on_an_offline_loopback_server(
    tmp_path: Path, record: Path
) -> None:
    config = _install(tmp_path)
    encoded: list[tuple[int, int, int]] = []
    released: list[str] = []
    renderer = _renderer(config, tmp_path, encoded=encoded, released=released)
    try:
        renderer.start(timeout_seconds=30)
        result = renderer.render(_request(tmp_path), timeout_seconds=30)
    finally:
        renderer.close()

    graph = json.loads((record / "graph.json").read_text(encoding="utf-8"))
    image_name = graph["8"]["inputs"]["image"]
    assert graph == ltx23_graph(
        image_name=image_name,
        motion=_MOTION,
        seed=1234,
        frames=17,
        width=704,
        height=1280,
        filename_prefix=graph["30"]["inputs"]["filename_prefix"],
    )
    assert graph["1"]["inputs"]["ckpt_name"] == LTX23_CHECKPOINT
    assert graph["10"]["inputs"] == {"width": 352, "height": 640, "length": 17, "batch_size": 1}
    assert [graph["11"]["inputs"]["strength"], graph["21"]["inputs"]["strength"]] == [0.7, 1.0]
    assert graph["16"]["inputs"]["sigmas"].count(",") == 8  # 8 distilled steps
    assert graph["25"]["inputs"]["sigmas"].count(",") == 3  # 3 refinement steps
    assert graph["17"]["inputs"]["noise_seed"] == 1234
    assert graph["5"]["inputs"]["text"] == ltx23_prompt(_MOTION)
    assert (record / "image_present").read_text(encoding="utf-8") == "True"

    argv = json.loads((record / "argv.json").read_text(encoding="utf-8"))
    assert argv[argv.index("--listen") + 1] == "127.0.0.1"
    assert argv[argv.index("--base-directory") + 1] == str(tmp_path / "workspace")
    assert {"--disable-all-custom-nodes", "--offline", "--disable-auto-launch"} <= set(argv)
    assert "--use-sage-attention" not in argv and "--fast" not in argv
    assert (record / "offline").read_text(encoding="utf-8") == "1"
    paths = (tmp_path / "workspace" / "extra_model_paths.yaml").read_text(encoding="utf-8")
    assert json.dumps((tmp_path / "models").resolve().as_posix()) in paths
    assert "  checkpoints: checkpoints" in paths

    assert released == ["released"]
    assert encoded == [(17, 1080, 1920)]
    assert (tmp_path / "clip.mp4").read_bytes()[4:8] == b"ftyp"
    assert not list((tmp_path / "workspace" / "output").rglob("*.png"))
    assert result.peak_vram_mib == 18_000 and result.peak_ram_bytes == 40 * 1024**3
    assert _gone(int((record / "pid").read_text(encoding="utf-8")))
    assert not renderer.running


def test_speed_options_are_passed_only_when_enabled(tmp_path: Path, record: Path) -> None:
    config = _install(tmp_path, fp16_accumulation=True)
    renderer = _renderer(config, tmp_path)
    try:
        renderer.start(timeout_seconds=30)
    finally:
        renderer.close()

    argv = json.loads((record / "argv.json").read_text(encoding="utf-8"))
    assert argv[argv.index("--fast") + 1] == "fp16_accumulation"


def test_sage_attention_must_be_installed_in_the_comfyui_environment(
    tmp_path: Path, record: Path
) -> None:
    renderer = _renderer(_install(tmp_path, sage_attention=True), tmp_path)

    with pytest.raises(SceneVideoMisconfiguredError) as raised:
        renderer.start(timeout_seconds=30)

    assert raised.value.code == "CONTENT_SCENE_ANIMATION_SAGE_ATTENTION_UNAVAILABLE"
    assert not (record / "argv.json").exists()


def test_a_tampered_weight_file_stops_before_anything_starts(tmp_path: Path, record: Path) -> None:
    config = _install(tmp_path)
    (tmp_path / "models" / f"checkpoints/{LTX23_CHECKPOINT}").write_bytes(b"other weights")
    released: list[str] = []
    renderer = _renderer(config, tmp_path, released=released)

    with pytest.raises(SceneVideoMisconfiguredError) as raised:
        renderer.start(timeout_seconds=30)

    assert raised.value.code == "CONTENT_SCENE_ANIMATION_MODEL_HASH_MISMATCH"
    assert not (record / "argv.json").exists()
    assert released == []


def test_a_missing_weight_file_is_never_downloaded(tmp_path: Path, record: Path) -> None:
    config = _install(tmp_path)
    (tmp_path / "models" / f"text_encoders/{LTX23_TEXT_ENCODER}").unlink()

    with pytest.raises(SceneVideoMisconfiguredError) as raised:
        _renderer(config, tmp_path).start(timeout_seconds=30)

    assert raised.value.code == "CONTENT_SCENE_ANIMATION_MODEL_UNAVAILABLE"
    assert not (record / "argv.json").exists()


def test_another_comfyui_version_is_refused(tmp_path: Path, record: Path) -> None:
    with pytest.raises(SceneVideoMisconfiguredError) as raised:
        _renderer(_install(tmp_path, version="0.39.0"), tmp_path).start(timeout_seconds=30)

    assert raised.value.code == "CONTENT_SCENE_ANIMATION_COMFYUI_VERSION_MISMATCH"


def test_a_server_that_answers_with_another_version_is_stopped(
    tmp_path: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COMFYUI_VERSION", "0.41.0")
    renderer = _renderer(_install(tmp_path), tmp_path)

    with pytest.raises(SceneVideoMisconfiguredError):
        renderer.start(timeout_seconds=30)

    assert _gone(int((record / "pid").read_text(encoding="utf-8")))


def test_not_enough_free_ram_frees_the_text_model_and_starts_nothing(
    tmp_path: Path, record: Path
) -> None:
    released: list[str] = []
    renderer = _renderer(_install(tmp_path), tmp_path, released=released, ram=512)

    with pytest.raises(SceneVideoUnavailableError) as raised:
        renderer.start(timeout_seconds=30)

    assert raised.value.code == "CONTENT_SCENE_ANIMATION_RAM_INSUFFICIENT"
    assert released == ["released"]
    assert not (record / "argv.json").exists()


def test_a_server_that_exits_while_starting_is_retryable(
    tmp_path: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COMFYUI_MODE", "exit")
    renderer = _renderer(_install(tmp_path), tmp_path)

    with pytest.raises(SceneVideoUnavailableError) as raised:
        renderer.start(timeout_seconds=30)

    assert raised.value.code == "CONTENT_SCENE_ANIMATION_COMFYUI_EXITED"
    assert not renderer.running


def test_a_clip_past_its_timeout_kills_the_whole_process_tree(
    tmp_path: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COMFYUI_MODE", "hang")
    renderer = _renderer(_install(tmp_path), tmp_path)
    try:
        renderer.start(timeout_seconds=30)
        started = time.monotonic()
        with pytest.raises(SceneClipTimeoutError):
            renderer.render(_request(tmp_path), timeout_seconds=1.0)
        elapsed = time.monotonic() - started
    finally:
        renderer.close()

    assert elapsed < 30
    assert not renderer.running
    assert _gone(int((record / "pid").read_text(encoding="utf-8")))
    assert _gone(int((record / "child_pid").read_text(encoding="utf-8")))


def test_a_clip_the_graph_fails_on_leaves_the_runtime_running(
    tmp_path: Path, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_COMFYUI_MODE", "error")
    renderer = _renderer(_install(tmp_path), tmp_path)
    try:
        renderer.start(timeout_seconds=30)
        with pytest.raises(SceneClipFailedError) as raised:
            renderer.render(_request(tmp_path), timeout_seconds=30)
        assert renderer.running
    finally:
        renderer.close()

    assert raised.value.code == "CONTENT_SCENE_ANIMATION_CLIP_FAILED"
    assert not (tmp_path / "clip.mp4").exists()


def test_an_image_that_changed_since_a3_is_not_animated(tmp_path: Path, record: Path) -> None:
    renderer = _renderer(_install(tmp_path), tmp_path)
    request = _request(tmp_path)
    request.image_path.write_bytes(b"changed")
    try:
        renderer.start(timeout_seconds=30)
        with pytest.raises(SceneClipFailedError) as raised:
            renderer.render(request, timeout_seconds=30)
    finally:
        renderer.close()

    assert raised.value.code == "CONTENT_SCENE_ANIMATION_IMAGE_HASH_MISMATCH"
    assert not (record / "graph.json").exists()


def test_the_graph_accepts_only_frame_counts_the_model_takes() -> None:
    with pytest.raises(ValueError, match="8k"):
        ltx23_graph(
            image_name="a.png",
            motion=_MOTION,
            seed=1,
            frames=20,
            width=704,
            height=1280,
            filename_prefix="clip/frame",
        )


def test_the_runtime_and_the_contract_agree_on_fps_and_inventory_digest() -> None:
    assert LTX23_FPS == SCENE_CLIP_FPS
    inventory = {"b": "2", "a": "1"}
    assert model_inventory_sha256(inventory) == hashlib.sha256(b"a:1\nb:2").hexdigest()

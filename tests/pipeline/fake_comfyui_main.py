"""Fake ComfyUI for the runtime tests: the HTTP API the renderer uses, no model.

Copied as ``main.py`` into a temporary ComfyUI folder and started by the real
renderer as a separate process. ``FAKE_COMFYUI_MODE`` chooses what it does with
each prompt: ``ok`` writes the requested frames, ``error`` reports an execution
error, ``hang`` never finishes (and keeps a child process alive, so the tests
can check that the whole tree is killed), ``exit`` stops before listening.
Everything it receives is written to ``FAKE_COMFYUI_RECORD``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

parser = argparse.ArgumentParser()
parser.add_argument("--listen")
parser.add_argument("--port", type=int)
parser.add_argument("--base-directory")
parser.add_argument("--extra-model-paths-config")
parser.add_argument("--disable-auto-launch", action="store_true")
parser.add_argument("--disable-all-custom-nodes", action="store_true")
parser.add_argument("--offline", action="store_true")
parser.add_argument("--use-sage-attention", action="store_true")
parser.add_argument("--fast", nargs="*")
args = parser.parse_args()

mode = os.environ.get("FAKE_COMFYUI_MODE", "ok")
record = Path(os.environ["FAKE_COMFYUI_RECORD"])
record.mkdir(parents=True, exist_ok=True)
(record / "argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
(record / "pid").write_text(str(os.getpid()), encoding="utf-8")
(record / "offline").write_text(os.environ.get("HF_HUB_OFFLINE", ""), encoding="utf-8")
if mode == "exit":
    sys.exit(3)
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    (record / "child_pid").write_text(str(child.pid), encoding="utf-8")

base = Path(args.base_directory)
history: dict[str, dict[str, Any]] = {}


def _node(graph: dict[str, Any], class_type: str) -> dict[str, Any]:
    return next(node for node in graph.values() if node["class_type"] == class_type)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return

    def _send(self, code: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/system_stats":
            version = os.environ.get("FAKE_COMFYUI_VERSION", "0.40.0")
            self._send(200, {"system": {"comfyui_version": version}})
        elif self.path.startswith("/history/"):
            prompt_id = self.path.rsplit("/", 1)[-1]
            self._send(200, {prompt_id: history[prompt_id]} if prompt_id in history else {})
        else:
            self._send(404, {})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/prompt":
            self._send(404, {})
            return
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        graph = data["prompt"]
        (record / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
        image = _node(graph, "LoadImage")["inputs"]["image"]
        (record / "image_present").write_text(
            str((base / "input" / image).is_file()), encoding="utf-8"
        )
        prompt_id = uuid.uuid4().hex
        if mode == "ok":
            length = _node(graph, "EmptyLTXVLatentVideo")["inputs"]["length"]
            prefix = Path(_node(graph, "SaveImage")["inputs"]["filename_prefix"])
            folder = base / "output" / prefix.parent
            folder.mkdir(parents=True, exist_ok=True)
            for number in range(1, length + 1):
                (folder / f"{prefix.name}_{number:05d}_.png").write_bytes(b"png")
            history[prompt_id] = {
                "status": {"status_str": "success", "completed": True, "messages": []}
            }
        elif mode == "error":
            history[prompt_id] = {
                "status": {
                    "status_str": "error",
                    "completed": False,
                    "messages": [["execution_error", {"exception_message": "out of memory"}]],
                }
            }
        self._send(200, {"prompt_id": prompt_id})


ThreadingHTTPServer((args.listen, args.port), Handler).serve_forever()

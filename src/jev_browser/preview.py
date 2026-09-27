"""Opt-in inspector screenshots; never part of a model observation."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path


class LivePreview:
    def __init__(self, output: Path, capture, *, archive=False):
        self.output, self.capture = output, capture
        self.task = None
        self.archive = archive
        self.sequence = 0

    def write(self, name, data):
        target = self.output / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)

    def status(self, active, **extra):
        self.write(
            "live.json", json.dumps({"active": active, "time": time.time(), **extra}).encode()
        )

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        try:
            while True:
                try:
                    captured = await self.capture()
                    image, url = captured[:2]
                    metadata = captured[2] if len(captured) > 2 else {}
                except Exception as exc:
                    # Navigation or a busy renderer can interrupt a single frame.
                    # Keep sampling so the preview recovers with the task page.
                    self.status(False, error=type(exc).__name__)
                    await asyncio.sleep(0.5)
                    continue
                self.write("live.jpg", image)
                frame_time = time.time()
                frame = {"width": 1280, "height": 900, **metadata, "time": frame_time}
                if self.archive:
                    folder = self.output / "preview"
                    folder.mkdir(exist_ok=True)
                    resource = f"preview/{self.sequence:06d}.jpg"
                    self.write(resource, image)
                    with (self.output / "frames.jsonl").open("a") as stream:
                        stream.write(json.dumps({**frame, "resource": resource}) + "\n")
                    frame["frame_index"] = self.sequence
                    self.sequence += 1
                self.status(True, **{**frame, "url": url})
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Preview failures must never stop the controller or leak page/credential data.
            self.status(False, error=type(exc).__name__)
        finally:
            self.status(False)

    async def close(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

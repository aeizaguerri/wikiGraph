from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.browser_diagnostics import GraphClickDiagnostics


class FakeTracing:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self, **kwargs: object) -> None:
        self.started = True
        assert kwargs == {"screenshots": True, "snapshots": True, "sources": True}

    async def stop(self, *, path: str) -> None:
        self.stopped = True
        Path(path).write_bytes(b"trace")


class FakeContext:
    def __init__(self) -> None:
        self.tracing = FakeTracing()


class FakePage:
    def __init__(self) -> None:
        self.context = FakeContext()
        self.handlers: dict[str, object] = {}

    def on(self, event: str, handler: object) -> None:
        self.handlers[event] = handler

    async def evaluate(self, script: str) -> dict[str, object]:
        return {"cardTitle": "Selected article"}

    async def screenshot(self, *, path: str) -> None:
        Path(path).write_bytes(b"screenshot")


@pytest.mark.asyncio
async def test_disabled_capture_does_not_touch_browser_context():
    page = FakePage()
    diagnostics = GraphClickDiagnostics(Path("test-results/graph-click/not-created"))

    await diagnostics.start(page, enabled=False)

    assert not page.context.tracing.started
    assert not Path("test-results/graph-click/not-created").exists()


@pytest.mark.asyncio
async def test_capture_emits_evidence_without_masking_original_failure():
    output = Path("test-results/graph-click/diagnostic-unit-test")
    page = FakePage()
    diagnostics = GraphClickDiagnostics(output)
    try:
        await diagnostics.start(page, enabled=True)
        with pytest.raises(RuntimeError, match="original test failure"):
            try:
                raise RuntimeError("original test failure")
            finally:
                await diagnostics.finish(page, {"click": {"x": 12, "y": 34}})

        assert page.context.tracing.started
        assert page.context.tracing.stopped
        assert {path.name for path in output.iterdir()} == {
            "diagnostics.json",
            "screenshot.png",
            "trace.zip",
        }
        assert '"cardTitle": "Selected article"' in (output / "diagnostics.json").read_text()
    finally:
        for path in output.glob("*"):
            path.unlink()
        output.rmdir()

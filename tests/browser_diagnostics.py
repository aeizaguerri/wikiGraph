from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class GraphClickDiagnostics:
    """Best-effort evidence capture isolated to the graph-click browser test."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.errors: list[str] = []
        self.evidence: dict[str, object] = {"server": "local stub"}
        self.started = False

    async def start(self, page: Any, *, enabled: bool) -> None:
        if not enabled:
            return
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            await page.context.tracing.start(
                screenshots=True, snapshots=True, sources=True
            )
            self.started = True
            page.on("pageerror", lambda error: self.errors.append(str(error)))
            page.on(
                "console",
                lambda message: self.errors.append(message.text)
                if message.type == "error"
                else None,
            )
        except Exception as error:
            # Instrumentation must not change the test verdict or mask its failure.
            self.errors.append(f"diagnostic setup failed: {type(error).__name__}")

    async def finish(self, page: Any, evidence: dict[str, object]) -> None:
        if not self.started:
            return
        payload = dict(evidence)
        payload["browser_errors"] = [message[:500] for message in self.errors[:30]]
        try:
            payload["final_page"] = await page.evaluate("""() => ({
              cardTitle: document.getElementById('card-title')?.textContent ?? null,
              selected: window.__wikigraph?.getSelected?.() ?? null,
              activeElement: document.activeElement?.id ?? null,
              viewport: {width: innerWidth, height: innerHeight}
            })""")
        except Exception as error:
            payload["final_capture_error"] = type(error).__name__

        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(payload, ensure_ascii=True)
            if len(serialized) > 20_000:
                serialized = json.dumps({"diagnostics_truncated": True})
            (self.output_dir / "diagnostics.json").write_text(
                serialized, encoding="utf-8"
            )
        except Exception:
            pass
        try:
            await page.screenshot(path=str(self.output_dir / "screenshot.png"))
        except Exception:
            pass
        try:
            await page.context.tracing.stop(path=str(self.output_dir / "trace.zip"))
        except Exception:
            pass

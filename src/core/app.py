#!/usr/bin/env python3
"""aibt main application. Entry point for the aibt service (run via systemd)."""
from __future__ import annotations
import sys
import os
import asyncio
import signal

# Resolve project root and ensure src/ and webui/backend/ are importable.
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (os.path.join(_ROOT_DIR, "src"), os.path.join(_ROOT_DIR, "webui", "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.config import load_config
from core.logging_utils import init_logging, log


class AibtApp:
    """Orchestrates config loading, logging, and WebUI server lifecycle."""

    def __init__(self) -> None:
        self.root_dir = _ROOT_DIR
        self.config: dict = {}
        self.webui_server = None

    async def start(self) -> None:
        """Load config, init subsystems, start WebUI server."""
        log("core", "info", "Service startup initiated")
        try:
            self.config = load_config(self.root_dir)
            init_logging(self.config, self.root_dir)
            log("core", "info", f"Config: {self.config.get('title', 'aibt')} / instance: {self.config.get('instance', '?')}")
            from memoryd import get_memoryd_service
            memoryd_service = get_memoryd_service(self.root_dir, self.config)
            memoryd_service.initialize()
        except Exception as e:
            log("core", "critical", f"Config load failed: {e}")
            raise

        try:
            from app import WebUIServer  # webui/backend/app.py
            self.webui_server = WebUIServer(self.root_dir, self.config)
        except Exception as e:
            log("core", "critical", f"WebUI init failed: {e}")
            raise

        webui_task = asyncio.create_task(self._run_webui(), name="aibt-webui")
        await self.webui_server.started_event.wait()
        log("core", "info", "Service startup completed")
        await webui_task

    async def _run_webui(self) -> None:
        try:
            await self.webui_server.start()
        except Exception as e:
            log("core", "error", f"WebUI error: {e}")

    async def stop(self) -> None:
        """Graceful shutdown of all subsystems."""
        log("core", "info", "Service shutdown initiated")
        if self.webui_server:
            await self.webui_server.stop()
        log("core", "info", "Service shutdown completed")


async def main() -> int:
    # Bootstrap logging with defaults before config is available.
    init_logging({}, _ROOT_DIR)

    app = AibtApp()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    _shutdown_started = False

    def _on_signal(sig: int, _frame) -> None:
        nonlocal _shutdown_started
        import signal as _sig
        name = getattr(_sig.Signals, str(sig), str(sig))
        if not _shutdown_started:
            _shutdown_started = True
            log("core", "notice", f"Signal {name} received, shutting down")
        loop.call_soon_threadsafe(stop_event.set)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    def _asyncio_exc(loop_obj: asyncio.AbstractEventLoop, ctx: dict) -> None:
        exc = ctx.get("exception")
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            return
        msg = ctx.get("message", "Unhandled asyncio exception")
        log("core", "error", f"{msg}: {exc}" if exc else msg)

    loop.set_exception_handler(_asyncio_exc)

    # Install global Python exception hook so unhandled errors reach the log.
    _orig_excepthook = sys.excepthook

    def _excepthook(exc_type, exc_value, exc_tb):
        if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            import traceback
            tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb)).strip()
            log("core", "critical", f"Unhandled exception: {tb}")
        _orig_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    start_task = asyncio.create_task(app.start(), name="aibt-start")
    stop_task = asyncio.create_task(stop_event.wait(), name="aibt-stop-wait")

    try:
        done, _ = await asyncio.wait({start_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if start_task in done:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            await start_task
            return 0
        await app.stop()
        if not start_task.done():
            try:
                await asyncio.wait_for(start_task, timeout=5.0)
            except asyncio.TimeoutError:
                start_task.cancel()
        await asyncio.gather(start_task, return_exceptions=True)
        return 0
    except Exception as e:
        log("core", "critical", f"Fatal error in main loop: {e}")
        return 1
    finally:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

import asyncio
import logging
import signal
import sys
import threading
from urllib.error import URLError
from urllib.request import Request, urlopen

from .gsmtc import GSMTCAdapter
from .artwork import artwork_processor
from .core_audio import CoreAudioController
from .server import BRIDGE_HOST, BRIDGE_PORT, create_server
from .state import MediaStateCache
from .lifecycle import CompanionLifecycle, NamedMutex
from .logging_config import configure_logging
from .paths import CompanionPaths, ensure_token, load_token


def shutdown_signals():
    signals = [signal.SIGINT]
    for name in ("SIGBREAK", "SIGTERM"):
        value = getattr(signal, name, None)
        if value is not None and value not in signals:
            signals.append(value)
    return signals


def install_signal_handlers(loop, stop_event):
    previous_handlers = {}

    def notify_shutdown(_signal_number, _frame):
        loop.call_soon_threadsafe(stop_event.set)

    for signal_name in shutdown_signals():
        try:
            previous_handlers[signal_name] = signal.signal(
                signal_name, notify_shutdown
            )
        except (OSError, ValueError):
            pass
    return previous_handlers


def restore_signal_handlers(previous_handlers):
    for signal_name, previous_handler in previous_handlers.items():
        try:
            signal.signal(signal_name, previous_handler)
        except (OSError, ValueError):
            pass


async def run_bridge(token, lifecycle=None):
    loop = asyncio.get_running_loop()
    lifecycle = lifecycle or CompanionLifecycle()
    stop_event = asyncio.Event()
    previous_handlers = install_signal_handlers(loop, stop_event)
    adapter = audio = server = server_thread = refresh_task = None
    server_started = False
    try:
        cache = MediaStateCache()
        adapter = GSMTCAdapter(cache)
        await adapter.start()
        audio = CoreAudioController(cache)
        await asyncio.to_thread(audio.refresh)
        server = create_server(
            cache, adapter.command, loop, audio_commander=audio.command,
            artwork_lookup=artwork_processor.get_cached, token=token, lifecycle=lifecycle,
            request_stop=lambda: loop.call_soon_threadsafe(stop_event.set),
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        server_started = True

        def update_readiness():
            if lifecycle.status != "stopping":
                state = cache.get()
                lifecycle.set_status("ready" if state.available or state.audio_available
                                     else "degraded")

        async def refresh_periodically():
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5)
                except TimeoutError:
                    await asyncio.gather(adapter.refresh(), asyncio.to_thread(audio.refresh))
                    update_readiness()

        update_readiness()
        logging.getLogger("d200_bridge").info("companion_listening")
        refresh_task = asyncio.create_task(refresh_periodically())
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        lifecycle.set_status("stopping")
        stop_event.set()
        cleanup_errors = []
        async def safely_await(awaitable):
            try:
                await awaitable
            except BaseException as error:
                cleanup_errors.append(error)
        def safely(operation):
            try:
                operation()
            except BaseException as error:
                cleanup_errors.append(error)
        if refresh_task:
            await safely_await(refresh_task)
        for operation in (server.shutdown if server_started else None,
                          server.server_close if server else None):
            if operation:
                safely(operation)
        if adapter:
            await safely_await(adapter.stop())
        if audio:
            safely(audio.stop)
        if server_started:
            safely(lambda: server_thread.join(timeout=2))
        safely(lambda: restore_signal_handlers(previous_handlers))
        if cleanup_errors and sys.exc_info()[0] is None:
            raise cleanup_errors[0]


def stop_running_companion():
    token = load_token()
    request = Request(
        f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/lifecycle/stop", data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=2) as response:
        return 0 if response.status == 200 else 1


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--stop"]:
        try:
            return stop_running_companion()
        except (OSError, RuntimeError, URLError):
            return 1
    if arguments:
        return 2
    mutex = NamedMutex()
    try:
        if not mutex.acquire():
            return 1 if mutex.unavailable else 0
        paths = CompanionPaths.from_environment()
        token = ensure_token(paths.token)
        configure_logging(paths.logs, token=token, console=True)
        asyncio.run(run_bridge(token=token))
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, ValueError):
        logging.getLogger("d200_bridge").error("startup_failed")
        return 1
    finally:
        mutex.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

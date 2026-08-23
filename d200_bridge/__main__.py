import asyncio
import signal
import threading

from .gsmtc import GSMTCAdapter
from .artwork import artwork_processor
from .core_audio import CoreAudioController
from .server import BRIDGE_HOST, BRIDGE_PORT, create_server
from .state import MediaStateCache


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


async def run_bridge():
    loop = asyncio.get_running_loop()
    cache = MediaStateCache()
    adapter = GSMTCAdapter(cache)
    audio = CoreAudioController(cache)
    await adapter.start()
    await asyncio.to_thread(audio.refresh)
    server = create_server(
        cache, adapter.command, loop, audio_commander=audio.command,
        artwork_lookup=artwork_processor.get_cached,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"D200 GSMTC bridge listening on http://{BRIDGE_HOST}:{BRIDGE_PORT}")

    stop_event = asyncio.Event()

    async def refresh_periodically():
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5)
            except TimeoutError:
                await asyncio.gather(adapter.refresh(), asyncio.to_thread(audio.refresh))

    refresh_task = asyncio.create_task(refresh_periodically())
    previous_handlers = install_signal_handlers(loop, stop_event)
    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            stop_event.set()
            await refresh_task
            server.shutdown()
            server.server_close()
            await adapter.stop()
            audio.stop()
            server_thread.join(timeout=2)
        finally:
            restore_signal_handlers(previous_handlers)


def main():
    try:
        asyncio.run(run_bridge())
    except KeyboardInterrupt:
        return 0
    except OSError as error:
        print(f"Bridge could not start: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI entrypoint for running the BridgeServer standalone or daemonized."""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from .constants import DEFAULT_BRIDGE_HOST, DEFAULT_BRIDGE_PORT
from .server import BridgeServer


def main():
    parser = argparse.ArgumentParser(description="CTF Operations Bridge Server")
    parser.add_argument("--host", default=DEFAULT_BRIDGE_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_BRIDGE_PORT)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    server = BridgeServer(host=args.host, port=args.port, token=args.token)

    async def run():
        await server.start()
        stop_event = asyncio.Event()

        def _signal_handler():
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except (NotImplementedError, RuntimeError):
                pass

        await stop_event.wait()
        await server.stop()

    try:
        loop.run_until_complete(run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()

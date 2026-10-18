"""Bounded subprocess transport for provider-owned browser components."""

import asyncio
import json
from pathlib import Path

from .errors import AccountPaused, ProviderError, RateLimited, RemoteNotFound


class BrowserTransport:
    def __init__(self, script: Path, cdp_url, ensure_ready=None):
        self.script = script
        self.cdp_url = cdp_url
        self.ensure_ready = ensure_ready
        self.tab_id = None
        self.authenticated = False

    async def events(self, mode, **options):
        if mode != "close" and self.ensure_ready:
            await asyncio.to_thread(self.ensure_ready)
        process = await asyncio.create_subprocess_exec(
            "node",
            str(self.script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=32 * 1024 * 1024,
        )
        try:
            process.stdin.write(
                json.dumps(
                    dict(cdp_url=self.cdp_url, tab_id=self.tab_id, mode=mode, **options)
                ).encode()
            )
            await process.stdin.drain()
            process.stdin.close()
            while line := await asyncio.wait_for(process.stdout.readline(), 120):
                event = json.loads(line)
                if event.get("type") == "tab":
                    self.tab_id = event["id"]
                elif event.get("type") == "error":
                    message = event.get("message", "Provider browser operation failed")
                    error = {401: AccountPaused, 404: RemoteNotFound}.get(
                        event.get("status"), ProviderError
                    )
                    if error is AccountPaused:
                        self.authenticated = False
                    if event.get("status") == 429:
                        raise RateLimited()
                    raise error(message)
                else:
                    self.authenticated = True
                    yield event
            if await process.wait():
                raise ProviderError("Provider browser operation did not complete")
        except asyncio.TimeoutError:
            raise ProviderError(
                "Provider browser timed out; submitted requests were not replayed"
            ) from None
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

    async def request(self, mode, **options):
        result = None
        found = False
        async for event in self.events(mode, **options):
            if event.get("type") == "result":
                result, found = event.get("data"), True
        if not found:
            raise ProviderError("Provider operation returned no result")
        return result

    async def close(self):
        if self.tab_id:
            try:
                _ = [event async for event in self.events("close")]
            except ProviderError:
                # Closing an already disconnected browser is best effort.
                pass
            finally:
                self.tab_id = None

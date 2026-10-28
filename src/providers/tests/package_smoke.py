"""Smoke the standalone provider wheel from its clean site-packages install."""

import asyncio
import subprocess
import tempfile
from importlib.resources import files
from pathlib import Path

from providers import ProviderClient, SendRequest


def descendants(root):
    for item in root.iterdir():
        if item.is_dir():
            yield from descendants(item)
        else:
            yield item


def main():
    root = files("providers")
    scripts = [item for item in descendants(root) if item.name.endswith(".cjs")]
    for script in scripts:
        subprocess.run(["node", "--check", str(script)], check=True)

    async def verify_lazy_account_factory():
        with tempfile.TemporaryDirectory() as folder:
            client = ProviderClient()
            backend = client.connect(
                "chatgpt",
                "package-smoke",
                credential_file=Path(folder) / "session.json",
            )
            if backend.id != "chatgpt" or backend.account_id != "package-smoke":
                raise SystemExit("standalone provider factory returned the wrong account")
            request = SendRequest("chatgpt", "package-smoke", "local", "smoke")
            if (request.provider_id, request.account_id) != (backend.id, backend.account_id):
                raise SystemExit("public request model did not preserve account identity")
            await client.close()

    asyncio.run(verify_lazy_account_factory())


if __name__ == "__main__":
    main()

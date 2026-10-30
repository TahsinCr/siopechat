"""Smoke the standalone provider wheel from its clean site-packages install."""

import asyncio
import tempfile
from importlib.resources import files
from pathlib import Path

import quickjs

from providers import ProviderClient, SendRequest


def main():
    sign_js = files("providers.chatgpt").joinpath("scripts", "sign.js")
    if not sign_js.is_file():
        raise SystemExit("scripts/sign.js was not packaged in providers.chatgpt")
    context = quickjs.Context()
    context.eval(sign_js.read_text(encoding="utf-8"))
    if int(context.eval("typeof readJsonInput === 'function' ? 1 : 0")) != 1:
        raise SystemExit("sign.js did not expose readJsonInput in QuickJS-NG")

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

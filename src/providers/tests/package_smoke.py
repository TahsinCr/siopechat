"""Smoke the standalone provider wheel from its clean site-packages install."""

import asyncio
import tempfile
from pathlib import Path

from providers import ProviderClient, SendRequest
from providers.chatgpt.sentinel import SentinelRuntime, generate_enforcement_proof


def main():
    with SentinelRuntime(user_agent="Mozilla/5.0 (X11; Linux x86_64)") as runtime:
        initial = runtime.create_initial_proof()
        if not initial.startswith("gAAAAAC") or not initial.endswith("~S"):
            raise SystemExit("SentinelRuntime did not produce a valid initial proof token")
        proof = generate_enforcement_proof(
            initial,
            {
                "proofofwork": {
                    "required": True,
                    "seed": "smoke-seed",
                    "difficulty": "f",
                }
            },
        )
        if not proof.startswith("gAAAAAB") or not proof.endswith("~S"):
            raise SystemExit("generate_enforcement_proof did not produce a valid proof token")

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

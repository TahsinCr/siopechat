"""Provider-owned native project deletion without application/frontend imports."""

import asyncio
import json
from pathlib import Path

from .errors import ProviderError


async def delete_project(cdp_url, provider, remote, name=None):
    process = await asyncio.create_subprocess_exec(
        "node",
        str(Path(__file__).parent / "scripts" / "manage.cjs"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(
            process.communicate(
                json.dumps(
                    {
                        "cdp_url": cdp_url,
                        "provider": provider,
                        "remote": remote,
                        "name": name,
                    }
                ).encode()
            ),
            35,
        )
        if process.returncode or not json.loads(output).get("submitted"):
            raise ProviderError(error.decode(errors="replace")[:300] or "Proje silinemedi")
    except asyncio.TimeoutError:
        raise ProviderError(
            "Proje silme sonucu belirsiz; yeniden denemeden sağlayıcıyı kontrol edin"
        ) from None
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

"""Provider-owned project controls exposed through a frontend-neutral contract."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .browser import BrowserTransport
from .errors import ProviderError
from .models import Choice


@dataclass(frozen=True)
class ProjectOptions:
    fields: tuple[str, ...] = ()
    visibility: tuple[Choice, ...] = ()
    memory: tuple[Choice, ...] = ()
    sources: tuple[dict, ...] = ()
    remove_sources: bool = False
    source_error: str | None = None

    @classmethod
    def from_dict(cls, value):
        return cls(
            tuple(value.get("fields", ())),
            tuple(Choice(**v) for v in value.get("visibility", ())),
            tuple(Choice(**v) for v in value.get("memory", ())),
            tuple(value.get("sources", ())),
            bool(value.get("remove_sources")),
            value.get("source_error"),
        )


class ProjectEditor:
    """Own a separate browser component; adapters serialize and verify its writes."""

    def __init__(self, script, cdp_url, ensure_ready=None):
        self.browser = BrowserTransport(Path(script), cdp_url, ensure_ready)

    async def options(self, project_id=None):
        return ProjectOptions.from_dict(
            await self.browser.request("options", project_id=project_id)
        )

    async def write(self, operation, project_id=None, **values):
        if operation == "add_source" and values.get("kind") == "file":
            path = Path(values["path"]).expanduser().resolve(strict=True)
            if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                raise ValueError("Kaynak dosyası en fazla 20 MiB olabilir")
            values["path"] = str(path)
        return await self.browser.request(operation, project_id=project_id, **values)

    async def close(self):
        await self.browser.close()


class ProjectEditing:
    """Common verification; native fields and actions belong to each adapter."""

    async def project_options(self, project_id=None):
        async with self._gate:
            return await self.editor.options(project_id)

    async def _project_after_write(self, project_id, confirmed):
        for _ in range(12):
            project = await self.get_project(project_id)
            if confirmed(project):
                return project
            await asyncio.sleep(0.4)
        raise ProviderError(
            "Proje değişikliği sunucuda doğrulanamadı; tekrarlamadan önce eşitleyin"
        )

    async def update_project(self, project):
        if project.origin.provider_id != self.id or project.origin.account_id != self.account_id:
            raise ValueError("Proje başka bir sağlayıcı hesabına ait")
        remote = project.origin.remote_id
        if not project.name.strip():
            raise ValueError("Proje adı gerekir")
        current = await self.get_project(remote)
        changes = {}
        if current.name != project.name:
            changes["name"] = project.name
        if (current.instructions or "") != (project.instructions or ""):
            changes["instructions"] = project.instructions or ""
        if not changes:
            return current
        async with self._gate:
            await self.editor.write("update", remote, **changes)
        return await self._project_after_write(
            remote,
            lambda p: (
                p.name == project.name and (p.instructions or "") == (project.instructions or "")
            ),
        )

    async def add_project_resource(self, project_id, kind, **values):
        before = await self.get_project(project_id)
        ids = {r.id for r in before.resources}
        async with self._gate:
            await self.editor.write("add_source", project_id, kind=kind, **values)
        return await self._project_after_write(
            project_id, lambda p: any(r.id not in ids for r in p.resources)
        )

    async def remove_project_resource(self, project_id, resource_id):
        before = await self.get_project(project_id)
        source = next((r for r in before.resources if r.id == resource_id), None)
        if source is None:
            raise ValueError("Proje kaynağı bulunamadı")
        async with self._gate:
            await self.editor.write(
                "remove_source", project_id, resource_id=resource_id, resource_name=source.name
            )
        return await self._project_after_write(
            project_id, lambda p: all(r.id != resource_id for r in p.resources)
        )

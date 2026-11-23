"""AccountsProjects account operations backed by webaiprovider v2 ProjectsCollection."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from webaiprovider import Capability

from .shared import identity, origin


class AccountsProjects:
    def list_projects(self, key: str, cursor=None, force: bool = False):
        self.require_visible_account(key)
        prov = self.provider(key)
        if Capability.PROJECTS not in prov.capabilities:
            return {"items": [], "cursor": None}

        def fetch():
            page = self.open_provider(key).projects._fetch_page(cursor, None)
            return {
                "items": [self.project_record(key, p) for p in page.items],
                "cursor": page.next_cursor,
            }

        return self.read_record(key, "containers", cursor, fetch, force, fresh=force)

    def all_projects(self, cursor=None, force=False, account_keys=None, on_account=None):
        cursors = json.loads(cursor) if cursor else None
        accounts = [
            item
            for item in self.visible_accounts()
            if (account_keys is None or item["key"] in account_keys)
            and (cursors is None or item["key"] in cursors)
        ]
        items, errors, failed, more = [], [], [], {}

        def report(account, outcome):
            if on_account is None:
                return
            try:
                on_account(account, outcome)
            except Exception:
                return

        with ThreadPoolExecutor(
            max_workers=min(6, max(1, len(accounts))), thread_name_prefix="project-list"
        ) as pool:
            futures = {
                pool.submit(
                    self.list_projects,
                    account["key"],
                    cursors.get(account["key"]) if cursors else None,
                    force,
                ): account
                for account in accounts
            }
            for future in as_completed(futures):
                account = futures[future]
                try:
                    page = future.result()
                    report(account, page)
                    items.extend(page["items"])
                    if page.get("cursor"):
                        more[account["key"]] = page["cursor"]
                except Exception as error:
                    report(account, error)
                    errors.append(account["label"])
                    failed.append(account["key"])
        return {
            "items": items,
            "cursor": json.dumps(more) if more else None,
            "errors": errors,
            "failed_accounts": failed,
        }

    @staticmethod
    def project_record(key: str, project: Any) -> dict[str, Any]:
        provider_project = getattr(project, "_provider_project", None)
        remote_id = project.remote_id or project.id
        provider_id = key.split("/")[0]
        raw_meta = dict(getattr(provider_project, "metadata", None) or {})
        raw_resources = (
            getattr(project.attachments, "_items", None)
            or getattr(provider_project, "resources", None)
            or ()
        )
        resources = [
            {
                "id": res.id,
                "name": res.name,
                "media_type": res.media_type,
                "remote_url": res.remote_url,
                "size": res.size,
                "metadata": dict(res.metadata),
            }
            for res in raw_resources
        ]
        default_kind = "notebook" if provider_id == "gemini" else "project"
        return {
            "id": identity(key, remote_id),
            "name": project.title,
            "remote_id": remote_id,
            "instructions": project.instruction._text,
            "resources": resources,
            "context": key,
            "provider_id": provider_id,
            "kind": raw_meta.get("kind", default_kind),
            "metadata": raw_meta,
        }

    def create_project(
        self, key: str, name: str, visibility: str = "private", memory: str = "default"
    ):
        self.require_visible_account(key)
        prov = self.open_provider(key)
        if key.startswith("chatgpt/"):
            project = prov.projects.create(name, memory_share=(memory != "project_only"))
        else:
            del visibility, memory
            project = prov.projects.create(name)
        return self.store_project(key, project)

    def project_options(self, key: str, value: str | None = None):
        self.require_visible_account(key)
        context, remote = origin(value, key)
        if context != key:
            raise ValueError("Proje seçili hesaba ait olmalı")
        return self.open_provider(key).project_options(remote)

    def project_details(self, key: str, value: str):
        self.require_visible_account(key)
        context, remote = origin(value, key)
        if context != key or not remote:
            raise ValueError("Proje seçili hesaba ait olmalı")
        if not self.status(key)["saved_snapshot"]:
            archive = self.archive()
            cached = archive.get(key, "container", remote) if archive else None
            if cached is None and archive:
                cached = next(
                    (
                        item
                        for item in archive.containers(key, limit=500)
                        if item.get("remote_id") == remote
                        or origin(item.get("id"), key)[1] == remote
                    ),
                    None,
                )
            if cached is None:
                raise RuntimeError("Kayıtlı oturum yok; Hesaplar bölümünden doğrulayın")
            return dict(cached, requires_auth=True)
        return self.store_project(key, self.open_provider(key).projects.get(remote))

    def store_project(self, key: str, project: Any):
        record = self.project_record(key, project)
        remote_id = project.remote_id or project.id
        archive = self.archive()
        if archive:
            try:
                archive.put(key, "container", remote_id, record)
                for state in archive.states():
                    if state["account"] != key or state["kind"] != "containers":
                        continue
                    page = archive.get(key, "containers", state["remote"])
                    if page is not None:
                        items = [
                            record if x.get("id") == record["id"] else x
                            for x in page.get("items", [])
                        ]
                        if not state["remote"] and not any(
                            x.get("id") == record["id"] for x in items
                        ):
                            items.insert(0, record)
                        archive.put(key, "containers", state["remote"], dict(page, items=items))
                archive.mark_ready(key, "container", remote_id)
            except OSError:
                record["cache_warning"] = "Proje güncellendi ancak yerel arşiv kaydı tamamlanamadı"
        return record

    def change_project(self, key: str, value: str, operation: str = "update", **values):
        self.require_visible_account(key)
        context, remote = origin(value, key)
        if context != key or not remote:
            raise ValueError("Proje seçili hesaba ait olmalı")
        prov = self.open_provider(key)
        if operation == "update":
            result = prov.projects.update(
                remote,
                title=values.get("name"),
                instruction=values.get("instructions"),
            )
        elif operation == "add_source":
            proj = prov.projects.get(remote)
            kind = values.get("kind") or values.get("resource_type")
            if "path" in values and values["path"] is not None:
                proj.attachments.create(values["path"], resource_type=kind or "file")
            elif "text" in values and values["text"] is not None:
                proj.attachments.create(
                    values["text"],
                    resource_type="text",
                    name=values.get("name") or values.get("title") or "Text",
                )
            else:
                raise ValueError("Kaynak için dosya yolu veya metin gerekir")
            result = proj
        elif operation == "remove_source":
            proj = prov.projects.get(remote)
            proj.attachments.delete(values["resource_id"])
            result = proj
        else:
            raise ValueError("Bilinmeyen proje işlemi")
        return self.store_project(key, result)

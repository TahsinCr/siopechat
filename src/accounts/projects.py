"""AccountsProjects account operations."""

from __future__ import annotations

import json
from dataclasses import asdict, replace

from providers import Capability

from .shared import identity, origin


class AccountsProjects:
    def list_projects(self, key, cursor=None, force=False):
        provider = self.provider(key)
        if Capability.PROJECTS not in provider.capabilities:
            return {"items": [], "cursor": None}

        def fetch():
            page = self.call(provider.list_projects(cursor))
            return {
                "items": [self.project_record(key, p) for p in page.items],
                "cursor": page.next_cursor,
            }

        return self.read_record(key, "containers", cursor, fetch, force, fresh=force)

    def all_projects(self, cursor=None, force=False, account_keys=None, on_account=None):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        cursors = json.loads(cursor) if cursor else None
        accounts = [
            item
            for item in self.items()
            if self.enabled(item["provider"])
            and (account_keys is None or item["key"] in account_keys)
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
    def project_record(key, project):
        return {
            "id": identity(key, project.origin.remote_id),
            "name": project.name,
            "remote_id": project.origin.remote_id,
            "instructions": project.instructions,
            "resources": [asdict(resource) for resource in project.resources],
            "context": key,
            "provider_id": key.split("/")[0],
            "kind": project.metadata.get("kind", "project"),
            "metadata": project.metadata,
        }

    def create_project(self, key, name, visibility="private", memory="default"):
        project = self.call(
            self.provider(key).create_project(name, visibility=visibility, memory=memory)
        )
        return self.store_project(key, project)

    def project_options(self, key, value=None):
        context, remote = origin(value, key)
        if context != key:
            raise ValueError("Proje seçili hesaba ait olmalı")
        return self.call(self.provider(key).project_options(remote), timeout=90)

    def project_details(self, key, value):
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
        return self.store_project(key, self.call(self.provider(key).get_project(remote)))

    def store_project(self, key, project):
        record = self.project_record(key, project)
        archive = self.archive()
        if archive:
            try:
                archive.put(key, "container", project.origin.remote_id, record)
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
                archive.mark_ready(key, "container", project.origin.remote_id)
            except OSError:
                record["cache_warning"] = "Proje güncellendi ancak yerel arşiv kaydı tamamlanamadı"
        return record

    def change_project(self, key, value, operation="update", **values):
        context, remote = origin(value, key)
        if context != key or not remote:
            raise ValueError("Proje seçili hesaba ait olmalı")
        provider = self.provider(key)
        if operation == "update":
            current = self.call(provider.get_project(remote))
            result = self.call(provider.update_project(replace(current, **values)), timeout=90)
        elif operation == "add_source":
            result = self.call(provider.add_project_resource(remote, **values), timeout=90)
        elif operation == "remove_source":
            result = self.call(provider.remove_project_resource(remote, **values), timeout=90)
        else:
            raise ValueError("Bilinmeyen proje işlemi")
        return self.store_project(key, result)

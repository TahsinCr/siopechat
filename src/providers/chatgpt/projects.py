"""ChatGPT project controls through the authenticated HTTP client."""

import asyncio
import mimetypes
from urllib.parse import quote

from ..models import Choice
from ..projects import ProjectOptions, source_file


class ChatGPTProjectEditor:
    def __init__(self, session, factory):
        self.session, self.factory = session, factory

    async def close(self):
        pass

    async def options(self, project_id=None):
        def read():
            if not project_id:
                return ProjectOptions(
                    fields=("name",),
                    visibility=(Choice("private", "Özel"),),
                    memory=(
                        Choice("default", "Varsayılan"),
                        Choice("project_only", "Yalnızca proje"),
                    ),
                )
            with self.factory(self.session) as client:
                raw = client.get_project(project_id)["metadata"]
            for _ in range(3):
                if not isinstance(raw.get("gizmo"), dict):
                    break
                raw = raw["gizmo"]
            if raw.get("current_user_permission", {}).get("can_write") is False:
                return ProjectOptions(source_error="Proje salt okunur")
            accepted = next(
                (
                    tool.get("accepted_mime_types")
                    for tool in raw.get("tools", [])
                    if tool.get("type") == "retrieval"
                ),
                None,
            )
            return ProjectOptions(
                fields=("name", "instructions"),
                sources=(
                    {
                        "id": "file",
                        "title": "Dosya",
                        "accept": ",".join(accepted) if accepted else "*/*",
                    },
                    {"id": "text", "title": "Metin"},
                ),
                remove_sources=True,
            )

        return await asyncio.to_thread(read)

    async def write(self, operation, project_id=None, **values):
        def send():
            with self.factory(self.session) as client:
                path = "https://chatgpt.com/backend-api/projects/" + quote(project_id, safe="")
                if operation == "update":
                    current = client.get_project(project_id)
                    native = current["metadata"]
                    for _ in range(3):
                        if not isinstance(native.get("gizmo"), dict):
                            break
                        native = native["gizmo"]
                    display = native.get("display") or {}
                    client.request(
                        "PATCH",
                        path,
                        json={
                            "name": values.get("name", current["name"]),
                            "instructions": values.get("instructions", current["instructions"]),
                            "emoji": display.get("emoji"),
                            "theme": display.get("theme"),
                            "memory_scope": native.get("memory_scope"),
                        },
                    )
                elif operation == "add_source":
                    if values["kind"] == "text":
                        name, data, mime = values["name"], values["text"].encode(), "text/plain"
                        if not name.endswith(".txt"):
                            name += ".txt"
                    elif values["kind"] == "file":
                        file = source_file(values["path"])
                        name, data = file.name, file.read_bytes()
                        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                    else:
                        raise ValueError("Unsupported project source type")
                    uploaded = client.upload_file(name, mime, data, project_id=project_id)
                    client.request(
                        "POST",
                        path + "/files",
                        json={
                            "files": [
                                {
                                    "file_id": uploaded["id"],
                                    "name": name,
                                    "size": len(data),
                                    "type": mime,
                                    "location": "fs",
                                }
                            ]
                        },
                    )
                elif operation == "remove_source":
                    current = client.get_project(project_id)
                    rid = values["resource_id"]
                    if not any(
                        (x.get("file_id") or x.get("id")) == rid for x in current["resources"]
                    ):
                        raise ValueError("Kaynak bu projeye ait değil")
                    client.request("DELETE", path + "/files/" + quote(rid, safe=""))
                else:
                    raise ValueError("Unknown project operation")

        return await asyncio.to_thread(send)

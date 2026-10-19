"""Gemini notebook RPCs through the authenticated SDK HTTP session."""

import json
from dataclasses import dataclass

from ..errors import ProviderError
from .notebooks import notebook_id


@dataclass(frozen=True)
class RPCPayload:
    rpcid: str
    payload: str

    def serialize(self):
        return [self.rpcid, self.payload, None, "generic"]


class NotebookRPC:
    def __init__(self, connect):
        self.connect = connect

    async def call(self, rpc, args, source_path="/notebooks/view", allow_empty=False):

        client = await self.connect()
        response = await client._batch_execute(
            [RPCPayload(rpcid=rpc, payload=json.dumps(args))],
            source_path=source_path,
            current_retry=0,
        )
        parts = []
        for line in response.text.splitlines():
            if not line.startswith('[["wrb.fr"'):
                continue
            for row in json.loads(line):
                if row[0] == "wrb.fr" and row[1] == rpc:
                    if row[2] is None:
                        if allow_empty:
                            return []
                        raise ProviderError(
                            "Gemini işlem izni vermedi veya RPC sözleşmesi değişmiş"
                        )
                    parts.append(json.loads(row[2]))
        if not parts:
            raise ProviderError("Gemini notebook RPC response changed")
        return parts

    async def read(self, mode, *, remote=None, operation="list", cursor=None):
        if mode != "notebooks":
            raise ValueError("Unsupported Gemini RPC group")
        if remote:
            remote = notebook_id(remote)
        if operation == "chats":
            if not remote:
                raise ValueError("Notebook required")
            rpc, args = "MaZiqc", [10, cursor, [None, None, 1, remote, 1]]
        elif remote:
            rpc, args = "HcT8bb", [remote, ["en"], 0]
        else:
            rpc, args = "CNgdBe", [2, ["en"], 0, None, [2]]
        path = "/notebook/" + remote.split("/")[1] if remote else "/notebooks/view"
        return await self.call(rpc, args, path)


class NotebookEditor:
    def __init__(self, rpc, notebooks):
        self.rpc, self.notebooks = rpc, notebooks

    async def close(self):
        pass

    async def options(self, project_id=None):
        from ..projects import ProjectOptions

        return ProjectOptions(
            fields=("name", "instructions") if project_id else ("name",),
            sources=({"id": "text", "title": "Metin"},) if project_id else (),
            remove_sources=bool(project_id),
        )

    async def write(self, operation, project_id=None, **values):
        if operation == "create":
            detail = [
                values["name"],
                "",
                None,
                None,
                None,
                None,
                None,
                None,
                0,
                None,
                1,
                None,
                None,
                None,
                None,
                None,
                [2, None, None, None, 1],
            ]
            parts = await self.rpc.call("oMH3Zd", [detail])
            matches = []

            def collect(item):
                if not isinstance(item, list):
                    return
                if len(item) == 2 and isinstance(item[0], str) and item[0].startswith("notebooks/"):
                    matches.append(notebook_id(item[0]))
                else:
                    for child in item:
                        collect(child)

            collect(parts)
            if len(set(matches)) != 1:
                raise ProviderError("Defter oluşturma kimliği doğrulanamadı; işlem tekrarlanmadı")
            return matches[0]
        remote = notebook_id(project_id)
        path = "/notebook/" + remote.split("/")[1]
        if operation == "delete":
            return await self.rpc.call("Nwkn9", [remote], path, allow_empty=True)
        if operation == "update":
            current = await self.notebooks.get(remote)
            native = current.metadata["native"][1]
            if len(native) < 13 or not isinstance(native[12], list):
                raise ProviderError("Gemini notebook settings contract changed")
            detail = [
                values.get("name", current.name),
                current.metadata.get("description") or "",
                values.get("instructions", current.instructions or ""),
                None,
                None,
                None,
                None,
                None,
                0,
                0,
                None,
                None,
                [None, None, 0, 1, 0],
                None,
                None,
                0,
                native[12],
                None,
                None,
            ]
            return await self.rpc.call("kHv0Vd", [remote, detail], path, allow_empty=True)
        if operation == "add_source":
            if values["kind"] != "text":
                raise ValueError("Unsupported notebook source type")
            detail = [
                None,
                values["name"],
                None,
                None,
                None,
                [values["text"], 1],
                None,
                None,
                "text/plain",
            ]
            return await self.rpc.call("ko3zcd", [remote, detail, [1, 3]], path)
        if operation == "remove_source":
            resource = values["resource_id"]
            if not resource.startswith(remote + "/sources/"):
                raise ValueError("Source belongs to another notebook")
            return await self.rpc.call("AptDmf", [resource, [1, 3]], path, allow_empty=True)
        raise ValueError("Unknown notebook operation")

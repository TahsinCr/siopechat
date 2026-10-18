"""Small replaceable provider mappings; raw provider metadata is retained."""

from copy import deepcopy


class Mapping:
    container_kind = "project"

    def normalize(self, kind, value):
        result = deepcopy(value)
        if kind == "container":
            result.setdefault("kind", self.container_kind)
        return result

    def merge_container(self, remote, local):
        """Keep local file references only for sources still present remotely."""
        previous = {r.get("id"): r for r in (local or {}).get("resources", []) if r.get("id")}
        for resource in remote.get("resources", []):
            prior = previous.get(resource.get("id"), {})
            for name in ("path", "sha256"):
                if prior.get(name) and not resource.get(name):
                    resource[name] = prior[name]
        return remote


class GeminiMapping(Mapping):
    container_kind = "notebook"


MAPPINGS = {"chatgpt": Mapping(), "gemini": GeminiMapping(), "claude": Mapping()}

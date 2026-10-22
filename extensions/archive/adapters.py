"""Small replaceable provider mappings; raw provider metadata is retained."""


class Mapping:
    container_kind = "project"

    def normalize(self, kind, value):
        result = dict(value)
        # These are the only dictionaries archive normalization mutates. Text
        # bodies and native metadata remain read-only until SQL serialization.
        for collection in ("items", "resources"):
            if collection in result:
                result[collection] = [dict(item) for item in result[collection]]
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

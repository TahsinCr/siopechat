"""Small replaceable provider mappings; raw provider metadata is retained."""


class Mapping:
    container_kind = "project"

    def normalize(self, kind, value):
        result = dict(value)
        if kind == "conversation":
            result.pop("history_window", None)
            result.pop("history_version", None)
        # These are the only dictionaries archive normalization mutates. Text
        # bodies and native metadata remain read-only until SQL serialization.
        for collection in ("items", "resources"):
            if collection in result:
                result[collection] = [dict(item) for item in result[collection]]
        if kind == "container":
            result.setdefault("kind", self.container_kind)
        return result

    def merge_container(self, remote, local):
        """Use provider source metadata without restoring archived upload paths."""
        return remote


class GeminiMapping(Mapping):
    container_kind = "notebook"


MAPPINGS = {"chatgpt": Mapping(), "gemini": GeminiMapping(), "claude": Mapping()}

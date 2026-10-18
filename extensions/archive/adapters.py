"""Small replaceable provider mappings; raw provider metadata is retained."""

from copy import deepcopy


class Mapping:
    container_kind = "project"

    def normalize(self, kind, value):
        result = deepcopy(value)
        if kind == "container":
            result.setdefault("kind", self.container_kind)
        return result


class GeminiMapping(Mapping):
    container_kind = "notebook"


MAPPINGS = {"chatgpt": Mapping(), "gemini": GeminiMapping(), "claude": Mapping()}

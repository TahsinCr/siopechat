"""Gemini account routing, archived responses and temporary-session cleanup."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from webaiprovider import GeminiWebAIProvider, WebAIAccount
from webaiprovider.resources.message.protocol import (
    ConversationBound,
    ResponseComplete,
    TextReplacement,
)
from webaiprovider.resources.model.model import EffortOption, Model, ModelCatalog, ModelCatalogState
from webaiprovider.resources.project.types import ProjectData


class AccountNativeTests(unittest.TestCase):
    def test_native_result_passes_through_account_send_and_archive_and_temporary_cleanup(self):
        import tempfile

        from accounts import identity
        from bootstrap import create_application

        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                accounts = api.get("accounts")
                key = accounts.add("gemini", "Native fixture")
                provider = GeminiWebAIProvider(
                    WebAIAccount(key.split("/")[1], "gemini", accounts.credentials(key)),
                    notebooks=SimpleNamespace(get=lambda pid: ProjectData(pid, "Notebook")),
                )
                provider._opened = True
                provider._connect = Mock(side_effect=AssertionError("SDK must not send"))
                provider._load_catalog = Mock(
                    return_value=ModelCatalog(
                        ModelCatalogState.AVAILABLE,
                        (
                            Model(
                                "gemini-flash",
                                "Gemini Flash",
                                efforts=(EffortOption("standard", "Standard"),),
                                default_effort="standard",
                            ),
                        ),
                    )
                )
                requests = []

                def stream(request):
                    requests.append(request)
                    if not request.conversation_id:
                        self.assertEqual(request.project_id, "notebooks/abcdef")
                    yield ConversationBound("c_abcdef")
                    yield TextReplacement("Reasoning summary", "analysis")
                    yield TextReplacement("Answer", "final")
                    yield ResponseComplete()

                provider._stream_provider_events = stream
                provider._temporary["temporary_fixture"] = ["temporary_fixture"]
                accounts._providers[key] = provider
                delivered = []
                result = accounts.send(
                    key,
                    None,
                    "Question",
                    [],
                    delivered.append,
                    lambda _: None,
                    model="gemini-flash",
                    effort="standard",
                    project_id=identity(key, "notebooks/abcdef"),
                )
                self.assertIsNone(result["cache_warning"])
                record = accounts.cached(key, "c_abcdef")
                self.assertEqual(
                    [m["text"] for m in record["messages"]],
                    ["Question", "Reasoning summary", "Answer"],
                )
                self.assertEqual(record["project_id"], identity(key, "notebooks/abcdef"))
                self.assertEqual(
                    next(
                        event["text"]
                        for event in reversed(delivered)
                        if event.get("type") in ("delta", "replace")
                        and event.get("channel") == "final"
                    ),
                    "Answer",
                )
                accounts.send(
                    key,
                    result["conversation_id"],
                    "Follow-up",
                    [],
                    delivered.append,
                    lambda _: None,
                    model="gemini-flash",
                    effort="standard",
                )
                continued = accounts.cached(key, "c_abcdef")
                self.assertEqual(continued["project_id"], identity(key, "notebooks/abcdef"))
                self.assertEqual(len(continued["messages"]), 6)
                self.assertEqual(
                    [request.conversation_id for request in requests], [None, "c_abcdef"]
                )
                provider._connect.assert_not_called()
                accounts.callbacks(key)["forget_temporary"](identity(key, "temporary_fixture"))
                self.assertNotIn("temporary_fixture", provider._temporary)
            finally:
                api.runtime.close()

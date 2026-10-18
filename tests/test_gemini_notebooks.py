"""Synthetic native notebook records; no captured personal data in fixtures."""

import unittest
from unittest.mock import AsyncMock

from accounts import Accounts
from providers.errors import ProviderError
from providers.gemini.notebooks import Notebooks, project_from_native


def notebook():
    source = [None] * 22
    source[1], source[2], source[5] = 11, "Research notes", "notebooks/abcdef/sources/source1"
    detail = [None] * 15
    detail[0], detail[1], detail[10], detail[12] = (
        "My notebook",
        "Notebook description",
        [[source], []],
        [2, None, None, None, 3],
    )
    return ["notebooks/abcdef", detail, ["Use citations"]]


class NotebookTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_details_resources_and_native_types_stay_in_gemini(self):
        service = Notebooks("http://127.0.0.1:9231", "account")
        service.read = AsyncMock(return_value=[[None, None, [notebook()]]])
        page = await service.list()
        project = page.items[0]
        self.assertEqual(project.origin.provider_id, "gemini")
        self.assertEqual(project.origin.account_id, "account")
        self.assertEqual(project.name, "My notebook")
        self.assertEqual(project.instructions, "Use citations")
        self.assertEqual(project.metadata["description"], "Notebook description")
        self.assertEqual(project.resources[0].name, "Research notes")
        self.assertEqual(project.metadata["kind"], "notebook")
        self.assertEqual(project.metadata["notebook_type"], 3)
        self.assertTrue(project.metadata["url"].startswith("https://gemini.google.com/"))
        common = Accounts.project_record("gemini/account", project)
        self.assertEqual(common["kind"], "notebook")
        self.assertEqual(common["name"], project.name)
        self.assertEqual(common["instructions"], project.instructions)
        self.assertEqual(common["resources"][0]["name"], "Research notes")
        self.assertEqual(common["provider_id"], "gemini")
        self.assertTrue(common["metadata"]["supports_chat"])
        service.read.return_value = [[notebook()]]
        self.assertEqual(await service.get("notebooks/abcdef"), project)

    async def test_unknown_native_contract_is_explicit_and_does_not_invent_data(self):
        with self.assertRaises(ProviderError):
            project_from_native(["wrong-id", []], "account")
        service = Notebooks("http://127.0.0.1:9231", "account")
        service.read = AsyncMock(return_value=[[]])
        with self.assertRaises(ProviderError):
            await service.get("notebooks/missing")

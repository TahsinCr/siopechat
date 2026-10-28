"""Gemini notebook protocol tests without application imports."""

import unittest
from unittest.mock import AsyncMock

from providers.errors import ProviderError
from providers.gemini.notebooks import Notebooks, project_from_native

from .fixtures import notebook


class NotebookTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_notebook_details_and_resources_are_normalized(self):
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
        service.read.return_value = [[notebook()]]
        self.assertEqual(await service.get("notebooks/abcdef"), project)

    async def test_unknown_native_contract_fails_without_inventing_data(self):
        with self.assertRaises(ProviderError):
            project_from_native(["wrong-id", []], "account")
        service = Notebooks("http://127.0.0.1:9231", "account")
        service.read = AsyncMock(return_value=[[]])
        with self.assertRaises(ProviderError):
            await service.get("notebooks/missing")

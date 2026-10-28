"""Application projection of provider-native Gemini notebook records."""

import unittest

from accounts import Accounts
from providers.gemini.notebooks import project_from_native
from providers.tests.fixtures import notebook


class GeminiProjectMappingTests(unittest.TestCase):
    def test_native_notebook_maps_to_the_shared_project_record(self):
        project = project_from_native(notebook(), "account")
        common = Accounts.project_record("gemini/account", project)
        self.assertEqual(common["kind"], "notebook")
        self.assertEqual(common["name"], project.name)
        self.assertEqual(common["instructions"], project.instructions)
        self.assertEqual(common["resources"][0]["name"], "Research notes")
        self.assertEqual(common["provider_id"], "gemini")
        self.assertTrue(common["metadata"]["supports_chat"])

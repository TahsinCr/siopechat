"""Application projection of provider-native Gemini notebook records."""

import unittest

from webaiprovider import Project
from webaiprovider.providers.gemini.notebooks import project_from_native

from accounts import Accounts
from tests.provider_fakes import notebook


class GeminiProjectMappingTests(unittest.TestCase):
    def test_native_notebook_maps_to_the_shared_project_record(self):
        data = project_from_native(notebook(), "account")
        project = Project(data.id, data.name, remote_id=data.id)
        project._apply_provider_project(data)
        common = Accounts.project_record("gemini/account", project)
        self.assertEqual(common["kind"], "notebook")
        self.assertEqual(common["name"], data.name)
        self.assertEqual(common["instructions"], data.instructions)
        self.assertEqual(common["resources"][0]["name"], "Research notes")
        self.assertEqual(common["provider_id"], "gemini")
        self.assertTrue(common["metadata"]["supports_chat"])

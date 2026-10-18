"""Project writes, provider ownership, dynamic forms and archive source references."""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bootstrap import create_application
from providers import Capability
from providers.models import Origin, Project, Resource
from providers.projects import ProjectEditing, ProjectOptions
from tests import test_cli_interaction as interaction
from tests.test_account_ui import Accounts
from tests.test_cli_interaction import terminal
from tests.test_ui_controls import immediate


class ProjectWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_write_is_not_repeated_while_readback_is_eventually_consistent(self):
        provider = ProjectEditing()
        provider.id, provider.account_id = "claude", "personal"
        provider._gate = asyncio.Lock()
        old = Project(Origin("claude", "personal", "one"), "Old")
        new = replace(old, name="New", instructions="Instructions")
        provider.editor = Mock(write=AsyncMock())
        provider.get_project = AsyncMock(side_effect=[old, old, new])
        with patch("providers.projects.asyncio.sleep", new=AsyncMock()):
            self.assertEqual(await provider.update_project(new), new)
        provider.editor.write.assert_awaited_once()
        with self.assertRaisesRegex(ValueError, "başka"):
            await provider.update_project(replace(new, origin=Origin("gemini", "personal", "one")))
        provider.editor.write.assert_awaited_once()


class ProjectFormTests(unittest.IsolatedAsyncioTestCase):
    settle = interaction.InputTests.settle

    async def test_creation_names_the_account_and_hides_unsupported_settings(self):
        service = Accounts()
        service.active_accounts = lambda: service.items()
        service.capabilities = lambda key: frozenset({Capability.PROJECT_CREATE})
        service.project_options = Mock(return_value=ProjectOptions(fields=("name",)))
        service.create_project = Mock(
            return_value={"id": "@gemini/personal/notebooks%2Fone", "name": "My notebook"}
        )
        with terminal(account_service=service) as (v, pipe):
            v._read_in_background = immediate
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                v.open_project_create()
                v.forms.fields["account"].current_value = "gemini/personal"
                pipe.send_bytes(b"\x13")
                await self.settle(lambda: "name" in v.forms.fields)
                self.assertEqual(set(v.forms.fields), {"name"})
                service.project_options.assert_called_once_with("gemini/personal")
                v.forms.fields["name"].text = "My notebook"
                pipe.send_bytes(b"\x13")
                await self.settle(lambda: service.create_project.called)
                service.create_project.assert_called_once_with(
                    "gemini/personal", "My notebook", "private", "default"
                )
                self.assertEqual(v.account_key, "chatgpt/default")
            finally:
                v.app.exit()
                await run

    async def test_instructions_accept_newlines_and_save_through_the_shared_form(self):
        saved = Mock()
        with terminal() as (v, pipe):
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                v.forms.open("Instructions", [("instructions", "Talimatlar", "First", None)], saved)
                v.forms.fields["instructions"].buffer.cursor_position = 5
                pipe.send_bytes(b"\rSecond")
                await self.settle(lambda: "Second" in v.forms.fields["instructions"].text)
                saved.assert_not_called()
                pipe.send_bytes(b"\x13")
                await self.settle(lambda: saved.called)
                saved.assert_called_once_with({"instructions": "First\nSecond"})
            finally:
                v.app.exit()
                await run


class ProjectArchiveTests(unittest.TestCase):
    def test_source_paths_survive_revalidation_without_resurrecting_removed_resources(self):
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                archive = api.get("archive")
                archive.put(
                    "gemini/personal",
                    "container",
                    "one",
                    {
                        "resources": [
                            {"id": "a", "name": "File", "path": "abc/source", "sha256": "digest"},
                            {"id": "b", "name": "Removed"},
                        ]
                    },
                )
                archive.put(
                    "gemini/personal",
                    "container",
                    "one",
                    {"resources": [{"id": "a", "name": "Renamed"}]},
                )
                value = archive.get("gemini/personal", "container", "one")
                self.assertEqual(
                    value["resources"],
                    [{"id": "a", "name": "Renamed", "path": "abc/source", "sha256": "digest"}],
                )
            finally:
                api.runtime.close()

    def test_local_failure_after_remote_project_write_does_not_report_remote_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                accounts = api.get("accounts")
                project = Project(
                    Origin("chatgpt", "default", "one"),
                    "Saved",
                    resources=(Resource("a", "Source"),),
                )
                with patch.object(api.get("archive"), "put", side_effect=OSError("disk full")):
                    value = accounts.store_project("chatgpt/default", project)
                self.assertEqual(value["name"], "Saved")
                self.assertIn("cache_warning", value)
            finally:
                api.runtime.close()

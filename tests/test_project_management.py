"""Project writes, provider ownership, dynamic forms and archive source references."""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bootstrap import create_application
from cli.projects import ProjectPanel
from providers import Capability
from providers.models import Origin, Project, Resource
from providers.projects import ProjectEditing, ProjectOptions
from tests import cli_support as interaction
from tests.cli_support import immediate, terminal
from tests.fakes import Accounts


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
    settle = staticmethod(interaction.wait_until)

    async def test_edit_uses_the_shared_picker_and_opens_inline_on_the_project_tab(self):
        service = Accounts()
        with terminal(account_service=service) as (v, _):
            value = "@chatgpt/default/project-one"
            v.project_items = {value: {"id": value, "name": "Example"}}
            v.projects_loaded = True
            v.project_panel.edit()
            self.assertEqual(v.palette_items[0]["id"], "project.edit:" + value)
            project = {"id": value, "name": "Example", "instructions": "Instructions"}

            def details(_):
                v.tab_kind = "project"
                v.current_project = project

            v.show_project_details = Mock(side_effect=details)
            v.project_panel.context = lambda callback, target: callback(
                "chatgpt/default", target, project, ProjectOptions(fields=("name", "instructions"))
            )
            v.palette_filtered = v.palette_items
            v.palette_index = 0
            v.execute_palette_item()
            v.show_project_details.assert_called_once_with(value)
            self.assertEqual(v.forms.inline_project, value)
            self.assertEqual(set(v.forms.fields), {"name", "instructions"})

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

    async def test_project_file_source_uses_shared_provider_filtered_file_picker(self):
        opened = []
        view = Mock()
        view.tr = lambda value: value
        view.forms.open = lambda *args, **kwargs: opened.append((args, kwargs))
        panel = ProjectPanel(view)
        panel.label = lambda _key: "ChatGPT · test"
        options = ProjectOptions(
            sources=({"id": "file", "title": "PDF", "accept": "application/pdf"},),
            remove_sources=True,
        )
        panel.context = lambda callback, value=None: callback(
            "chatgpt/default", "project-one", {}, options
        )

        panel.add_source()
        first_args, _ = opened.pop()
        next_form = first_args[2]({"kind": "file"})
        next_form()
        second_args, second_kwargs = opened.pop()

        self.assertEqual(second_args[1][0][0], "path")
        self.assertEqual(len(second_kwargs["actions"]), 1)
        with patch("cli.projects.browse_form_field") as browse:
            second_kwargs["actions"][0][1]()
        browse.assert_called_once_with(view, "path", "application/pdf", multiple=False)


class ProjectArchiveTests(unittest.TestCase):
    def test_added_project_source_keeps_provider_metadata_without_copying_local_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                accounts = api.get("accounts")
                key = "chatgpt/default"
                source = Path(folder) / "source.pdf"
                source.write_bytes(b"private source body")
                project = Project(
                    Origin("chatgpt", "default", "one"),
                    "Project",
                    resources=(Resource("source-1", "source.pdf", "application/pdf"),),
                )
                provider = Mock(
                    add_project_resource=AsyncMock(return_value=project), close=AsyncMock()
                )
                accounts._providers[key] = provider

                result = accounts.change_project(
                    key,
                    "@chatgpt/default/one",
                    "add_source",
                    kind="file",
                    path=str(source),
                )

                self.assertEqual(result["resources"][0]["name"], "source.pdf")
                self.assertNotIn("path", result["resources"][0])
                self.assertEqual(list(api.get("archive").files.root.glob("??/" + "?" * 64)), [])
                self.assertTrue(source.exists())
            finally:
                api.runtime.close()

    def test_revalidation_does_not_restore_stale_local_source_paths(self):
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
                self.assertEqual(value["resources"], [{"id": "a", "name": "Renamed"}])
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

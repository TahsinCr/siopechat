"""Workspace tab, response and temporary-chat lifecycle behavior."""

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from prompt_toolkit.utils import get_cwidth

from backend import BackendService
from bootstrap import create_application
from providers.chatgpt.formats import normalize_message
from tests.tui_support import immediate, terminal
from tui.markdown import visible_chat_message
from tui.styles import ICONS
from tui.tab_session import TabSessionStore


class WorkspaceTests(unittest.TestCase):
    def test_tabs_are_capped_adjacent_and_center_titles(self):
        with terminal() as (v, _):
            v.title = "Demo"
            fragments = v.render_tabs()
            self.assertEqual(sum(get_cwidth(f[1]) for f in fragments), v.terminal_width())
            self.assertEqual(fragments[0][1], f" {ICONS['menu'].icon} SiopeChat ")
            self.assertEqual(fragments[1][1], "│")
            label = fragments[2][1]
            self.assertLessEqual(
                abs((len(label) - len(label.lstrip())) - (len(label) - len(label.rstrip()))), 1
            )

    def test_settings_reuses_tab_and_contains_accounts(self):
        with terminal(settings_rows=lambda: [], session_action=lambda _: {}) as (v, _):
            v._read_in_background = immediate
            v.messages = [{"role": "user", "text": "Keep"}]
            v.settings_dialog.open()
            self.assertEqual(v.tab_kind, "settings")
            v.session_dialog.open()
            self.assertEqual(v.settings_dialog.category, "accounts")
            self.assertEqual(len(v.tabs), 2)
            v.settings_dialog.close()
            self.assertEqual(v.messages[0]["text"], "Keep")

    def test_extension_category_toggle_uses_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            api = create_application(Path(directory))
            self.addCleanup(api.runtime.close)

            def change(section, key, value):
                asyncio.run(
                    api.actions.invoke("settings.change", section=section, key=key, value=value)
                )
                return api.settings.snapshot()

            with terminal(
                settings_rows=lambda: asyncio.run(api.actions.invoke("settings.show")),
                change_setting=change,
            ) as (v, _):
                v._read_in_background = immediate
                v.settings_dialog.open("extensions")
                i = next(
                    i for i, r in enumerate(v.settings_dialog.filtered) if r["key"] == "enabled"
                )
                v.settings_dialog.toggle(i)
                self.assertFalse(v.layout_enabled)
                v.settings_dialog.toggle(i)
                self.assertTrue(v.layout_enabled)

    def test_project_and_temporary_contexts_are_per_tab(self):
        move = Mock()
        with terminal(move_conversation=move) as (v, _):
            v._read_in_background = immediate
            v.project_items = {"g-p-work": {"id": "g-p-work", "name": "Work"}}
            v.conversation_id = "old"
            v.messages = [{"role": "user", "text": "Keep"}]
            v.handle_command("/project")
            self.assertIn("project.select:g-p-work", [i["id"] for i in v.palette_items])
            v.choose_project("g-p-work")
            self.assertEqual(v.current_project["id"], "g-p-work")
            move.assert_called_once_with("old", "g-p-work")
            self.assertEqual(len(v.tabs), 1)
            self.assertEqual(v.conversation_id, "old")
            self.assertEqual(v.tabs[0]["messages"][0]["text"], "Keep")
            v.new_temporary_chat()
            self.assertTrue(v.temporary)
            v.choose_project("g-p-work")
            self.assertEqual(v.current_project, {})
            v.switch_tab(-1)
            self.assertFalse(v.temporary)

    def test_reasoning_is_visible_but_hidden_protocol_nodes_are_not(self):
        raw = {
            "author": {"role": "assistant"},
            "channel": "analysis",
            "content": {"content_type": "thoughts", "thoughts": [{"summary": "Summary"}]},
        }
        self.assertEqual(
            visible_chat_message(normalize_message(raw)), {"role": "reasoning", "text": "Summary"}
        )
        raw["metadata"] = {"is_visually_hidden_from_conversation": True}
        self.assertIsNone(visible_chat_message(normalize_message(raw)))

    def test_reasoning_stream_does_not_replace_final_answer(self):
        def send(*args, **kw):
            args[3]({"type": "delta", "channel": "analysis", "message_id": "r", "text": "Summary"})
            args[3]({"type": "delta", "channel": "final", "text": "Answer"})
            return {}

        with terminal() as (v, _):
            v.send_prompt = send
            v.messages = [{"role": "user", "text": "Q"}, {"role": "assistant", "text": ""}]
            v.run_send("Q", [], 0, v.messages, None, "model", None)
            self.assertEqual([m["role"] for m in v.messages], ["user", "reasoning", "assistant"])
            self.assertEqual(v.messages[-1]["text"], "Answer")

    def test_reasoning_identity_is_scoped_to_each_turn(self):
        def send(*args, **kw):
            args[3]({"type": "delta", "channel": "analysis", "text": "Summary"})
            args[3]({"type": "replace", "text": "Answer"})
            return {}

        with terminal() as (v, _):
            for _ in range(2):
                v.messages.extend(
                    [{"role": "user", "text": "Q"}, {"role": "assistant", "text": ""}]
                )
                v.run_send("Q", [], 0, v.messages, None, "model", None, send_callback=send)
            thoughts = [m for m in v.messages if m["role"] == "reasoning"]
            self.assertEqual([m["text"] for m in thoughts], ["Summary", "Summary"])
            self.assertNotEqual(thoughts[0]["stream_id"], thoughts[1]["stream_id"])

    def test_background_streams_keep_tab_state_and_cancellation_separate(self):
        gates = [threading.Event(), threading.Event()]
        started = [threading.Event(), threading.Event()]
        connections = [Mock(), Mock()]

        def sender(index):
            def send(*args, **kw):
                args[4](connections[index])
                started[index].set()
                if not gates[index].wait(2):
                    raise TimeoutError("test gate")
                args[3]({"type": "replace", "channel": "analysis", "text": f"Thought {index}"})
                args[3]({"type": "replace", "text": f"Answer {index}"})
                return {"conversation_id": f"chat-{index}"}

            return send

        with terminal() as (v, _):
            try:
                for index in range(2):
                    v.send_prompt = sender(index)
                    v.input.text = f"Question {index}"
                    v.submit_prompt()
                    self.assertTrue(started[index].wait(1))
                    if index == 0:
                        v.new_tab()
                v.switch_tab(-1)
                self.assertIs(v.cancel_connection, connections[0])
                self.assertTrue(v.active_busy())
                # Finish one request while the other remains active.
                gates[0].set()
                deadline = time.monotonic() + 1
                while v.tabs[0].get("busy") and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertFalse(v.tabs[0]["busy"])
                self.assertTrue(v.tabs[1]["busy"])
                self.assertTrue(v.busy)
                self.assertIsNone(v.cancel_connection)
                self.assertEqual(v.messages[-1]["text"], "Answer 0")
                v.switch_tab(1)
                self.assertIs(v.cancel_connection, connections[1])
                self.assertEqual(v.messages[0]["text"], "Question 1")
            finally:
                for gate in gates:
                    gate.set()
                deadline = time.monotonic() + 1
                while v.busy and time.monotonic() < deadline:
                    time.sleep(0.005)
            self.assertFalse(v.busy)
            self.assertEqual(v.messages[-1]["text"], "Answer 1")
            self.assertEqual(v.tabs[0]["messages"][-1]["text"], "Answer 0")

    def test_closing_stream_keeps_sidebar_entry_and_reopens_same_state(self):
        import gc
        import weakref

        class Source(str):
            pass

        gate = threading.Event()
        started = threading.Event()

        def send(*args, **kwargs):
            started.set()
            if not gate.wait(2):
                raise TimeoutError("test gate")
            args[3]({"type": "reasoning", "text": "Background summary"})
            args[3]({"type": "replace", "text": "Background answer"})
            return {"conversation_id": "background-result"}

        with terminal() as (v, _):
            historical = Source("History no longer needed by the stream\n" * 10_000)
            historical_ref = weakref.ref(historical)
            v.messages = [{"role": "assistant", "text": historical}]
            del historical
            v.send_prompt = send
            v.input.text = "Background question"
            v.submit_prompt()
            self.assertTrue(started.wait(1))
            original = v.tabs[0]
            try:
                v.close_tab()
                gc.collect()
                self.assertIsNone(historical_ref())
                self.assertEqual(len(original["messages"]), 2)
                self.assertIsNot(v.tabs[0], original)
                self.assertTrue(v.busy)
                self.assertFalse(v.active_busy())
                self.assertIs(v.background_tabs[0], original)
                v.render_sidebar()
                row_index = next(
                    i for i, row in enumerate(v.sidebar_rows) if row.get("live_tab") is original
                )
                self.assertIn("●", "".join(part[1] for part in v.render_sidebar()))
                v.sidebar_index = row_index
                v.activate_sidebar_item()
                self.assertIs(v.tabs[v.active_tab], original)
                self.assertTrue(v.active_busy())
                # Detach again; its completion must not overwrite the replacement draft.
                v.close_tab()
                replacement = v.tabs[v.active_tab]
                gate.set()
                deadline = time.monotonic() + 1
                while v.busy and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertFalse(v.busy)
                self.assertIs(v.tabs[v.active_tab], replacement)
                self.assertEqual(v.messages, [])
                self.assertEqual(original["messages"][-1]["text"], "Background answer")
                self.assertEqual(original["conversation_id"], "background-result")
                self.assertIn("✓", "".join(part[1] for part in v.render_sidebar()))
                v.resume_conversation("background-result")
                self.assertIs(v.tabs[v.active_tab], original)
                self.assertEqual(v.messages[-1]["text"], "Background answer")
            finally:
                gate.set()

    def test_busy_composer_queues_one_followup_with_selected_context(self):
        with terminal() as (view, _):
            tab = view.tabs[0]
            tab["busy"] = True
            view.busy = True
            view.current_project = {"id": "project"}
            view.model = "queued-model"
            view.effort = None
            view.input.text = "Follow-up"

            view.submit_prompt()

            queued = tab["queued_submit"]
            self.assertEqual(queued["prompt"], "Follow-up")
            self.assertEqual(queued["model"], "queued-model")
            self.assertIsNone(queued["effort"])
            self.assertEqual(queued["project_id"], "project")
            self.assertFalse(view.input.text)
            self.assertEqual(view.messages[-1]["text"], "Follow-up")
            self.assertTrue(view.messages[-1].get("queued"))

            view.input.text = "Another follow-up"
            view.submit_prompt()
            self.assertEqual(tab["queued_submit"], queued)
            self.assertEqual(view.input.text, "Another follow-up")

    def test_queued_send_preserves_explicit_defaults_and_attachment_prompt(self):
        attachment = {"queue_id": "queued-file", "name": "notes.txt"}
        with terminal() as (view, _):
            tab = view.tabs[0]
            tab.update(model="old-model", effort="high", project={"id": "old-project"})
            queued = {
                "prompt": "",
                "attachments": [attachment],
                "model": "new-model",
                "effort": None,
                "project_id": None,
                "temporary": False,
            }

            with patch("tui.chat.sending.threading.Thread") as thread:
                view._execute_queued_submit(tab, queued)

            args = thread.call_args.kwargs["args"]
            self.assertEqual(args[0], view.tr("Please review the attached files."))
            self.assertEqual(args[5:], ("new-model", None, None, False))
            self.assertTrue(view._attachments_in_flight)
            thread.return_value.start.assert_called_once()

    def test_queued_send_keeps_completed_background_tab_tracked(self):
        queued = {
            "prompt": "Follow-up",
            "attachments": [],
            "model": "model",
            "effort": None,
            "project_id": None,
            "temporary": False,
        }
        with terminal() as (view, _):
            target = view.tabs[0]
            target["busy"] = True
            target["messages"] = [
                {"role": "user", "text": "First question"},
                {"role": "assistant", "text": ""},
            ]
            target["queued_submit"] = queued
            view.busy = True
            view.close_tab()
            target = view.background_tabs[0]
            view.archive_service = lambda: object()

            def send(*args, **kwargs):
                return {"conversation_id": "first-chat"}

            with patch("tui.chat.sending.threading.Thread") as thread:
                view.run_send(
                    "First question",
                    [],
                    0,
                    target["messages"],
                    None,
                    "model",
                    None,
                    send_callback=send,
                    target_tab=target,
                )

            self.assertIn(target, view.background_tabs)
            self.assertTrue(target["busy"])
            self.assertEqual(target["messages"][-2]["text"], "Follow-up")
            thread.return_value.start.assert_called_once()

    def test_failed_original_send_restores_queued_attachments_for_background_tab(self):
        queued_attachment = {"queue_id": "queued-file", "name": "queued.txt"}
        existing_attachment = {"queue_id": "existing-file", "name": "existing.txt"}
        with terminal() as (view, _):
            target = view.tabs[0]
            target["empty_workspace"] = False
            target["busy"] = True
            view.messages = [
                {"role": "user", "text": "First question"},
                {"role": "assistant", "text": ""},
                {"role": "user", "text": "Follow-up", "queued": True},
            ]
            target["messages"] = view.messages
            target["queued_submit"] = {
                "prompt": "Follow-up",
                "attachments": [queued_attachment],
                "model": "model",
                "effort": None,
                "project_id": None,
                "temporary": False,
            }
            view.pending_attachments = [existing_attachment]
            view.new_tab()

            def fail(*args, **kwargs):
                raise RuntimeError("offline")

            view.run_send(
                "First question",
                [],
                0,
                target["messages"],
                None,
                "model",
                None,
                send_callback=fail,
                target_tab=target,
            )

            self.assertEqual(target["draft"], "Follow-up")
            self.assertEqual(
                [item["queue_id"] for item in view.pending_attachments],
                ["queued-file", "existing-file"],
            )
            self.assertEqual(len(target["messages"]), 2)
            self.assertFalse(any(m.get("queued") for m in target["messages"]))

    def test_quit_confirmation_includes_background_requests_and_queued_drafts(self):
        with terminal() as (view, _):
            view.app.exit = Mock()
            view.app.invalidate = Mock()
            cases = (
                {"busy": True, "stream_received": False},
                {
                    "busy": True,
                    "stream_received": True,
                    "queued_submit": {"prompt": "Follow-up", "attachments": []},
                },
            )
            for case in cases:
                with self.subTest(case=case):
                    view.background_tabs = [case]
                    view.quit_armed_until = 0
                    view.request_quit()
                    view.app.exit.assert_not_called()
                    view.app.invalidate.assert_called()

    def test_queued_attachments_block_a_second_attachment_send(self):
        with terminal() as (view, _):
            tab = view.tabs[0]
            tab["busy"] = True
            tab["queued_submit"] = {
                "prompt": "Follow-up",
                "attachments": [{"queue_id": "queued-file", "name": "queued.txt"}],
            }
            view.busy = True
            view.pending_attachments = [{"queue_id": "new-file", "name": "new.txt"}]
            view.input.text = "Another message"

            view.submit_prompt()

            self.assertEqual(view.input.text, "Another message")
            self.assertIn("başka bir mesajla", view.status)

    def test_sidebar_opens_another_chat_during_a_response(self):
        gate, started = threading.Event(), threading.Event()

        def send(*args, **kwargs):
            started.set()
            gate.wait(2)
            args[3]({"type": "replace", "text": "Original answer"})
            return {"conversation_id": "original"}

        with terminal() as (v, _):
            v.send_prompt = send
            v.load_conversation = lambda _: {
                "title": "Other chat",
                "messages": [{"role": "assistant", "text": "History"}],
            }
            v.input.text = "Question"
            v.submit_prompt()
            self.assertTrue(started.wait(1))
            original = v.tabs[0]
            try:
                v.sidebar_items = [{"id": "other", "title": "Other chat"}]
                v._build_sidebar_rows()
                v.sidebar_index = next(
                    i for i, row in enumerate(v.sidebar_rows) if row.get("id") == "other"
                )
                v.activate_sidebar_item()
                deadline = time.monotonic() + 1
                while v.loading_conversation and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(v.conversation_id, "other")
                self.assertTrue(original["busy"])
                self.assertEqual(v.messages[-1]["text"], "History")
                gate.set()
                deadline = time.monotonic() + 1
                while v.busy and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(v.messages[-1]["text"], "History")
                self.assertEqual(original["messages"][-1]["text"], "Original answer")
            finally:
                gate.set()

    def test_sync_is_allowed_while_a_response_is_running(self):
        with terminal() as (v, _):
            v.account_service = Mock()
            v._read_in_background = immediate
            v.refresh_sidebar = Mock()
            v.refresh_provider_catalog = Mock()
            v.apply_archive_updates = Mock()
            v.busy = True
            v.tabs[0]["busy"] = True
            v.synchronize("conversation")
            v.account_service.synchronize.assert_called_once_with(
                "conversation", v.account_key, v.conversation_id
            )
            self.assertTrue(v.tabs[0]["busy"])
            self.assertFalse(v._synchronize_pending)
            v.apply_archive_updates.assert_called_once_with()

    def test_closed_temporary_tab_forgets_remote_context_locally(self):
        forgotten = Mock(return_value={"forgotten": True})
        with terminal(forget_temporary=forgotten) as (v, _):
            v._read_in_background = immediate
            v.new_temporary_chat()
            v.conversation_id = "temporary-id"
            v._save_active_tab()
            v.close_tab()
            forgotten.assert_called_once_with("temporary-id")
            self.assertFalse(v.temporary)

    def test_empty_project_picker_does_not_poll_forever(self):
        listing = Mock(return_value={"items": [], "cursor": None})
        with terminal(list_projects=listing) as (v, _):
            v._read_in_background = immediate
            v.open_project_picker()
            v.open_project_picker()
            listing.assert_called_once_with(None)
            self.assertTrue(v.projects_loaded)

    def test_temporary_cache_never_writes(self):
        b = BackendService()
        with (
            patch.object(b, "atomic_json_write") as write,
            patch.object(b, "set_active_conversation") as active,
        ):
            b.cache_conversation({"id": "temp", "temporary": True, "messages": []})
            write.assert_not_called()
            active.assert_not_called()

    def test_queued_message_is_saved_as_draft_in_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TabSessionStore(Path(tmp) / "session.json")
            with terminal() as (v, _):
                v.session_store = store
                v.busy = True
                v.tabs[0]["busy"] = True
                v.input.text = "queued follow up"
                v.submit_prompt()
                self.assertEqual(v.tabs[0].get("draft", ""), "")

                v._save_active_tab()
                self.assertEqual(v.tabs[0].get("draft", ""), "")
                v._persist_session()

                self.assertEqual(v.tabs[0].get("draft", ""), "")
                self.assertEqual(store.load()["tabs"][0]["draft"], "queued follow up")

    def test_four_busy_tabs_allow_queuing_followup_on_busy_tab(self):
        with terminal() as (v, _):
            for _ in range(3):
                v.new_tab()
            for tab in v.tabs:
                tab["busy"] = True
            v.busy = True
            v.input.text = "queued follow up"
            v.submit_prompt()
            self.assertEqual(
                v.tabs[v.active_tab].get("queued_submit", {}).get("prompt"),
                "queued follow up",
            )
            v.new_tab()
            v.busy = False
            v.tabs[v.active_tab]["busy"] = False
            v.input.text = "blocked fifth submit"
            v.submit_prompt()
            self.assertIn("Aynı anda en fazla 4 bekleyen istek", v.status)

    def test_close_tab_does_not_wait_for_renderer_and_schedules_one_cleanup(self):
        with terminal() as (v, _):
            jobs = []
            v._read_in_background = lambda fn, ok, err, session_bound=True: jobs.append(
                (fn, session_bound)
            )
            entered, release = threading.Event(), threading.Event()

            def hold_renderer():
                with v.renderer._lock:
                    entered.set()
                    release.wait(5)

            holder = threading.Thread(target=hold_renderer)
            holder.start()
            self.assertTrue(entered.wait(3))
            closed = threading.Event()
            closer = threading.Thread(target=lambda: (v.close_tab(), closed.set()))
            try:
                closer.start()
                self.assertTrue(closed.wait(3), "tab close waited for the renderer lock")
            finally:
                release.set()
                holder.join()
                closer.join()
            self.assertEqual(len(jobs), 1)
            fn, bound = jobs[0]
            self.assertFalse(bound)
            with patch("tui.chat.tabs.trim_memory") as trim:
                fn()
            trim.assert_called_once_with()

    def test_trim_memory_cross_platform_execution(self):
        from utils.process import trim_memory

        trim_memory()

    def test_close_all_tabs_invokes_trim_memory_in_background(self):
        with terminal() as (v, _):
            v.new_tab()
            v.messages = [{"role": "user", "text": "Hello"}]
            v.tabs[v.active_tab]["messages"] = v.messages
            jobs = []
            v._read_in_background = lambda fn, ok, err, session_bound=True: jobs.append(
                (fn, session_bound)
            )
            v.close_all_tabs()
            self.assertEqual(len(v.tabs), 1)
            self.assertEqual(v.messages, [])
            self.assertIsNone(v.conversation_id)
            self.assertEqual(len(jobs), 1)
            fn, bound = jobs[0]
            self.assertFalse(bound)
            with patch("tui.chat.tabs.trim_memory") as trim:
                fn()
            trim.assert_called_once_with()

    def test_removing_multiple_deleted_tabs_schedules_one_cleanup(self):
        with terminal() as (view, _):
            view.new_tab()
            view.new_tab()
            ids = [f"@chatgpt/default/{i}" for i in range(3)]
            for tab, conversation_id in zip(view.tabs, ids):
                tab["conversation_id"] = conversation_id
            jobs = []
            view._read_in_background = lambda fn, ok, err, session_bound=True: jobs.append(
                (fn, session_bound)
            )
            view.remove_deleted_conversation_tabs(ids)
            self.assertEqual(len(jobs), 1)
            self.assertFalse(jobs[0][1])
            self.assertTrue(all(tab.get("conversation_id") not in ids for tab in view.tabs))

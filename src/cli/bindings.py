"""Config-driven terminal shortcuts; every mapping comes from config.json."""

from typing import Any, Callable

from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings

from .editing import bind_editing, copy_selection


def key_bindings(view) -> KeyBindings:
    bindings = KeyBindings()
    configured = view.keybindings
    view.action_handlers = {}

    def bind(action_id: str, callback: Callable[[Any], None], condition=None) -> None:
        view.action_handlers.setdefault(action_id, callback)
        keys = configured.get(action_id)
        if not keys:
            return
        available = Condition(
            lambda: (
                action_id
                in (
                    "app.quit",
                    "app.quit_eof",
                    "command.help",
                    "command.session",
                    "command.settings",
                    "chat.new",
                    "chat.next_tab",
                    "chat.previous_tab",
                    "chat.close_tab",
                    "chat.temporary",
                    "ui.command_palette",
                )
                or view.palette_visible
                or view.resume_visible
                or (not view.settings_dialog.visible and not view.session_dialog.visible)
            )
        )
        bindings.add(
            *keys.lower().split(),
            is_global=True,
            filter=(available & Condition(lambda: not view.forms.visible)) & condition
            if condition is not None
            else available & Condition(lambda: not view.forms.visible),
        )(callback)

    completion_visible = Condition(
        lambda: (
            view.app.layout.current_control is view.input.control
            and view.input.buffer.complete_state is not None
        )
    )
    sidebar_focused = Condition(lambda: view.app.layout.current_control is view.sidebar_control)
    list_focused = Condition(
        lambda: (
            view.palette_visible
            or view.resume_visible
            or view.app.layout.current_control
            in (view.sidebar_control, view.sidebar_search.control)
        )
    )
    transcript_or_composer = Condition(
        lambda: view.app.layout.current_control in (view.input.control, view.transcript_control)
    )
    # Ctrl+H is indistinguishable from Backspace in many terminals. A user's old
    # reload binding must never consume editing keys in a text field.
    not_editing = Condition(lambda: not hasattr(view.app.layout.current_control, "buffer"))

    def create_tab(_event: Any) -> None:
        view.new_tab()

    def close_tab(_event: Any) -> None:
        view.close_tab()

    def next_tab(_event: Any) -> None:
        view.switch_tab(1)

    def previous_tab(_event: Any) -> None:
        view.switch_tab(-1)

    def command_palette(_event: Any) -> None:
        view.open_palette()

    def search(_event: Any) -> None:
        view.open_sidebar_search()

    def toggle_sidebar(_event: Any) -> None:
        view.toggle_sidebar()

    def focus_sidebar(_event: Any) -> None:
        view.focus_sidebar()

    def toggle_sidebar_group(_event: Any) -> None:
        if view.app.layout.current_control is view.sidebar_control:
            view.toggle_sidebar_group()

    def resume(_event: Any) -> None:
        view.open_resume()

    def send(event: Any) -> None:
        if view.palette_visible:
            view.execute_palette_item()
        elif view.resume_visible:
            view.select_resume()
        elif event.app.layout.current_control in (
            view.sidebar_control,
            view.sidebar_search.control,
        ):
            view.activate_sidebar_item()
        elif event.app.layout.current_control is view.input.control:
            state = view.input.buffer.complete_state
            if state is not None and state.completions:
                view.input.buffer.apply_completion(state.current_completion or state.completions[0])
            else:
                view.submit_prompt()

    def newline(event: Any) -> None:
        if event.app.layout.current_control is view.input.control:
            view.input.buffer.insert_text("\n")

    def paste(_event: Any) -> None:
        view.paste_clipboard_image()

    def interrupt(_event: Any) -> None:
        if copy_selection(view):
            return
        control = view.app.layout.current_control
        if control in (
            view.palette_search.control,
            view.resume_search.control,
            view.sidebar_search.control,
        ):
            control.buffer.reset()
        elif view.active_busy():
            view.status = "Stopping response…"
            connection = view.cancel_connection
            if connection is not None:
                try:
                    connection.shutdown(2)
                except OSError:
                    pass
                try:
                    connection.close()
                except OSError:
                    pass
        elif view.input.text and control is view.input.control:
            view.input.buffer.reset()
            view.status = "Prompt cleared"
        else:
            view.status = "Use the command palette to quit"
        view.app.invalidate()

    def quit_app(event: Any) -> None:
        view.request_quit()

    def quit_eof(event: Any) -> None:
        view.request_quit()

    def help_action(_event: Any) -> None:
        view.handle_command("/help")

    def rename_action(_event: Any) -> None:
        view.input.text = "/rename "
        view.app.layout.focus(view.input)

    def projects_action(_event: Any) -> None:
        view.show_projects()

    def project_action(_event: Any) -> None:
        view.show_project_details()

    def refresh_action(_event: Any) -> None:
        view.refresh_sidebar()
        view.refresh_provider_catalog(force=True)

    def settings_action(_event: Any) -> None:
        view.settings_dialog.open()

    def clear_transcript(_event: Any) -> None:
        view.handle_command("/clear")

    def reload_conversation(_event: Any) -> None:
        view.handle_command("/history")

    def copy_response(_event: Any) -> None:
        view.copy_latest_response()

    def remove_attachment(_event: Any) -> None:
        view.handle_command("/remove")

    def move_up(_event: Any) -> None:
        if view.palette_visible:
            view.palette_index = max(0, view.palette_index - 1)
        elif view.resume_visible:
            view.resume_index = max(0, view.resume_index - 1)
        elif view.app.layout.current_control in (view.sidebar_control, view.sidebar_search.control):
            view._build_sidebar_rows()
            view.sidebar_index = max(0, view.sidebar_index - 1)
            view.ensure_sidebar_selection_visible()
        else:
            return
        view.app.invalidate()

    def move_down(_event: Any) -> None:
        if view.palette_visible:
            view.palette_index = min(len(view.palette_filtered) - 1, view.palette_index + 1)
        elif view.resume_visible:
            view.resume_index = min(len(view.resume_filtered) - 1, view.resume_index + 1)
        elif view.app.layout.current_control in (view.sidebar_control, view.sidebar_search.control):
            view._build_sidebar_rows()
            view.sidebar_index = min(len(view.sidebar_rows) - 1, view.sidebar_index + 1)
            view.ensure_sidebar_selection_visible()
        else:
            return
        view.app.invalidate()

    def close_overlay(_event: Any) -> None:
        if view.palette_visible:
            view.close_palette()
        elif view.resume_visible:
            view.close_resume()
        else:
            if view.layout_controller:
                view.layout_controller.pending = None
            view.app.layout.focus(view.input)

    def focus_composer(_event: Any) -> None:
        view.palette_visible = False
        view.resume_visible = False
        view.app.layout.focus(view.input)

    def complete_command(event: Any) -> None:
        if event.app.layout.current_control is not view.input.control:
            return
        state = view.input.buffer.complete_state
        if state is not None and state.completions:
            view.input.buffer.apply_completion(state.current_completion or state.completions[0])
        else:
            view.input.buffer.start_completion(select_first=True)

    def dismiss_completion(_event: Any) -> None:
        view.input.buffer.cancel_completion()

    def page_up(_event: Any) -> None:
        view.pause_following()
        view.transcript_window.vertical_scroll = max(
            0, view.transcript_window.vertical_scroll - view.transcript_page_size()
        )

    def page_down(_event: Any) -> None:
        view.pause_following()
        view.transcript_window.vertical_scroll += view.transcript_page_size()

    def transcript_start(event: Any) -> None:
        if event.app.layout.current_control is view.input.control:
            view.input.buffer.cursor_position += (
                view.input.buffer.document.get_start_of_line_position()
            )
        else:
            view.pause_following()
            view.transcript_window.vertical_scroll = 0

    def transcript_end(event: Any) -> None:
        if event.app.layout.current_control is view.input.control:
            view.input.buffer.cursor_position += (
                view.input.buffer.document.get_end_of_line_position()
            )
        else:
            view.follow_output = True
            view.transcript_window.vertical_scroll = 10**9
        view.app.invalidate()

    for action_id, callback in (
        ("chat.new", create_tab),
        ("chat.close_tab", close_tab),
        ("chat.next_tab", next_tab),
        ("chat.previous_tab", previous_tab),
        ("ui.command_palette", command_palette),
        ("ui.search", search),
        ("navigation.focus_sidebar", focus_sidebar),
        ("ui.toggle_sidebar", toggle_sidebar),
        ("chat.resume", resume),
        ("chat.send", send),
        ("chat.newline", newline),
        ("chat.paste_image", paste),
        ("chat.interrupt", interrupt),
        ("app.quit", quit_app),
        ("command.help", help_action),
        ("command.rename", rename_action),
        ("command.projects", projects_action),
        ("command.settings", settings_action),
        ("chat.clear_transcript", clear_transcript),
        ("chat.copy_response", copy_response),
        ("chat.remove_attachment", remove_attachment),
        ("navigation.close_overlay", close_overlay),
        ("navigation.composer_next", complete_command),
        ("navigation.focus_composer", focus_composer),
        ("navigation.transcript_up", page_up),
        ("navigation.transcript_down", page_down),
        ("command.project", project_action),
        ("command.refresh", refresh_action),
    ):
        bind(action_id, callback)
    composer_focused = Condition(lambda: view.app.layout.current_control is view.input.control)
    bind("chat.newline_shift", newline, composer_focused)
    bind("chat.newline_lf", newline, composer_focused)
    bind("projects.create", lambda _: view.open_project_create())
    bind("chat.delete", lambda _: view.delete_record())
    bind("projects.delete", lambda _: view.delete_record("container"))
    bind("command.model", lambda _: view.handle_command("/model"))
    bind("command.effort", lambda _: view.handle_command("/effort"))
    bind("chat.attach", lambda _: view.open_attachment())
    bind("ui.filter", lambda _: view.open_filters())
    bind("accounts.add", lambda _: view.add_account())
    bind("layout.split", lambda _: view.begin_split())
    bind("layout.split_down", lambda _: view.begin_split("rows"))
    bind(
        "layout.unsplit",
        lambda _: view.layout_controller.unsplit() if view.layout_controller else None,
    )
    bind("chat.temporary", lambda _: view.new_temporary_chat())
    bind("command.session", lambda _: view.session_dialog.open())
    bind("chat.scroll_top", lambda _: view.scroll_top())
    bind("chat.scroll_bottom", lambda _: view.scroll_bottom())
    bind(
        "chat.reload",
        reload_conversation,
        not_editing if configured.get("chat.reload") == "c-h" else None,
    )
    bind("navigation.sidebar_up", move_up, list_focused)
    bind("navigation.sidebar_down", move_down, list_focused)
    bind("navigation.sidebar_toggle_group", toggle_sidebar_group, sidebar_focused)
    bind("navigation.transcript_start", transcript_start, transcript_or_composer)
    bind("navigation.transcript_end", transcript_end, transcript_or_composer)
    bind(
        "app.quit_eof",
        quit_eof,
        Condition(
            lambda: (
                view.app.layout.current_control is view.input.control
                or not hasattr(view.app.layout.current_control, "buffer")
            )
        ),
    )
    bind("navigation.close_overlay", dismiss_completion, completion_visible)
    bind_editing(bindings, view)
    return bindings

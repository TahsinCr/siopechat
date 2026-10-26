"""Clipboard and selection behavior shared by composer, settings and transcript."""

from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.filters import Condition
from prompt_toolkit.selection import SelectionState, SelectionType

from utils import clipboard


def active_buffer(view):
    return getattr(view.app.layout.current_control, "buffer", None)


def select_all(view):
    buffer = active_buffer(view)
    if buffer is not None:
        if not buffer.text:
            return
        buffer.cursor_position = len(buffer.text)
        buffer.selection_state = SelectionState(0, SelectionType.CHARACTERS)
    elif view.app.layout.current_control is view.transcript_control and view.messages:
        reference = getattr(view, "selected_message_ref", None)
        view.selected_message = next(
            (i for i, item in enumerate(view.messages) if item is reference),
            min(max(0, view.selected_message), len(view.messages) - 1),
        )
        view.transcript_control.clear_selection()
        view.message_selection = True
    view.app.invalidate()


def has_selection(view):
    buffer = active_buffer(view)
    return (
        bool(buffer.selection_state)
        if buffer is not None
        else bool(
            view.message_selection
            or (
                view.app.layout.current_control is view.transcript_control
                and view.transcript_control.selected_text
            )
        )
    )


def copy_selection(view, cut=False):
    buffer = active_buffer(view)
    if buffer is not None and buffer.selection_state:
        data = buffer.cut_selection() if cut and not buffer.read_only() else buffer.copy_selection()
        text = data.text
    elif (
        view.app.layout.current_control is view.transcript_control
        and view.transcript_control.selected_text
    ):
        text = view.transcript_control.selected_text
    elif view.message_selection and 0 <= view.selected_message < len(view.messages):
        reference = getattr(view, "selected_message_ref", None)
        index = next(
            (i for i, item in enumerate(view.messages) if item is reference), view.selected_message
        )
        text = view.messages[index]["text"]
    else:
        return False
    view.app.clipboard.set_data(ClipboardData(text))

    def copied(native):
        if not native:
            view.app.output.write_raw(clipboard.terminal_sequence(text))
            view.app.output.flush()
        view.show_temporary_status(view.tr("Kopyalandı"))

    view._read_in_background(
        lambda: clipboard.copy_text(text), copied, lambda _: copied(False), session_bound=False
    )
    return True


def paste(view):
    buffer = active_buffer(view)
    if buffer is None or buffer.read_only():
        return

    def loaded(text):
        if text is None:
            text = view.app.clipboard.get_data().text
        if text:
            if buffer.selection_state:
                buffer.cut_selection()
            buffer.insert_text(text)
        elif buffer is view.input.buffer:
            view.paste_clipboard_image()
        view.app.invalidate()

    view._read_in_background(
        clipboard.read_text,
        loaded,
        lambda reason: setattr(view, "status", str(reason)),
        session_bound=False,
    )


def bind_editing(bindings, view):
    def modified_enter(event):
        buffer = active_buffer(view)
        if buffer is not None and buffer.multiline():
            buffer.insert_text("\n")

    # Do not let the Escape prefix of a modified Enter close a text form.
    bindings.add(
        "escape",
        "c-j",
        is_global=True,
        filter=Condition(
            lambda: (
                active_buffer(view) is not None
                and view.app.layout.current_control is not view.input.control
            )
        ),
    )(modified_enter)
    # Ctrl+C keeps the existing interrupt binding when there is no selection.
    for action, callback in [
        ("edit.select_all", lambda: select_all(view)),
        ("edit.cut", lambda: copy_selection(view, cut=True)),
    ]:
        view.action_handlers[action] = lambda event, callback=callback: callback()
        keys = view.keybindings.get(action)
        if keys:
            bindings.add(
                *keys.split(),
                filter=Condition(
                    lambda: (
                        active_buffer(view) is not None
                        or view.app.layout.current_control is view.transcript_control
                    )
                ),
            )(lambda event, callback=callback: callback())
    bindings.add(
        *view.keybindings["chat.interrupt"].split(),
        filter=Condition(lambda: has_selection(view)),
        eager=True,
    )(lambda event: copy_selection(view))
    bindings.add(
        *view.keybindings["chat.paste_image"].split(),
        filter=Condition(lambda: active_buffer(view) is not None),
    )(lambda event: paste(view))

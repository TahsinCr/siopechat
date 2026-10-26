"""Partial mouse selection in rendered messages and reasoning summaries."""

import unittest
from unittest.mock import Mock, patch

from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from tests.cli_support import immediate, terminal


class PartialSelectionTests(unittest.TestCase):
    def test_reverse_drag_spans_rows_and_preserves_unicode(self):
        with terminal(
            initial_messages=[
                {"role": "reasoning", "text": "first"},
                {"role": "assistant", "text": "second"},
            ]
        ) as (view, _):
            rows = (((("", "αβ first"),), 0), ((("", "second row"),), 1))
            view.transcript_rows = Mock(return_value=rows)
            control = view.transcript_control
            control.create_content(80, 20)
            control.mouse_handler(
                MouseEvent(Point(6, 1), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset())
            )
            control.mouse_handler(
                MouseEvent(Point(3, 0), MouseEventType.MOUSE_MOVE, MouseButton.LEFT, frozenset())
            )
            control.mouse_handler(
                MouseEvent(Point(3, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
            )
            self.assertEqual(control.selected_text, "first\nsecond")

    def test_drag_on_a_link_selects_text_without_launching_browser(self):
        with terminal(
            initial_messages=[
                {"role": "assistant", "text": "[Select this link](https://example.com)"}
            ]
        ) as (view, _):
            control = view.transcript_control
            control.create_content(80, 20)
            y = next(
                i
                for i, (spans, _) in enumerate(control.rows)
                if any(len(span) > 2 for span in spans)
            )
            x = "".join(span[1] for span in control.rows[y][0]).index("Select")
            with patch("cli.markdown.subprocess.Popen") as launch:
                for kind, column in (
                    (MouseEventType.MOUSE_DOWN, x),
                    (MouseEventType.MOUSE_MOVE, x + 6),
                    (MouseEventType.MOUSE_UP, x + 6),
                ):
                    control.mouse_handler(
                        MouseEvent(Point(column, y), kind, MouseButton.LEFT, frozenset())
                    )
                self.assertEqual(control.selected_text, "Select")
                launch.assert_not_called()

    def test_drag_copies_rendered_substring_without_opening_link(self):
        from cli.editing import copy_selection

        with terminal(
            initial_messages=[{"role": "reasoning", "text": "Before **selected text** afterwards."}]
        ) as (view, _):
            view._read_in_background = immediate
            control = view.transcript_control
            control.create_content(80, 20)
            for y, (spans, _) in enumerate(control.rows):
                line = "".join(span[1] for span in spans)
                if "selected text" in line:
                    x = line.index("selected text")
                    break

            def event(kind, position):
                return MouseEvent(Point(position, y), kind, MouseButton.LEFT, frozenset())

            control.mouse_handler(event(MouseEventType.MOUSE_DOWN, x))
            control.mouse_handler(event(MouseEventType.MOUSE_MOVE, x + len("selected text")))
            control.mouse_handler(event(MouseEventType.MOUSE_UP, x + len("selected text")))
            self.assertEqual(control.selected_text, "selected text")
            with (
                patch("utils.clipboard.copy_text", return_value=True) as copied,
                patch.object(view, "show_temporary_status") as status,
            ):
                self.assertTrue(copy_selection(view))
                copied.assert_called_with("selected text")
                status.assert_called_once()
            self.assertFalse(view.message_selection)
            view.new_tab()
            self.assertEqual(control.selected_text, "")

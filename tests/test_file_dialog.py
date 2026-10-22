"""Provider-aware file chooser filters on each supported desktop path."""

import sys
import types
import unittest
from unittest.mock import Mock, patch

from utils.file_dialog import accepted_extensions, choose_files


class FileDialogTests(unittest.TestCase):
    def test_accept_value_supports_extensions_mime_lists_and_wildcards(self):
        self.assertEqual(accepted_extensions(".pdf,.txt"), (".pdf", ".txt"))
        self.assertIn(".pdf", accepted_extensions("application/pdf"))
        self.assertEqual(accepted_extensions("*/*"), ())

    def test_kdialog_receives_name_filter_and_multiple_selection_flags(self):
        with (
            patch("utils.file_dialog.sys.platform", "linux"),
            patch(
                "utils.file_dialog.shutil.which",
                side_effect=lambda name: "/bin/kdialog" if name == "kdialog" else None,
            ),
            patch("utils.file_dialog._run", return_value=["/tmp/a.pdf"]) as run,
        ):
            self.assertEqual(choose_files(extensions=(".pdf",)), ["/tmp/a.pdf"])

        command = run.call_args.args[0]
        self.assertIn("Supported files (*.pdf);;All files (*)", command)
        self.assertIn("--multiple", command)
        self.assertIn("--separate-output", command)

    def test_zenity_receives_gtk_filter_syntax_and_all_files_fallback(self):
        with (
            patch("utils.file_dialog.sys.platform", "linux"),
            patch(
                "utils.file_dialog.shutil.which",
                side_effect=lambda name: "/bin/zenity" if name == "zenity" else None,
            ),
            patch("utils.file_dialog._run", return_value=["/tmp/a.txt"]) as run,
        ):
            self.assertEqual(choose_files(extensions=(".txt",)), ["/tmp/a.txt"])

        command = run.call_args.args[0]
        self.assertIn("--file-filter=Supported files | *.txt", command)
        self.assertIn("--file-filter=All files | *", command)

    def test_kdialog_single_file_mode_does_not_request_multiple_selection(self):
        with (
            patch("utils.file_dialog.sys.platform", "linux"),
            patch(
                "utils.file_dialog.shutil.which",
                side_effect=lambda name: "/bin/kdialog" if name == "kdialog" else None,
            ),
            patch("utils.file_dialog._run", return_value=["/tmp/a.pdf"]) as run,
        ):
            self.assertEqual(choose_files(extensions=(".pdf",), multiple=False), ["/tmp/a.pdf"])
        self.assertNotIn("--multiple", run.call_args.args[0])

    def test_macos_chooser_uses_single_or_multiple_selection_mode(self):
        for multiple, expression in (
            (True, "multiple selections allowed"),
            (False, "{choose file}"),
        ):
            with (
                patch("utils.file_dialog.sys.platform", "darwin"),
                patch("utils.file_dialog.shutil.which", return_value="/usr/bin/osascript"),
                patch("utils.file_dialog._run", return_value=["/tmp/a.txt"]) as run,
            ):
                self.assertEqual(choose_files(multiple=multiple), ["/tmp/a.txt"])
            self.assertIn(expression, run.call_args.args[0][-1])

    def test_windows_tk_chooser_shows_all_files_and_uses_single_selection_api(self):
        root = Mock()
        filedialog = types.SimpleNamespace(askopenfilename=Mock(return_value="C:/docs/a.pdf"))
        tkinter = types.ModuleType("tkinter")
        tkinter.Tk = Mock(return_value=root)
        tkinter.filedialog = filedialog
        with (
            patch("utils.file_dialog.sys.platform", "win32"),
            patch.dict(sys.modules, {"tkinter": tkinter, "tkinter.filedialog": filedialog}),
        ):
            self.assertEqual(choose_files(extensions=(".pdf",), multiple=False), ["C:/docs/a.pdf"])

        options = filedialog.askopenfilename.call_args.kwargs
        self.assertIn(("All files", "*"), options["filetypes"])
        filedialog.askopenfilename.assert_called_once()
        root.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()

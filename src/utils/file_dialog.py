"""Open the desktop's native multi-file chooser when one is available."""

import mimetypes
import os
import shutil
import subprocess
import sys


def _run(command):
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def accepted_extensions(accept):
    """Translate a provider MIME/extension accept value into chooser extensions."""
    if isinstance(accept, str):
        values = accept.replace(";", ",").replace(" ", ",").split(",")
    elif accept:
        values = accept
    else:
        return ()

    extensions = set()
    for raw in values:
        value = str(raw).strip().lower()
        if not value or value in {"*", "*/*"}:
            return ()
        if value.startswith("*."):
            extensions.add(value[1:])
        elif value.startswith("."):
            extensions.add(value)
        elif value.endswith("/*"):
            media_type = value[:-1]
            extensions.update(
                extension.lower()
                for extension, mime in mimetypes.types_map.items()
                if mime.startswith(media_type)
            )
        elif "/" in value:
            extensions.update(
                extension.lower()
                for extension in mimetypes.guess_all_extensions(value, strict=False)
            )
        else:
            extensions.add("." + value.lstrip("."))

    # Very broad MIME declarations are more useful as an unfiltered chooser.
    return tuple(sorted(extensions)) if 0 < len(extensions) <= 80 else ()


def _filter(extensions):
    """Build a Qt/KDialog name-filter string."""
    values = [str(x).strip().lower().lstrip(".") for x in extensions if x]
    if not values:
        return "All files (*)"
    patterns = " ".join("*." + value for value in values)
    return f"Supported files ({patterns});;All files (*)"


def _gtk_filters(extensions):
    """Return separate GTK filters in Zenity/Yad's `NAME | PATTERNS` format."""
    values = [str(x).strip().lower().lstrip(".") for x in extensions if x]
    if not values:
        return ["--file-filter=All files | *"]
    patterns = " ".join("*." + value for value in values)
    return [
        f"--file-filter=Supported files | {patterns}",
        "--file-filter=All files | *",
    ]


def choose_files(title="Dosya seç", extensions=(), *, multiple=True):
    """Return selected paths, [] on cancellation, or None if no chooser exists."""
    pattern = _filter(extensions)
    if sys.platform.startswith("linux"):
        if shutil.which("kdialog"):
            command = ["kdialog", "--getopenfilename", os.getcwd(), pattern]
            if multiple:
                command.extend(("--multiple", "--separate-output"))
            command.extend(("--title", title))
            result = _run(command)
            if result is not None:
                return result
        for binary in ("zenity", "yad"):
            if not shutil.which(binary):
                continue
            command = [binary, "--file-selection", "--title", title]
            if multiple:
                command.extend(("--multiple", "--separator=\n"))
            command.extend(_gtk_filters(extensions))
            result = _run(command)
            if result is not None:
                return result
    elif sys.platform == "darwin" and shutil.which("osascript"):
        script = (
            "set fs to choose file with multiple selections allowed"
            if multiple
            else "set fs to {choose file}"
        )
        script += '\nset out to ""\nrepeat with f in fs\nset out to out & POSIX path of f & linefeed\nend repeat\nreturn out'
        result = _run(["osascript", "-e", script])
        if result is not None:
            return result
    # Tk is a stdlib fallback on Windows and Linux installations without a
    # desktop-specific dialog binary. It is imported only when needed.
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            options = {
                "title": title,
                "filetypes": [
                    ("Supported files", " ".join("*" + x for x in extensions)),
                    ("All files", "*"),
                ]
                if extensions
                else [
                    ("All files", "*"),
                ],
            }
            if multiple:
                return list(filedialog.askopenfilenames(**options))
            selected = filedialog.askopenfilename(**options)
            return [selected] if selected else []
        finally:
            root.destroy()
    except Exception:
        return None

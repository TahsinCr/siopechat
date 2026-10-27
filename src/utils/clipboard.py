"""Platform clipboard helpers; terminal presentation stays in tui."""

import base64
import shutil
import subprocess


def read_png() -> bytes:
    commands = []
    if shutil.which("wl-paste"):
        commands.append(["wl-paste", "--no-newline", "--type", "image/png"])
    if shutil.which("xclip"):
        commands.append(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"])
    if shutil.which("pngpaste"):
        commands.append(["pngpaste", "-"])
    for command in commands:
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            return result.stdout
    raise RuntimeError("clipboard does not contain a PNG image")


def copy_text(text: str) -> bool:
    commands = (
        ("wl-copy", ["wl-copy"]),
        ("xclip", ["xclip", "-selection", "clipboard"]),
        ("xsel", ["xsel", "--clipboard", "--input"]),
        ("pbcopy", ["pbcopy"]),
        (
            "powershell.exe",
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Set-Clipboard -Value ([Console]::In.ReadToEnd())",
            ],
        ),
    )
    for executable, command in commands:
        if shutil.which(executable) is None:
            continue
        try:
            subprocess.run(
                command,
                input=text,
                text=True,
                check=True,
                timeout=3,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def terminal_sequence(text: str) -> str:
    encoded = base64.b64encode(text.encode()).decode("ascii")
    return f"\x1b]52;c;{encoded}\x07"


def read_text() -> str | None:
    """Read plain text without interpreting it as commands or terminal escapes."""
    commands = (
        ("wl-paste", ["wl-paste", "--no-newline", "--type", "text"]),
        ("xclip", ["xclip", "-selection", "clipboard", "-o"]),
        ("xsel", ["xsel", "--clipboard", "--output"]),
        ("pbpaste", ["pbpaste"]),
        ("powershell.exe", ["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard -Raw"]),
    )
    for executable, command in commands:
        if shutil.which(executable) is None:
            continue
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
            if result.returncode == 0:
                return result.stdout
        except (OSError, subprocess.SubprocessError, UnicodeError):
            continue
    return None

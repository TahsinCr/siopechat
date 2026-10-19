"""Explicit, domain-scoped import from the default browser's existing profile.

No browser launch, remote debugging, password-store mutation or profile writes.
Decryption is delegated to browser-cookie3 and the user's OS keyring.
"""

from __future__ import annotations

import configparser
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


class ProfileError(RuntimeError):
    """Safe diagnostic: never include decryption exceptions or cookie values."""


@dataclass(frozen=True)
class BrowserProfile:
    browser: str
    cookies: Path
    key_file: Path | None = None


# Host matching below is stricter than browser-cookie3's SQL substring filter.
HOSTS = {"chatgpt": "chatgpt.com", "gemini": "gemini.google.com", "claude": "claude.ai"}
NAMES = {
    "chatgpt": {"oai-did", "cf_clearance", "__cf_bm", "__Host-next-auth.csrf-token"},
    "gemini": {
        "__Secure-1PSID",
        "__Secure-1PSIDTS",
        "__Secure-3PSID",
        "__Secure-3PSIDTS",
        "SID",
        "HSID",
        "SSID",
        "APISID",
        "SAPISID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
    },
    "claude": {"sessionKey", "lastActiveOrg", "anthropic-device-id", "__cf_bm", "cf_clearance"},
}
AUTH_COOKIE = re.compile(r"__Secure-(?:next-auth|authjs)\.session-token(?:\.\d+)?\Z")


def _read_json(path):
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _default_id():
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        ) as key:
            return winreg.QueryValueEx(key, "ProgId")[0].lower()
    if sys.platform == "darwin":
        result = subprocess.run(
            ["defaults", "export", "com.apple.LaunchServices/com.apple.launchservices.secure", "-"],
            capture_output=True,
            timeout=5,
            check=True,
        )
        for item in plistlib.loads(result.stdout).get("LSHandlers", []):
            if item.get("LSHandlerURLScheme") == "https":
                return item.get("LSHandlerRoleAll", "").lower()
        return ""
    result = subprocess.run(
        ["xdg-settings", "get", "default-web-browser"],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    return result.stdout.strip().lower()


def default_profile():
    """Resolve one browser and one profile; never merge or probe other accounts."""
    try:
        name = _default_id().lower()
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ProfileError("Varsayılan tarayıcı belirlenemedi") from None
    if any(channel in name for channel in ("-beta", "-nightly", "-dev", ".beta", ".nightly")):
        raise ProfileError("Varsayılan tarayıcının profili desteklenmiyor")
    if "brave" in name and "origin" in name:
        name = "brave-origin"
    browser = next(
        (
            value
            for token, value in (
                ("brave-origin", "brave-origin"),
                ("brave.origin", "brave-origin"),
                ("brave", "brave"),
                ("firefox", "firefox"),
                ("msedge", "edge"),
                ("microsoft-edge", "edge"),
                ("com.microsoft.edge", "edge"),
                ("edg.", "edge"),
                ("chromium", "chromium"),
                ("chrome", "chrome"),
                ("vivaldi", "vivaldi"),
            )
            if token in name
        ),
        None,
    )
    if browser is None:
        raise ProfileError("Varsayılan tarayıcının profili desteklenmiyor")
    home = Path.home()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        paths = {
            "chrome": local / "Google/Chrome/User Data",
            "chromium": local / "Chromium/User Data",
            "brave": local / "BraveSoftware/Brave-Browser/User Data",
            "brave-origin": local / "BraveSoftware/Brave-Origin/User Data",
            "edge": local / "Microsoft/Edge/User Data",
            "vivaldi": local / "Vivaldi/User Data",
            "firefox": Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
            / "Mozilla/Firefox",
        }
    elif sys.platform == "darwin":
        base = home / "Library/Application Support"
        paths = {
            key: base / part
            for key, part in {
                "chrome": "Google/Chrome",
                "chromium": "Chromium",
                "brave": "BraveSoftware/Brave-Browser",
                "brave-origin": "BraveSoftware/Brave-Origin",
                "edge": "Microsoft Edge",
                "vivaldi": "Vivaldi",
                "firefox": "Firefox",
            }.items()
        }
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        paths = {
            key: base / part
            for key, part in {
                "chrome": "google-chrome",
                "chromium": "chromium",
                "brave": "BraveSoftware/Brave-Browser",
                "brave-origin": "BraveSoftware/Brave-Origin",
                "edge": "microsoft-edge",
                "vivaldi": "vivaldi",
            }.items()
        }
        paths["firefox"] = home / ".mozilla/firefox"
        if "flatpak" in name or name.startswith("org.mozilla."):
            flatpak = home / ".var/app/org.mozilla.firefox/.mozilla/firefox"
            if flatpak.is_dir():
                paths["firefox"] = flatpak
        if name.startswith("com.google.chrome"):
            paths["chrome"] = home / ".var/app/com.google.Chrome/config/google-chrome"
    root = paths[browser]
    if browser == "firefox":
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(root / "profiles.ini", encoding="utf-8")
            defaults = {
                parser[s]["Default"]
                for s in parser.sections()
                if s.startswith("Install") and parser[s].get("Default")
            }
            if not defaults:
                defaults = {
                    parser[s]["Path"]
                    for s in parser.sections()
                    if s.startswith("Profile") and parser[s].get("Default") == "1"
                }
            if len(defaults) != 1:
                raise ProfileError(
                    "Varsayılan profil belirlenemedi; tarayıcıda varsayılan profil seçin"
                )
            profile = root / defaults.pop()
        except (OSError, configparser.Error, KeyError):
            raise ProfileError("Varsayılan tarayıcı profili bulunamadı") from None
        cookies, key = profile / "cookies.sqlite", None
    else:
        state = _read_json(root / "Local State")
        if (root / "Local State").is_file() and not state:
            raise ProfileError("Varsayılan tarayıcı profili bulunamadı")
        profile = state.get("profile", {})
        selected = profile.get("last_used", "Default") if isinstance(profile, dict) else "Default"
        if (
            not isinstance(selected, str)
            or selected in (".", "..")
            or "/" in selected
            or "\\" in selected
        ):
            raise ProfileError("Varsayılan tarayıcı profili bulunamadı")
        directory = root / selected
        cookies = directory / "Network/Cookies"
        if not cookies.is_file():
            cookies = directory / "Cookies"
        key = root / "Local State"
    if not cookies.is_file():
        raise ProfileError("Varsayılan tarayıcı profili bulunamadı")
    return BrowserProfile(browser, cookies, key)


def select_cookies(jar, provider):
    """Retain only the provider's authentication cookies for its exact host."""
    host, values = HOSTS[provider], {}
    for cookie in sorted(jar, key=lambda c: len(c.domain or "")):
        domain = (cookie.domain or "").lstrip(".").lower()
        if not domain or not (host == domain or host.endswith("." + domain)):
            continue
        if cookie.is_expired() or cookie.path != "/":
            continue
        if cookie.name not in NAMES[provider] and not (
            provider == "chatgpt" and AUTH_COOKIE.fullmatch(cookie.name)
        ):
            continue
        if not isinstance(cookie.value, str) or any(
            c in cookie.name + cookie.value for c in "\r\n\0"
        ):
            continue
        values[cookie.name] = cookie.value
    required = (
        any(AUTH_COOKIE.fullmatch(name) for name in values)
        if provider == "chatgpt"
        else bool(values.get("__Secure-1PSID" if provider == "gemini" else "sessionKey"))
    )
    if not required:
        raise ProfileError("Bu profilde sağlayıcıya ait açık oturum bulunamadı")
    return values


def import_cookies(provider):
    """Bound keyring/SQLite work in a child; stdout is a private pipe, never a terminal."""
    if provider not in HOSTS:
        raise ValueError("unsupported provider")
    profile = default_profile()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "providers.profiles"],
            input=json.dumps(
                {
                    "provider": provider,
                    "browser": profile.browser,
                    "cookies": str(profile.cookies),
                    "key_file": str(profile.key_file) if profile.key_file else None,
                }
            ),
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
            env=dict(
                os.environ,
                PYTHONPATH=str(Path(__file__).resolve().parent.parent)
                + os.pathsep
                + os.environ.get("PYTHONPATH", ""),
            ),
        )
    except subprocess.TimeoutExpired:
        raise ProfileError(
            "Profil okuma zaman aşımına uğradı; sistem anahtarlığının kilidini açın"
        ) from None
    if result.returncode == 2:
        raise ProfileError("Bu profilde sağlayıcıya ait açık oturum bulunamadı")
    if result.returncode:
        raise ProfileError(
            "Profil okunamadı; tarayıcıyı kapatıp sistem anahtarlığının kilidini açın. Tarayıcının şifrelemesi desteklenmiyor olabilir"
        )
    try:
        if len(result.stdout) > 1_048_576:
            raise ValueError("invalid result")
        values = json.loads(result.stdout)
        if (
            not isinstance(values, dict)
            or not values
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items())
        ):
            raise ValueError("invalid result")
    except ValueError:
        raise ProfileError("Profil okuma sonucu geçersiz") from None
    return profile.browser, values


def _main():
    try:
        import browser_cookie3

        data = json.load(sys.stdin)
        browser = "brave" if data["browser"] == "brave-origin" else data["browser"]
        if browser == "firefox":
            # Firefox containers can contain other signed-in accounts. Read only
            # the default container; the upstream loader merges container rows.
            from http.cookiejar import CookieJar

            jar = CookieJar()
            path = Path(data["cookies"]).resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(path, uri=True, timeout=3)) as database:
                columns = {row[1] for row in database.execute("PRAGMA table_info(moz_cookies)")}
                scope = " AND originAttributes = ''" if "originAttributes" in columns else ""
                host = HOSTS[data["provider"]]
                domains = [host, "." + host]
                if data["provider"] == "gemini":
                    domains.extend(["google.com", ".google.com"])
                query = (
                    "SELECT host,path,isSecure,expiry,name,value,isHttpOnly FROM moz_cookies "
                    "WHERE host IN (" + ",".join("?" for _ in domains) + ")" + scope
                )
                for row in database.execute(query, domains):
                    jar.set_cookie(browser_cookie3.create_cookie(*row))
        else:
            jar = getattr(browser_cookie3, browser)(
                cookie_file=data["cookies"],
                key_file=data["key_file"],
                domain_name="google.com"
                if data["provider"] == "gemini"
                else HOSTS[data["provider"]],
            )
        values = select_cookies(jar, data["provider"])
        sys.stdout.write(json.dumps(values))
    except ProfileError:
        return 2
    except Exception:
        # Upstream exceptions can contain decrypted bytes. Never forward them.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())

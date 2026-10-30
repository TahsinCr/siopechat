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
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


class ProfileError(RuntimeError):
    """Safe diagnostic: never include decryption exceptions or cookie values."""


PROFILE_FAILURES = {
    1: "Profil okunamadı (beklenmeyen hata); Python bağımlılıklarını ve profil erişimini kontrol edin",
    3: "Profil okuyucu bağımlılıkları eksik; uygulamanın Python ortamında bağımlılıkları güncelleyin",
    4: "Profil dosyasına erişim reddedildi; uygulamayı profilin sahibi olan kullanıcıyla çalıştırın",
    5: "Çerez veritabanı okunamadı; tarayıcıyı kapatıp yeniden deneyin",
    6: "Sistem anahtarlığına erişilemedi; masaüstü oturumunu ve anahtarlık kilidini kontrol edin",
    7: "Tarayıcı çerezleri çözülemedi; anahtarlık erişimi veya tarayıcı şifrelemesi uyumsuz olabilir",
}


def _failure_code(error):
    """Classify locally; raw exceptions may contain secrets and never leave the worker."""
    if isinstance(error, ImportError):
        return 3
    if isinstance(error, PermissionError):
        return 4
    if isinstance(error, sqlite3.Error):
        return 5
    message = str(error).lower()
    if any(
        token in message
        for token in (
            "unable to read database",
            "not a chromium",
            "not a firefox",
            "not a safari",
        )
    ):
        return 5
    if type(error).__module__.startswith(("jeepney", "dbus", "secretstorage")) or any(
        token in message
        for token in (
            "can not find secret",
            "kde wallet",
            "dbus_session_bus_address",
            "org.freedesktop.secret",
        )
    ):
        return 6
    if any(token in message for token in ("decrypt", "dpapi", "app-bound", "encrypted key")):
        return 7
    return 1


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


def _safari_cookie_file(home):
    """Resolve Safari's last active profile without falling back across accounts."""
    library = home / "Library/Containers/com.apple.Safari/Data/Library"
    try:
        result = subprocess.run(
            ["defaults", "read", "com.apple.Safari", "SafariProfilesLastActiveProfileUUIDString"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        result = None

    active_profile = result.stdout.strip() if result and result.returncode == 0 else ""
    if active_profile and active_profile.casefold() != "defaultprofile":
        try:
            profile_id = str(uuid.UUID(active_profile)).lower()
        except ValueError:
            raise ProfileError("Son kullanılan Safari profili belirlenemedi") from None
        cookies = library / "WebKit/WebsiteDataStore" / profile_id / "Cookies/Cookies.binarycookies"
        if cookies.is_file():
            return cookies
        raise ProfileError("Son kullanılan Safari profilinin çerez dosyası bulunamadı")

    if not active_profile:
        profile_store = library / "WebKit/WebsiteDataStore"
        try:
            has_named_profiles = any(
                path.is_dir()
                for path in profile_store.glob("*")
                if _is_safari_profile_id(path.name)
            )
        except OSError:
            raise ProfileError(
                "Safari profili belirlenemedi; Safari'de kullanacağınız profili açın"
            ) from None
        if has_named_profiles:
            raise ProfileError(
                "Safari profili belirlenemedi; Safari'de kullanacağınız profili açın"
            )

    cookie_files = (
        library / "Cookies/Cookies.binarycookies",
        home / "Library/Cookies/Cookies.binarycookies",
    )
    cookies = next((path for path in cookie_files if path.is_file()), None)
    if cookies is None:
        raise ProfileError("Varsayılan tarayıcı profili bulunamadı")
    return cookies


def _is_safari_profile_id(value):
    try:
        uuid.UUID(value)
    except (AttributeError, ValueError):
        return False
    return True


def default_profile():
    """Resolve one browser and one profile; never merge or probe other accounts."""
    try:
        name = _default_id().lower()
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ProfileError("Varsayılan tarayıcı belirlenemedi") from None
    if any(
        channel in name
        for channel in (
            "-beta",
            "-nightly",
            "-dev",
            ".beta",
            ".nightly",
            "technologypreview",
        )
    ):
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
                ("zen", "zen"),
                ("firefox", "firefox"),
                ("msedge", "edge"),
                ("microsoft-edge", "edge"),
                ("com.microsoft.edge", "edge"),
                ("edg.", "edge"),
                ("chromium", "chromium"),
                ("chrome", "chrome"),
                ("vivaldi", "vivaldi"),
                ("safari", "safari"),
            )
            if token in name
        ),
        None,
    )
    if browser is None:
        raise ProfileError("Varsayılan tarayıcının profili desteklenmiyor")
    home = Path.home()
    if browser == "safari":
        if sys.platform != "darwin":
            raise ProfileError("Safari profilleri yalnızca macOS üzerinde destekleniyor")
        return BrowserProfile(browser, _safari_cookie_file(home))
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
            "zen": Path(os.environ.get("APPDATA", home / "AppData/Roaming")) / "zen",
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
                "zen": "zen",
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
        firefox_roots = [paths["firefox"], home / "snap/firefox/common/.mozilla/firefox"]
        flatpak = home / ".var/app/org.mozilla.firefox/.mozilla/firefox"
        if "flatpak" in name or name.startswith("org.mozilla."):
            firefox_roots.insert(0, flatpak)
        if "firefox_firefox" in name:
            firefox_roots.reverse()
        paths["firefox"] = next(
            (root for root in firefox_roots if (root / "profiles.ini").is_file()), paths["firefox"]
        )
        zen_roots = [base / "zen", home / ".zen"]
        if "app.zen_browser" in name:
            sandbox = home / ".var/app/app.zen_browser.zen"
            zen_roots = [sandbox / "config/zen", sandbox / ".zen", sandbox / "zen"]
        paths["zen"] = next(
            (root for root in zen_roots if (root / "profiles.ini").is_file()), zen_roots[0]
        )
        if name.startswith("com.google.chrome"):
            paths["chrome"] = home / ".var/app/com.google.Chrome/config/google-chrome"
    root = paths[browser]
    if browser in ("firefox", "zen"):
        cookies, key = _firefox_profile(root) / "cookies.sqlite", None
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


def _firefox_profile(root):
    parser = configparser.ConfigParser(interpolation=None)
    installs = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(root / "profiles.ini", encoding="utf-8")
        installs.read(root / "installs.ini", encoding="utf-8")
        defaults = {
            parser[s]["Default"]
            for s in parser.sections()
            if s.startswith("Install") and parser[s].get("Default")
        }
        if not defaults:
            defaults = {
                installs[s]["Default"] for s in installs.sections() if installs[s].get("Default")
            }
        if not defaults:
            defaults = {
                parser[s]["Path"]
                for s in parser.sections()
                if s.startswith("Profile") and parser[s].get("Default") == "1"
            }
        if not defaults:
            defaults = {
                parser[s]["Path"]
                for s in parser.sections()
                if s.startswith("Profile") and parser[s].get("Path")
            }
        if len(defaults) != 1:
            raise ProfileError(
                "Varsayılan profil belirlenemedi; tarayıcıda varsayılan profil seçin"
            )
        return root / defaults.pop()
    except (OSError, configparser.Error, KeyError):
        raise ProfileError("Varsayılan tarayıcı profili bulunamadı") from None


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
        raise ProfileError(PROFILE_FAILURES.get(result.returncode, PROFILE_FAILURES[1]))
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


def _firefox_rows(cookie_file, provider):
    """Read a consistent private SQLite/WAL copy if the live database is locked."""
    source = Path(cookie_file)

    def read(path, private=False):
        uri = path.resolve().as_uri() + ("?mode=rw" if private else "?mode=ro")
        with closing(sqlite3.connect(uri, uri=True, timeout=0.25)) as database:
            columns = {row[1] for row in database.execute("PRAGMA table_info(moz_cookies)")}
            scope = (
                " AND COALESCE(originAttributes, '') = ''" if "originAttributes" in columns else ""
            )
            host = HOSTS[provider]
            domains = [host, "." + host]
            if provider == "gemini":
                domains.extend(["google.com", ".google.com"])
            query = (
                "SELECT host,path,isSecure,expiry,name,value,isHttpOnly FROM moz_cookies WHERE host IN ("
                + ",".join("?" for _ in domains)
                + ")"
                + scope
            )
            return list(database.execute(query, domains))

    try:
        return read(source)
    except sqlite3.OperationalError:
        # Browser locks and read-only WAL shared-memory access vary by platform.
        # Never use immutable=1 on the original: it would ignore committed WAL data.
        pass
    sidecars = [source.with_name(source.name + suffix) for suffix in ("-wal", "-journal")]
    for _ in range(3):
        with tempfile.TemporaryDirectory(prefix="siope-profile-") as folder:
            try:
                files = [source] + [path for path in sidecars if path.is_file()]
                before = [(p.stat().st_size, p.stat().st_mtime_ns) for p in files]
                target = Path(folder) / source.name
                for path in files:
                    shutil.copyfile(path, Path(folder) / path.name)
                after = [(p.stat().st_size, p.stat().st_mtime_ns) for p in files]
                if before != after or any(
                    path.is_file() and path not in files for path in sidecars
                ):
                    continue
                # Recovery, if required, writes only inside the private temporary directory.
                return read(target, private=True)
            except FileNotFoundError:
                # A browser checkpoint can remove a WAL/journal while copying it.
                continue
    raise sqlite3.OperationalError("Cookie snapshot changed during copy")


def _firefox_session_cookies(directory, provider):
    """Read the newest session snapshot, never combine historical recovery files."""
    candidates = [
        directory / "sessionstore-backups/recovery.jsonlz4",
        directory / "sessionstore.jsonlz4",
        directory / "sessionstore.js",
    ]
    limit = 64 * 1024 * 1024
    try:
        existing = [path for path in candidates if path.is_file()]
        if not existing:
            return []
        path = max(existing, key=lambda item: item.stat().st_mtime_ns)
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            return []
        if path.suffix == ".jsonlz4":
            import lz4.block

            if not raw.startswith(b"mozLz40\0") or len(raw) < 12:
                return []
            if int.from_bytes(raw[8:12], "little") > limit:
                return []
            raw = lz4.block.decompress(raw[8:])
        data = json.loads(raw)
        if not isinstance(data, dict):
            return []
        cookies = list(data.get("cookies") or [])
        for window in data.get("windows") or []:
            if isinstance(window, dict):
                cookies.extend(window.get("cookies") or [])
    except (OSError, ValueError, RuntimeError):
        # A partially written recovery file must not invalidate persistent cookies.
        return []
    domains = {HOSTS[provider], "." + HOSTS[provider]}
    if provider == "gemini":
        domains.update({"google.com", ".google.com"})
    return [
        cookie
        for cookie in cookies
        if isinstance(cookie, dict)
        and cookie.get("host") in domains
        and not cookie.get("originAttributes")
        and not cookie.get("userContextId")
        and not cookie.get("privateBrowsingId")
    ]


def _main():
    try:
        import browser_cookie3

        data = json.load(sys.stdin)
        browser = "brave" if data["browser"] == "brave-origin" else data["browser"]
        if browser in ("firefox", "zen"):
            # Firefox containers can contain other signed-in accounts. Read only
            # the default container; the upstream loader merges container rows.
            from http.cookiejar import CookieJar

            jar = CookieJar()
            for row in _firefox_rows(data["cookies"], data["provider"]):
                jar.set_cookie(browser_cookie3.create_cookie(*row))
            for cookie in _firefox_session_cookies(Path(data["cookies"]).parent, data["provider"]):
                jar.set_cookie(
                    browser_cookie3.create_cookie(
                        cookie.get("host", ""),
                        cookie.get("path", "/"),
                        cookie.get("secure", False),
                        cookie.get("expiry"),
                        cookie.get("name", ""),
                        cookie.get("value", ""),
                        cookie.get("httponly", False),
                    )
                )
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
    except Exception as error:
        # Return only an allowlisted category, never the exception text.
        return _failure_code(error)
    return 0


if __name__ == "__main__":
    sys.exit(_main())

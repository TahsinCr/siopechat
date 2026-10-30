[![Version: v1.5.1-beta][version-shield]](CHANGELOG.md)
[![Python 3.11+][python-shield]](https://www.python.org/downloads/)
[![GPL-3.0-only][license-shield]](LICENSE)

**English** | [Türkçe](README-TR.md) | [Русский](README-RU.md)

# SiopeChat

### ChatGPT, Gemini and Claude web chats — together in one terminal.

Keep conversations moving, organize provider projects and compare replies side by side, all from a focused terminal workspace.

> **For web chat, not API products.** SiopeChat is an independent, unofficial client that works through your signed-in ChatGPT, Gemini and Claude web sessions. It does not connect through the providers’ official developer APIs or use coding-agent products such as Codex or Claude Code. No API key is required; your plan’s model access and usage limits still apply. Changes to provider websites can affect compatibility.

![SiopeChat terminal workspace](assets/siopechat.png)

*A real terminal capture of the default SiopeChat interface, shown with demonstration data.*

## What you can do

| Conversations | Projects and context | Local-first history |
| --- | --- | --- |
| Keep chats open in tabs, preserve drafts and scroll positions, or compare up to four panes in a group. | Use ChatGPT and Claude projects or Gemini notebooks, with provider-supported instructions and sources. | Open saved conversations immediately while history refreshes in the background. |
| Select models and effort per account; edit, retry or branch replies when the provider supports it. | Search and sort projects and chats, with filters for provider accounts. | Keep conversation history and resource metadata in a local SQLite archive. |

| Files and downloads | Keyboard and extensions | Your language |
| --- | --- | --- |
| Download generated files from replies and track progress in the built-in download manager. | Use the command palette, configurable shortcuts, mouse controls and local extensions. | Choose the English, Turkish or Russian interface. |

> **Latest: v1.5.1-beta** — Provider-published sources, thinking summaries and search activity can accompany replies. Generated files can be downloaded from the conversation, and chats can be renamed. Gemini effort choices follow the signed-in account’s web policy. The Python distribution version is `1.5.1b0`.

## Install and sign in

### Requirements

You need Python **3.11+** for SiopeChat and Node.js **22+** to run ChatGPT's published Sentinel SDK for initial context generation and challenge types such as Turnstile. The server-issued proof-of-work calculation runs in Python. Browser login and session capture run through the Python provider package. The included installers and launchers check both required versions before continuing. The standalone provider package keeps a separate Python 3.10+ contract. Chrome/Chromium is needed only for the explicit sign-in step. Use a Unicode terminal. True color and mouse reporting are recommended.

### Install and launch

Run the one-time installer from the checkout. It checks Python 3.11+ and Node.js 22+, reuses a supported virtual environment if it finds one, and installs SiopeChat plus its Python dependencies there. If no environment exists, it creates `python/` without replacing an existing directory.

| Platform | Installer |
| --- | --- |
| Linux / macOS terminal | `./install.sh` |
| macOS Finder | `install.command` |
| Windows PowerShell | `./install.ps1` |
| Windows Command Prompt | `install.bat` |

| Platform | Launcher |
| --- | --- |
| Linux / macOS terminal | `./siopechat.sh` |
| macOS Finder | `siopechat.command` |
| Windows Command Prompt | `siopechat.bat` |
| Windows PowerShell | `./siopechat.ps1` |
| Installed console command | `siopechat` |

<details>
<summary>Virtual environments and dependency details</summary>

Installers and launchers look for environments in this order: `python/`, `.venv/`, `venv/`, `env/`. Set `SIOPECHAT_VENV` to a different environment directory to override that lookup; relative paths are resolved from the checkout. Installers only create an environment when none is available, and they leave an existing but invalid directory untouched. Run an installer again after updating the checkout to install changed dependencies. Launchers check the required runtimes and start the selected environment directly; they do not install packages or access the network on each launch.

The installer uses the dependency ranges in `pyproject.toml` with pip, so uv is not required for end-user installation. `uv.lock` remains the exact dependency resolution used by CI and development workflows.

</details>

### Connect an account

New installations start with no accounts. Add or select an account under **Settings → Accounts**, then choose **Verify from profile**. First sign in to the provider in your default browser’s last-used profile. SiopeChat imports and verifies that session without launching a browser. Chat, history, model and project operations then use HTTP. If the session expires, sign in again in your usual browser and repeat verification.

Add, edit and remove account buttons sit beneath the account list. Edit changes only the selected account’s display name; its login and history stay intact. Profile verification and logout share a separate row below the session details. Labels shorten in narrow terminals.

<details>
<summary>Supported browser profiles and troubleshooting</summary>

**Verify from profile** imports the selected provider’s signed-in session from the default browser’s last-used profile, without starting a browser or enabling debugging flags. Chromium-family browsers share the same profile resolution and provider-cookie filtering flow; standard locations are included for Chrome, Chromium, Brave/Brave Origin, Edge and Vivaldi. Their native keyring labels remain browser-specific because each vendor encrypts cookies with its own OS credential identity. Firefox and Zen share one Gecko profile reader based on `profiles.ini` and `installs.ini`. Safari uses its separate WebKit cookie reader on macOS. Only the selected provider’s authentication cookies are retained. The candidate session is checked with the provider before replacing a saved session. Your OS keyring may request access. Browser vendors use different profile directories and keyring labels, so those identifiers remain in a small platform map. Locked databases, custom/sandboxed profile locations and unsupported encryption (including some Windows Chromium configurations) can prevent import; the error is shown without deleting the saved session. Safari profile access can also depend on macOS file permissions. Import errors distinguish missing reader dependencies, file permissions, database access, keyring access and decryption failures. Update dependencies using the same Python environment that launches SiopeChat. Session cookies in the current Firefox/Zen recovery snapshot are also supported; separate containers are excluded and old recovery snapshots are not combined. If the live cookie database is locked, the reader retries from a private copy including its WAL/journal; it does not modify your profile.

Firefox and Zen use the same read-only Gecko profile and cookie reader. Profile selection follows `profiles.ini` and `installs.ini`; ambiguous profiles are reported rather than merged. Linux discovery includes Firefox Snap/Flatpak locations and Zen’s current XDG, legacy and Flatpak locations. Portable or custom profile roots are not guessed; set the browser as the system default and use its standard profile location.

</details>

### Multiple accounts

You can add several accounts. One account per provider is selected for sending; browsing another account’s history does not silently change that selection. To verify a different account, first select its signed-in profile in your default browser, then use **Verify from profile** on the corresponding SiopeChat account.

Click an account row to select it; the arrow marks the account currently being managed. **Remove account** (Alt+Backspace while viewing accounts) asks for confirmation and removes the local account record, saved login and model preferences. It keeps the provider account and archived conversations. Tabs belonging to the removed account close, obsolete filters are cleared, and another enabled account is selected. Add a replacement before removing the last enabled account.

## Everyday use

### Start and navigate conversations

Write in the composer and press **Enter** to send. **Shift+Enter** inserts a newline on terminals with extended keyboard reporting, which SiopeChat requests while active. If your terminal sends the same code for Enter and Shift+Enter, use **Alt+Enter** or **Ctrl+J**. The composer grows to its configured height, then scrolls. Ctrl+A selects text, Ctrl+C copies a selection, and Ctrl+V pastes. Click a message to select and copy its contents.

A new chat offers models from the selected accounts of enabled providers. Its status counts the models in those catalogs and shows how many provider catalogs have loaded; remaining catalogs load in the background. An existing chat stays with its original account. Model and effort choices come from provider catalogs; unavailable or upgrade-only entries are filtered when that information is available. A listed model can still be refused by the server. Provider-published thinking summaries appear separately from answers; private reasoning is not available.

Click a Markdown web link in a message to open it in your default browser. Clicking elsewhere in the message still selects it for copying.

### Message actions and generated files

Each message has a compact toolbar. Copy works locally; user messages can be edited and sent again. ChatGPT and Claude replies can be retried, while ChatGPT and Gemini replies can start a separate branch. Provider-published sources appear with the associated thinking summaries. The reply's available model and effort appear on the right, before its saved-version controls. Tools depend on the message and provider; model access follows the selected account. Editing or retrying updates the active history after provider confirmation, and branching opens a new chat. Conversation deletion and splitting are beside the upper synchronization button.

Generated files appear inside the assistant reply as clickable file cards with their available name, type and size. Clicking a card starts a background transfer in the download manager; file references and metadata remain in the archived conversation. Downloads use the owning provider and account to resolve protected resources. Provider retention and file availability still apply.

### Download manager

File links download in the background to `downloads/` under the project directory. Change this in **Settings → General → Download folder**; relative paths are resolved against the project and absolute paths are supported. At most two transfers run at once, progress appears in the status bar, existing files are preserved and failed transfers are cleaned up. Ordinary web pages still open in your default browser. ChatGPT sandbox files are resolved through the owning conversation; Gemini image placeholders use actual provider media records, and Claude sandbox/computer links use the owning conversation’s file store. Protected downloads use the selected account’s saved session; account credentials are not forwarded to other origins. Missing or expired resources show an error instead of a fabricated URL.

Open **Downloads** between Help and Settings, use `/downloads`, or press **Alt+J**. The workspace shows received bytes, total size and remaining bytes when known, completed and failed transfers, and the source chat. Date groups collapse; search matches filenames, chat titles and accounts. Filter by status and sort by date, filename or size. **Sync** checks file presence in the background; the same check runs every ten seconds. A removed file stays marked **Deleted**, and known deleted chats are labelled accordingly. **Delete file** keeps its history entry; **Remove entry** leaves the file on disk. The archive extension persists new download records in SQLite across restarts; disabling it keeps only the current session history. Transfers interrupted by shutdown are marked **Interrupted**. Existing files downloaded before history tracking was added have no automatic source-chat record.

Downloads default to a 512 MiB limit. General settings expose the size limit in bytes and an opt-in for local-network downloads. Public downloads reject private and loopback addresses, validate every redirect and keep HTTPS connections on HTTPS. The checked address is used for the actual connection, while the original hostname is retained for TLS and HTTP.

### Keyboard, selection and background replies

Drag across a response or thinking summary to select part of its rendered text, then press Ctrl+C. Click a message and press Ctrl+A to select the whole message.

Closing a conversation tab keeps its running response in the background. In the sidebar, a green **●** marks a running conversation and **✓** marks a completed background reply. Select it to reopen the same conversation. You can open other conversations and synchronize history while a response is running. This continues only while SiopeChat is running.

| Default shortcut | Action |
| --- | --- |
| F1 | Open or close help |
| Ctrl+P | Command palette |
| Ctrl+T / Ctrl+W | New tab / close tab |
| Alt+Left / Alt+Right | Previous / next tab |
| Alt+[ / Alt+] | Previous / next pane in the split group |
| Alt+W | Close all idle tabs |
| F7 | Toggle sidebar |
| F8 / F10 | Synchronize conversation / library |
| F5 / F12 | Settings / accounts |

Shortcut changes take effect immediately throughout the interface. Every binding is editable under **Settings → Keyboard** and in the local `config.json`. The command palette exposes the same actions, including extension actions.

Settings is a regular workspace tab: switch back to a conversation with Alt+Left/Right, reopen the same Settings tab with F5, or close it with Ctrl+W.

### Projects and split views

Use `/project` to choose a project for a new chat or move the current chat where supported. Choose **Unassigned** to remove membership. `/project-new` asks for the provider/account and then its supported creation fields. Project details provide name/instruction editing and source controls; Ctrl+S saves the inline editor.

You can also write directly from project details. Choose a model and effort for that project's account and send: a separate conversation tab opens inside the project. The composer and selectors are hidden while editing the project itself.

Choose **Split**, `/split` or `/split-down`, then select another chat from the sidebar. Click anywhere in an inactive pane to activate it. Scrolling a passive pane does not change focus. `/unsplit` returns to one pane. Use `layout.create` in the command palette to save the current chats, split direction and active pane. Open a saved workspace from the sidebar to restore it. Only conversation references are saved; temporary chats, drafts and running requests are excluded.

Use **Delete** beside the composer to delete a conversation. `/project-delete` opens a project picker; project details also have a delete button. Both ask for permanent-deletion confirmation, with **No** focused initially. Deletion is submitted to the provider and checked there before removing local records. Project deletion follows the provider's rules for its contained chats. Closing tabs does not delete conversations.

### Provider differences

| Provider | Project support | Current limits |
| --- | --- | --- |
| ChatGPT | Projects, instructions, sources and supported chat moves | Models, effort, temporary mode and project options depend on the account. |
| Gemini | Gemini notebooks shown as a distinct project type; notebook-context chats, instructions and text sources | Existing chats cannot be moved into notebooks. File/URL source import is not exposed. Thinking choices and defaults follow the account's native web model policy. |
| Claude | Projects, instructions, text/file sources and supported membership changes | Incognito chats are supported outside projects; privacy/memory overrides beyond private/default are not exposed. |

Gemini notebooks use the Gemini account session; a separate NotebookLM login is not required. Container types and native metadata remain distinct internally. SiopeChat does not pretend that every provider has the same capabilities.

Attachments are limited locally to **20 MiB** per file; providers may have stricter limits. Temporary mode is offered only when supported and does not save conversation snapshots locally. Provider retention rules still apply.

## Configuration and local data

### Settings and privacy

`config.json` is created locally and excluded from Git. Defaults are defined in `src/settings.py`. Choose a language under **Settings → General → Language** (`en`, `tr`, `ru`), enable providers under **Providers**, and manage feature packages under **Extensions**. At least one provider must remain enabled.

Set `SIOPECHAT_HOME` to use a different configuration/data directory. Keep configuration, account sessions and `data/` private; do not attach them to bug reports.

### Archive and file retention

The archive extension stores conversations, ordered messages, published thinking, projects/notebooks, memberships and resource metadata in `data/archive.sqlite3`. Uploaded files are never copied into the archive: only provider identifiers and available file metadata are retained, without the local file path or bytes. Upgrading from an older archive removes its content-addressed upload copies while preserving conversation and resource metadata. Downloaded files remain in the configured downloads folder; the archive keeps only their download-history metadata. Remote images and sources are retained as references, and not every remote file is downloaded automatically.

**Upgrade note:** The schema v5 migration permanently removes legacy upload copies. Before opening an older archive with this version, back up `data/archive.sqlite3` and `data/files/` together if you need those offline copies or may return to an older SiopeChat version. Adjust these paths if you use a custom `SIOPECHAT_HOME` directory.

### Loading, memory and synchronization

With the archive enabled, chats initially read at most 80 visible messages within a 262,144-character serialized-record budget per SQLite batch. Scrolling loads older batches without removing messages already loaded; stable message IDs keep the visible position anchored. Only lean display text and metadata remain in the tab, shared with its layout. The renderer prioritizes the viewport, then prepares two screens above and below it. Rendered rows use a one-megabyte LRU per view; large ANSI cache entries are compressed within a separate 256 KiB budget. Inactive main-tab layouts share a 4 MiB eviction budget. Accepted turns update only a bounded SQL tail. Closing a tab releases its source and render references; an active background response retains its current turn until it is saved. A single message is never truncated to fit a batch. Source memory grows with visited history, while render caches remain bounded. Python may retain freed process memory, so RSS does not necessarily fall immediately.

Previously loaded conversations open from the local archive and refresh in the background. The composer stays available while local history and provider data load; if you send before older messages arrive, the app merges them into the conversation afterward. Cached sidebar and project pages stay usable as later pages are fetched in the background. Synchronization indicators appear in the footer and conversation list. Refreshes reconcile newer local turns instead of replacing an active reply. Failed refreshes retain the last good data. Remote mutations are not automatically replayed after an ambiguous failure: synchronize before resending.

The archive has bounded memory and worker queues. Large transcript preparation runs outside the UI event loop. The sidebar draws visible rows on demand and reuses its filtered, sorted list until library data changes. Disabling the archive preserves its files and leaves direct provider loading and sending available. Disabling layout removes split controls without disabling providers.

## Extensions

Add `extensions/my_feature/plugin.py`. Export an `ExtensionSpec` whose activation function registers contributions through the shared API:

```python
from extension import Action, ExtensionSpec, SettingField


def activate(context):
    context.settings(SettingField("enabled", bool, True, "my_feature.enabled"))
    context.translations("en", {"enabled": "Enable my feature"})
    context.translations("tr", {"enabled": "Özelliğimi etkinleştir"})
    context.translations("ru", {"enabled": "Включить расширение"})
    context.action(Action("my_feature.hello", "Hello", lambda: "Hello"))


SPEC = ExtensionSpec("my_feature", activate, api_version=3)
```

Each extension must include a `manifest.json` beside `plugin.py`. It declares metadata without executing Python:

```json
{"id": "my_feature", "api_version": 3, "requires": []}
```

Keep `requires` consistent with `ExtensionSpec`. Disabled extensions are not imported; enabling one loads its Python module. The manifest and `ExtensionSpec` must declare the same API version.

Register extension-owned services with `context.provide()` and release other resources with `context.on_dispose()`.

Restart to discover the package, then enable or disable it in **Settings → Extensions**. Settings live under `extensions.my_feature`; contributed language bundles are namespaced and merged with the application catalog. Declare dependencies with `requires=("archive",)` when needed. Actions can declare input fields for the command palette; extensions can also contribute services and views.

For archive access, use `context.api.get("archive")`. Its `conversations`, `messages` and `resources` methods provide paginated records scoped by provider/account. The replaceable repository contract and SQLite adapter live in `extensions/archive/repository.py`; provider normalization lives in `adapters.py`.

Extensions execute local code. Load only packages you trust. Keep network work out of render functions and use the shared lifecycle for cleanup.

Archive lists have a stable remote-ID order; UI sorting is applied separately. `siopechat action archive.inspect` reports missing references and unreferenced content files. `siopechat action archive.collect` previews cleanup; pass `'{"dry_run": false}'` as the action arguments only when you want to remove the candidates. Collection preserves shared references, unknown files and files saved within the last hour. A database from a newer schema version is rejected before its schema is changed.

## Development

Account sessions, model catalogs, verification records and the archive-disabled conversation-cache fallback are stored in `data/accounts.sqlite3`, separated by provider and account. The cache fallback includes conversation snapshots, active-chat pointers and deletion markers; with the archive enabled, conversation history remains in `data/archive.sqlite3`. The account schema upgrade is additive and leaves the existing session/catalog table intact. Provider-specific fields remain JSON inside the database. On Unix the database is restricted to its owner; it is not encrypted. Account labels, selection and preferences remain application configuration. The independent provider library accepts a credential file or an injected credential store and never imports the application database. Gemini uses a native HTTPS transport with no Gemini SDK or browser process. Legacy account files are removed only after their database/archive replacement is confirmed.

Bundled providers use their Python HTTPS transports for conversations and projects. Browser discovery, session capture and local Chrome DevTools Protocol (CDP) access also run in Python. For ChatGPT Sentinel, Python solves the server-issued proof-of-work challenge; Node.js runs the published SDK to create its initial context and handle dynamic challenge types such as Turnstile.

The provider library is independent of the terminal, archive and application runtime. `accounts.py` routes provider/account work; `runtime.py`, `actions.py` and `extension.py` manage shared services and contributions. The command-line entry point is `src/cli.py`; terminal UI implementation lives in `src/tui/`. Bundled extensions use the generic extension contract in `extension.py` and, when they contribute terminal UI, the explicit `tui.api` surface rather than TUI internals. Optional features live in `extensions/`, common helpers in `src/utils/`. A GUI can consume the same application services; no GUI is included in this beta.

The provider directory is also an independently installable package: `python -m pip install ./src/providers`. `ProviderClient` routes accounts by provider and account identity, manages stream cancellation and closes owned connections. The same `create_provider` factory is used by the application. The typed `Backend` API covers models, chats, projects, sources, uploads and download resolution. See the [provider package documentation](src/providers/README.md) for usage and its portable tests; the directory can become a separate repository or submodule.

Tests run without requiring your personal account sessions. Install [uv](https://docs.astral.sh/uv/) first; the first command creates the lockfile-backed development environment:

```sh
uv sync --locked --all-groups
python run_tests.py
```

The script runs application tests, standalone provider API and credential tests, the provider smoke script against the current checkout, and JavaScript tests. It uses an active Python virtual environment first, then the locked `uv` environment, a project virtual environment, and finally the interpreter used to start the script. Python tests appear in the standard verbose `unittest` format. Node.js is required for JavaScript tests and the provider smoke check. Clean wheel/source-archive installation checks, lint/format, and package-build checks remain separate CI checks. To run the checks individually:

```sh
PYTHONPATH=src:. uv run --locked python -m unittest discover -s tests
node --test tests/js/*.test.cjs
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
uv run --locked python -m build
uv run --locked python -m twine check dist/*
```

To profile SiopeChat while using it, run `uv run --group performance python tools/measure_performance.py`. The CSV records process-tree CPU, RSS/USS/PSS memory, disk I/O, open handles, thread count and process count. A companion JSON summary compares file counts and sizes by category under `data/` and the configured download folder before and after the run, including browser profiles, caches, account data and the archive database. The storage scan reads file metadata only; it does not read file contents or follow symlinks. Pass `--interval 0.5` for two samples per second, `--storage-root PATH` (repeatable) to select other directories, or `--no-storage` to skip directory scans. Arguments after `--` are passed to SiopeChat. Process-tree CPU can exceed 100% when multiple cores are busy; USS/PSS or disk I/O may be unavailable on some operating systems and will be left blank.

Before reporting an issue, note the provider, operation, terminal, operating system and error message. Remove credentials and private conversation content. The [changelog](CHANGELOG.md) records release changes.

`Accounts` sends use the shared `TaskService`, with an account resolver, response limits and persistence inside the account queue. Cancellation releases the account after transport cleanup and accepted-response writes finish. Provider registration accepts an `account_factory` for isolated adapter instances. Terminal reads, mutations and Markdown preparation use separate bounded worker pools; closing a tab does not cancel its response.

The GitHub Actions workflow runs the locked application Python 3.11/3.14 and Node 22/24 matrix on Linux, Windows and macOS. The standalone provider package keeps its separate Python 3.10+ contract. Passing unit tests or CI does not establish live provider compatibility or native terminal interaction. Session files still use private file permissions rather than an operating-system credential vault; the signing helper is a separate process with restricted inputs and environment, and Node VM execution is not an OS security sandbox.

## License

Copyright © TahsinCr. Licensed under [GPL-3.0-only](LICENSE). Vendored components retain their own notices in `src/utils/`.

[version-shield]: https://img.shields.io/badge/version-v1.5.1--beta-b7a3e3.svg?style=for-the-badge
[python-shield]: https://img.shields.io/badge/Python-3.11%2B-3776AB.svg?style=for-the-badge&logo=python&logoColor=white
[license-shield]: https://img.shields.io/badge/license-GPL--3.0--only-blue.svg?style=for-the-badge

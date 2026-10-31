[![Version: v1.5.2-beta][version-shield]](CHANGELOG.md)
[![Python 3.11+][python-shield]](https://www.python.org/downloads/)
[![GPL-3.0-only][license-shield]](LICENSE)

**English** | [Türkçe](README-TR.md) | [Русский](README-RU.md)

# SiopeChat

### ChatGPT, Gemini and Claude web chats — together in one terminal.

Bring your chats, projects and model comparisons into one terminal workspace. Switch between tabs, compare replies side by side and keep working without leaving your terminal.

> SiopeChat is an independent, unofficial client for ChatGPT, Gemini and Claude **web chats**. It uses your browser session, so no API key is required. Your plan's model access and usage limits still apply. It does not connect to official developer APIs, Codex or Claude Code. Changes to provider websites can affect compatibility.

![SiopeChat TUI screenshot](assets/siopechat.png)

*SiopeChat's terminal interface with sample conversations.*

<p align="center">
  <a href="#install-and-sign-in"><strong>Install</strong></a> &nbsp; | &nbsp;
  <a href="#everyday-use"><strong>Everyday use</strong></a> &nbsp; | &nbsp;
  <a href="#extensions"><strong>Extensions</strong></a> &nbsp; | &nbsp;
  <a href="#development"><strong>Development</strong></a>
</p>

## What you can do

| Conversations | Projects and context | Local-first history |
| --- | --- | --- |
| Keep chats open in tabs, preserve drafts and scroll positions, or compare up to four panes in a group. | Use ChatGPT and Claude projects or Gemini notebooks, with provider-supported instructions and sources. | Open saved conversations immediately while history refreshes in the background. |
| Select models and effort per account; edit, retry or branch replies when the provider supports it. | Search and sort projects and chats, with filters for provider accounts. | Keep conversation history and resource metadata in a local SQLite archive. |

| Files and downloads | Keyboard and extensions | Your language |
| --- | --- | --- |
| Download generated files from replies and track progress in the built-in download manager. | Use the command palette, configurable shortcuts, mouse controls and local extensions. | Choose the English, Turkish or Russian interface. |

## Less Waiting, Better Flow

Keep writing while SiopeChat takes care of the last response. Shared chat improvements and provider-specific optimizations reduce avoidable waiting.

| Where it helps | What changed |
| --- | --- |
| **ChatGPT, Gemini and Claude** | Saved chats open from the local archive while history refreshes in the background. Model catalogs reuse valid caches, and large history preparation stays off the UI event loop. When the provider signals that text generation is complete, the status and timer finish; connection finalization can continue in the background. Queue a follow-up while the current send finishes. |
| **ChatGPT** | Reuse eligible conversation snapshots and completed stream messages to avoid duplicate history requests. Prepare Sentinel headers ahead of the next send. Native Python initial-proof generation takes **0.329 ms median** in the recorded offline benchmark: about **10.7× faster** than a reused QuickJS-NG context and **12.6× faster** than an already-running Node/V8 process with a fresh VM per sample. |
| **Gemini** | Correct Flash routing, preserve the provider's default thinking policy, reuse valid continuation IDs, and avoid repeating the activity RPC within five minutes. Earlier limited live trials reported first-text latency falling from **65–110 s to 1.89–3.80 s**. These are recorded ranges, not a controlled benchmark or a guarantee for every model and account. |

**Read the numbers in context:** the ChatGPT ratios compare local initial-proof medians, not whole-message speed. The Gemini trials have no published paired samples or p95, so they do not establish a reliable “× faster” figure. Shared UI improvements have no measured multiplier. Network conditions, model choice and server load still affect response times. See the benchmark details below and the [changelog](CHANGELOG.md). The Python package version is `1.5.2b0`.

## Install and sign in

### Requirements

Use **Python 3.11+** and a Unicode terminal; true color and mouse support are recommended. The installers check your Python version. Import an existing login from a supported browser profile as described below. Routine chat operations use HTTP and do not need a running browser.

ChatGPT's supported Sentinel checks run in Python using the bundled `SentinelSpec` profile. **No Node.js or JavaScript engine is required.**

<details>
<summary>Performance figures: what was measured?</summary>

The recorded offline benchmark used 60 samples on Linux x86_64 with Python 3.14.8 and Node.js 26.12.1. Both JavaScript paths used the same 33,820-byte SDK; Python used the corresponding Sentinel profile rather than executing that SDK.

| Initial-proof implementation | Median | p95 | Median time ÷ Python time |
| --- | --- | --- | --- |
| Pure Python, reused runtime | 0.329 ms | 1.925 ms | 1× |
| QuickJS-NG, reused context | 3.526 ms | 11.220 ms | 10.7× |
| QuickJS-NG, fresh context | 8.831 ms | 17.285 ms | 26.8× |
| Node/V8, running process, fresh VM | 4.140 ms | 6.742 ms | 12.6× |
| Node/V8, new process per operation | 57.944 ms | 62.409 ms | 176.1× |

The last row includes process startup; a no-op Node launch alone measured 32.004 ms median / 32.448 ms p95. It is not equivalent to reusing a process. Node/V8 beat QuickJS-NG with fresh contexts in this fixture, while Python had the lowest proof-stage median. These figures exclude network requests, model generation and UI rendering. They are historical measurements, not a new benchmark of this revision.

</details>

### Install and launch

Run the one-time installer from the checkout. It checks Python 3.11+, reuses a supported virtual environment if it finds one, and installs SiopeChat plus its Python dependencies there. If no environment exists, it creates `python/` without replacing an existing directory.

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

**Verify from profile** reads the selected provider's session from the default browser's last-used profile. It does not start the browser or require debugging flags. Only that provider's authentication cookies are retained, and the new session is verified before it replaces a saved one.

Chrome, Chromium, Brave/Brave Origin, Edge and Vivaldi share the Chromium profile reader. Firefox and Zen share a read-only Gecko reader using `profiles.ini` and `installs.ini`. Safari uses a separate WebKit cookie reader on macOS. Cookie decryption uses browser-specific OS keyring identities; your keyring may request access.

Locked databases, custom or sandboxed profile locations, and unsupported encryption can prevent import, including some Chromium configurations on Windows. Errors distinguish missing dependencies, file permissions, database access, keyring access and decryption. A failed import preserves your saved session. Update dependencies in the environment that launches SiopeChat.

The reader supports session cookies from the current Firefox/Zen recovery snapshot; separate containers and old snapshots are not merged. A locked cookie database is retried from a private temporary copy with its WAL/journal files. Your browser profile is not modified.

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

Each message has a compact toolbar. Copy works locally; supported user messages can be edited and sent again. Replies can be retried when that message and provider allow it. ChatGPT and Gemini also support branching into a separate conversation. Provider-published sources stay with the reply they belong to. Available model and effort details appear on the right, before the saved-version controls. Editing or retrying updates the active history after provider confirmation. Conversation deletion and split-view controls sit beside the upper synchronization button.

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

Saved chats open from the archive first, then refresh from the provider in the background. You can keep typing and sending while history loads. Older messages merge with newer local turns without overwriting an active reply. Cached sidebar and project pages remain usable during refresh; a failed refresh keeps the last good data.

Large history preparation runs outside the UI event loop, and the sidebar draws only visible rows. Archive queues and render caches are bounded. Disabling the archive keeps its files and leaves direct provider operations available. Disabling the layout extension removes split controls without disabling providers.

<details>
<summary>History paging and memory budgets</summary>

Each initial SQLite batch contains up to 80 visible messages within a 262,144-character serialized-record budget. Scrolling adds older batches without dropping loaded messages; stable message IDs keep your position anchored. A single message is not truncated to meet the batch budget.

The renderer prepares the viewport first, then two screens above and below it. Each view has a 1 MiB LRU row cache and a separate 256 KiB ANSI cache with compression for large entries. Inactive main-tab layouts share a 4 MiB eviction budget. Text is shared with its layout where possible, and accepted turns update only a bounded tail of the SQL history.

Closing a tab releases its transcript and render references; a running background response retains its current turn until persistence finishes. Source memory grows with the history you visit, while render caches remain bounded. Python may retain freed allocations, so process RSS does not necessarily fall immediately.

</details>

After an ambiguous remote write failure, synchronize before resending. SiopeChat does not automatically replay an operation that may already have reached the provider.



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

### Set up and validate

Use Python 3.11+ for application development. With [uv](https://docs.astral.sh/uv/) installed, run these commands from the repository root. Select the project's existing `python/` environment so development and the launchers use the same dependencies:

```sh
# Linux / macOS
UV_PROJECT_ENVIRONMENT=python uv sync --locked --all-groups
python/bin/python run_tests.py
python/bin/python -m ruff check .
python/bin/python -m ruff format --check .
python/bin/python -m build
python/bin/python -m twine check dist/*
```

In Windows PowerShell, set `$env:UV_PROJECT_ENVIRONMENT = "python"`, run `uv sync --locked --all-groups`, and replace `python/bin/python` with `./python/Scripts/python.exe` in the commands above.

`run_tests.py` runs the application suite, an isolated copy of the provider tests, and the provider package smoke check. Tests use fakes and fixtures; personal account sessions are not required. The runner prefers an active virtual environment, then project environments (`python/` before `.venv/`), then uv, and finally the invoking interpreter. Run it through the intended interpreter explicitly to avoid ambiguity.

Lint, formatting and distribution builds are separate checks. GitHub Actions tests the application on Linux, Windows and macOS with Python 3.11/3.14, and the provider package separately with Python 3.10/3.14. Offline success does not prove live-provider compatibility or real-terminal behavior.

### Find the right layer

| Location | Responsibility |
| --- | --- |
| `src/bootstrap.py`, `src/cli.py` | Application setup and command-line entry points |
| `src/accounts.py`, `src/tasks.py`, `src/runtime.py` | Account routing, task ownership and service lifecycle |
| `src/providers/` | Independent web-session clients, transports and provider contracts |
| `src/tui/` | Terminal views, input handling and rendering |
| `src/extension.py`, `src/tui/api.py` | Extension lifecycle and public TUI extension API |
| `extensions/archive/`, `extensions/layout/` | Local history and saved workspace layouts |
| `tests/`, `src/providers/tests/` | Application regressions and independent provider tests |

Keep protocol details inside the owning provider. Provider code must not import the application database, settings, extensions or TUI. Extensions register services through `extension.py` and use `tui.api` for terminal contributions. Keep network requests and expensive Markdown work out of render callbacks.

Sends run through `TaskService` with per-account serialization. Cancellation releases an account only after transport cleanup and accepted-response persistence finish. Closing a tab leaves its response running while the application remains open. Changes must preserve provider/account identity, message parents, branch semantics and cancellation; ambiguous remote writes must not be automatically resent.

### Use the provider library independently

Install it with `python -m pip install ./src/providers` in your chosen environment. `ProviderClient` manages account adapters, streams and connection cleanup; `Backend` exposes models, chats, projects, sources, uploads and download resolution. The application uses the same `create_provider` factory. See the [provider guide](src/providers/README.md) for examples and standalone tests.

Transports, browser-profile discovery and local CDP access are implemented in Python. Gemini uses the bundled HTTPS transport without a Gemini SDK. ChatGPT handles supported Sentinel challenges using its Python runtime and bundled profile. Changed or unsupported server requirements need explicit compatibility checks; offline fixtures do not cover every live challenge.

### Understand local storage

| File | Contents |
| --- | --- |
| `config.json` | Preferences, account labels and selection |
| `data/accounts.sqlite3` | Account sessions, model catalogs, verification records and fallback conversation cache |
| `data/archive.sqlite3` | Archived history, resource metadata, download records and local reply versions |
| `data/workspaces.sqlite3` | Saved chat layouts |

These paths are relative to the configured application directory. Provider-specific fields remain JSON inside the owning database. The provider library accepts caller-owned credential storage and does not depend on these SQLite schemas. Account storage is not encrypted; Unix permissions restrict it to its owner. Keep migrations transactional and preserve existing user data. See the upgrade warning above before opening an older archive.

### Measure performance and report issues

After installing the performance dependency group, run:

```sh
python/bin/python tools/measure_performance.py --interval 0.5
```

The CSV records process-tree CPU, RSS/USS/PSS memory, disk I/O, handles, threads and process counts. A companion JSON file compares storage counts and sizes before and after the run. Scans read metadata only and do not follow symlinks. Use repeatable `--storage-root PATH` options for other directories, `--no-storage` to skip scans, and `--` to pass arguments to SiopeChat. CPU can exceed 100% across multiple cores; unsupported OS metrics remain blank.

For ChatGPT send-stage diagnostics, enable `SIOPECHAT_SEND_TIMINGS=1` before launching. Compare the same account, model, effort, conversation state and network conditions. Distinguish connection setup, first provider text, first visible UI text and finalization; report sample counts, medians and p95. Never include sessions, prompts or token contents in diagnostic logs.

When filing an issue, include the version, provider, operation, terminal, OS and sanitized error. Describe whether the problem occurred in offline tests or a live session. Release changes are recorded in the [changelog](CHANGELOG.md).

## License

Copyright © TahsinCr. Licensed under [GPL-3.0-only](LICENSE). Vendored components retain their own notices in `src/utils/`.

[version-shield]: https://img.shields.io/badge/version-v1.5.2--beta-b7a3e3.svg?style=for-the-badge
[python-shield]: https://img.shields.io/badge/Python-3.11%2B-3776AB.svg?style=for-the-badge&logo=python&logoColor=white
[license-shield]: https://img.shields.io/badge/license-GPL--3.0--only-blue.svg?style=for-the-badge

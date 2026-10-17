# SiopeChat

A modular terminal application built on reusable AI web-session providers. Modules live directly under `src/`; there is no `core` or application-name package.

## Development status

SiopeChat is developed without numbered releases for now. Each completed feature is recorded in CHANGELOG.md and its own commit. Package metadata is a development placeholder, not a release promise. Browser-session integrations use undocumented web protocols and are verified separately from offline tests.

## Run

Python 3.10+, Node.js 22+ and Chrome/Chromium are needed for the ChatGPT compatibility path. Install the project into your existing Python environment:

```sh
python -m pip install -e .
./siopechat.sh --help
./siopechat.sh
```

Start the terminal, open **Settings → Accounts**, add/select an account, open **Connected browser**, sign in and **Verify**. A valid saved session can be verified without reopening a browser. Gemini requires Python 3.11+. The `login`/`auth` management commands remain available for the legacy default ChatGPT account.

On Windows Command Prompt use `siopechat.bat`; PowerShell can use `siopechat.ps1`. `siopechat.sh` is the Linux/macOS shell launcher and `siopechat.py` is the shared Python entry point. Startup logic is shared in `src/bootstrap.py`; the existing `python/` environment is reused when present. Launchers never create a project virtual environment.

## Structure and runtime

Small modules are plain Python files. Packages are reserved for the terminal UI, the independent provider library, reusable utilities and resource bundles.

```text
extensions/layout/       bundled workspace feature, outside src
themes/soft.json          optional external theme tokens
src/
  runtime.py             shared service lifecycle and dependency resolution
  settings.py            defaults, field schemas and config.json persistence
  actions.py             ActionService: dispatch, search and shortcut mapping
  tasks.py               TaskService: streaming jobs and bounded observers
  accounts.py            isolated account adapters, browser login and routing
  backend.py             compatibility IPC service, sessions and snapshots
  extension.py           public extension API and lifecycle
  language.py            merged translation catalog
  theme.py               theme service and built-in terminal/Markdown defaults
  lang/                  shipped language JSON files
  cli/                   chat view, Markdown rendering, widgets and bindings
  providers/             reusable backend library and provider-local scripts
  utils/                 paths, clipboard, processes, container and event library
```

`Runtime` owns one `DependencyContainer` and a bounded-history `EventBus`. All application services are registered there, and the extension API resolves those same instances. There is no parallel dictionary-based service container. Action, task, settings, view, extension and service lifecycle notifications use this shared bus; events carry identifiers and invalidations rather than prompt or response copies.

The container and event implementations are vendored unchanged from [vehicle-autonomy-core](https://github.com/TahsinCr/vehicle-autonomy-core/tree/2bac9cd5b1cbeae82b7d3beef670a37d7ee5caa2), under `utils/container` and `utils/events`. Origin, commit and copyright are recorded in `utils/NOTICE`; their GPL-3.0-only license is retained in `utils/LICENSE`. Vehicle, mission and MAVLink modules are not included. Python 3.10 compatibility dependencies are declared conditionally in pyproject.toml.

Extensions receive an `ExtensionContext`: `context.api.get(name)` resolves shared services, `context.container` is an owned child scope, and `context.subscribe(...)` registers event subscriptions that are removed on disposal. The context also contributes actions, views, settings, translations and themes.

Explicit disablement uses `disable_async` when resources need asynchronous cleanup. Runtime shutdown withdraws extension contributions before closing shared services and preserves saved enablement preferences. Cleanup can be retried after failure; cancelling a shutdown waiter does not cancel the underlying cleanup. Event callbacks run synchronously and must remain short; use background work for slow operations. Extensions remain trusted in-process Python code.

The chat UI is separated into `cli/chat.py`, `cli/markdown.py`, `cli/widgets.py` and `cli/bindings.py`. Platform clipboard operations are in `utils/clipboard.py`. Provider scripts are grouped under `providers/chatgpt/scripts/browser` and `scripts/sentinel`, with shared stdin and debugger metadata helpers.

## Configuration and local data

`config.json` lives at the project root and is created from `src/settings.py` if absent. Extension configuration lives under `extensions.<id>`. Extensions contribute typed fields to the same settings interface.

The top-level `keybindings` object contains every built-in terminal shortcut by action ID. Change values there (for example, `"ui.command_palette": "c-p"`); `/help` prints the active map, the command palette searches built-in commands and registered actions, and the editable Settings form shows and saves the mappings without restarting. You can also change one from the CLI: `./siopechat.sh action settings.change '{"section":"keybindings","key":"ui.command_palette","value":"f9"}'`.

`data/` is created automatically at the project root and is excluded from Git. Workspace SQLite data and new compatibility snapshots live there. Legacy snapshots remain readable without moving or deleting them. The default ChatGPT account retains its existing session/profile paths. Additional accounts use separate directories under `data/accounts/`; credentials are not put into `config.json`.

Set `SIOPECHAT_HOME` to select another configuration/data root. An installed application without a checkout uses the working directory unless this variable is set.

```sh
./siopechat.py settings
./siopechat.py settings set language tr
./siopechat.py extensions
./siopechat.py extensions disable layout
./siopechat.py extensions enable layout
./siopechat.py providers
./siopechat.py actions
./siopechat.py action layout.create '{"name":"Research"}'
./siopechat.py workspace list
```

Language bundles are owned by their extension and merged under namespaced keys, with locale and English fallback. Disabling an extension removes its live contributions and preserves its saved configuration. Optional theme JSON files live under the root themes/ directory; the built-in default lives in src/theme.py. Invalid optional theme files are recorded as failures without disabling the default. Some legacy status text still needs localization/theme coverage.

## Independent provider use

The `providers/` directory has no imports from the application, terminal, extension system, settings or database. It can be copied into another project with its own dependencies (`httpx` plus Node/Chrome for live ChatGPT operations, or `gemini-webapi` for Gemini).

```python
from pathlib import Path
from providers.chatgpt import BrowserSession, ChatGPTClient, SessionPaths

session = BrowserSession(
    SessionPaths(
        session_file=Path("sessions/chatgpt.json"),
        cache=Path("cache/chatgpt"),
        profile=Path("profiles/chatgpt"),
    )
)
# Explicit user action; opens a login browser if needed:
session.launch()

# After login:
with ChatGPTClient(session) as client:
    conversations = client.list_conversations(maximum=20)
```

`ChatGPTProvider` exposes the asynchronous common backend API over this client. Conversation records preserve provider/account/remote IDs, accessible message channels and provider metadata. Unknown data is retained rather than silently discarded.

The common contracts cover browser login, explicit authentication, local snapshot removal, conversations, streaming, projects, membership, model catalogs and effort choices. An existing snapshot is not reported as verified authentication; removing it does not delete the browser profile. Feature states distinguish available, unsupported, unknown and temporarily unavailable behavior. The ChatGPT adapter implements history, rename, streaming with separate reasoning channels, live account model/effort discovery, paginated project reads, project details and project conversations. New conversation requests can carry project membership. Existing conversations can be moved into/out of projects through `move_chat`; the client requires a read-back of native membership before confirming success. Project creation/update/deletion remain unavailable. Catalogs retain provider IDs and metadata and never infer availability from chat history.

## Current migration boundary

The terminal uses a full-width tab strip with a persistent compact SiopeChat brand, centered tabs capped at 26 cells and navigation/new controls fixed at the right, thin tab separators and close/previous/next/new controls. Active tabs change text color without a different background. The sidebar is collapsible and stays between 22 and 30 cells. The bottom selection bar shows orange model, green effort and blue clickable project state, wrapping on narrow terminals. User/assistant messages use grey/black surfaces and green `>` / orange robot markers; available reasoning summaries use a blue surface and brain marker before the answer. Role markers belong to the first message line and scroll with it. Reading width is capped at 100 cells. Explicit transcript background painting covers empty cells after panel changes. Neutral charcoal surfaces keep emphasis on the conversation; Send stays at the edge of the composer; a return-to-latest control appears only while reading older messages. One bottom status line provides F1 help and clickable settings/account controls, without persistent shortcut instructions. Settings opens as a workspace tab with General, Providers, Accounts, Extensions and Keyboard categories. Shared account/filter/file/project forms use a centered, bounded charcoal surface with clear checked and focused states. Account operations live inside that panel; the footer only shows a connection indicator. Click a chat to load its provider-returned history; click a project arrow to expand its chats or **ⓘ** for instructions and resource metadata. Mouse input is enabled by default and can be disabled in Settings; use the terminal's Shift-selection gesture when selecting text with mouse reporting active.

The full-screen terminal keeps the whole conversation in its transcript view and uses prompt_toolkit's normal resize handling. It no longer clears the terminal or replays conversation text when a chat opens or the window resizes. Use the mouse wheel, visible previous/next controls or `PageUp`/`PageDown` to scroll the transcript. `Ctrl+Home` goes to the beginning and `Ctrl+End` / **Son mesaja git** returns to live following. Manual scrolling pauses following while a response continues in the background.

The sidebar groups project-linked chats under **Projeler**, unassigned chats under **Sohbetler**, and saved layout-extension workspaces under **Düzenler**. `Ctrl+G` opens the sidebar and focuses its tree; use `Up`/`Down` and `Enter` to open a chat or expand/collapse a section. `Space` toggles the focused project/section, `Ctrl+F` focuses search, and `F7` hides or shows the sidebar. Search covers loaded chats. The first history page contains at most 50 chats; **Daha eski sohbetleri yükle…** fetches another page on demand. Refresh reloads the first page. `Ctrl+R` opens the same recent-chat list; `Enter` opens the selected conversation in a tab. `/resume ID` opens a known conversation directly.

Tabs support new (`Ctrl+T`), switch (`Alt+Left`/`Alt+Right`), and close (`Ctrl+W`). Switching to an already open tab works while a reply streams; `●` marks an active reply and `✓` marks a reply finished in another tab. Up to four tabs can have pending requests. Each tab retains its own stream, cancellation connection and elapsed time. Account-scoped adapters serialize operations within one account; different account instances can progress independently. The legacy management daemon still serializes its own operations.

Projects come from the account's web project list, including shared projects. Expand a project with Enter/Space to load one conversation page; **Daha fazla sohbet…** and **Daha fazla proje…** load subsequent pages. F9 or `/project info` opens instructions and resource metadata in a separate read-only tab, preserving the chat. Project files are listed without downloading their contents. F10 or `/refresh` refreshes models, projects and recent chats. Loading and errors are independent, and a failed refresh retains the last good data. Older conversation snapshots refresh once to recover missing project metadata.

`/model` opens the live account model picker, and `/effort` opens choices supplied for the selected model. The chat picker follows the provider's public `categories` / `supported_models`, rather than displaying every raw model record. Work-mode (`-wm`) and non-picker records remain in the provider catalog but are not chat choices. Display names use the advertised version and chat mode; versions are not hardcoded. The command palette also includes these selections. `/model MODEL_ID`, `/effort ID` and `/effort default` support explicit selection. The application prefers `medium` only when offered; otherwise it uses an advertised provider default or sends no effort override. Switching to a model without effort choices clears the old effort. No model ID or `extended` preference is forced. Selections are kept per tab and applied to direct, captured-recipe and browser-driven conversation requests; the browser path requires the debugging browser to be available.

Search fields support normal editing, including Backspace, Delete, spaces, Home/End and Ctrl+D. Conversation reload defaults to F8 because Ctrl+H is also Backspace in many terminals. Sidebar, tab and Markdown widths account for the actual content area and wide Unicode names. Long menus scroll to the keyboard selection. Drafts are preserved independently per tab.

`F5`, the bottom status line or `/settings` opens the categorized settings workspace. Settings is a normal reusable tab, not a popup. Extensions has enabled/disabled controls that run the extension lifecycle. Search application, provider, extension and shortcut fields, select a row, edit its value and Save (`Ctrl+S`). Use Left/Right on the settings list or the clickable choice arrows to cycle dynamically supplied options. Tab/Shift+Tab moves between fields and buttons; Esc closes. Language, terminal theme, mouse input and shortcuts apply immediately; model/effort defaults use the current provider catalog. The form validates and atomically persists values through the same `settings.change` action used by the CLI and extensions. Disabling the layout extension hides its sidebar section.

`F12`, the connection indicator, `/session`, `/login` or `/logout` opens the Accounts category inside Settings. **Oturumu doğrula** reuses the saved session without opening a browser when it is still valid. For first login or an expired session, open **Bağlı tarayıcı**, sign in there and verify. **Varsayılan tarayıcı** opens the website using the operating system's default browser, but cannot automatically import that browser's session. Local logout requires confirmation, removes the selected application session and clears that account’s sensitive UI state. The legacy default-account logout also stops its daemon. It retains website login, browser profile and local conversation files; sign out on the website separately when needed. The terminal never asks for a password.

Clicking the composer restores typing focus after selecting a message. Global command shortcuts also work from transcript focus; Ctrl+D quits with an empty draft, while retaining Delete behavior inside search/settings text fields. Consecutive mouse-wheel events accumulate their scroll distance even before a redraw. The composer grows upward to six visible lines, then scrolls internally. Its scrollbar appears only when needed. `Ctrl+A` selects all text, `Ctrl+C` copies a selection, `Ctrl+X` cuts editable text, and `Ctrl+V` pastes text or uses the image path when no text is available. Bracketed terminal paste is also supported. Click a message and use `Ctrl+A` / `Ctrl+C` to select/copy that message's original text. A selection takes priority over response interruption. Native clipboard helpers support Linux/macOS/Windows; OSC 52 is a fallback when supported by the terminal.

`/project` opens the project picker. `/proje` is the same command. Choosing a project moves the current existing chat; **Projesiz** removes its project membership. An unsent draft simply adopts the selection. The UI preserves history, draft text and the tab, and only updates membership after server confirmation; failure remains visible without a false success. `/project info` and F9 open project details. The draft carries its project ID through IPC into the actual provider request body; `/project ID` accepts a loaded project ID. Project lists and subsequent pages remain dynamically loaded.

`/temporary`, `/private` or `Alt+T` starts a temporary chat. Temporary state is per tab and cannot be combined with a project. Requests set the provider's history-disabled flag. Local conversation files and active-conversation pointers are not written; continuation is kept in adapter memory (up to 32 contexts; ChatGPT additionally bounds text per context), forgotten when a completed temporary tab closes or the UI exits normally, and lost when the owning provider stops. A missing context fails explicitly rather than becoming a saved conversation. Temporary sends require direct transport; browser recipe capture/fallback is disabled so private prompts are not inserted into a persistent browser draft. This is an experimental web-protocol integration, not anonymous or zero-retention messaging: [OpenAI may retain temporary chats for up to 30 days](https://help.openai.com/en/articles/8914046). Real-account temporary/project sends and project membership mutations have not been validated by the offline tests. The membership write uses the undocumented conversation PATCH body and read-back validation; compatibility with the current live website remains unverified.

Provider-returned visible reasoning/summary content is kept separately from the final answer in history, streaming events and task snapshots. The General setting `show_reasoning` controls display. Hidden protocol nodes and tool-directed messages are not shown; unavailable reasoning is never fabricated.

Status-only redraws reuse the rendered transcript, Markdown caches stay bounded, and authentication prefers the saved snapshot over re-capturing browser traffic. Authenticated HTTP calls are restricted to the provider origin; session snapshots and IPC messages have size limits. These controls do not remove provider usage limits or guarantee account restrictions cannot occur.

Saved workspaces appear in **Düzenler** and invoke the layout extension. Completing localization/theme coverage of legacy transcript messages, archive synchronization, cross-provider transfer and a Claude adapter remain future work. Saved workspace metadata exists, but saving/restoring the new live split groups is not wired into that storage yet. `CHATGPT_WEB_MODEL`, `CHATGPT_WEB_CDP_URL` and `CHATGPT_WEB_SEND_MODE` remain compatibility settings. Persistent daemons with an older IPC protocol are replaced at startup before discovery commands are sent. Web endpoints are undocumented and can change independently of this application.

## Development

Use the existing environment; install project dependencies with `python -m pip install -e .`. From a checkout, the current test command is:

```sh
PYTHONPATH=src python/bin/python -m unittest discover -s tests -v
node --test tests/js/*.test.cjs
uvx --from 'ruff>=0.11,<1' ruff check src extensions themes tests siopechat.py
uvx --from 'ruff>=0.11,<1' ruff format --check src extensions themes tests siopechat.py
```

Tests use controlled providers, temporary databases and mocked transports. They do not send messages to an account. Project decisions, outstanding work and the work log live in `MEMORY.md`.

### Terminal editing and settings navigation

Shift+Enter inserts a newline with CSI-u and xterm modified-Enter sequences. Alt+Enter and Ctrl+J are fallbacks when a terminal sends Shift+Enter indistinguishably from Enter. Settings lists use the available page height, aligned value columns and a scrollbar; the wheel, arrows and PageUp/PageDown move the selection with its editor.

### Split conversations

With the layout extension enabled, click **Böl** beside Send or run `/split` (Alt+V) / `/split-down` (Alt+H), then select another conversation from the sidebar. Up to four panes form one tab group. Click a pane header to type into it; other panes keep their own scroll positions. Switching tabs preserves manual reading position; role markers use the same fixed gutter in every pane. `/unsplit` (Alt+U) returns to one pane. Disabling the extension removes its presentation without deleting conversations. Account adapters queue operations within one account; separate accounts can progress independently.

### Gemini transport

`providers.gemini.GeminiProvider` uses [gemini-webapi](https://github.com/HanaokaYuzu/Gemini-API), an unofficial web-session transport, on Python 3.11+. It discovers account models, reads history, streams answers/thoughts and accepts files. Thinking options map to the transport’s standard/extended boolean, not invented low/medium/high levels. Credentials are injected from a private cookie file. Offline transport tests do not prove live account compatibility.

### Accounts, provider filtering and files

Settings → Accounts lists accounts and offers Add account (ChatGPT or Gemini), selection, browser login, verification and local logout. Each account has its own browser profile, provider instance and local cache. First login uses the connected debugging browser; default-browser login does not silently import another profile. Gemini cookie capture reads only the selected local Gemini tab. Cookie files/cache live in private directories under ignored data; credentials never enter config.json.

Conversation identity is `(provider, account, remote ID)`. Tabs preserve their account, model and draft when switching. The sidebar shows ◈ ChatGPT / ✦ Gemini; its search-adjacent filter appears only with multiple configured providers and supports multiple selections. Account lists retain pagination and isolate failures. The terminal now calls account-scoped providers through one persistent background event loop; legacy management commands keep their compatibility daemon path.

Use **Dosya**, `/attach` or Alt+A to choose a path (Tab completion). Gemini accepts web-supported files; ChatGPT uses native upload slots and attachment metadata, with PNG image dimensions retained. Attachments are limited to 20 MiB in this client. File uploads, multi-account browser login and provider sends require live verification beyond the offline tests.

### Action catalog

Keyboard mappings, the command palette and Settings → Shortcuts use the same canonical action IDs and category order (chat, model, projects, layout, accounts, view, editing, navigation, application). All default bindings are listed in config.json. Model/effort pickers also have Alt+M / Alt+E bindings. While the terminal runs, its actions are contributed to the shared ActionService for extensions; closing the terminal withdraws those contributions.

### Creating projects and notebooks

For an account that supports project creation, use the sidebar header **+**, `/project-new` or Alt+P. Choose a name, private/shared visibility and default/project-only memory. The ChatGPT component drives the connected website’s own form and reads back the new project/memory. It does not guess a private creation endpoint or retry creation automatically. A changed or missing control fails only this operation. Shared creation fails before submission if the native form does not offer sharing; use the website’s sharing step in that case. Live creation/sharing compatibility is still unverified.

[Google now documents notebooks and a Gemini Notebook connection](https://support.google.com/gemini/answer/16972047?co=GENIE.Platform%3DDesktop&hl=en). Availability and operations depend on the account. The current Gemini transport has no verified notebook project contract, so this adapter exposes chats and does not claim automatic NotebookLM synchronization.

### Verification limits

Offline tests exercise actual terminal key processing/render cells and controlled provider/browser transports. They do not prove compatibility with the current ChatGPT/Google websites. Live Gemini login/models/history, file uploads and ChatGPT project writes still require authenticated-account checks. Windows/macOS launchers share Python startup logic but have not been run on those operating systems in this checkout. Browser-session access remains subject to provider limits; this client does not remove limits or promise protection against account restrictions.

### Terminal output and responsive settings

The Gemini adapter suppresses its SDK namespace logs by default, including messages the SDK emits despite `verbose=False`; it leaves host logging handlers intact. Independent-library callers can pass `quiet=False` when they manage SDK logging themselves. The full-screen terminal also uses prompt_toolkit stdout/stderr coordination for incidental output.

Settings fills the available page. Below 76 columns, categories become a compact previous/current/next strip; list rows and value columns follow the current terminal dimensions. The settings/account page scrolls to focused controls on short terminals, and account actions stack when their content area is narrow. Resizing does not discard the draft setting value.

# SiopeChat

A terminal workspace for AI conversations, built around reusable web-session providers.

Keep conversations in tabs, select one account per provider without mixing their history, and choose the models available to those accounts. ChatGPT, Gemini and Claude are included. A shared service and extension API keeps the provider layer independent of the terminal and leaves room for other frontends.

![SiopeChat terminal workspace](assets/siopechat.png)

*An actual terminal capture using the Graphite theme and demonstration data. No personal conversations are shown.*

## Getting started

Requirements:

- Python **3.10+** for ChatGPT/Claude, **3.11+** for Gemini.
- Node.js **22+** for first-login helpers and the ChatGPT request signer.
- Chrome or Chromium for the explicit initial sign-in only. Conversation, model, history and project requests use saved web sessions over HTTP; they do not start or require a browser process.
- A terminal with Unicode support; true color and mouse reporting improve the experience.

Install from the checkout using your Python environment:

```sh
python -m pip install -e .
```

The launchers reuse the checkout's `python/` environment when present. They do not create a virtual environment.

| System | Start |
| --- | --- |
| Linux | `./siopechat.sh` |
| macOS terminal | `./siopechat.sh` |
| macOS Finder | Open `siopechat.command` |
| Windows Command Prompt | `siopechat.bat` |
| Windows PowerShell | `./siopechat.ps1` |
| Installed console command | `siopechat` |

Open **Settings → Accounts**, add or select an account, then choose **Connected browser**. Sign in on the provider website and return to **Verify**. Verification saves a private account session. You can then close the login browser, including its process. A valid saved session is reused without signing in again; an expired or revoked session requires explicit verification. Opening the default browser does not import its login session into SiopeChat. When adding an account, **Browser profile** can explicitly reuse an existing account’s connected profile for a different provider. Separate profiles remain the default. Shared profiles retain independent account records and verification markers, but share the browser’s website logins.

This project uses unofficial web-session transports. It requires your own provider accounts and remains subject to their access and usage limits. It does not provide unlimited access or guarantee compatibility with future website changes.

## Working with conversations

- **Tabs:** keep drafts, scroll positions and conversation state separate. Active replies and replies completed in another tab have distinct indicators. The selected tab or split group uses the theme’s active color; inactive open tabs remain white.
- **Sidebar:** search loaded chats, expand projects and load older pages when needed. ChatGPT uses a white ring (`◎`), Gemini a light-blue star (`✦`), and Claude a terracotta asterisk (`✳`). Chat titles use the same text color. Open conversations are bold white in the sidebar and inactive open tab labels are pure white. Projects display their names rather than qualified internal IDs. The sidebar lists projects and notebooks across providers independently of the selected model. Only the conversation project picker beside the effort selector follows the current model account: ChatGPT projects for GPT, Gemini notebooks for Gemini, and Claude projects for Claude. All three providers have distinct conversation icons; a multi-select filter appears when multiple providers are configured.
- **Models and effort:** a new chat lists models from the selected account of each enabled provider. Choosing a model routes that draft to its provider/account; an existing chat remains tied to its original account. Lists come from the accounts, rather than a hardcoded version list. The last model and effort selection is remembered per account and reused in new chats, including an explicit provider-default effort choice. An obsolete choice falls back to an available model. ChatGPT uses its account catalog, Gemini uses SDK account availability, and Claude uses its account-entitled selector. Older catalog snapshots are discarded when their schema changes.
- **Projects:** ChatGPT project chats, instructions and resource metadata are available in the sidebar. `/project` moves the current chat or assigns a new draft; **Unassigned** removes membership. `/project-new` first selects the provider/account, then shows its native creation fields. Unsupported memory/privacy choices are hidden. Open project details to use **Edit**, **Add source** or **Remove source**; the same actions are available as `/project-edit`, `/project-source-add` and `/project-source-remove` (Alt+R, Alt+I, Alt+O). `/project-edit` opens the shared project picker across providers and opens the selected project tab. **Edit** on that tab uses the same inline editor, rather than a second popup. Instructions support multiple lines; Ctrl+S saves. Long forms scroll and keep their buttons accessible. Source types and file filters come from the selected provider's transport capabilities. File uploads are limited locally to 20 MiB; the provider may enforce stricter limits.
- **Files:** use the attachment command or button to choose a file. This client limits each file to 20 MiB; provider restrictions still apply.
- **Temporary chats:** click **Geçici: Kapalı / Açık** beside the project selector to change a draft before its first message, or use `/temporary` to open a separate temporary chat. Enabling temporary mode clears the draft's project assignment and keeps its typed text. Existing conversations cannot change mode. Unsupported providers show **Desteklenmiyor**. Temporary requests do not write local conversation snapshots; provider retention policies still apply.

With the layout extension enabled, choose **Split**, `/split` or `/split-down`, then select another chat from the sidebar. Up to four panes share one tab group. Click a pane heading to make it active; `/unsplit` returns to a single pane. Saved workspace metadata is available, but saving and restoring the new live split groups is still pending.

Up to four tabs can have pending requests. Operations within one account are queued; separate account instances can progress independently. Model catalogs use a five-minute memory cache and an account-local disk snapshot. Refresh failures retain the last usable catalog instead of clearing the picker.

Opening a saved conversation restores its own account's remembered model and effort. Its model picker stays within that account; the model recorded on an older answer does not override your selection. Library synchronization and unrelated background jobs do not block sending. The archive can be disabled in Settings → Extensions; direct provider history and generation remain available.

### Keyboard and command palette

| Key | Action |
| --- | --- |
| `Ctrl+P` | Search commands and actions |
| `F1` | Help and the active shortcut map |
| `Ctrl+T` / `Ctrl+W` | New / close tab |
| `Alt+Left` / `Alt+Right` | Previous / next tab |
| `Alt+M` / `Alt+E` | Model / effort picker |
| `Ctrl+G` / `F7` | Focus / toggle the sidebar |
| `Alt+V` / `Alt+H` / `Alt+U` | Split columns / rows / remove split |
| `Alt+A` | Attach a file |
| `F5` / `F12` | Settings / accounts |
| `F10` | Synchronize models, library and current chat |
| `Ctrl+D` / `Ctrl+Q` | Exit; repeat within three seconds if a draft or reply is open |

Shift+Enter adds a line when the terminal sends a distinct modified-Enter sequence. Alt+Enter and Ctrl+J are alternatives. Dynamic text controls bound their cursor to the exact rendered snapshot, including after long-history reloads, provider filtering and resize. The composer grows before scrolling. Ctrl+A/C/X/V select, copy, cut and paste; selecting a message lets you copy its original text. In search and settings editors, Ctrl+D retains its text-editing meaning.

Every default shortcut is listed under `keybindings` in `config.json` and can be edited in Settings. The palette uses the same action IDs as keyboard bindings and extensions. Extension actions can declare fields so the palette asks for their arguments.

## Settings and local data

`config.json` is created in the project root. Set `SIOPECHAT_HOME` to use another configuration and data directory.

Settings has General, Providers, Accounts, Extensions and Keyboard categories. Save and Close stay at the bottom right. Categories collapse into a horizontal selector on narrow terminals, and the page scrolls to focused controls on short screens.

Providers has independent enabled switches. At least one provider with a configured account must stay enabled. Disabling a provider retains its credentials and history; sending is paused until it is enabled again. A provider with pending replies must finish or be stopped before disablement.

```json
{
  "providers": {
    "chatgpt": {"enabled": true},
    "gemini": {"enabled": true}
  },
  "extensions": {
    "layout": {"enabled": true}
  }
}
```

One selected account per provider is stored in `accounts.selected`; selecting Gemini does not deselect the ChatGPT account. Disabled providers are excluded from new-chat model discovery and navigation. Already open tabs from another account remain readable; activate that account again before sending. Account preferences live under `accounts.preferences`. Credentials are kept separately from configuration. New accounts use isolated directories under `data/accounts/`; the original ChatGPT account retains its legacy login paths when using the normal application root. An alternate application root keeps its session and login profile inside that root, including in tests. Conversation cache fallback is restricted to that original account, so an empty Gemini workspace cannot reopen a ChatGPT record.

Local logout forgets the selected application session. It does not sign out the provider website or delete saved conversation files. `data/`, login profiles, environments and local agent memory are excluded from Git.

## Local archive and synchronization

The bundled **archive** extension is enabled by default and can be switched off in Settings → Extensions. With it enabled, terminal conversations use `data/archive.sqlite3`; old JSON records remain readable for migration. New conversation bodies are not duplicated into the legacy JSON cache. Disabling the extension retains its database and falls back to the existing provider/cache path.

A saved conversation, project page or notebook opens from local storage first. The restored active chat is also revalidated on startup while its saved history stays visible. On the first access after startup, or after **F10**, background reads compare remote data with the saved record. Ordinary repeated reads share a 60-second revalidation window. Identical content only updates its check time. Concurrent reads of the same account and record share one job, and late reads cannot overwrite a newer completed reply.

The bottom status bar shows synchronization and errors; sidebar rows have `↻` while updating and `!` on failure. Reply generation and synchronization have separate state. Cold history loading belongs to its tab, so another tab can be opened while it loads. Initial Markdown rendering is prepared outside the UI thread, and rendered messages are reused on reopening.

The schema separates conversations, ordered messages, containers, memberships, resources, cached pages and synchronization state. Provider/account identity is present in every record. Messages preserve roles, channels, accessible thinking summaries, content parts and provider metadata. ChatGPT projects and Gemini notebooks keep distinct container kinds and their original metadata; they are not reduced to interchangeable writable projects.

Large file content belongs under `data/files/`, with content hashes and paths in resource records. Locally available file attachments are copied there after a successful normal send. Remote source/image references are retained as metadata; this release does not automatically download every remote source or image. Temporary chats do not create archive records.

The hot cache has an 8 MiB serialized-payload budget, with at most three synchronization workers and 32 queued/in-flight records. SQLite uses WAL and scoped transactions; disk writes do not hold the hot-cache lock. A failed background refresh retains the last good record. A local storage failure after a successful reply is reported separately and never retries the remote send.

Other extensions can declare `requires=("archive",)` and use the shared service:

```python
archive = context.api.get("archive")
conversations = archive.conversations("chatgpt/default", limit=50)
messages = archive.messages("chatgpt/default", "remote-chat-id", limit=100)
resources = archive.resources("chatgpt/default", "conversation", "remote-chat-id")
```

`extensions/archive/repository.py` defines the replaceable `Repository` contract and SQLite adapter. `sync.py` owns scheduling and memory limits; `adapters.py` contains provider-specific normalization. A provider mapping can be replaced through `archive.register_mapping(provider_id, mapping)` without changing the other providers. Native network parsing stays in the independent provider library.

## Gemini notebooks

Gemini notebooks appear beside ChatGPT and Claude projects, with their own `notebook` type and provider/account identity. Their names, instructions, resource metadata and chat lists use the saved Gemini session over HTTP. No separate NotebookLM login is required. This follows [Gemini's notebook workflow](https://support.google.com/gemini/answer/16972047?hl=en).

Choose a Gemini model, select a notebook beside the effort selector or through `/project`, and send a message. Notebook context is sent as native notebook metadata; source text is not silently copied into an ordinary prompt. Earlier saved notebook URL identities remain compatible. Creation, deletion, renaming, instructions and text-source addition/removal have direct HTTP implementations. File/URL notebook source import is not currently offered by this transport. Moving an existing conversation into a notebook remains unsupported.

Models come from the signed-in account's SDK catalog. The HTTP transport exposes Standard/Extended thinking, rather than inventing equivalents for every website-only control. Accessible published thinking summaries remain separate from answer text. A request stops after 120 seconds without an event or 10 minutes overall. After an ambiguous failure, synchronize the conversation before resending; SiopeChat does not automatically submit the prompt again.

Gemini credentials are read from the selected account's private file. SDK-wide cookie discovery and guest fallback are disabled. Rotated cookies are saved atomically to that same account. Temporary conversation context lives in memory and is discarded when forgotten.

## Adding an extension

Create `extensions/<id>/plugin.py`. The directory name must match `SPEC.id`. SiopeChat discovers local extensions at startup; no marketplace or installation service is required.

```python
from extension import Action, ExtensionSpec, SettingField


def activate(context):
    context.settings(SettingField("enabled", bool, True, "notes.enabled"))
    context.translations("en", {"enabled": "Notes"})
    context.action(Action("notes.hello", "Say hello", lambda: {"message": "Hello"}))


SPEC = ExtensionSpec("notes", activate)
```

Enable or disable it in Settings → Extensions. Its saved configuration remains under `extensions.notes` when disabled.

Extensions can contribute actions, view factories, settings fields, translations, themes and named services. `context.api.get(name)` resolves a shared service; `context.container` is an owned scope. Use `context.subscribe(...)` for subscriptions and `context.on_dispose(...)` for cleanup. Returned async cleanup is handled during asynchronous disablement and shutdown. Declare dependencies with `ExtensionSpec(..., requires=("other",))`.

An action requiring input can expose it to the command palette:

```python
context.action(
    Action(
        "notes.create",
        "Create a note",
        lambda name: {"name": name},
        parameters=({"key": "name", "label": "Note name", "required": True},),
    )
)
```

Use relative imports for helper modules beside `plugin.py`. Keep network work out of render functions and keep event handlers short. Extensions and Python themes execute trusted local code; only load code you intend to run.

The bundled `extensions/archive/` package supplies the local archive; `extensions/layout/` supplies split layouts and workspace metadata. Neither provider adapter imports these extensions or the terminal frontend.

## Creating a theme

Place a JSON file in `themes/` and select its `id` in Settings → General → Theme:

```json
{
  "id": "my-theme",
  "tokens": {
    "background": "#202020",
    "foreground": "#dedede",
    "accent": "#b7a3e3",
    "model": "#f0ad66",
    "effort": "#86d49b",
    "project": "#75b5ef"
  },
  "styles": {"tab.active": "bold #b7a3e3"},
  "markdown": {"markdown.link": "underline #75b5ef"},
  "options": {"sidebar_width": 28, "message_width": 100, "composer_max_lines": 6, "tab_width": 26}
}
```

Tokens also cover surfaces, borders, muted text, user/assistant/reasoning backgrounds and their markers. Colors use six-digit hexadecimal values. Terminal styles use Prompt Toolkit syntax; Markdown styles use Rich syntax. Unspecified values inherit the default theme.

For computed colors, use `themes/my_theme.py` and export the same dictionary as `THEME`. The included [Graphite theme](themes/graphite.py) is an example. Extensions can register themes with `context.theme(id, tokens, styles=..., markdown=..., options=...)`.

| Layout option | Range |
| --- | --- |
| `sidebar_width` | 16–40 cells |
| `message_width` | 40–160 cells |
| `composer_max_lines` | 3–16 lines |
| `tab_width` | 12–40 cells |

Invalid optional themes are recorded as failures; invalid frontend styles fall back to the default appearance. Shared semantic colors live in `src/theme.py`; terminal-specific defaults live in `src/cli/styles.py`.

## Architecture and development

```text
src/
  providers/     independent provider contracts, HTTP adapters and login helpers
  accounts.py    provider/account routing, preferences and archive integration
  actions.py     interface-neutral action registry
  views.py       interface-neutral view registry
  settings.py    validated configuration and field catalog
  extension.py   extension contributions and lifecycle
  runtime.py     shared container and service lifecycle
  tasks.py       streaming jobs and observers
  theme.py       semantic theme registry and loading
  language.py    merged language catalogs
  cli/           terminal widgets, styles, commands and keyboard handling
  utils/         common paths, clipboard and vendored container/event code
extensions/      local feature packages
themes/          optional JSON and Python themes
```

`create_application()` composes the shared services without importing `cli`. The terminal is one consumer of those services. A future GUI can use the same accounts, providers, actions and extension lifecycle; no GUI is included yet. The provider package has no application, terminal or database imports and can be reused independently.

Run the checks using the existing environment:

```sh
PYTHONPATH=src python/bin/python -m unittest discover -s tests -q
node --test tests/js/*.test.cjs
uvx --from 'ruff>=0.11,<1' ruff check .
uvx --from 'ruff>=0.11,<1' ruff format --check .
```

Development currently stays under **Unreleased**. [CHANGELOG.md](CHANGELOG.md) follows Keep a Changelog; numbered releases will use Semantic Versioning. Package version `0.0.0` is a development placeholder.

### Current limits

Web endpoints can change independently of this project. Direct generation has been checked with real ChatGPT, Gemini and Claude sessions; Gemini notebook source context and project CRUD/text-source operations have also been checked with disposable records. These checks do not validate every model, account tier, file format or Windows/macOS execution. A model advertised by an account catalog can still be rejected by the generation service; that failure is reported and the prompt is not silently sent to another model. Gemini file/URL notebook source import, moving existing Gemini chats into notebooks, bulk remote file downloads, archive transfer and complete localization remain unfinished.

## License and credits

Copyright (C) 2026 **TahsinCr**. SiopeChat is licensed under [GNU GPL version 3](LICENSE), SPDX `GPL-3.0-only`.

ChatGPT transport work originates from [tritao/chatgpt-web](https://github.com/tritao/chatgpt-web). Gemini uses [gemini-webapi](https://github.com/HanaokaYuzu/Gemini-API). The container and event modules are vendored from [vehicle-autonomy-core](https://github.com/TahsinCr/vehicle-autonomy-core/tree/2bac9cd5b1cbeae82b7d3beef670a37d7ee5caa2); their original notices and license remain in `src/utils/NOTICE` and `src/utils/LICENSE`. Third-party authors retain their copyrights.

### Delete chats and projects

Use **Chat: Delete** or **Projects: Delete** in the command palette, or `/delete [ID]` and `/project-delete [ID]`. The default shortcuts are Alt+D and Alt+Delete. Both operations ask **“Kalıcı olarak silinsin mi?”** with **Evet / Hayır** buttons; **Hayır** is focused initially. Without an ID, `/project-delete` opens a searchable picker containing projects from all providers. The old `/delete-project` spelling remains an alias. Conversation deletion is also available beside the composer controls; project details have a **Sil** button. Project deletion follows the provider’s behavior for its contained chats. Only delete projects you own and no longer need.

All configured providers use their authenticated HTTP sessions for deletion. Deletion happens on the provider first, followed by bounded server readback. Only reads are retried; the delete request is submitted once. A missing delete route or a network error does not count as confirmed absence. If confirmation fails, the local record is retained. Confirmed deletion removes cached list references and normalized archive rows and stores a persistent tombstone, preventing an older in-flight sync from restoring the record. Provider failures preserve the archive. Shared content-addressed files are retained rather than removed blindly. Unsupported or unauthorized operations return an explicit error.

### Open saved conversations immediately

Previously opened chats display their archived messages immediately and revalidate in the background. Cold chats can accept typing and sends once their remote ID is known; the provider resolves any parent/context it needs before submitting. The status bar and chat synchronization indicator show background work. A late history load never replaces a draft or a streaming answer.

Remote history is merged with locally accepted turns: missing earlier messages are inserted, matching messages are deduplicated, and local attachment paths and available thinking summaries are retained. Confirmed remote absence is distinguished from network failures. An incomplete/paginated listing is not proof that a conversation was deleted.

### Background execution and synchronization

Cached data remains readable while remote records are checked by at most three archive workers, with at most 32 outstanding records and one fetch per record. Failed checks wait 60 seconds before an automatic retry; explicit refresh bypasses that delay. Refresh runs outside the terminal UI thread. Closing the archive cancels queued work and lets running reads finish before releasing the repository.

Sends from one account are queued through the local save to preserve message order, including when two tabs open the same conversation. Different accounts can progress independently. Transcript copies, attachment storage and catalog snapshot writes run outside the common provider event loop. Shutdown drains accepted-response writes before unloading archive extensions. A storage failure is reported separately from successful provider delivery.

The hot cache stores immutable JSON snapshots within its byte budget; returned records remain independent mutable copies. These limits bound local work and do not change provider quotas or guarantee remote response times.

### Synchronization controls

**F10**, `/refresh` and `/sync` use one synchronization operation. The sidebar `↻` refreshes the library; `↻ Eşitle` above the transcript refreshes that conversation. `/sync library` and `/sync conversation` select those scopes. Startup restores cached models and records, then revalidates through saved HTTP sessions. `!` means the last background check failed; saved content remains visible and a successful synchronization clears the indicator.

### Claude

Add **Claude** in Settings → Accounts, sign in through **Connected browser**, then choose **Verify**. The account session is saved privately, outside configuration. Generation, history and projects then use HTTPS directly.

Requests retain the selected website workspace (`lastActiveOrg`). An inaccessible selection or ambiguous account produces an explicit error. Model/thinking choices come from that account’s authoritative model selector, including plan and workspace restrictions. Disabled and upgrade-required entries are excluded. Effort levels and thinking modes retain their native choices, including combinations when both controls are available. The server can still reject a listed model. Published thinking blocks remain separate from final answers, and an incomplete stream is an error. Requests are never automatically resubmitted. Chat attachments, temporary Claude chats and privacy/memory overrides beyond private/default remain unsupported.

The provider supports chat/history, rename/delete, projects and instruction/source metadata, private project creation/deletion and project membership changes. HTTP project CRUD, text sources and project file upload/removal have been checked with disposable records. No terminal or archive modules are imported by the adapter.

### Project editing API

The independent `providers` package exposes `project_options(project_id=None)`,
`update_project(project)`, `add_project_resource(project_id, kind, **values)` and
`remove_project_resource(project_id, resource_id)`. `ProjectOptions` describes
available fields, creation choices and source kinds. Each adapter owns its native
controls; the shared layer verifies writes with a fresh server read without
replaying mutations. A failed source component does not disable name/instruction
editing. Resource IDs retain provider/account ownership, and local file paths
survive archive revalidation only while the corresponding remote source exists.

Disabling the archive extension retains its files and SQLite data. Conversation
loading and sending continue through the provider; enabling the extension again
creates a fresh synchronization service. Local archive failures after an accepted
remote response or project edit are reported separately from the remote result.



### Synchronization while replying

A refreshed history is reconciled with locally accepted turns before updating the
transcript. Message identities and pending markers survive display conversion;
late snapshots cannot discard a new response or its published thinking summary.
Accepted turns are saved atomically with any history loaded in the meantime.
Disabling the archive uses the same display reconciliation without importing the
extension into the UI.


### Published thinking

Only provider-published thinking text is displayed. Hidden messages and tool-directed protocol nodes are excluded. ChatGPT snapshots can provide summaries absent from stream deltas; Gemini history retains published summaries and native message IDs; Claude streams explicit thinking blocks. A model that does not publish a summary has no thinking block.

### Opening saved conversations

Saved histories are read and formatted on a worker, including Markdown and ANSI
conversion. The composer remains usable while a known conversation loads. The
initial prepared transcript is reused for painting and recent tab switches, with
a bounded character budget; superseded loads stop preparing their old transcript.
Live archive polling also gathers its storage snapshot on a worker, so a slow
SQLite read cannot stall keyboard input. Navigation pointer writes are coalesced
in an ordered background queue and drained on shutdown.

### Large-history terminal rendering

Opening several saved histories does not parse or lay out their Markdown on the
terminal event loop. A single background renderer prepares styled rows; the
terminal paints visible rows and handles clicks without rebuilding the complete
history. Preparations for inactive tabs are skipped while their loaded messages
remain available. Prepared views use an LRU character budget; a single oversized
active view is retained so long histories remain readable. Switching tabs,
scrolling and background archive updates keep their existing state. Message
selection follows the same message when older history is inserted.

### Library filters and ordering

Filter, project-provider and deletion forms share a responsive, scrollable body. Their choices remain visible as the terminal is resized.

The filter button beside sidebar search opens a shared account checklist and sort
selector, including when only one provider is configured. Each account is labeled
with its provider. Choose newest/oldest first or A–Z/Z–A; the same order applies to
projects, project children and unassigned conversations. Entries without a native
date follow dated entries. Gemini chat and notebook timestamps are normalized by
the provider adapter rather than guessed by the terminal UI.

The library can browse multiple configured accounts without switching the active
sending account. Only the selected account for each provider can send messages;
viewing a different account's archive does not silently activate it. Filters are
session-local and apply to the sidebar, independently of the draft's model and
project picker.

The **⊗** button at the far right of the tab bar closes all idle tabs and leaves one empty draft. Use **Alt+W**, `/tabs-close`, or **Tüm sekmeleri kapat** in the command palette for the same action. Running responses, loads and uploads must finish first. Closing tabs does not delete saved conversations.

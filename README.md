# SiopeChat

A terminal workspace for AI conversations, built around reusable web-session providers.

Keep conversations in tabs, switch accounts without mixing their history, and choose the models available to each account. ChatGPT and Gemini are included. A shared service and extension API keeps the provider layer independent of the terminal and leaves room for other frontends.

![SiopeChat terminal workspace](assets/siopechat.png)

*An actual terminal capture using the Graphite theme and demonstration data. No personal conversations are shown.*

## Getting started

Requirements:

- Python **3.10+** for ChatGPT, **3.11+** for Gemini.
- Node.js **22+** for browser-session helpers.
- Chrome or Chromium for connecting a login session.
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

Open **Settings → Accounts**, add or select an account, then choose **Connected browser**. Sign in on the provider website and return to **Verify**. A valid saved session can be reused without signing in again. Opening the default browser does not import its login session into SiopeChat.

This project uses unofficial web-session transports. It requires your own provider accounts and remains subject to their access and usage limits. It does not provide unlimited access or guarantee compatibility with future website changes.

## Working with conversations

- **Tabs:** keep drafts, scroll positions and conversation state separate. Active replies and replies completed in another tab have distinct indicators.
- **Sidebar:** search loaded chats, expand projects and load older pages when needed. ChatGPT and Gemini conversations have provider icons; a multi-select filter appears when multiple providers are configured.
- **Models and effort:** lists come from the account, rather than a hardcoded version list. The last model and effort selection is remembered per account and reused in new chats, including an explicit provider-default effort choice. An obsolete choice falls back to an available model.
- **Projects:** ChatGPT project chats, instructions and resource metadata are available in the sidebar. `/project` moves the current chat or assigns a new draft; **Unassigned** removes membership. `/project-new` opens the creation form.
- **Files:** use the attachment command or button to choose a file. This client limits each file to 20 MiB; provider restrictions still apply.
- **Temporary chats:** `/temporary` uses the provider's temporary/history-disabled request. These chats do not write local conversation snapshots; provider retention policies still apply.

With the layout extension enabled, choose **Split**, `/split` or `/split-down`, then select another chat from the sidebar. Up to four panes share one tab group. Click a pane heading to make it active; `/unsplit` returns to a single pane. Saved workspace metadata is available, but saving and restoring the new live split groups is still pending.

Up to four tabs can have pending requests. Operations within one account are queued; separate account instances can progress independently. Model catalogs use a five-minute memory cache and an account-local disk snapshot. Refresh failures retain the last usable catalog instead of clearing the picker.

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
| `F10` | Refresh provider data |
| `Ctrl+D` / `Ctrl+Q` | Exit; repeat within three seconds if a draft or reply is open |

Shift+Enter adds a line when the terminal sends a distinct modified-Enter sequence. Alt+Enter and Ctrl+J are alternatives. The composer grows before scrolling. Ctrl+A/C/X/V select, copy, cut and paste; selecting a message lets you copy its original text. In search and settings editors, Ctrl+D retains its text-editing meaning.

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

Account preferences live under `accounts.preferences`. Credentials are kept separately from configuration. New accounts use isolated directories under `data/accounts/`; the original ChatGPT account retains its legacy login paths. Conversation cache fallback is restricted to that original account, so an empty Gemini workspace cannot reopen a ChatGPT record.

Local logout forgets the selected application session. It does not sign out the provider website or delete saved conversation files. `data/`, login profiles, environments and local agent memory are excluded from Git.

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
  providers/     independent provider contracts, adapters and browser helpers
  accounts.py    account routing, provider state, preferences and discovery cache
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

Web endpoints can change independently of this project. Gemini discovery and a temporary Flash-Lite request have been checked against a real account through controlled terminal input; that does not validate every model or desktop terminal. ChatGPT project writes, file compatibility and Windows/macOS execution still need live platform checks. Project sharing is rejected before creation when the native form does not offer it. NotebookLM synchronization, archive transfer and complete localization remain unfinished.

## License and credits

Copyright (C) 2026 **TahsinCr**. SiopeChat is licensed under [GNU GPL version 3](LICENSE), SPDX `GPL-3.0-only`.

ChatGPT transport work originates from [tritao/chatgpt-web](https://github.com/tritao/chatgpt-web). Gemini uses [gemini-webapi](https://github.com/HanaokaYuzu/Gemini-API). The container and event modules are vendored from [vehicle-autonomy-core](https://github.com/TahsinCr/vehicle-autonomy-core/tree/2bac9cd5b1cbeae82b7d3beef670a37d7ee5caa2); their original notices and license remain in `src/utils/NOTICE` and `src/utils/LICENSE`. Third-party authors retain their copyrights.

# Changelog

All notable changes to SiopeChat are documented here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
Numbered releases will follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).
The project is currently unreleased; no release dates or tags have been assigned.

## Unreleased

### Added

- Optional SQLite archive extension with provider/account isolation, ordered message/content records, projects/notebooks, memberships, resource paths and synchronization state.
- Replaceable archive repository and provider mapping interfaces, content-addressed file storage and paginated data access for other extensions.
- Bounded hot cache, single-flight background revalidation, explicit refresh and synchronization indicators.
- One selected account per provider and a combined model picker for new chats across enabled providers.

- Native Gemini notebook listing, details, instruction/resource metadata and notebook chat lists through the same account browser; retain notebook-specific metadata separately from ChatGPT projects.

- Independent ChatGPT and Gemini web-session adapters with account-scoped model discovery, history and streaming.
- Multiple accounts, isolated browser profiles, session verification, local logout and mixed-provider sidebar filtering.
- Tabbed conversations, visible background reply status, project navigation and command/action search.
- Optional layout extension with side-by-side or stacked panes, grouped tabs and workspace metadata storage.
- Account-specific remembered model and effort selections across new tabs and restarts.
- Provider enable/disable controls with a minimum of one enabled provider and configured account.
- File attachment forms, temporary chats and native ChatGPT project creation with memory/privacy fields.
- Shared extension lifecycle, automatic local extension discovery and argument forms for contributed actions.
- JSON and Python themes with semantic tokens, terminal/Markdown overrides and bounded layout options.
- Linux/macOS shell, macOS Finder `.command`, Windows CMD and PowerShell launchers sharing Python startup.
- GPL-3.0-only license, usage and extension/theme documentation, and an actual terminal screenshot using sample data.

### Changed

- Use a white ChatGPT rosette and light-blue Gemini icon while keeping conversation text consistent; render open chat labels in bold pure white.
- Display project names without qualified account/remote IDs in navigation and project headings.

- Keep small shared modules directly under `src/`; place terminal commands, styles and widgets under `src/cli`.
- Separate the shared view registry and settings catalog from the terminal frontend for future GUI reuse.
- Cache account catalogs in memory for five minutes and restore account-tagged snapshots before live discovery completes.
- Coalesce simultaneous discovery requests and bound model, history and host read operations.
- Keep settings Save/Close controls at the bottom right; adapt categories and account actions to narrow screens.
- Use one canonical terminal action catalog for shortcuts, command search and shortcut settings.

### Removed

- The global default-provider selector; providers are enabled independently and conversations retain their account identity.
- Application-name package wrappers, the extra `.venv` setup path and `requirements.txt` management.
- Competing settings-page filler, repeated shortcut hints and SDK logs written over the terminal interface.

### Fixed

- Revalidate the restored active conversation at startup without clearing its visible history or draft.

- Reopening a saved chat waiting for the entire remote history before displaying it.
- Cold history loads blocking tab creation/navigation; late load results now update their owning tab only.
- First history Markdown rendering blocking the UI and cached renders being discarded on reopening.
- Old account rows remaining visible after per-provider account selection changes.
- Late background UI callbacks accessing a closed application container.
- Manual refresh waiting on failed records without a cached value.
- Local archive write failures making an already completed reply appear to have failed remotely.

- Preserve an explicit provider-default effort choice as an absent request override after restore and refresh.

- Empty selected models preventing sends despite an available catalog.
- New tabs losing their provider catalog or model/effort context.
- Gemini and additional accounts reopening legacy ChatGPT conversation files.
- Stuck refresh indicators and late account results overwriting newer request state.
- A slow account blocking successful history results from other accounts.
- Shift+Enter handling, composer focus restoration and accumulated mouse-wheel scrolling.
- Ctrl+D silently doing nothing with a draft; exit now offers a timed repeat confirmation.
- Command search and Enter execution from settings, with editing keys retained in search fields.
- Tab widths, right-aligned tab controls and settings scaling down to small terminal dimensions.
- Background streams clearing another tab's activity, cancellation connection or elapsed time.
- Reasoning from later turns modifying earlier reasoning messages.
- Re-rendering old messages unnecessarily in busy inactive panes.
- Explicit CDP URL overrides being lost in account routing.

### Security

- Keep credentials out of configuration and isolate account caches and browser profiles.
- Restrict browser helpers to local debugging endpoints and the expected provider origin.
- Avoid local conversation snapshots for temporary chats and reject unsupported temporary transport fallbacks.
- Fail unsupported public project creation before submission and validate native project writes by reading them back.
- Preserve third-party copyright notices and treat local Python themes/extensions as trusted executable code.

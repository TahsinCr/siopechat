# Changelog

All notable changes to SiopeChat are documented here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
Numbered releases will follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).
The project is currently unreleased; no release dates or tags have been assigned.

## Unreleased

### Added

- Add a clickable temporary-chat control beside the project selector for providers that support temporary chats.

- Independent Claude web-session provider with native model/effort discovery, chat history and generation, published thinking summaries, chat rename/deletion, projects, instruction/source metadata, private project creation/deletion and verified project membership changes.
- Explicit cross-provider browser-profile sharing with independent account/verification/archive records and a shared launch lock.
- Shared bounded browser transport, page handling and Markdown renderer; isolated Claude catalog, composer and record components.

- One synchronization operation with conversation/library scopes, clickable controls, F10 and `/sync` (alias of `/refresh`).

- Confirmed chat/project deletion through provider APIs and the command palette, `/delete` / `/delete-project`, and configurable shortcuts.
- Persistent archive tombstones with transactional list-reference and project-member cleanup, including protection against late sync resurrection.

- Deterministic synchronization/concurrency regressions covering slow storage, stale reads, bounded work queues, independent accounts and shutdown during accepted-response persistence.

- Isolated Gemini native-browser sending component with owned-tab lifecycle, attachment upload, incremental Markdown extraction and bounded cancellation.

- Gemini notebook-context chat requests, including compatibility with earlier saved Gemini notebook URL identities.

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

- Distinct provider icons: white ChatGPT ring, light-blue Gemini star and terracotta Claude asterisk. F8 and the chat synchronization button use the same scoped synchronization operation as F10.

- Store hot-cache records as immutable JSON snapshots to reduce copy cost and keep cache memory accounting tied to stored bytes.

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

- Preserve legacy Claude binary thinking controls alongside current effort-level menus, with failures isolated to the affected model feature.
- Open the requested provider login page when reusing an already running connected browser.
- Wait for the new Claude document and fully closed menus before reading model/effort controls; ignore fading choices and use native submenu keyboard navigation.

- Gemini history/list synchronization falsely requiring login because reads used a stale SDK cookie session while sends used the authenticated browser.
- Passive startup opening account browsers to refresh cached catalogs and histories; closed profiles now remain closed until sending or explicit synchronization.

- Display cached conversation bodies immediately and hydrate cold histories without blocking drafts or replacing a running response.
- Merge missing remote history with locally accepted turns, preserving attachment paths and published thinking summaries without duplicating confirmed messages.
- Refresh Gemini conversation metadata from the server instead of reusing the SDK's initial in-memory list; paginate recent conversations.
- Use the connected Gemini browser session for chat deletion and allow for notebook-list propagation after native deletion without repeating the destructive request.
- Reconcile complete sidebar snapshots and distinguish confirmed missing records from transport failures.

- Prevent stale cold reads from replacing newer local records or returning an obsolete remote result after a completed send.
- Move transcript/attachment persistence, model snapshot writes and manual archive refresh away from provider/UI event loops.
- Serialize sends through persistence per account, while letting different accounts start browsers and stream independently.
- Throttle failed revalidation, preserve worker/queue bounds during refresh bursts, and close repositories only after running reads finish.
- Drain accepted-response writes before shutting down archive extensions; prevent closed account services from restarting their background loop.

- Keep verified account links across browser closure and application restart; display browser connectivity separately and revoke verification only on local logout or an explicit authentication rejection.
- Preserve archived notebook/project association when continuing a chat without repeating its project option, and accept reselecting the current project without attempting an unsupported move.

- Recover closed account browsers from their saved profiles before sending; Gemini model and notebook reads share the same serialized startup. Never replay a submitted prompt.
- Account indicators check the live local browser connection instead of treating cached model lists as authentication. Clarify disconnected Gemini transport errors.

- Gemini generation hanging on the legacy `StreamGenerate` endpoint after the native frontend switched this account to `processSession`; application sends now use the native browser composer with no automatic SDK resend.
- Gemini model/thinking choices being taken from the SDK's older boolean option: IDs and labels now come from native menus, with selection checked before sending.
- Native composer input being submitted before its send button was ready, background-page rendering, and model verification failing when the header also shows an effort label.
- Temporary Gemini context now remains in its own managed browser tab, supports follow-ups, and closes that tab when the application forgets the conversation.

- Sidebar projects disappearing when switching model accounts: the sidebar lists providers together; only the conversation project picker is scoped to the model account.

- Gemini notebook sends now include native notebook context settings alongside the notebook identity; an identity passed as a Gem alone did not establish notebook context.
- Expired Gemini sessions silently falling back to an unauthenticated guest account and exposing an incomplete model catalog.
- The send status remaining at “Preparing secure request” while waiting for provider output.
- Unbounded Gemini stream waits: idle cancellation after 120 seconds and a 10-minute overall deadline, with explicit guidance before retrying.
- Notebook transport compatibility now handles SDK endpoint enums, keeps context across reconnects, and restores request scope on cancellation or failure.

- Valid Gemini notebook chats being rejected with `projects: unsupported` before sending.

- Wrapped terminal controls crashing with an out-of-range cursor after content shrinks or project lists change.
- Conversation project pickers offering projects from another model account.

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

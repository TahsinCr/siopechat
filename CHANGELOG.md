# Changelog

All notable changes to SiopeChat are documented here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
Numbered releases will follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).
The project is currently unreleased; no release dates or tags have been assigned.

## Unreleased

### Added

- Direct Yes/No permanent-deletion confirmations, conversation composer and project-details delete buttons, and `/project-delete` with a searchable cross-provider project picker.
- Close-all-tabs control at the far right of the tab bar, `/tabs-close`, and configurable Alt+W action; active work is protected and temporary-chat cleanup keeps account ownership.

- Account-scoped HTTPS transports for Claude and Gemini, with reusable connections, private atomic session persistence and no browser dependency after explicit login verification.
- Direct HTTP project creation, editing and source management for ChatGPT and Claude; Gemini notebook CRUD, instructions and text sources through account-owned RPCs.
- Regression tests for origin restrictions, authentication versus access errors, mutation non-replay, session rotation, guest rejection and notebook request mappings.

- Sidebar account filters labeled with provider names, plus newest/oldest and ascending/descending name ordering shared by projects and conversations. Browsing another configured account does not change the active sending account.

- Provider-scoped project creation and editing forms, multiline instructions and dynamically discovered native source types for ChatGPT, Claude and Gemini notebooks.
- Project edit/source actions in details, commands, configurable shortcuts and the command palette; long shared forms scroll with accessible save/cancel controls.
- Frontend-independent project editing API with exact resource ownership, file size validation and server readback after each native write.

- Add a temporary-chat toggle beside the project selector, with explicit on/off and unsupported states. Toggle a draft in place before its first message, preserving typed text and clearing incompatible project membership.

- Independent Claude web-session provider with native model/effort discovery, chat history and generation, published thinking summaries, chat rename/deletion, projects, instruction/source metadata, private project creation/deletion and verified project membership changes.

- One synchronization operation with conversation/library scopes, clickable controls, F10 and `/sync` (alias of `/refresh`).

- Confirmed chat/project deletion through provider APIs and the command palette, `/delete` / `/delete-project`, and configurable shortcuts.
- Persistent archive tombstones with transactional list-reference and project-member cleanup, including protection against late sync resurrection.

- Deterministic synchronization/concurrency regressions covering slow storage, stale reads, bounded work queues, independent accounts and shutdown during accepted-response persistence.


- Gemini notebook-context chat requests, including compatibility with earlier saved Gemini notebook URL identities.

- Optional SQLite archive extension with provider/account isolation, ordered message/content records, projects/notebooks, memberships, resource paths and synchronization state.
- Replaceable archive repository and provider mapping interfaces, content-addressed file storage and paginated data access for other extensions.
- Bounded hot cache, single-flight background revalidation, explicit refresh and synchronization indicators.
- One selected account per provider and a combined model picker for new chats across enabled providers.


- Independent ChatGPT and Gemini web-session adapters with account-scoped model discovery, history and streaming.
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

- Use the existing direct ChatGPT HTTP/signing path for generation; remove browser prerequisites and automatic browser/recipe fallbacks from normal account operations.
- Bootstrap Gemini exclusively from its selected account file, without global SDK cookie discovery or guest fallback. Preserve Standard/Extended HTTP thinking choices and published summaries.
- Read Gemini history over the account HTTP session independently of the generation lock; preserve native message IDs and metadata.
- Require explicit re-verification only for expired/revoked sessions, not a closed login window or a transient transport error.

- Open project name/instruction editing inline on the existing project tab; use the shared project picker for `/project-edit`, retain the creation popup, and avoid focusing editors after switching to another tab.

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

- Inline project editors fill the available conversation panel width and resize with the terminal instead of collapsing to their labels and buttons.

- Require server readback after ChatGPT, Gemini and Claude conversation deletion before purging local archives; retry verification reads without replaying deletion.
- Distinguish a missing mutation route from a missing record, and reject missing or malformed Gemini RPC acknowledgements instead of reporting false success.

- Use Claude’s account-entitled model selector instead of global bootstrap definitions, excluding plan-locked models and retaining dynamic effort/mode settings.
- Invalidate catalog snapshots from before entitlement-aware discovery instead of restoring unavailable model choices after an upgrade.

- Restore visible filter, project-provider and deletion choices by giving shared form viewports a bounded preferred height; preserve checkbox account IDs on submission.
- Keep active tab and split-group colors from being overridden by the generic open-tab text style.

- Delete Claude project files using their native file identities and the file-deletion endpoint, while keeping document deletion separate.

- Send Claude thinking choices through `thinking_mode` rather than the rejected `paprika_mode` field. Keep model selection on completion requests rather than the empty-conversation creation call.
- Bind Gemini conversation identities as soon as streaming exposes them, retaining the correct remote identity if a later response fails.

- Isolate alternate application roots from the real ChatGPT credential/profile paths, preventing temporary application tests from touching a user's session.
- Include existing display and memory fields when editing ChatGPT projects, avoiding HTTP 422 responses.
- Use Gemini's notebook write-settings envelope rather than its display-icon metadata when saving instructions.
- Keep successful remote operations successful when only local rotated-session persistence fails.

- Exclude disabled providers from both cached sidebar project and conversation rows, and limit each synchronization request to its target account.

- Honor Claude’s selected native workspace for chat and project requests; reject ambiguous or inaccessible workspace selection instead of silently reading the first organization.
- Observe shared model-discovery task failures after a tab cancels its waiter, preventing unhandled background task errors.

- Keep message selection attached to its original message when older history is inserted, remove the obsolete full-fragment painter, and retain a single oversized prepared transcript instead of repeatedly displaying its loading placeholder.

- Remove full-history terminal wrapping and per-fragment click-handler construction from conversation paints. Prepare Markdown and indexed rows in one background renderer, skip obsolete inactive views and paint only requested rows, including streaming updates.

- Move saved-history reads, Markdown/ANSI preparation and live archive snapshot reads off the UI thread; avoid synchronous cache reads on conversation selection and hydration callbacks.
- Reuse complete prepared transcripts across initial paint and recent tab switches, with bounded storage; cancel preparation for superseded selections.
- Coalesce navigation pointer writes in a background persistence queue and drain them on shutdown instead of blocking tab changes on disk writes.
- Add blocking-storage, worker-rendering, tab-return and shutdown regressions for conversation opening.

- Exclude hidden/tool-directed ChatGPT completion blocks and scan prior message identities once, keeping summary reconciliation linear for large histories.

- Separate Gemini composer and history/notebook reader tabs and locks; drain read/catalog helpers, recover disconnected reads once, and keep transient transport errors separate from authentication loss.
- Reject pending CDP operations promptly on disconnect and bound debugger connection startup.
- Publish ChatGPT completion thinking summaries, preserve nonempty Gemini summaries, and stream available Claude thinking blocks separately.

- Reconcile late history snapshots with accepted visible turns instead of replacing the transcript; retain message identity, pending replies and published thinking during synchronization.
- Atomically save accepted turns alongside history hydrated during persistence; share provider-neutral history reconciliation between UI and optional archive.

- Activate the hit-tested ChatGPT project deletion control directly, avoiding confirmation focus races during native dialog mounting.

- Do not reject a successful ChatGPT authentication capture when the debugger reports an error during intentional socket shutdown; create projects in an owned tab rather than a potentially busy user tab.
- Allow native Gemini notebook reads to finish within their network timeout, and confirm void source-deletion replies by reading the exact source identity back from the server.

- Keep accepted project edits successful when only local archiving fails; preserve uploaded source paths across revalidation without resurrecting removed resources.
- Read Gemini custom instructions from their native instruction field rather than the notebook description; remove Gemini sources by exact native identity instead of truncated display names.
- Apply background history reads with the archive disabled and refresh cached synchronization state when the extension is replaced.

- Preserve the conversation's provider/account when restoring cached or cold histories; use that account's remembered model and effort rather than overwriting selection with the last response model.
- Keep message sending independent of unrelated background jobs, block only a pending membership change in the same tab, and reject unsupported temporary drafts before clearing their text.
- Hydrate cached histories even with the archive disabled; publish the completed synchronization state after applying the history and recognize replacement archive instances after re-enabling the extension.

- Preserve legacy Claude binary thinking controls alongside current effort-level menus, with failures isolated to the affected model feature.


- Display cached conversation bodies immediately and hydrate cold histories without blocking drafts or replacing a running response.
- Merge missing remote history with locally accepted turns, preserving attachment paths and published thinking summaries without duplicating confirmed messages.
- Refresh Gemini conversation metadata from the server instead of reusing the SDK's initial in-memory list; paginate recent conversations.
- Reconcile complete sidebar snapshots and distinguish confirmed missing records from transport failures.

- Prevent stale cold reads from replacing newer local records or returning an obsolete remote result after a completed send.
- Move transcript/attachment persistence, model snapshot writes and manual archive refresh away from provider/UI event loops.
- Throttle failed revalidation, preserve worker/queue bounds during refresh bursts, and close repositories only after running reads finish.
- Drain accepted-response writes before shutting down archive extensions; prevent closed account services from restarting their background loop.

- Preserve archived notebook/project association when continuing a chat without repeating its project option, and accept reselecting the current project without attempting an unsupported move.



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

- Avoid local conversation snapshots for temporary chats and reject unsupported temporary transport fallbacks.
- Fail unsupported public project creation before submission and validate native project writes by reading them back.
- Preserve third-party copyright notices and treat local Python themes/extensions as trusted executable code.

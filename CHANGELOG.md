# Changelog

All notable changes to SiopeChat are documented here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
Release versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html);
Python distributions use the corresponding PEP 440 version (`1.3b0` for `v1.3-beta`).

## [Unreleased]

### Added

- Show a stop button in place of Send while the active chat is generating; pressing Esc twice within three seconds also stops it, with an inline status hint after the first press.

### Changed

- Prioritize the visible transcript before rendering a two-screen buffer above and below it, keep recently visited rows in a one-megabyte LRU, and avoid rescanning the full message list on every terminal paint.

## [1.3-beta] - 2026-10-22

### Added

- Provider-aware attachment selection with a desktop file chooser, path-based fallback, size/type/count preflight and review, removal of excess files, and background uploads.
- Keep staged attachments in a shared queue across chat tabs, show the queued file count beside the composer action, and allow clearing the entire queue.
- Use the same provider-aware desktop file picker for project sources and chat attachments, with an all-files filter available on desktop platforms.

### Changed

- Bound formatted transcript memory to a one-megabyte cache, discard closed-tab render entries, and prepare only viewport pages in background workers.
- Coalesce redundant streaming scroll updates while retaining every response and reasoning fragment.
- Re-upload staged files for the destination account when a message is sent from another provider; keep queued files after failed sends and remove them only after success or explicit clearing.
- Use `uv.lock` for the cross-platform CI matrix and check upgraded dependency versions separately on Linux.
- Render only visible download-history rows and cache formatted fragments until their content changes.
- Reconcile download history with revision-guarded bulk SQLite writes, avoiding full-history rewrites on startup; check each download directory once when refreshing file status.

### Fixed

- Serialize concurrent writes to each provider session file so Windows cannot reject overlapping atomic replacements with `WinError 5`.
- Match queued file paths using normalized filesystem identities, including macOS symlink aliases and Windows path casing, so selecting an already queued file does not add it twice.
- Drain account persistence writes before stopping provider workers, and await archive workers during async shutdown so SQLite files are not left in use after runtime teardown.
- Keep expected provider failures during cancellation from turning a clean shutdown into an error.
- Clear submitted files from the composer immediately when sending starts, restore them if the send fails, and retain their local data until the provider finishes using it.
- Clear the exact attachment snapshot after a successful send even when a provider omits file IDs, while preserving files queued during that send; allow switching to existing or loading chats during background attachment uploads.
- Isolate task observer failures from provider streaming and completed-response persistence.
- Keep a provider event loop open when cleanup times out or its thread misses the shutdown deadline, allowing a later retry.
- Reject HTML login or verification pages returned in place of named downloads, while allowing explicit HTML attachments.
- Parse ChatGPT SSE `data:` fields with or without a space and join multiline event payloads while retaining the existing provider stream format.

### Security

- Prevent stale concurrent download-history snapshots from overwriting newer SQLite records.

## [1.2-beta] - 2026-10-21

### Added

- Downloads workspace between Help and Settings, with live transfer progress, searchable history, collapsible dates, status filters, sorting and links back to source chats. Open it with `/downloads` or Alt+J.
- Archive-backed download history with an atomic v1-to-v2 SQLite migration, periodic file reconciliation, interrupted-transfer recovery, and separate file deletion and history removal.
- GitHub Actions checks for Python 3.10/3.14 and Node 22/24 on Linux, Windows and macOS, including lint, tests and distribution builds.
- Save open conversation references, split direction and active pane through layout.create; restore saved arrangements from the workspace sidebar.
- Archive inspection and reference-safe, preview-first cleanup with shared-content protection and a one-hour grace period.

### Changed

- Update English, Turkish and Russian documentation for job ownership, safe downloads, extension manifests, archive maintenance and saved split workspaces; state the remaining live-platform and credential-storage boundaries explicitly.
- Use separate bounded pools for terminal reads, mutations and Markdown preparation, with queue rejection and shutdown handling.
- Publish Claude text and thinking as incremental events and count SSE event size without rescanning accumulated lines.
- Route application sends through the shared TaskService with account-specific provider factories, bounded responses, stable job identity and persistence inside the account queue.
- Keep archive listing order explicitly based on remote IDs; presentation sorting remains independent.

### Fixed

- Avoid cancelling provider-loop shutdown on Python 3.10, and keep Gemini history media parsing independent of the optional transport SDK.
- Retry transient Windows file replacement locks; normalize cross-platform paths, preserve Markdown links on Windows and use a compatible selector loop in terminal tests.
- Always fetch uncached archive records on fresh system starts, even when the monotonic clock has been running for less than a minute.
- Isolate malformed legacy IPC requests, sanitize failure responses and accept bounded concurrent clients without blocking ping behind a long send. Keep mutations serialized and temporary conversation context shared.
- Bind send activity and background completions to stable job/tab identity so closing or switching tabs cannot redirect a late result.
- Require a confirmed ChatGPT stream ending and a visible assistant channel; reject malformed or CID-only responses without resending the request.
- Handle explicitly permitted empty ChatGPT mutation responses, use real IANA timezone information, validate Gemini notebook envelopes and share session parsing errors.
- Drain cancelled sends before releasing account state, expose cancellation in progress, and preserve successful remote sends when local archival fails.
- Read back project children after deletion and tombstone only confirmed missing chats; keep surviving or uncertain histories available without the deleted project.
- Reject future archive schemas before changing their structure and flush archived content before publishing its filename.

### Security

- Discover extension metadata from manifests and defer Python imports until activation, including disabled legacy packages; retain dependency and API-version checks.
- Validate and pin download addresses, block private networks by default, reject HTTPS downgrades and preserve account/origin boundaries across redirects.
- Limit downloads to a configurable 512 MiB by default, enforce both declared and streamed sizes, and expose local-network opt-in and size settings in all three languages.
- Restrict Sentinel SDK origins, redirects, source size and fetch duration; validate cached SDK paths and limit the helper process environment.

## [1.1-beta] - 2026-10-20

### Added

- Background file downloads with a configurable General → Download folder (default: project downloads/), two concurrent transfers, streamed writes, collision-safe filenames and incomplete-file cleanup.
- Drag to select and copy part of a rendered response or thinking summary; Ctrl+A still selects the complete message.
- Provider-owned file resolution for ChatGPT sandbox links and Gemini generated images, shared with cached history and split views.

- Edit a selected account’s display name from the account toolbar or the accounts.edit action (Alt+C), preserving its saved session and history.

- Close a streaming conversation tab without interrupting its response; the sidebar marks running chats in green and completed background replies with a check mark. Reopen them without restarting the request.

- Zen profile import through the shared Firefox reader, including current XDG, legacy and Flatpak locations; Firefox Snap discovery and installs.ini profile selection.
- Confirmed local account removal, credential/preference cleanup and automatic selection of another enabled account, with an account-settings action and Alt+Backspace shortcut.
- Verify from profile in account settings: import the selected provider's session from the default browser's existing profile without launching a browser or enabling debugging flags.
- Default-profile discovery for Chrome, Chromium, Brave/Brave Origin, Edge, Vivaldi and Firefox standard locations, with localized import status and errors.

### Changed

- Render only visible sidebar rows and reuse the filtered, sorted navigation list until its source data changes.
- Keep tab rendering in cli/layout.py and library row assembly in cli/library.py; share chat-only model lookup instead of repeating it throughout the view controller.
- Share atomic JSON persistence between application settings and cached records, reuse provider session persistence for ChatGPT, and centralize project-source file validation without changing limits or provider error messages.
- Start new installations with an empty account list. Accounts are added explicitly; the first added account becomes active, and existing configurations are preserved.

- Simplify account settings to profile verification and logout, and remove the unused login-profile sharing selector from account creation.

### Removed

- Browser-launch and separate browser-verification controls from account settings; profile import is now the account login flow.
- Retired browser-based conversation and project transports plus their private scripts; the HTTP providers and login/session-signing path remain in place.
- Unused terminal rendering state and obsolete controls; shared Markdown and account-routing tests remain in place.

### Fixed

- Remove the formatted-text newline from each lazy sidebar row so it is not shown as a blue `^J` control character.
- Authenticate ChatGPT Estuary file downloads with the selected account instead of treating protected URLs as public CDN links, fixing HTTP 403 on generated files.
- Use the native streaming HTTP transport for Gemini and Claude downloads, with scoped account cookies and no credential forwarding across origins.
- Resolve Claude sandbox and computer file links through the owning conversation's file listing and download endpoint.
- Keep incomplete HTTP 206 media responses out of the downloads folder while the provider is still generating the file.

- Resolve Gemini image_generation_content placeholders from actual provider media records instead of opening placeholder URLs that return 404.
- Make plain web URLs clickable without turning code examples into links; dragging across a link selects text without opening it.

- Retry locked Firefox/Zen cookie reads from a private SQLite snapshot, preserving committed WAL data and leaving the source profile untouched.

- Include Firefox/Zen session-store cookies when importing a profile, rather than reading only persistent SQLite cookies. Read one current recovery snapshot without combining containers or old backups.

- Keep role gutters aligned with the rendered transcript snapshot when toggling F1 help or replacing history, and ignore obsolete message indices safely.
- Allow switching away from and closing the Settings tab with global shortcuts; reopening Settings reuses its existing tab, including while another conversation is running.

- Open message links with the native desktop URL launcher, ignoring a stale BROWSER override that could silently select a removed browser. Add a terminal mouse-protocol regression for rendered links.

- Make Markdown web links clickable in conversation messages, cached history and split panes; preserve link targets during ANSI rendering and scrolling.

- Apply shortcut changes immediately across previously focused controls; replaced keys no longer remain active in cached layouts.

- Allow opening other conversations and the history picker during response generation; keep synchronization independent of streaming.

- Route streaming updates to their original conversation state instead of a shifting tab index. Background responses count toward the concurrency limit.

- Align account creation and removal beneath the account list with equal button widths and shared columns; keep profile verification and logout together below session details, with compact labels in narrow terminals.

- Clear obsolete account/provider filters after local account removal so the remaining history stays visible.
- Request extended terminal key reporting so Shift+Enter inserts a newline and Enter sends, preserve modified Ctrl/Alt shortcuts, and reset keyboard modes on exit. Modified Enter no longer cancels text forms through its Escape prefix.
- Account-row clicks now update the selected row, keyboard focus and managed account together; full row width is clickable.
- Distinguish missing profile-reader dependencies, filesystem permissions, cookie database failures, keyring access and decryption errors without exposing cookie values or raw exceptions.
- Keep default-browser diagnostics out of the terminal UI while reporting launch failures.

### Security

- Scope profile imports to provider authentication cookies, exclude Firefox containers, bound keyring work, and validate candidate sessions before replacing existing account credentials.

## [1.0-beta] - 2026-10-19

First public beta.

### Added

- Independent ChatGPT, Gemini and Claude providers with account-scoped saved web sessions, model catalogs and reasoning effort selection.
- HTTP conversation, history and project operations after explicit login verification; normal operations do not require a running browser.
- A terminal workspace with separate drafts and reading positions, reply indicators, mouse controls and a searchable command palette.
- Up to four panes per split group, click-to-activate across inactive panes, group-only keyboard navigation and close-all-tabs controls.
- ChatGPT/Claude project support and Gemini notebooks with native container identity, instructions, sources and provider-specific creation options.
- New project conversations directly from the details composer, with account-scoped model/effort selectors hidden during project editing.
- Shared project creation, editing, source and permanent-deletion forms, including cross-provider project pickers and direct Yes/No confirmation.
- Account filters and date/name ordering for projects and conversations, expandable project sections and paginated history.
- Multiple account records with one selected account per provider, isolated credentials and explicit login-profile reuse.
- A SQLite archive with ordered messages, published thinking summaries, container memberships, resource metadata and content-addressed local files.
- Bounded caches and background synchronization, replaceable storage/provider mappings, and retention of the last usable data after refresh failures.
- Local extensions with shared actions, services, views, settings, translations, themes and dependency lifecycle.
- JSON and Python themes with semantic colors, Markdown styles and configurable terminal dimensions.
- English, Turkish and Russian interface catalogs, including command descriptions, project/account forms, status messages and bundled extension settings.
- English, Turkish and Russian READMEs with installation, usage, extension/theme development and provider limitations.
- Linux/macOS shell, macOS Finder, Windows batch and PowerShell launchers; Python package metadata for `1.0b0`.

### Changed

- New conversations expose models from selected enabled accounts; existing conversations remain scoped to their original account.
- Model and effort preferences are remembered per account. Provider account catalogs replace global or upgrade-only model listings where availability is reported.
- Project and conversation library visibility is independent of the draft's model; the project membership picker follows the draft account.
- Personal configuration is generated locally from Python defaults and excluded from version control.

### Fixed

- Installed distributions discover all bundled extensions from their package path, including the archive, rather than assuming a source checkout.

- UI stalls while opening large histories: transcript preparation and archive reads run outside the UI event loop, with bounded render reuse.
- Late history and synchronization results overwriting another tab's state, drafts, newer replies or published thinking.
- Local archive failures reporting an already accepted remote response or project update as a remote failure.
- Stale synchronization indicators, old account rows, superseded background callbacks and failed catalog refreshes clearing usable choices.
- Permanent deletion retaining remote records or allowing stale background results to resurrect locally deleted entries; provider readback is required before archive cleanup.
- Empty shared forms, narrow inline project editors, tab sizing, keyboard interception and multiline input handling.
- Help accumulating in transcripts: F1 now toggles it and restores the previous reading position.
- Missing localization at UI boundaries; source-string translations retain dynamic model names, account labels and user content.

### Security

- Remove personal `config.json` from every historical commit while retaining the commit sequence; rewrite and verify signatures with the configured SSH signing key.
- Keep session files private and account-scoped; isolate alternate application roots and tests from real credentials.
- Reject unsupported temporary-mode fallbacks and avoid local conversation snapshots for temporary chats.
- Validate origin ownership and native project writes; never automatically replay ambiguous remote mutations.
- Preserve third-party notices and document that local extensions/Python themes execute trusted code.

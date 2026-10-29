# Changelog

All notable changes to SiopeChat are documented here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
Release versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html);
Python distributions use the corresponding PEP 440 version (`1.5b0` for `v1.5-beta`).

## [Unreleased]

### Added

- List every built-in and registered extension action in Settings → Shortcuts, including actions without a default shortcut; allow users to leave a shortcut unassigned.
- Add copy and paste to the shared action handlers, and bind shortcuts for registered extension actions.
- Split the terminal chat view into focused sync, account, tab, sidebar, project, transcript, message-action, history, and send components composed by `ChatView`.
- Move built-in action titles and group labels into the English, Turkish, and Russian locale bundles.

### Changed

- Replace emoji and full-width UI glyphs with monochrome, single-cell symbols and an ASCII spinner to reduce platform-dependent rendering and terminal-width drift.
- Keep application and provider glyphs together in the typed `ICONS` dictionary in `src/tui/styles.py`; each `IconStyleItem` owns its symbol and colors, while provider registry entries contain only provider metadata.
- Distinguish transcript roles with a prompt-style `>` for user messages, a filled `◆` for assistant replies, and an outlined `◇` for reasoning.
- Let transcript gutter markers inherit their matching message background instead of forcing a separate fixed background.
- Store terminal and Markdown styles as typed records with separate foreground, background, and text-attribute fields.
- Require named fields in TUI style records and require a glyph on every icon-style entry.
- Give user, assistant, and thinking messages distinct dark surfaces, with separately configurable text and role-icon colors.
- Permit duplicate shortcut assignments and mark every conflicting action in red in Settings.
- Reduce the sidebar width on narrow terminals so the transcript and its controls retain usable space.
- Resolve built-in action labels from the active locale and use the existing English catalog fallback for untranslated keys.

### Removed

- Remove obsolete UI-literal translations for built-in action labels after moving them to stable action keys in each locale bundle.

### Fixed

- Drain view-owned background work when shared TUI test terminals close, before temporary SQLite files are removed on Windows.
- Kept the response-version counter visible in compact message toolbars and simplified the chat footer controls to show values without field labels; inactive temporary chat now uses a muted style while retaining its status icon.
- Drain TUI-owned background reads before application services and temporary data are closed; wait for actual prompt-toolkit rendering in the Windows mouse-navigation test instead of relying on short fixed sleeps.
- Keep the project information icon inset from the sidebar divider, including when the sidebar is narrow.
- Close the active provider connection correctly when a user stops a response.
- Format elapsed time as a static helper so the running footer redraw does not crash.
- Keep dynamic running/completed reply markers formatted correctly in English, Turkish, and Russian help text.
- Mark the transcript message-key helper static so initial rendering does not fail on an unexpected bound `self` argument.
- Keep transcript role markers in the content coordinate space so reply-version arrow hit targets remain aligned; compact the toolbar and preserve navigation as the terminal narrows.
- Keep compact message-toolbar controls visually separated without displacing reply-version navigation at narrow widths.

## [1.5-beta] - 2026-10-28

### Added

- Add provider-specific error types for Claude and Gemini and stable error codes plus prompt-submission state to the shared provider contract.
- Add conversation renaming to the active-chat toolbar and inline rename/delete actions to supported sidebar chat rows, including chats nested under projects.
- Add Gemini's native chat rename action through its authenticated web GraphQL mutation and confirm the returned server title before reporting success.
- Add a per-answer Sources action that opens provider-published citations with validated links, titles, and descriptions in a scrollable panel.
- Show provider-published thinking summaries and web-search activity with the assistant reply they belong to, without presenting them as private reasoning.
- Show generated-file cards inside assistant replies with available file type and size.
- Carry generated-file descriptors through the common provider metadata event and SQLite history; file-card clicks use the shared background download manager and authenticated provider resolution.

### Changed

- Move provider protocol, transport, catalog, session, history, upload, and message-action tests into the standalone `src/providers/tests` suite; keep application integration tests under `tests/`.
- Run application, standalone provider, provider-package smoke, and JavaScript tests from `run_tests.py`; prefer the existing `python/` virtual environment before other environment fallbacks.
- Capture all browser cookies scoped to `gemini.google.com` during Gemini verification instead of restricting the provider session to a fixed name allowlist.
- Read Gemini thinking choices, disabled states, and defaults from the account's native web model policy; display each available effort's provider identifier unchanged in the shared picker.
- Resolve per-user data, state, cache, and runtime directories from the normalized project name in package metadata; pass the caller-owned cache path to the Sentinel process instead of maintaining a second hardcoded cache root.
- Batch title-override reads while saving provider conversation pages, avoiding one SQLite metadata query per conversation.
- Preserve the unchanged message prefix when syncing archived histories; SQLite now replaces only the changed suffix instead of deleting and reinserting every message.
- Show cited sources as wrapped, real-URL links with their provider-supplied title and description in a mouse- and keyboard-scrollable popup.
- Let title changes proceed during active generation: ChatGPT uses an isolated request client, Claude bypasses its generation lock, and Gemini separates authenticated session bootstrap from model-catalog discovery. Rename-only locks still coordinate ChatGPT/Claude shutdown.
- Reuse ChatGPT's prepared HTTP client for sends; discard the pool after canceled or failed streams while preserving interruption behavior.
- Make the sidebar divider width explicit, add a magnifier action for the existing chat-search popup, and allow mouse focus in the inline sidebar filter.
- Center the empty-chat prompt and show one localized instruction instead of repeating the SiopeChat brand in the transcript.
- Translate the remaining English Turkish UI strings, including conversation loading, command-palette descriptions, and workspace actions.
- Make split-group lookup read-only during redraws and reconcile pane state only when group membership changes.
- Consolidate extension-facing view contracts and their registry in `src/extension.py`; route extension TUI access through its lazy public API loader.
- Keep `tui.api` as the TUI extension surface, loaded through `extension.load_tui_api` only when needed.
- Separate the command-line entry point (`src/cli.py`) from the terminal UI package (`src/tui/`).
- Import ChatGPT discovery functions directly so the HTTP client no longer depends on the public package initializer.
- Display per-response model and effort at the right of the message toolbar, immediately before reply-version controls when present; remove the separate details popup.
- Move conversation deletion and split-pane controls beside the upper synchronization button.
- Add a trash icon to the conversation delete action in the upper toolbar.
- Present generated files as compact inline links with natural wrapping, matching the assistant background and using blue underlined text.
- Group project pagination checks with provider UI tests and cursor-boundary checks with terminal responsiveness tests.

### Removed

- Remove the stale setuptools declaration for the retired top-level `views` module so package builds do not warn about a missing source file.
- Remove the app-suite test that copied and reran the full provider suite; `run_tests.py` now runs provider tests once from an isolated temporary package copy, while CI checks the installed wheel and source distribution.
- Remove the unused `data_dir` path helper and replace misleading legacy-path naming with explicit application-root and user-data paths.
- Remove static locale-key/source scans and the source-tree JavaScript file-set check; keep runtime text-format validation, architecture boundaries, and built-package smoke checks.

### Fixed

- Keep rapid previous/next answer-version clicks in order while an archived version is loading; report a missing archive version instead of silently ignoring the selection.
- Keep answer-version arrows inside the transcript's clickable area, clear of the scrollbar margin, and separate toolbar presses from transcript text selection.
- Preserve Gemini's total generation deadline on Python 3.10, the standalone provider package's minimum supported version, while keeping caller cancellation distinct.
- Preserve ChatGPT Sentinel incompatibility errors through the provider/task layers, report clearly that the prompt was not sent, and release the chat operation so the user can try again after an update.
- Keep signer diagnostics out of user-visible errors and exceptions; the Sentinel failure reports a safe compatibility message without exposing raw subprocess output.
- Separate Gemini's total request deadline from visible response events; the transport read timeout handles actual network silence without canceling a stream that is still delivering non-visible frames.
- Parse Gemini's length-prefixed stream frames by UTF-16 code units, including multiple frames and Unicode split across network chunks.
- Send Gemini's `bard_activity_enabled` activity RPC with the upstream JSON nesting immediately before generation; this is one request per send, without periodic polling.
- Cancel and join Gemini's streaming HTTP transfer on interruption so a stalled connection cannot keep the provider's generation gate occupied after cancellation.
- Send Gemini's selected thinking level consistently in the request body and model header, including native Medium; match the temporary-chat flag in both locations.
- Bind Gemini conversations at the server's initial acknowledgement instead of waiting for answer text, preserving the remote identity during slow or interrupted generation.
- Build the Sources popup from the transcript's already validated citations before scanning raw provider metadata, so large web-search payloads cannot exhaust the bounded source traversal and leave the popup empty.
- Render the Sources popup in an opaque, explicitly sized frame whose width matches the overlay; keep long citation lists scrollable without exposing transcript text through the popup.
- Route sidebar renames through the conversation's owning provider account, and retain confirmed names when an older in-flight history read completes.
- Restore the visible tab state when opening a saved conversation after the last chat tab was deleted.
- Read source citations from both archived provider metadata and prepared transcript rows; source titles and URLs open the validated citation URL directly instead of entering the file-download flow.
- Removed deleted conversations from open tabs, selected the preceding tab when the active chat was deleted, and released its transcript/render state without replacing it with a visible new-chat tab.
- Kept renamed conversation titles canonical in SQLite and cached listing pages; stale provider responses retain the confirmed title until the provider reports it, after which later provider renames are accepted.
- Passed declared provider capabilities through the lazy sidebar row renderer, so supported rename/delete actions render at the far right instead of disappearing; unsupported provider actions remain hidden.
- Kept empty cached conversations in a loading state until the background provider read and its current archive snapshot finish; stale snapshots that race a provider write are discarded instead of briefly hiding messages.
- Kept restored conversations eligible for independent archive hydration, retried temporary background-read queue saturation, and preserved the loading state so a saved tab cannot become a permanent empty conversation.
- Scoped Sources popup navigation keys to that visible popup, restoring transcript PageUp/PageDown behavior immediately after it closes.
- Kept the transcript pinned to its bottom edge while switching archived answer versions.
- Preserved bounded source snippets/descriptions in the shared citation metadata so the Sources popup can display them below each URL.
- Keep validated ChatGPT, Gemini, and Claude citations on their assistant answer so the Sources action remains with that answer, including when thinking is absent or loaded from another SQLite page.
- Read Gemini's published web-search activity labels and result links from both history and streaming, combining those labels with its published thought summary without inferring private reasoning.
- Refresh cached conversations from their provider in the background on open, then apply the archive update without blocking the composer or replacing an in-progress local turn.
- Exit the one-shot Sentinel signer after its JSON result is flushed instead of waiting for the SDK's residual five-second fallback timer.
- Enable Claude's native web-search tool in web-session completion requests so it can search when a prompt needs current information.
- Keep the sidebar divider aligned with its search/filter row at supported terminal widths.
- Use ChatGPT's current public Sentinel SDK methods for request signing instead of matching private minified names; surface signer failures as request errors instead of falsely disabling chat.
- Align Gemini's stream read timeout with its 120-second idle limit and translate curl timeouts into a clear provider error without retrying an ambiguous send.
- Stop tab-bar redraws from triggering split-layout cleanup; index tabs once when building grouped titles and release stale pane state after close, restore, split, or unsplit actions.
- Resolve Gemini's published `[cite: N]` groups into validated source links, deduplicate them by citation order, and associate them with the matching thinking row. Responses without citations remain source-free.
- Traverse bounded nested citation and annotation lists in shared provider metadata, including wrapped URL citations, without inspecting answer text or retaining arbitrary provider payloads.
- Preserve confirmed response settings, source metadata, and resource metadata when a provider refresh omits them; refresh saved Gemini image references when downloading.
- Hide unknown provider model identifiers from reply toolbars; show only names resolved by the active account's model catalog.
- Keep a per-tab archive refresh pending when provider history finishes during an active response, then apply the refreshed SQL window after the response instead of losing the update behind the global archive revision.
- Reset conversation-specific history cursors, versions, pending reads, and scroll anchors when reusing a tab for another chat.

## [1.4.2-beta] - 2026-10-26

### Added

- Added provider-aware message editing, reply retries for ChatGPT and Claude, and separate conversation branches for ChatGPT and Gemini. Gemini also exposes its native retry action on the latest eligible reply.
- Added native message identities and available actions to the standalone provider API, with streamed source metadata kept separate from published thinking summaries.
- Kept ChatGPT's native branch preview available locally until its first accepted reply; Gemini branches remain out of the sidebar until Gemini lists them.
- Saved successful retries only in the SQLite archive and added compact previous/current/next controls shared by all providers.

### Changed

- Shortened transcript toolbar labels to one word in English, Turkish, and Russian.
- Require Python 3.11+ for the application, replace local Python 3.10 backports with standard-library types, and keep the standalone provider package's separate support range.
- Scope application CI test discovery to `tests/`; the independent provider package continues to run its own test suite in its wheel and source archive jobs.

### Removed

- Remove the global JSON conversation-cache read fallback. Those files are left untouched but are no longer imported; account-scoped cache-to-SQLite migrations remain to preserve local records.

### Fixed

- Built realistic older SQLite states in archive migration tests by removing schema-v6 tables before replaying migrations; isolated clipboard-selection assertions from the UI event-loop timer.
- Reconciled confirmed message edits and retries with the archive without allowing older background reads to restore replaced messages.
- Preserved ChatGPT branch previews across local reloads and bound them to the server conversation after the first accepted turn.
- Saved refreshed ChatGPT credentials through caller-owned storage without assuming a filesystem path.
- Displayed published web and file citations without inventing download URLs or treating answer-text links as sources.
- Removed superseded provider replies from the active transcript after retries while preserving them as selectable archived versions.
- Kept retry replacement anchored at the archived answer even when the provider returns only a partial history page.
- Kept Gemini's returned branch title and source project on the open tab without presenting an unlisted branch as a provider conversation.
- After the first successful reply in a provider-created branch, checked its provider conversation or project list once in the background; unconfirmed branches remain usable in their open tab and are not polled.
- Showed copy confirmation in the status line for two seconds, restoring its prior message unless a newer status replaced it.
- Kept message toolbar actions within the rendered row, used terminal-cell widths for mouse hit testing, and made disabled version arrows non-clickable.
- Restored ChatGPT transcript reads to its active message-path endpoint; reply versions now come only from locally captured SQLite retries.

## [1.4.1-beta] - 2026-10-25

### Added

- Added compact copy controls to user and assistant messages. Assistant replies also show provider-supplied sources and model details.
- Added Claude Incognito chats and restoration of open conversations and the selected tab after restart.

### Changed

- Refined the downloads workspace and transcript cards, with distinct styling for user messages and assistant content.
- Combined consecutive thinking summaries and removed blank rows between them.

### Fixed

- Kept the downloads panel compatible with the supported Prompt Toolkit layout API so startup succeeds.
- Displayed cached project conversations immediately, then reconciled them with provider history in the background.
- Preserved inactive transcript state across tab switches and bounded scroll positions to the available history.
- Kept split-pane message toolbars scoped to their chat and skipped stale transcript ranges during redraw.
- Added translated errors for saving the tab layout.

## [1.4-beta] - 2026-10-24

### Added

- Add one-time Linux, macOS and Windows installers that check Python and Node.js, reuse known virtual environments and install the checkout's dependencies.
- Add a root-level `run_tests.py` command for application and provider tests, provider smoke checks, and JavaScript tests.
- Add section-level project and conversation creation buttons to the sidebar using the existing creation actions.
- Store account sessions, model catalogs and verification records in a private, account-scoped SQLite repository with an injectable provider credential contract.
- Add clean-install wheel and source-archive checks for the standalone provider package to CI.
- Expand the optional process-tree profiler to record CPU, RSS/USS/PSS memory, disk I/O, handles, threads and processes, with metadata-only storage snapshots categorized by browser profile, cache, account data, archive and downloads.
- Publish a host-independent provider client, account factory and complete typed backend contract; the provider directory includes standalone packaging and portable API tests.
- Show a stop button in place of Send while the active chat is generating; pressing Esc twice within three seconds also stops it, with an inline status hint after the first press.

### Changed

- Exclude the application test tree from the root source distribution while retaining tests in the repository and CI.
- Make launch scripts select an existing supported environment consistently and check Python 3.10+ and Node.js 22+ without installing dependencies at startup.
- Make archive repository operations explicit and required; remove optional-method fallbacks that could load a complete conversation during a send or synchronization race.
- Discard unknown application settings while loading configuration so removed preferences do not prevent startup; unsupported settings are not exposed in the application.
- Remove redundant file/service-presence, duplicate import, README-text and stale-setting tests while keeping provider boundaries, data migration, lifecycle and user-visible behavior coverage.
- Organize tests around neutral shared fixtures and focused archive, history, provider UI, profile and workspace modules; merge small related suites to avoid over-fragmenting the test tree.
- Isolate streamed event handling and provider snapshot reconciliation from the chat send coordinator without changing the send lifecycle.
- Move legacy per-account conversation caches, active-chat pointers and deletion markers into the existing SQLite stores. The account database migration is additive and preserves its session/catalog schema; the archive remains the canonical history store when enabled.
- Count models across selected accounts of enabled providers and show how many provider catalogs are ready; fetch missing catalogs in the background without holding up chat startup.
- Share the failed model-catalog request cleanup used by startup refresh and the model picker, while preserving their separate UI refresh behavior.
- Keep the composer available while a known chat's local history and provider refresh load; merge late history with any turn already sent from the tab.
- Continue provider chat, project and expanded-project-chat pagination in the background so cached first pages remain usable while the rest of the library fills in.
- Cache Gemini and Claude model catalogs for five minutes with explicit refresh support, and decode large Claude JSON and Gemini RPC responses off the provider event loop.
- Reuse ChatGPT's account HTTP pool for project options and edits; stream project source files from disk instead of loading their entire content into memory.
- Prepare enabled accounts with saved sessions in bounded background workers at chat startup; provider requests share or wait for the same first-session initialization.
- Reuse ChatGPT's account-scoped HTTP client and keep-alive pool for normal reads, while uploads and cancellable generation retain their isolated clients.
- Replace the Gemini SDK and subclass wrappers with native HTTPS authentication, model discovery, RPC, disk uploads and bounded streaming parsing.
- Remove the Gemini SDK dependency and its application-level cookie-cache initialization.
- Render large fenced code messages through bounded transcript windows, avoiding full-message ANSI buffers and syntax tokenization.
- Keep uploaded file metadata without retaining their bytes, and remove legacy archive copies during the schema v5 migration.
- Document the irreversible schema v5 cleanup and pre-upgrade backup scope in all README versions.
- Show each provider's projects as soon as its background refresh completes.
- Read cached chats in SQLite batches of at most 80 visible messages and a 262,144-character serialized-record budget; read complete edge ranges off-thread for direct top/bottom navigation and reject mixed archive revisions.
- Share lean source strings with transcript layouts, compress large ANSI cache entries within 256 KiB, and retain warm inactive tab layouts under a shared 4 MiB eviction budget.
- Release queued render payloads and closed-tab caches immediately; detached generation keeps only the current turn until persistence completes.
- Persist accepted turns by reconciling a bounded SQL tail. Upgrade archives transactionally to schema 5, retaining ordered messages and native provider metadata without duplicating full bodies in listing headers or the hot cache.
- Reuse bounded Markdown caches across streaming updates, share message ordering across transcript views, and prepare inactive split panes off the terminal thread.
- Prioritize the visible transcript before rendering a two-screen buffer above and below it, keep recently visited rows in a one-megabyte LRU, and avoid rescanning the full message list on every terminal paint.

### Removed

- Remove the obsolete account `models.json` snapshot compatibility test; SQLite-backed catalog caching and provider entitlement behavior remain covered.
- Remove settings-catalog row-presence assertions from the download-path behavior test; default, validation and changed-directory behavior remain covered.
- Remove selectable application themes and theme contributions from the extension API; the CLI now uses one fixed default appearance and extensions must use API v3.

- Remove the vendored dependency-injection container. Runtime now keeps explicitly owned service instances, and extension API 2 uses `context.provide()` and `context.on_dispose()` without container scopes or injection decorators.
- Remove deprecated CLI aliases `/quit`, `/delete-project`, `/proje`, `/private`, `/sync` and `/file`; canonical commands remain available.
- Remove the legacy single-record download-history write API and its fallback; all history writes use the revision-aware batch contract.
- Remove the `CHATGPT_WEB_MODEL` environment override in favor of the selected account's model catalog and current chat preference.
- Require extensions to provide a manifest and an explicit matching API version; remove the implicit version-1 and manifest-less extension paths.
- Remove the archive history import shim and use the provider package's shared history module directly.
- Call the required archive repository history methods directly during partial-history merging; remove optional method-discovery fallbacks.

### Fixed

- Serialize Gemini session shutdown and credential removal with connection setup so an in-flight login cannot recreate a session after sign-out; retry transient refresh network/server failures with bounded backoff.
- Drain queued active-chat persistence before the archive becomes unavailable during shutdown, and compensate account settings if credential deletion fails.
- Bound partial-history reconciliation to the merge prefix and tail, and process schema v5 cleanup in 256-row keyset batches instead of loading whole tables into memory.
- Close Claude and Gemini transports even when provider-specific editor cleanup raises, while preserving the original cleanup error.
- Run every standalone provider test in wheel and source-archive CI jobs, including credential tests.
- Pin GitHub Actions to Node 24-compatible releases and prevent parallel `uv` cache saves from racing across CI jobs.
- Fix Ruff import-order and formatting failures so CI can continue to the test and package checks.
- Compare project upload paths by canonical file identity in CI tests, accounting for macOS temporary-directory symlinks.
- Keep the CLI startup scheduling test independent of a native console on headless Windows runners.
- Add the missing synchronization failure status to all three language catalogs.

- Route transcript PageUp/PageDown and Home/End through shared history navigation so keyboard scrolling loads adjacent SQL pages instead of stopping inside the current window; preserve Home/End editing in the composer.
- Extend loaded history with older SQLite batches without removing already loaded messages; preserve provider order and message-ID scroll anchors through rapid direction changes.
- Use the newly published transcript geometry for wheel and page-key bounds before the next terminal repaint, preventing stale heights from skipping visible content.
- Stop history windows from oscillating between short thinking pages and long answers while idle: page transitions now consume the latest wheel or page-key direction instead of being triggered by every repaint.
- Page displayed history over visible messages so hidden tool records cannot consume entire SQL windows; navigate older windows backwards across sparse ordinal gaps and retain raw archive records.
- Display text contained in structured ChatGPT response blocks, including cached audio transcriptions, without requiring a history resync.
- Preserve the latest explicit top/bottom history navigation during an in-flight SQL read and distinguish manual scrolling from automatic layout position changes.
- Reuse bounded, content-keyed message line counts across SQL history windows even after rendered strings are evicted, avoiding full Markdown conversions solely to rebuild scroll geometry.
- Share one latest-viewport render queue between main and split views; reuse unchanged message geometry and rendered rows when history grows instead of repeating Markdown conversions.
- Keep cached transcript rows visible while Markdown pages render, and preserve the latest visible message when scrolling during an older history page's render preparation.
- Clear a chat's background-sync marker on refresh failure while retaining the last cached history.
- Serialize Gemini credential rotation and finish in-flight credential writes before cancellation releases their lock; persist the final Gemini cookies during shutdown.
- Coalesce Claude's initial bootstrap, workspace selection and model discovery so simultaneous warmup and chat reads do not repeat the first handshake.
- Read Gemini notebook membership from the general conversation list so notebook chats stay under their projects; preserve known membership when stale listing pages omit it during archive refresh or pagination.
- Keep the sidebar divider aligned and default all project groups to collapsed, including projects inferred from conversation listings; load chats only when their group is opened.
- Report accounts with missing sessions in listing errors instead of silently excluding their conversations and projects. Explicit refresh and project-chat expansion await fresh results while retaining the cached page if refresh fails; bound account listing workers.
- Do not reuse a prepared transcript for a different conversation, and show the actual loading or failure state while a cold history is being fetched.
- Keep cached ChatGPT project details readable without repeating an account-wide missing-login error on every project; live refresh and edits still require a verified session.
- Hash conversation records incrementally before SQLite writes, avoiding a second full-history JSON string during provider sync.
- Merge a concurrent local send from a bounded SQL tail when the provider returns complete history, and keep partial-history merges lossless.
- Parse ChatGPT, Claude and Gemini histories off their shared asynchronous provider loop.
- Preserve the user's latest scroll anchor when an older SQLite history page finishes loading late.
- Keep previously loaded provider projects visible when loading the next project page.
- Fix the provider wheel and source-archive CI command blocks so the workflow can be parsed and start jobs.
- Stop ChatGPT file uploads by cancelling the synchronous transfer, and reject files changed after preflight.
- Keep split-pane transcript render caches bounded and release inactive pane pages.
- Retain failed provider backends for a later `close()` retry instead of discarding them.
- Persist the provider runtime's canonical response snapshot even after a UI callback fails, so archived answers and reasoning are not truncated.
- Keep response cancellation available while staged files are re-uploaded to another provider account; cancellation now stops the in-flight upload before chat submission.
- Repaint the old and newly selected download-history rows when their cached fragments are reused.
- Run attachment path, type and size preflight on the background read pool, and use a file-neutral prompt when sending attachments without text.
- Stream attachment files into the content-addressed archive in bounded chunks instead of reading each file fully into memory.
- Remove the obsolete example-README source-manifest exclusion while retaining exclusions for local configuration and memory files.
- Declare the resource-only helper and translation packages explicitly so setuptools builds the wheel without namespace-discovery warnings.
- Release closed-tab and split-pane source, selection and render references; stop startup frames and archive polling from retaining complete conversation bodies.
- Preserve drafts, text selections and live responses when older SQL pages finish loading, and refresh streamed thinking even before the final answer changes.
- Count distinct files across Claude's full conversation tree before sending attachments, limit the new files to the remaining conversation quota, and stop safely when the provider cannot confirm current usage.
- Close the runtime before removing the paging test's temporary SQLite directory, fixing the Windows CI file-lock failure.
- Avoid re-queuing first-page records already fetched by an explicit library sync, pace ChatGPT history pages, honor provider retry delays after HTTP 429, and identify rate-limited sync failures in the status line while retaining cached data.
- Keep an existing archive intact when a partial provider history page cannot prove contiguous coverage, and report a response as completed if cancellation arrives during its durable save.
- Bound in-memory transcript history to the active page window during scrolling and refresh, while keeping the complete conversation in SQLite.

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

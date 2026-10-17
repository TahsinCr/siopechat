# Changelog

## Unreleased

### Account and narrow-pane polish

- Preserve the default account's explicit CDP URL override and prevent late session results from updating another account's UI state.
- Highlight keyboard account selection, allow its list to shrink with the terminal and compact composer controls inside narrow split panes.
- Use the shared role gutter in inactive panes, track theme changes and preserve manual reading position across tab switches.
- Keep shared account/filter/file/project forms compact and centered, with charcoal surfaces and visible checked/selected states instead of toolkit default blue/white dialogs.
- Reconcile README setup, provider/data ownership, concurrency and remaining verification boundaries.

### Independent tab activity

- Keep request status, cancellation connections and elapsed time per tab; completing one request no longer clears another tab's activity.
- Allow up to four pending tab requests, with per-account provider serialization and independent account instances. Preserve the captured provider callback across account switches.
- Scope thought stream identities to each turn and keep the active message list connected after tab restoration.
- Allow multiline draft editing while a reply is pending. Test overlapping controlled streams, tab switches, cancellation ownership and repeated reasoning turns.

### Project creation

- Add capability-gated project creation, sidebar button, `/project-new` and Alt+P with name, privacy and memory fields.
- Drive the native ChatGPT form and read back the new project/memory; fail unsupported sharing before submitting rather than silently creating private.
- Record the Notebook/Gemini Notebook research boundary: automatic notebook synchronization is not implemented or claimed. Native creation/sharing still needs live-account verification.

### Shared command catalog

- Replace the duplicated built-in palette list with canonical keyboard action handlers, shared readable titles and category ordering in Settings.
- Expose running terminal actions through the extension ActionService and withdraw them on close; deduplicate palette entries.
- Add configurable Alt+M/Alt+E model/thinking pickers.

### Account-aware terminal

- Wire the terminal to account-scoped ChatGPT/Gemini providers, with account creation/selection, separate profiles/session files/cache identities and local logout.
- Add mixed-provider conversation icons and a multi-select sidebar filter that stays hidden for a single configured provider; retain per-account pagination.
- Add shared keyboard/mouse forms and a file attachment button/path picker; carry actual uploaded document/image metadata or Gemini file paths into sends.
- Guard model catalog updates by account and snapshot send callbacks before a tab switch. Live browser/account/file compatibility remains a separate verification step.

### Gemini provider

- Add the independent Gemini web-session adapter with dynamic account models, history, streamed thought/answer separation, temporary contexts and file inputs.
- Map standard/extended thinking to the transport flag and validate provider/account origin on every send.
- Add gemini-webapi for Python 3.11+; keep ChatGPT usable on Python 3.10. Google live account flows are not exercised by offline tests.

### Split conversation views

- Add the layout extension’s real side-by-side/stacked pane renderer, up to four panes, pane focus and grouped tab labels.
- Add a Split button, sidebar selection step, `/split`, `/split-down`, `/unsplit` and configurable Alt+V/H/U actions.
- Keep layouts optional and preserve conversations when the extension is disabled.

### Terminal input and settings

- Distinguish Shift+Enter sequences from ordinary Enter, with Alt+Enter/Ctrl+J fallbacks and configurable bindings.
- Give settings a full-height scrollable list, aligned values and an integrated editor; support wheel and page navigation.
- Cap all tab widths and pin previous/next/new controls at the right edge.

### Clean development baseline

- Start SiopeChat history from the current modular Python application. No numbered releases or tags yet; subsequent features receive separate commits.
- Keep independent provider contracts, ChatGPT web-session transport, dynamic model/effort discovery, project reads, temporary chats and read-back guarded project-membership writes.
- Include tabbed terminal conversations, a collapsible project/chat sidebar, categorized settings/accounts, keyboard commands, clipboard editing and role-aware Markdown rendering.
- Keep settings/actions/runtime/extension modules directly under src, with bundled layout extensions and themes outside src. Reuse existing Python environments and shared Windows/macOS/Linux launchers.
- Preserve vendored container/event origins and licenses. Exclude sessions, local data, environments and agent memory from Git.
- Offline baseline: 170 Python tests and 8 Node tests; live ChatGPT project mutations, real split panes, Gemini, multi-account UI and NotebookLM remain unverified or unimplemented at this point.

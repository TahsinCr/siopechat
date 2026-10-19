# Changelog

All notable changes to SiopeChat are documented here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
Release versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html);
Python distributions use the corresponding PEP 440 version (`1.0b0` for `v1.0-beta`).

## [Unreleased]

### Added

- Verify from profile in account settings: import the selected provider's session from the default browser's existing profile without launching a browser or enabling debugging flags.
- Default-profile discovery for Chrome, Chromium, Brave/Brave Origin, Edge, Vivaldi and Firefox standard locations, with localized import status and errors.

### Changed

- Simplify account settings to profile verification and logout, and remove the unused login-profile sharing selector from account creation.

### Removed

- Browser-launch and separate browser-verification controls from account settings; profile import is now the account login flow.

### Security

- Scope profile imports to provider authentication cookies, exclude Firefox containers, bound keyring work, and validate candidate sessions before replacing existing account credentials.

### Fixed

- Keep default-browser diagnostics out of the terminal UI while reporting launch failures.

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

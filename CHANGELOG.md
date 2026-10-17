# Changelog

## Unreleased

### Clean development baseline

- Start SiopeChat history from the current modular Python application. No numbered releases or tags yet; subsequent features receive separate commits.
- Keep independent provider contracts, ChatGPT web-session transport, dynamic model/effort discovery, project reads, temporary chats and confirmed project-membership writes.
- Include tabbed terminal conversations, a collapsible project/chat sidebar, categorized settings/accounts, keyboard commands, clipboard editing and role-aware Markdown rendering.
- Keep settings/actions/runtime/extension modules directly under src, with bundled layout extensions and themes outside src. Reuse existing Python environments and shared Windows/macOS/Linux launchers.
- Preserve vendored container/event origins and licenses. Exclude sessions, local data, environments and agent memory from Git.
- Offline baseline: 170 Python tests and 8 Node tests; live ChatGPT project mutations, real split panes, Gemini, multi-account UI and NotebookLM remain unverified or unimplemented at this point.

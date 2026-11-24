[![Version: v1.5.6-beta][version-shield]](CHANGELOG.md)
[![Python 3.10+][python-shield]](https://www.python.org/downloads/)
[![GPL-3.0-only][license-shield]](LICENSE)

**English** | [Türkçe](README-TR.md) | [Русский](README-RU.md)

# SiopeChat

**ChatGPT, Gemini, Claude and Grok web chats in one terminal.**

Keep your chats and projects in one place, or put two replies side by side when you want to compare them.

> SiopeChat is an independent client for web chats. It uses your existing browser session and requires no API key. It doesn’t connect to official developer APIs or run coding agents such as Codex, Claude Code or Google Antigravity CLI. Your account’s model access and usage limits still apply.

[Install](#install) · [Connect an account](#connect-an-account) · [Compare providers](#provider-capabilities) · [Performance](#performance) · [Changelog](CHANGELOG.md)

![SiopeChat terminal interface](assets/siopechat.png)

*SiopeChat’s terminal interface with sample conversations.*

## Install

You’ll need Git, Python 3.10 or later, and a terminal with Unicode support.

**Linux / macOS**

```sh
git clone https://github.com/TahsinCr/siopechat.git
cd siopechat
./install.sh
./siopechat.sh
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/TahsinCr/siopechat.git
cd siopechat
.\install.ps1
.\siopechat.ps1
```

Run the installer once, then use the launcher for everyday starts. To update a Git checkout, run `git pull --ff-only` and rerun the installer for your platform.

## Connect an account

1. Sign in to the provider in the profile you last used in your default browser.
2. Open **Settings → Accounts** in SiopeChat and add or select the matching account.
3. Choose **Verify from profile** to import and verify the session. When it expires, sign in again in the browser and repeat this step.

## Features

- **Tabs and split panes.** Open several conversations without losing drafts or scroll positions. Compare up to four chats side by side and save the layout as a workspace.
- **Accounts and models.** Switch between accounts and choose from the models available to each one. Set reasoning effort on models that offer it.
- **Streaming replies.** Read replies as they arrive and switch tabs while a response is running. Edit messages, retry replies or branch a chat where the provider supports it.
- **Projects and notebooks.** Work with ChatGPT and Claude projects, Gemini notebooks or Grok workspaces. Manage instructions and sources, and move chats where supported.
- **History and downloads.** Reopen archived chats, export conversations as plain text or JSON, and keep track of generated files in the download manager.

## Provider capabilities

| In SiopeChat | ChatGPT | Gemini | Claude | Grok |
| --- | --- | --- | --- | --- |
| Streaming, chat history and model selection | Yes | Yes | Yes | Yes |
| Reasoning effort selection | Supported models | Supported models | Supported models | — |
| Edit messages and retry replies | Yes | Yes | Yes | Yes |
| Branch a chat | Yes | Yes | — | — |
| Temporary or incognito chats | Yes | Yes | Yes, outside projects | Yes, outside workspaces |
| Projects | Projects | Gemini notebooks | Projects | Workspaces |
| Project instructions and text or file sources | Yes | Yes | Yes | Yes |
| Move an existing chat into a project | Yes | — | Yes | Yes |
| Chat attachments | Yes | Yes | Yes | Yes |

The table describes SiopeChat’s integration. Model access, editable messages and attachment types depend on the provider, account and conversation. A dash marks an option SiopeChat does not offer for that provider.

## Performance

When you open a saved chat, SiopeChat shows the local copy while it checks for newer messages in the background. As a reply arrives, the terminal draws the first visible text right away and batches later updates to keep the interface responsive.

Under the hood, [WebAI Provider 2.0](https://pypi.org/project/webai-provider/2.0.0/) manages web sessions and streams replies. It reuses connections, valid model catalogs and conversation context where available. SiopeChat can warm up the next send while you’re writing. This saves work in the client. The time a model takes to start replying still depends on the provider and the network.

## Everyday controls

| Shortcut | Action |
| --- | --- |
| **Ctrl+P** | Open the command palette |
| **Ctrl+T / Ctrl+W** | Open or close a tab |
| **Alt+Left / Alt+Right** | Switch tabs |
| **Alt+J** | Open downloads |
| **F5 / F12** | Open Settings or Accounts |
| **F1** | Open keyboard help |

Choose English, Turkish or Russian under **Settings → General → Language**. Customize bindings under **Settings → Keyboard**.

## Local data

SiopeChat keeps history and resource metadata in a local SQLite archive. Repository launchers default to portable storage beside the checkout. Set `SIOPECHAT_PORTABLE=0` before launching to use your operating system’s user directories instead. You can choose a download folder in Settings. Temporary chats are excluded from the local archive where supported, and the provider’s retention rules still apply.

Before upgrading from an older version, back up `archive.sqlite3` and the `files/` directory if it exists.

## Project links

[Release notes](CHANGELOG.md) · [GitHub issues](https://github.com/TahsinCr/siopechat/issues) · [WebAI Provider](https://github.com/TahsinCr/webai-provider/) · [GPL-3.0-only](LICENSE) · [Copyright](COPYRIGHT)

[version-shield]: https://img.shields.io/badge/version-v1.5.6--beta-b7a3e3.svg?style=for-the-badge
[python-shield]: https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?style=for-the-badge&logo=python&logoColor=white
[license-shield]: https://img.shields.io/badge/license-GPL--3.0--only-blue.svg?style=for-the-badge

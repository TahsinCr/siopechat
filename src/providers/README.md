# SiopeChat providers

An independent Python library for the ChatGPT, Gemini and Claude **web chat** services.
These unofficial clients use a saved web session. They do not use paid API keys,
Codex or Claude Code subscriptions. The services can change their private protocols;
available models and features depend on the authenticated account.

This directory is a package root. It can be copied into its own repository or used
as a submodule without the SiopeChat terminal, database, settings or extensions.

```sh
python -m pip install .
```

Python 3.10+ supports ChatGPT and Claude. The Gemini transport requires Python
3.11+. Python discovers and downloads the current ChatGPT Sentinel SDK files into
`providers/chatgpt/` with size and SHA-256 checks. QuickJS-NG (`quickjs-ng`) runs
that published SDK with `providers/chatgpt/sign.js` to create the initial context
and handle Turnstile or other requirements outside the validated Python
proof-of-work path. Python computes proof-of-work from the current server
challenge when its shape is supported. Browser page discovery, local CDP access
and session capture run in Python. Importing the library or configuring an
account does not open a browser or access the network.

The SDK files are cached in `providers/chatgpt/`. On the next use after six hours,
the provider checks for updates and reuses unchanged SDK files without downloading
them again. Set `CHATGPT_WEB_SENTINEL_CACHE_TTL_MS` to change the interval in milliseconds.

## Using an account

```python
import asyncio
from providers import ProviderClient, SendRequest, TextDelta, ReplaceText


async def main():
    async with ProviderClient() as client:
        account = client.connect(
            "claude", "personal", credential_file="/private/claude-session.json"
        )
        await account.authenticate()
        catalog = await account.models()
        model = catalog.default_chat_model
        request = SendRequest(
            provider_id="claude",
            account_id="personal",
            chat_id="my-local-draft",
            prompt="Hello",
            model=model.id if model else None,
        )
        async for event in client.stream(request):
            if isinstance(event, (TextDelta, ReplaceText)):
                print(event.channel, event.text)


asyncio.run(main())
```

The caller supplies credential paths and imports a valid session through its own
login flow or the profile helpers in `providers.profiles`. No application config is
read. For ChatGPT, `create_provider` also accepts an existing `BrowserSession`, or
explicit `cache_directory` and `profile_directory` paths. Each account has its own
adapter, connections and concurrency controls.

Call `await account.prepare()` to establish the initial account connection before
interactive work starts. Concurrent first requests wait for the account's initial
setup. Model catalogs are cached for up to five minutes; `await account.models(force=True)`
requests an explicit refresh. Normal ChatGPT reads and project edits reuse an
account-scoped HTTP pool, while uploads and generation keep dedicated transports.
Large Claude JSON and Gemini RPC responses are parsed in background workers so
decoding does not run directly on the shared provider event loop. Credential
writes are serialized and complete before cancellation releases the save lock.

`client.account(provider_id, account_id)` returns the complete `Backend` contract:
session status and authentication, model catalogs, paginated chats and projects,
chat history, rename/delete, project editing and sources, file uploads and download
resolution. Use `describe()` and `project_options()` to discover supported actions.
Unsupported actions raise `FeatureUnavailable`; an empty list does not indicate
unsupported functionality.

History messages keep published sources and generated-file descriptors in their
metadata. Streaming `MessageMetadata` events expose the same `sources` and `files`
collections, plus the reply's available model and effort. File descriptors contain
the native reference and available name, media type and size; file bytes are not
included. Pass a descriptor's `url` to `resolve_download` with the owning chat and
message IDs to obtain an authenticated, transient `DownloadSource`. ChatGPT file
assets and sandbox outputs, Gemini image/video/audio references, and Claude's presented output
paths are resolved by their own provider. A provider may expire or remove a file.

To continue a chat, set `SendRequest.conversation_id` to the chat's
`origin.remote_id`. For a project, also set `project_id` to its remote ID. Local
`chat_id` is a caller-owned stable identity. Keep the provider and account identity
from `Origin` with all stored records; remote IDs alone are not globally unique.

Streams yield `TextDelta`, `ReplaceText`, `ConversationBound` and `MessageMetadata`. Replacement events
replace the corresponding channel's accumulated text. Published thinking summaries
use the analysis channel. Cancellation closes the transport; requests are never
automatically resent after an ambiguous send failure. Closing a client cancels its
active streams and closes all accounts, while keeping credential files intact.

Provider failures derive from `ProviderError` and expose a stable machine-readable
`code`. For send failures, `message_submitted is False` guarantees the prompt did
not reach the provider; `None` means the client cannot prove whether it did. The
ChatGPT Sentinel compatibility failure has its own code and is reported as
“message not sent.” Claude and Gemini expose provider-specific compatibility and
stream error types for callers that need to distinguish a changed web contract
from an interrupted response.

Message tools use provider identities, not list positions. `Message.actions` contains
supported operations for that message on the current conversation path. Use
`SendRequest(action=MessageAction.EDIT, message_id=..., prompt=...)` to edit a user
message, or `MessageAction.RETRY` to retry an assistant response without adding a
user message. Preserve `conversation_id` and the owning provider/account. Model
and effort choices are checked against that account's current catalog. An edit
selects a revised path; retrieve the confirmed history before replacing local
records. Failed or cancelled requests are never automatically resubmitted.

`await account.branch_chat(conversation_id, message_id)` returns a separate
conversation. ChatGPT and Gemini support this operation; Claude does not expose
it in this package. ChatGPT returns a branch preview: if its metadata contains
`pending_branch`, pass `BranchContext(**chat.metadata["pending_branch"])` as
`SendRequest.branch` on the first turn. The resulting `ConversationBound` identifies
the persisted conversation; do not try to fetch the preview from the server.
Persist the small continuation context if the preview must survive a restart.

Published sources are distinct from analysis-channel summaries. Source metadata
comes from native citations, including file references without a public URL;
answer-text links are not treated as verified sources. `MessageMetadata` supplies
native message identities, current-response tools and sources at stream completion.
No subscription-name allowlist is maintained: account model access is discovered
from the provider, while message operations follow the native supported contracts
and any message restrictions. A successful check on one subscription does not
establish availability on every plan; remote authorization errors remain authoritative.

Applications own persistence and synchronization. `get_chat()` returns the history
available from the provider; the library does not invent remote message pagination
where a web transport has none. The local SQL paging used by SiopeChat belongs to
its archive extension.

Offline API tests run after installation with:

```sh
python -m unittest discover -s tests
```

Gemini uses the bundled native HTTPS implementation. All three backends accept caller-owned credential storage through `providers.session.CredentialStore` (`load`, `save`, `exists`, `delete`); file paths remain supported. ChatGPT also needs a cache directory for Sentinel SDK resources when using an injected credential store. Database implementations belong to the host application.

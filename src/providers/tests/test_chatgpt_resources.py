import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from providers.chatgpt.errors import SentinelCompatibilityError
from providers.chatgpt.resources import load_sentinel_sdk

BOOTSTRAP = "https://chatgpt.com/backend-api/sentinel/sdk.js"
IMPLEMENTATION = "https://chatgpt.com/sentinel/test/sdk.js"
SOURCE = "window.SentinelSDK = {};"


class SentinelResourceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.cache_root = Path(self.tempdir.name) / "cache"

    @staticmethod
    def client_for(responses):
        def handle(request):
            value = responses.get(str(request.url))
            if isinstance(value, Exception):
                raise value
            if value is None:
                return httpx.Response(404)
            return value

        return httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)

    def test_download_validates_bootstrap_source_and_reuses_verified_cache(self):
        responses = {
            BOOTSTRAP: httpx.Response(
                200,
                text=f"var script = document.createElement('script'); script.src = '{IMPLEMENTATION}';",
            ),
            IMPLEMENTATION: httpx.Response(200, text=SOURCE),
        }
        with patch.dict("os.environ", {"CHATGPT_WEB_SENTINEL_CACHE_TTL_MS": "60000"}):
            with patch("providers.chatgpt.resources.time.time_ns", return_value=1_000_000_000):
                with self.client_for(responses) as client:
                    loaded = load_sentinel_sdk(self.cache_root, client=client)
                with self.client_for({}) as client:
                    cached = load_sentinel_sdk(self.cache_root, client=client)
        self.assertEqual(loaded.cache_status, "miss")
        self.assertEqual(loaded.implementation, IMPLEMENTATION)
        self.assertEqual(loaded.source, SOURCE)
        self.assertEqual(loaded.sha256, hashlib.sha256(SOURCE.encode()).hexdigest())
        self.assertEqual(cached.cache_status, "hit")
        self.assertEqual(cached.implementation, loaded.implementation)
        self.assertEqual(cached.source, loaded.source)
        self.assertEqual(cached.sha256, loaded.sha256)

    def test_corrupt_cached_sdk_is_replaced_after_a_successful_refresh(self):
        responses = {
            BOOTSTRAP: httpx.Response(200, text=f"script.src = '{IMPLEMENTATION}'"),
            IMPLEMENTATION: httpx.Response(200, text=SOURCE),
        }
        with patch.dict("os.environ", {"CHATGPT_WEB_SENTINEL_CACHE_TTL_MS": "0"}):
            with self.client_for(responses) as client:
                loaded = load_sentinel_sdk(self.cache_root, client=client)
            cached_file = self.cache_root / "sentinel" / f"sdk-{loaded.sha256[:16]}.js"
            cached_file.write_text("tampered", encoding="utf-8")
            with self.client_for(responses) as client:
                refreshed = load_sentinel_sdk(self.cache_root, client=client)
        self.assertEqual(refreshed.source, SOURCE)
        self.assertEqual(cached_file.read_text(encoding="utf-8"), SOURCE)

    def test_verified_stale_cache_is_used_when_refresh_fails(self):
        responses = {
            BOOTSTRAP: httpx.Response(200, text=f"script.src = '{IMPLEMENTATION}'"),
            IMPLEMENTATION: httpx.Response(200, text=SOURCE),
        }
        with patch.dict("os.environ", {"CHATGPT_WEB_SENTINEL_CACHE_TTL_MS": "10"}):
            with patch("providers.chatgpt.resources.time.time_ns", return_value=1_000_000_000):
                with self.client_for(responses) as client:
                    loaded = load_sentinel_sdk(self.cache_root, client=client)
            with patch("providers.chatgpt.resources.time.time_ns", return_value=1_020_000_000):
                offline = {
                    BOOTSTRAP: httpx.Response(503),
                }
                with self.client_for(offline) as client:
                    stale = load_sentinel_sdk(self.cache_root, client=client)
        self.assertEqual(stale.source, loaded.source)
        self.assertEqual(stale.cache_status, "stale-offline")

    def test_untrusted_bootstrap_url_is_rejected_before_fetch(self):
        responses = {
            BOOTSTRAP: httpx.Response(
                200, text="script.src = 'https://chatgpt.com.attacker.invalid/sdk.js'"
            )
        }
        with self.client_for(responses) as client:
            with self.assertRaises(SentinelCompatibilityError):
                load_sentinel_sdk(self.cache_root, client=client)

    def test_redirect_is_not_followed(self):
        responses = {
            BOOTSTRAP: httpx.Response(302, headers={"Location": IMPLEMENTATION}),
        }
        with self.client_for(responses) as client:
            with self.assertRaises(SentinelCompatibilityError):
                load_sentinel_sdk(self.cache_root, client=client)

    def test_sdk_download_is_bounded(self):
        responses = {
            BOOTSTRAP: httpx.Response(200, text=f"script.src = '{IMPLEMENTATION}'"),
            IMPLEMENTATION: httpx.Response(200, content=b"x" * 129),
        }
        with patch("providers.chatgpt.resources._MAX_SDK_BYTES", 128):
            with self.client_for(responses) as client:
                with self.assertRaises(SentinelCompatibilityError):
                    load_sentinel_sdk(self.cache_root, client=client)

    def test_cache_manifest_contains_only_the_verified_resource_identity(self):
        responses = {
            BOOTSTRAP: httpx.Response(200, text=f"script.src = '{IMPLEMENTATION}'"),
            IMPLEMENTATION: httpx.Response(200, text=SOURCE),
        }
        with self.client_for(responses) as client:
            loaded = load_sentinel_sdk(self.cache_root, client=client)
        manifest = json.loads(
            (self.cache_root / "sentinel" / "current.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["implementation"], loaded.implementation)
        self.assertEqual(manifest["sha256"], loaded.sha256)
        self.assertEqual(manifest["file"], f"sdk-{loaded.sha256[:16]}.js")

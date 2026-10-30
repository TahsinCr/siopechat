import hashlib
import json
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from providers.chatgpt.errors import SentinelCompatibilityError
from providers.chatgpt.resources import _MEMORY_CACHE, load_sentinel_sdk

BOOTSTRAP = "https://chatgpt.com/backend-api/sentinel/sdk.js"
IMPLEMENTATION = "https://chatgpt.com/sentinel/test/sdk.js"
VERSIONED_IMPLEMENTATION = "https://chatgpt.com/sentinel/20260810913b/sdk.js"
SOURCE = "window.SentinelSDK = {};"


class SentinelResourceTests(unittest.TestCase):
    def setUp(self):
        _MEMORY_CACHE.clear()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.cache_root = Path(self.tempdir.name) / "chatgpt"

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

    def populate_cache(self, source=SOURCE):
        responses = {
            BOOTSTRAP: httpx.Response(200, text=f"script.src = '{IMPLEMENTATION}'"),
            IMPLEMENTATION: httpx.Response(200, text=source),
        }
        with patch.dict("os.environ", {"CHATGPT_WEB_SENTINEL_CACHE_TTL_MS": "0"}):
            with self.client_for(responses) as client:
                return load_sentinel_sdk(self.cache_root, client=client)

    def test_download_validates_bootstrap_source_and_reuses_verified_cache(self):
        bootstrap_text = (
            f"var script = document.createElement('script'); script.src = '{IMPLEMENTATION}';"
        )
        responses = {
            BOOTSTRAP: httpx.Response(200, text=bootstrap_text),
            IMPLEMENTATION: httpx.Response(200, text=SOURCE),
        }
        with patch.dict("os.environ", {"CHATGPT_WEB_SENTINEL_CACHE_TTL_MS": "60000"}):
            with patch("providers.chatgpt.resources.time.time_ns", return_value=1_000_000_000):
                with self.client_for(responses) as client:
                    loaded = load_sentinel_sdk(self.cache_root, client=client)
                _MEMORY_CACHE.clear()
                with self.client_for({}) as client:
                    cached = load_sentinel_sdk(self.cache_root, client=client)
        bootstrap_sha = hashlib.sha256(bootstrap_text.encode()).hexdigest()
        self.assertEqual(loaded.cache_status, "miss")
        self.assertEqual(loaded.implementation, IMPLEMENTATION)
        self.assertEqual(loaded.source, SOURCE)
        self.assertEqual(loaded.sha256, hashlib.sha256(SOURCE.encode()).hexdigest())
        self.assertTrue((self.cache_root / f"sdk-{bootstrap_sha[:16]}.js").is_file())
        self.assertTrue((self.cache_root / f"sdk-{loaded.sha256[:16]}.js").is_file())
        self.assertEqual(cached.cache_status, "hit")
        self.assertEqual(cached.implementation, loaded.implementation)
        self.assertEqual(cached.source, loaded.source)
        self.assertEqual(cached.sha256, loaded.sha256)

    def test_unchanged_versioned_sdk_is_not_redownloaded_when_bootstrap_is_unchanged(self):
        bootstrap_text = f"script.src = '{VERSIONED_IMPLEMENTATION}'"
        sdk_requests = 0

        def handle(request):
            nonlocal sdk_requests
            if str(request.url) == BOOTSTRAP:
                return httpx.Response(200, text=bootstrap_text)
            if str(request.url) == VERSIONED_IMPLEMENTATION:
                sdk_requests += 1
                return httpx.Response(200, text=SOURCE, headers={"ETag": f'W/"pod-{sdk_requests}"'})
            return httpx.Response(404)

        with patch.dict("os.environ", {"CHATGPT_WEB_SENTINEL_CACHE_TTL_MS": "0"}):
            with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                first = load_sentinel_sdk(self.cache_root, client=client)
                _MEMORY_CACHE.clear()
                second = load_sentinel_sdk(self.cache_root, client=client)

        self.assertEqual(first.cache_status, "miss")
        self.assertEqual(second.cache_status, "revalidated")
        self.assertEqual(sdk_requests, 1)

    def test_future_cache_timestamp_forces_refresh(self):
        self.populate_cache()
        refreshed_source = "window.SentinelSDK = { version: 2 };"
        responses = {
            BOOTSTRAP: httpx.Response(200, text=f"script.src = '{IMPLEMENTATION}'"),
            IMPLEMENTATION: httpx.Response(200, text=refreshed_source),
        }
        with patch.dict("os.environ", {"CHATGPT_WEB_SENTINEL_CACHE_TTL_MS": "60000"}):
            with patch("providers.chatgpt.resources.time.time_ns", return_value=1_000_000_000):
                _MEMORY_CACHE.clear()
                with self.client_for(responses) as client:
                    refreshed = load_sentinel_sdk(self.cache_root, client=client)

        self.assertEqual(refreshed.source, refreshed_source)
        self.assertEqual(refreshed.cache_status, "updated")

    def test_corrupt_cached_sdk_is_replaced_after_a_successful_refresh(self):
        responses = {
            BOOTSTRAP: httpx.Response(200, text=f"script.src = '{IMPLEMENTATION}'"),
            IMPLEMENTATION: httpx.Response(200, text=SOURCE, headers={"ETag": '"original"'}),
        }
        with patch.dict("os.environ", {"CHATGPT_WEB_SENTINEL_CACHE_TTL_MS": "0"}):
            with self.client_for(responses) as client:
                loaded = load_sentinel_sdk(self.cache_root, client=client)
            cached_file = self.cache_root / f"sdk-{loaded.sha256[:16]}.js"
            cached_file.write_text("tampered", encoding="utf-8")

            def refresh(request):
                if str(request.url) == IMPLEMENTATION:
                    self.assertNotIn("If-None-Match", request.headers)
                return responses[str(request.url)]

            with httpx.Client(transport=httpx.MockTransport(refresh)) as client:
                refreshed = load_sentinel_sdk(self.cache_root, client=client)
        self.assertEqual(refreshed.source, SOURCE)
        self.assertEqual(cached_file.read_text(encoding="utf-8"), SOURCE)

    def test_oversized_cached_sdk_is_replaced_without_reading_it_into_memory(self):
        responses = {
            BOOTSTRAP: httpx.Response(200, text=f"script.src = '{IMPLEMENTATION}'"),
            IMPLEMENTATION: httpx.Response(200, text=SOURCE),
        }
        with self.client_for(responses) as client:
            loaded = load_sentinel_sdk(self.cache_root, client=client)
        cached_file = self.cache_root / f"sdk-{loaded.sha256[:16]}.js"
        with cached_file.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024)
        _MEMORY_CACHE.clear()

        with patch("providers.chatgpt.resources._MAX_SDK_BYTES", 128):
            with self.client_for(responses) as client:
                already_tracing = tracemalloc.is_tracing()
                if not already_tracing:
                    tracemalloc.start()
                baseline, _ = tracemalloc.get_traced_memory()
                tracemalloc.reset_peak()
                try:
                    refreshed = load_sentinel_sdk(self.cache_root, client=client)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    if not already_tracing:
                        tracemalloc.stop()

        self.assertEqual(refreshed.source, SOURCE)
        self.assertEqual(cached_file.read_text(encoding="utf-8"), SOURCE)
        self.assertLess(peak - baseline, 2 * 1024 * 1024)

    def test_oversized_cache_manifest_is_refreshed(self):
        responses = {
            BOOTSTRAP: httpx.Response(200, text=f"script.src = '{IMPLEMENTATION}'"),
            IMPLEMENTATION: httpx.Response(200, text=SOURCE),
        }
        with self.client_for(responses) as client:
            loaded = load_sentinel_sdk(self.cache_root, client=client)
        manifest = self.cache_root / "current.json"
        manifest.write_bytes(b"x" * 1025)
        _MEMORY_CACHE.clear()

        with patch("providers.chatgpt.resources._MAX_MANIFEST_BYTES", 1024):
            with self.client_for(responses) as client:
                refreshed = load_sentinel_sdk(self.cache_root, client=client)

        self.assertEqual(refreshed.source, loaded.source)
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["sha256"], loaded.sha256)

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

    def test_verified_stale_cache_is_used_when_refresh_has_a_network_error(self):
        loaded = self.populate_cache()
        with patch.dict("os.environ", {"CHATGPT_WEB_SENTINEL_CACHE_TTL_MS": "0"}):
            with self.client_for({BOOTSTRAP: httpx.ConnectError("offline")}) as client:
                stale = load_sentinel_sdk(self.cache_root, client=client)

        self.assertEqual(stale.source, loaded.source)
        self.assertEqual(stale.cache_status, "stale-offline")

    def test_permanent_http_error_is_reported_instead_of_using_stale_cache(self):
        self.populate_cache()
        with patch.dict("os.environ", {"CHATGPT_WEB_SENTINEL_CACHE_TTL_MS": "0"}):
            with self.client_for({BOOTSTRAP: httpx.Response(403)}) as client:
                with self.assertRaisesRegex(SentinelCompatibilityError, "HTTP 403"):
                    load_sentinel_sdk(self.cache_root, client=client)

    def test_changed_bootstrap_shape_is_not_hidden_by_a_stale_cache(self):
        self.populate_cache()
        with patch.dict("os.environ", {"CHATGPT_WEB_SENTINEL_CACHE_TTL_MS": "0"}):
            with self.client_for(
                {BOOTSTRAP: httpx.Response(200, text="new bootstrap format")}
            ) as client:
                with self.assertRaisesRegex(
                    SentinelCompatibilityError, "implementation URL was not found"
                ):
                    load_sentinel_sdk(self.cache_root, client=client)

    def test_expired_verified_cache_revalidates_without_downloading_sdk(self):
        for response_header, request_header, validator in (
            ("ETag", "If-None-Match", 'W/"sdk-1"'),
            ("Last-Modified", "If-Modified-Since", "Wed, 28 Oct 2026 05:21:00 GMT"),
        ):
            with (
                self.subTest(response_header=response_header),
                tempfile.TemporaryDirectory() as folder,
            ):
                requests = []

                def handle(request):
                    requests.append(request)
                    if str(request.url) == BOOTSTRAP:
                        return httpx.Response(200, text=f"script.src = '{IMPLEMENTATION}'")
                    if request.headers.get(request_header) == validator:
                        return httpx.Response(304)
                    return httpx.Response(200, text=SOURCE, headers={response_header: validator})

                root = Path(folder)
                with patch.dict("os.environ", {"CHATGPT_WEB_SENTINEL_CACHE_TTL_MS": "0"}):
                    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                        first = load_sentinel_sdk(root, client=client)
                        second = load_sentinel_sdk(root, client=client)

                self.assertEqual(second.source, first.source)
                self.assertEqual(second.cache_status, "revalidated")
                self.assertEqual(requests[-1].headers.get(request_header), validator)
                self.assertEqual(len(requests), 4)

    def test_changed_sdk_url_does_not_reuse_old_validator(self):
        replacement = "https://chatgpt.com/sentinel/new/sdk.js"
        responses = {
            BOOTSTRAP: httpx.Response(200, text=f"script.src = '{IMPLEMENTATION}'"),
            IMPLEMENTATION: httpx.Response(200, text=SOURCE, headers={"ETag": '"old"'}),
        }
        with patch.dict("os.environ", {"CHATGPT_WEB_SENTINEL_CACHE_TTL_MS": "0"}):
            with self.client_for(responses) as client:
                load_sentinel_sdk(self.cache_root, client=client)

            def handle(request):
                if str(request.url) == BOOTSTRAP:
                    return httpx.Response(200, text=f"script.src = '{replacement}'")
                self.assertNotIn("If-None-Match", request.headers)
                return httpx.Response(200, text="new SDK", headers={"ETag": '"new"'})

            with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                updated = load_sentinel_sdk(self.cache_root, client=client)
        self.assertEqual(updated.implementation, replacement)
        self.assertEqual(updated.source, "new SDK")

    def test_changed_sdk_at_same_url_replaces_verified_cache(self):
        responses = {
            BOOTSTRAP: httpx.Response(200, text=f"script.src = '{IMPLEMENTATION}'"),
            IMPLEMENTATION: httpx.Response(200, text=SOURCE, headers={"ETag": '"old"'}),
        }
        with patch.dict("os.environ", {"CHATGPT_WEB_SENTINEL_CACHE_TTL_MS": "0"}):
            with self.client_for(responses) as client:
                load_sentinel_sdk(self.cache_root, client=client)

            def handle(request):
                if str(request.url) == BOOTSTRAP:
                    return httpx.Response(200, text=f"script.src = '{IMPLEMENTATION}'")
                self.assertEqual(request.headers.get("If-None-Match"), '"old"')
                return httpx.Response(200, text="updated SDK", headers={"ETag": '"new"'})

            with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                updated = load_sentinel_sdk(self.cache_root, client=client)
        self.assertEqual(updated.source, "updated SDK")
        self.assertEqual(updated.etag, '"new"')

    def test_unsolicited_not_modified_cannot_supply_an_sdk(self):
        responses = {
            BOOTSTRAP: httpx.Response(200, text=f"script.src = '{IMPLEMENTATION}'"),
            IMPLEMENTATION: httpx.Response(304),
        }
        with self.client_for(responses) as client:
            with self.assertRaises(SentinelCompatibilityError):
                load_sentinel_sdk(self.cache_root, client=client)

    def test_untrusted_bootstrap_url_is_rejected_even_with_stale_cache(self):
        self.populate_cache()
        responses = {
            BOOTSTRAP: httpx.Response(
                200, text="script.src = 'https://chatgpt.com.attacker.invalid/sdk.js'"
            )
        }
        with patch.dict("os.environ", {"CHATGPT_WEB_SENTINEL_CACHE_TTL_MS": "0"}):
            with self.client_for(responses) as client:
                with self.assertRaisesRegex(SentinelCompatibilityError, "not trusted"):
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

    def test_cache_manifest_contains_verified_resource_identity_and_all_files(self):
        responses = {
            BOOTSTRAP: httpx.Response(200, text=f"script.src = '{IMPLEMENTATION}'"),
            IMPLEMENTATION: httpx.Response(200, text=SOURCE),
        }
        with self.client_for(responses) as client:
            loaded = load_sentinel_sdk(self.cache_root, client=client)
        manifest = json.loads((self.cache_root / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["implementation"], loaded.implementation)
        self.assertEqual(manifest["sha256"], loaded.sha256)
        self.assertEqual(manifest["file"], f"sdk-{loaded.sha256[:16]}.js")
        self.assertIn("bootstrap_file", manifest)
        self.assertIn("bootstrap_sha256", manifest)
        self.assertEqual(len(manifest["files"]), 1)

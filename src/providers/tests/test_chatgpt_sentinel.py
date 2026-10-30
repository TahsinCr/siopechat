import base64
import hashlib
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import quickjs

from providers.chatgpt.client import ChatGPTClient
from providers.chatgpt.errors import SentinelCompatibilityError
from providers.chatgpt.resources import SIGN_SCRIPT, SentinelSDK
from providers.chatgpt.sentinel import (
    SentinelRuntime,
    _js_number,
    _json_string,
    generate_enforcement_proof,
)

FAKE_SDK_SOURCE = """
window.SentinelSDK = {
  init: async () => {
    const frame = document.createElement("iframe");
    document.body.appendChild(frame);
    frame.contentWindow.postMessage({ p: "gAAAAACdGVzdC1pbml0aWFs~S" });
  },
  token: async () => {
    const frame = document.createElement("iframe");
    document.body.appendChild(frame);
    return new Promise((resolve) => {
      window.addEventListener("message", (event) => {
        if (event.data && event.data.type === "response") {
          const req = event.data.result.cachedChatReq || {};
          const t = req.turnstile && req.turnstile.required ? "sdk-turnstile" : null;
          resolve(
            JSON.stringify({
              p: "gAAAAABdGVzdC1wcm9vZg==~S",
              t,
              c: req.token || "default-req-token",
            })
          );
        }
      });
      frame.contentWindow.postMessage({
        requestId: "req-1",
        p: "gAAAAACdGVzdC1pbml0aWFs~S",
      });
    });
  },
};
"""


def make_sdk(source: str = FAKE_SDK_SOURCE) -> SentinelSDK:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return SentinelSDK(
        implementation="https://chatgpt.com/sentinel/test/sdk.js",
        source=source,
        sha256=digest,
        cache_status="hit",
        scripts=(
            "https://chatgpt.com/backend-api/sentinel/sdk.js",
            "https://chatgpt.com/sentinel/test/sdk.js",
        ),
    )


def client_with_sentinel_cache() -> ChatGPTClient:
    client = ChatGPTClient.__new__(ChatGPTClient)
    client.headers = {"user-agent": "Mozilla/5.0 (X11; Linux x86_64)"}
    client._sentinel_lock = threading.Lock()
    client._sentinel_runtime = None
    client._sentinel_runtime_key = None
    client.session = SimpleNamespace(
        paths=SimpleNamespace(cache=Path("sentinel-cache")),
    )
    return client


class ChatGPTSentinelProofTests(unittest.TestCase):
    def test_merged_sign_script_validates_bounded_json_input(self):
        context = quickjs.Context()
        context.eval(SIGN_SCRIPT.read_text(encoding="utf-8"))
        parsed = json.loads(
            context.eval('JSON.stringify(readJsonInput(\'{"ok":true,"text":"merhaba"}\', 64))')
        )
        self.assertEqual(parsed, {"ok": True, "text": "merhaba"})

        with self.assertRaisesRegex(quickjs.JSException, "exceeds size limit"):
            context.eval('readJsonInput(\'{"padding":"0123456789"}\', 8)')
        with self.assertRaisesRegex(quickjs.JSException, "invalid JSON input"):
            context.eval("readJsonInput('{unterminated', 64)")

    def test_sentinel_runtime_reuses_initial_context_for_enforcement(self):
        sdk = make_sdk()
        with SentinelRuntime(sdk, user_agent="Mozilla/5.0") as runtime:
            initial = runtime.create_initial_proof()
            init_again, proof, turnstile = runtime.create_enforcement_tokens(
                {"token": "req-tok", "turnstile": {"required": True}}
            )
        self.assertEqual(initial, "gAAAAACdGVzdC1pbml0aWFs~S")
        self.assertEqual(init_again, initial)
        self.assertEqual(proof, "gAAAAABdGVzdC1wcm9vZg==~S")
        self.assertEqual(turnstile, "sdk-turnstile")

    def test_sentinel_runtime_resets_request_state_for_fresh_initial_proof(self):
        source = """
var SentinelSDK = (() => {
  globalThis.__siopechatTestInitCounter = (globalThis.__siopechatTestInitCounter || 0) + 1;
  return {
    init: async () => {
      const frame = document.createElement("iframe");
      frame.contentWindow.postMessage({ p: `gAAAAAC${globalThis.__siopechatTestInitCounter}~S` });
    },
    token: async () => JSON.stringify({ p: "gAAAAABcHJvb2Y~S", t: "turnstile-token" })
  };
})();
"""
        with SentinelRuntime(make_sdk(source)) as runtime:
            first = runtime.create_initial_proof()
            first_tokens = runtime.create_enforcement_tokens({"turnstile": {"required": True}})
            second = runtime.create_initial_proof()
            second_tokens = runtime.create_enforcement_tokens({"turnstile": {"required": True}})

        self.assertEqual(first, "gAAAAAC1~S")
        self.assertEqual(second, "gAAAAAC2~S")
        self.assertEqual(first_tokens, (first, "gAAAAABcHJvb2Y~S", "turnstile-token"))
        self.assertEqual(second_tokens, (second, "gAAAAABcHJvb2Y~S", "turnstile-token"))

    def test_client_reuses_one_quickjs_runtime_until_sdk_or_session_changes(self):
        client = client_with_sentinel_cache()
        response = Mock()
        response.json.return_value = {
            "prepare_token": "server-issued",
            "turnstile": {"required": True, "dx": "dynamic-challenge"},
        }
        client.request = Mock(return_value=response)
        sdk = make_sdk()
        runtime = Mock()
        runtime.create_initial_proof.side_effect = ["gAAAAACfirst~S", "gAAAAACsecond~S"]
        runtime.create_enforcement_tokens.side_effect = [
            ("gAAAAACfirst~S", "gAAAAABfirst~S", "turnstile-first"),
            ("gAAAAACsecond~S", "gAAAAABsecond~S", "turnstile-second"),
        ]

        with (
            patch("providers.chatgpt.client.load_sentinel_sdk", return_value=sdk),
            patch("providers.chatgpt.client.SentinelRuntime", return_value=runtime) as factory,
        ):
            first = client.create_sentinel_headers()
            second = client.create_sentinel_headers()

        self.assertEqual(factory.call_count, 1)
        self.assertEqual(runtime.create_initial_proof.call_count, 2)
        self.assertEqual(runtime.create_enforcement_tokens.call_count, 2)
        self.assertEqual(first["openai-sentinel-proof-token"], "gAAAAABfirst~S")
        self.assertEqual(second["openai-sentinel-proof-token"], "gAAAAABsecond~S")

        client.http = Mock()
        client.close()
        runtime.close.assert_called_once_with()

    def test_sentinel_runtime_rejects_proof_after_close(self):
        runtime = SentinelRuntime(make_sdk())
        runtime.close()
        with self.assertRaisesRegex(SentinelCompatibilityError, "closed"):
            runtime.create_initial_proof()

    def test_sentinel_runtime_rejects_sdk_hash_mismatch(self):
        sdk = make_sdk()
        tampered = SentinelSDK(sdk.implementation, sdk.source, "0" * 64, "hit")
        with self.assertRaisesRegex(SentinelCompatibilityError, "integrity check failed"):
            SentinelRuntime(tampered)

    def test_proof_matches_the_published_sdk_calculation(self):
        context = [
            3000,
            "Fri Oct 30 2026 12:00:00 GMT+0300 (GMT+03:00)",
            None,
            1,
            "Mozilla/5.0 (X11; Linux x86_64)",
            "/sentinel/20260810913b/sdk.js",
            None,
            "en-US",
            "en-US",
            0,
            "",
            [],
            [],
            0,
            "00000000-0000-4000-8000-000000000000",
            "",
            8,
            1761814800000,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ]
        encoded_context = base64.b64encode(
            json.dumps(context, separators=(",", ":"), ensure_ascii=False).encode()
        ).decode("ascii")
        initial = "gAAAAAC" + encoded_context + "~S"
        requirements = {
            "proofofwork": {
                "required": True,
                "seed": "golden-seed",
                "difficulty": "0fffff",
            }
        }
        with patch(
            "providers.chatgpt.sentinel.time.perf_counter_ns",
            side_effect=[0] + [123_490_000] * 120,
        ):
            proof = generate_enforcement_proof(initial, requirements)

        self.assertTrue(proof.startswith("gAAAAAB"))
        self.assertTrue(proof.endswith("~S"))

    def test_rejects_invalid_initial_context_and_unknown_challenge_shape(self):
        requirements = {
            "proofofwork": {
                "required": True,
                "seed": "seed",
                "difficulty": "0fffff",
            }
        }
        with self.assertRaises(SentinelCompatibilityError):
            generate_enforcement_proof("invalid", requirements)
        with self.assertRaises(SentinelCompatibilityError):
            generate_enforcement_proof("gAAAAACe30=", {"proofofwork": {"required": False}})

    def test_rejects_invalid_server_difficulty_without_weakening_validation(self):
        initial = "gAAAAAC" + base64.b64encode(b"[0,0,0,0,0,0,0,0,0,0]").decode() + "~S"
        requirements = {
            "proofofwork": {
                "required": True,
                "seed": "seed",
                "difficulty": "not-hex",
            }
        }
        with self.assertRaises(SentinelCompatibilityError):
            generate_enforcement_proof(initial, requirements)

    def test_context_serialization_matches_javascript_string_and_number_rules(self):
        self.assertEqual(
            _json_string('quote " slash \\ newline\n'), '"quote \\" slash \\\\ newline\\n"'
        )
        self.assertEqual(_json_string(chr(0xD83D) + chr(0xDE00)), '"😀"')
        self.assertEqual(_json_string(chr(0xD800)), '"\\ud800"')
        self.assertEqual(_js_number(1e20), "100000000000000000000")
        self.assertEqual(_js_number(1e21), "1e+21")
        self.assertEqual(_js_number(1e-6), "0.000001")
        self.assertEqual(_js_number(1e-7), "1e-7")
        self.assertEqual(_js_number(-0.0), "0")

    def test_uses_python_for_only_the_supported_pow_challenge(self):
        initial = (
            "gAAAAAC"
            + base64.b64encode(b'[0,0,null,0,"ua","sdk",null,"en","en",0]').decode()
            + "~S"
        )
        requirements = {
            "prepare_token": "server-issued",
            "token": "sdk-requirements-token",
            "proofofwork": {
                "required": True,
                "seed": "server-seed",
                "difficulty": "ffffffff",
            },
            "turnstile": {"required": False},
        }
        client = client_with_sentinel_cache()
        response = Mock()
        response.json.return_value = requirements
        client.request = Mock(return_value=response)

        sdk = make_sdk()
        runtime = Mock()
        runtime.__enter__ = Mock(return_value=runtime)
        runtime.__exit__ = Mock(return_value=None)
        runtime.create_initial_proof = Mock(return_value=initial)
        runtime.create_enforcement_tokens = Mock()

        with (
            patch("providers.chatgpt.client.load_sentinel_sdk", return_value=sdk) as load_sdk,
            patch("providers.chatgpt.client.SentinelRuntime", return_value=runtime),
        ):
            headers = client.create_sentinel_headers()

        self.assertEqual(
            headers["openai-sentinel-chat-requirements-prepare-token"],
            "server-issued",
        )
        self.assertTrue(headers["openai-sentinel-proof-token"].startswith("gAAAAAB"))
        load_sdk.assert_called_once_with(Path("sentinel-cache"))
        runtime.create_initial_proof.assert_called_once_with()
        runtime.create_enforcement_tokens.assert_not_called()

    def test_keeps_sdk_for_turnstile_or_new_requirement_fields(self):
        requirements = {
            "prepare_token": "server-issued",
            "proofofwork": {
                "required": True,
                "seed": "server-seed",
                "difficulty": "ffffffff",
            },
            "turnstile": {"required": True, "dx": "dynamic-server-challenge"},
        }
        client = client_with_sentinel_cache()
        response = Mock()
        response.json.return_value = requirements
        client.request = Mock(return_value=response)

        sdk = make_sdk()
        with patch("providers.chatgpt.client.load_sentinel_sdk", return_value=sdk):
            headers = client.create_sentinel_headers()

        self.assertEqual(headers["openai-sentinel-proof-token"], "gAAAAABdGVzdC1wcm9vZg==~S")
        self.assertEqual(headers["openai-sentinel-turnstile-token"], "sdk-turnstile")

        requirements["turnstile"] = {"required": False}
        requirements["future_required_challenge"] = {"required": True}
        with (
            patch("providers.chatgpt.client.load_sentinel_sdk", return_value=sdk),
            patch(
                "providers.chatgpt.client.generate_enforcement_proof",
                return_value="python-proof",
            ) as proof_generator,
        ):
            headers = client.create_sentinel_headers()
        proof_generator.assert_not_called()
        self.assertEqual(headers["openai-sentinel-proof-token"], "gAAAAABdGVzdC1wcm9vZg==~S")
        self.assertNotIn("openai-sentinel-turnstile-token", headers)


if __name__ == "__main__":
    unittest.main()

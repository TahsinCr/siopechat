import base64
import json
import unittest
from unittest.mock import Mock, patch

from providers.chatgpt.client import ChatGPTClient
from providers.chatgpt.errors import SentinelCompatibilityError
from providers.chatgpt.sentinel import _js_number, _json_string, generate_enforcement_proof


class ChatGPTSentinelProofTests(unittest.TestCase):
    def test_proof_matches_the_published_sdk_calculation(self):
        context = [
            3000,
            "Fri Oct 30 2026 12:00:00 GMT+0300 (GMT+03:00)",
            None,
            1,
            "Node.js v26.12.1",
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
            side_effect=[0] + [123_490_000] * 28,
        ):
            proof = generate_enforcement_proof(initial, requirements)

        self.assertEqual(
            proof,
            "gAAAAABWzMwMDAsIkZyaSBPY3QgMzAgMjAyNiAxMjowMDowMCBHTVQrMDMwMCAoR01UKzAzOjAwKSIsbnVsbCwyNywiTm9kZS5qcyB2MjYuMTIuMSIsIi9zZW50aW5lbC8yMDI2MDgxMDkxM2Ivc2RrLmpzIixudWxsLCJlbi1VUyIsImVuLVVTIiwxMjMsIiIsW10sW10sMCwiMDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDAwIiwiIiw4LDE3NjE4MTQ4MDAwMDAsMCwwLDAsMCwwLDAsMF0=~S",
        )

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
        client = ChatGPTClient.__new__(ChatGPTClient)
        client.run_sentinel_node = Mock(return_value={"initial": initial})
        response = Mock()
        response.json.return_value = requirements
        client.request = Mock(return_value=response)

        headers = client.create_sentinel_headers()

        self.assertEqual(
            headers["openai-sentinel-chat-requirements-prepare-token"],
            "server-issued",
        )
        self.assertTrue(headers["openai-sentinel-proof-token"].startswith("gAAAAAB"))
        self.assertEqual(client.run_sentinel_node.call_count, 1)

    def test_keeps_sdk_for_turnstile_or_new_requirement_fields(self):
        initial = "gAAAAAC" + base64.b64encode(b"[0,0,null,0,0,0,0,0,0,0]").decode() + "~S"
        requirements = {
            "prepare_token": "server-issued",
            "proofofwork": {
                "required": True,
                "seed": "server-seed",
                "difficulty": "ffffffff",
            },
            "turnstile": {"required": True, "dx": "dynamic-server-challenge"},
        }
        client = ChatGPTClient.__new__(ChatGPTClient)
        client.run_sentinel_node = Mock(
            side_effect=[
                {"initial": initial},
                {"proof": "sdk-proof", "turnstile": "sdk-turnstile"},
            ]
        )
        response = Mock()
        response.json.return_value = requirements
        client.request = Mock(return_value=response)

        headers = client.create_sentinel_headers()

        self.assertEqual(headers["openai-sentinel-proof-token"], "sdk-proof")
        self.assertEqual(headers["openai-sentinel-turnstile-token"], "sdk-turnstile")
        self.assertEqual(client.run_sentinel_node.call_count, 2)

        requirements["turnstile"] = {"required": False}
        requirements["future_required_challenge"] = {"required": True}
        client.run_sentinel_node = Mock(
            side_effect=[{"initial": initial}, {"proof": "sdk-proof", "turnstile": None}]
        )
        with patch(
            "providers.chatgpt.client.generate_enforcement_proof",
            return_value="python-proof",
        ) as proof_generator:
            client.create_sentinel_headers()
        proof_generator.assert_not_called()
        self.assertEqual(client.run_sentinel_node.call_count, 2)


if __name__ == "__main__":
    unittest.main()

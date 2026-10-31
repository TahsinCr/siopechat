import base64
import json
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from providers.chatgpt.client import ChatGPTClient
from providers.chatgpt.errors import ChatGPTWebError, SentinelCompatibilityError
from providers.chatgpt.sentinel import (
    DEFAULT_SENTINEL_SPEC,
    SentinelRuntime,
    SentinelSpec,
    _atob,
    _btoa,
    _decode_initial_context,
    _fnv1a_avalanche,
    _js_number,
    _json_string,
    _xor_strings,
    generate_enforcement_proof,
)


def encode_vm_program(instructions: list[list[object]], key: str) -> str:
    raw = json.dumps(instructions, separators=(",", ":"))
    return _btoa(_xor_strings(raw, key))


def make_client() -> ChatGPTClient:
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
    def test_sentinel_runtime_generates_valid_initial_proof_and_25_slot_fingerprint(self):
        with SentinelRuntime(user_agent="Mozilla/5.0 (X11; Linux x86_64)") as runtime:
            initial = runtime.create_initial_proof()

        self.assertTrue(initial.startswith("gAAAAAC"))
        self.assertTrue(initial.endswith("~S"))
        context = _decode_initial_context(initial)
        self.assertEqual(len(context), 25)
        self.assertEqual(context[0], 3000)
        self.assertIn("GMT", context[1])
        self.assertEqual(context[2], 2_172_649_472)
        self.assertIsInstance(context[3], int)
        self.assertEqual(context[4], "Mozilla/5.0 (X11; Linux x86_64)")
        self.assertIn(context[5], DEFAULT_SENTINEL_SPEC.scripts)
        self.assertIsNone(context[6])
        self.assertEqual(context[7], "en-US")
        self.assertEqual(context[8], "en-US")
        self.assertIsInstance(context[9], int)
        self.assertIn("\u2212", context[10])
        self.assertIn(context[11], DEFAULT_SENTINEL_SPEC.document_keys)
        self.assertIn(context[12], DEFAULT_SENTINEL_SPEC.window_keys)
        self.assertIsInstance(context[13], (int, float))
        self.assertEqual(len(context[14]), 36)
        self.assertEqual(context[15], "")
        self.assertEqual(context[16], 8)
        self.assertIsInstance(context[17], (int, float))
        self.assertEqual(context[18:], [0, 0, 0, 0, 0, 0, 0])

        encoded_payload = initial[len("gAAAAAC") : -len("~S")]
        digest = _fnv1a_avalanche(runtime._requirements_seed + encoded_payload)
        self.assertEqual(digest[0], "0")

    def test_sentinel_runtime_solves_pow_and_turnstile_vm_challenges(self):
        with SentinelRuntime(user_agent="Mozilla/5.0 (X11; Linux x86_64)") as runtime:
            initial = runtime.create_initial_proof()
            program = [
                [2, 20, "navigator"],
                [6, 21, 10, 20],
                [2, 22, "userAgent"],
                [6, 23, 21, 22],
                [2, 24, r"sentinel/[0-9a-z]+/sdk\.js"],
                [11, 25, 24],
                [2, 26, "|"],
                [5, 23, 26],
                [5, 23, 25],
                [7, 3, 23],
            ]
            dx = encode_vm_program(program, initial)
            init_used, proof, turnstile = runtime.create_enforcement_tokens(
                {
                    "prepare_token": "prep-1",
                    "proofofwork": {
                        "required": True,
                        "seed": "turnstile-seed",
                        "difficulty": "f",
                    },
                    "turnstile": {
                        "required": True,
                        "dx": dx,
                    },
                }
            )

        self.assertEqual(init_used, initial)
        self.assertTrue(proof.startswith("gAAAAAB"))
        self.assertTrue(proof.endswith("~S"))
        self.assertIsNotNone(turnstile)
        self.assertEqual(
            _atob(turnstile),
            "Mozilla/5.0 (X11; Linux x86_64)|sentinel/20260810913b/sdk.js",
        )

    def test_sentinel_vm_executes_arithmetic_conditionals_subroutines_and_rejects_broken_challenges(
        self,
    ):
        with SentinelRuntime(user_agent="Mozilla/5.0") as runtime:
            initial = runtime.create_initial_proof()
            sub_dx = encode_vm_program([[2, 40, "sub-ok"], [7, 3, 40]], initial)
            program = [
                [2, 50, 6],
                [2, 51, 7],
                [33, 52, 50, 51],
                [2, 53, 2],
                [35, 54, 52, 53],
                [27, 54, 53],
                [30, 60, 62, [61], [[2, 63, 1], [5, 61, 63], [8, 62, 61]]],
                [17, 64, 60, 54],
                [2, 65, 20],
                [20, 64, 65, 2, 70, "branch-matched"],
                [2, 71, sub_dx],
                [17, 72, 0, 71],
                [18, 72],
                [2, 73, ":"],
                [5, 70, 73],
                [5, 70, 72],
                [7, 3, 70],
            ]
            dx = encode_vm_program(program, initial)
            _, _, turnstile = runtime.create_enforcement_tokens(
                {
                    "proofofwork": {"required": False},
                    "turnstile": {"required": True, "dx": dx},
                }
            )
            self.assertEqual(_atob(turnstile), "branch-matched:sub-ok")

            reject_dx = encode_vm_program([[2, 40, "denied"], [7, 4, 40]], initial)
            with self.assertRaisesRegex(SentinelCompatibilityError, "rejected challenge"):
                runtime.create_enforcement_tokens(
                    {
                        "proofofwork": {"required": False},
                        "turnstile": {"required": True, "dx": reject_dx},
                    }
                )

            broken_dx = _btoa(_xor_strings("{not-valid-json", initial))
            with self.assertRaisesRegex(SentinelCompatibilityError, "could not be decoded"):
                runtime.create_enforcement_tokens(
                    {
                        "proofofwork": {"required": False},
                        "turnstile": {"required": True, "dx": broken_dx},
                    }
                )

            unknown_op_dx = encode_vm_program([[17, 80, 999, 50], [7, 3, 80]], initial)
            with self.assertRaisesRegex(
                SentinelCompatibilityError, "Unsupported or non-callable Sentinel VM register"
            ):
                runtime.create_enforcement_tokens(
                    {
                        "proofofwork": {"required": False},
                        "turnstile": {"required": True, "dx": unknown_op_dx},
                    }
                )

            unresolved_dx = encode_vm_program([[2, 40, "never-resolved"]], initial)
            with self.assertRaisesRegex(SentinelCompatibilityError, "without resolving a token"):
                runtime.create_enforcement_tokens(
                    {
                        "proofofwork": {"required": False},
                        "turnstile": {"required": True, "dx": unresolved_dx},
                    }
                )

            version_mismatch_dx = encode_vm_program(
                [[2, 24, r"sentinel/20999999xxxx/sdk\.js"], [11, 25, 24], [7, 3, 25]],
                initial,
            )
            with self.assertRaisesRegex(SentinelCompatibilityError, "newer SDK script version"):
                runtime.create_enforcement_tokens(
                    {
                        "proofofwork": {"required": False},
                        "turnstile": {"required": True, "dx": version_mismatch_dx},
                    }
                )

        bounded_spec = replace(DEFAULT_SENTINEL_SPEC, max_vm_steps=5)
        with SentinelRuntime(bounded_spec) as bounded_runtime:
            init_bounded = bounded_runtime.create_initial_proof()
            many_steps = [[2, 40, idx] for idx in range(10)]
            loop_dx = encode_vm_program(many_steps, init_bounded)
            with self.assertRaisesRegex(SentinelCompatibilityError, "step limit"):
                bounded_runtime.create_enforcement_tokens(
                    {
                        "proofofwork": {"required": False},
                        "turnstile": {"required": True, "dx": loop_dx},
                    }
                )

    def test_sentinel_spec_adapts_from_updated_sdk_source(self):
        future_sdk_source = """
        const sdkUrl = "https://chatgpt.com/sentinel/20261201abcd/sdk.js";
        class PowSolver {
          constructor() {
            this.requirementsTokenPrefix = "gAAAAAD";
            this.enforcementTokenPrefix = "gAAAAAE";
            this.errorPrefix = "customErrorPrefix123";
            this.maxAttempts = 250000;
          }
          getConfig() {
            return [3000, ...["ai", "webgpu", "quantum"].map(e => Number(e in window))];
          }
          getRequirementsToken() {
            return this.requirementsTokenPrefix + "tok" + "~Z";
          }
          _runCheck(e, t, n, r, o) {
            e[4] = n, e[10] = r;
          }
        }
        Rn.set(99, t => { Cn = 0, e(btoa("" + t)) });
        Rn.set(88, (e, t) => {
          const n = new RegExp(vn.get(t));
          for (const t of document.scripts) { }
        });
        """
        spec = SentinelSpec.from_sdk_source(future_sdk_source)
        self.assertEqual(spec.sdk_version, "20261201abcd")
        self.assertEqual(spec.sdk_url, "https://chatgpt.com/sentinel/20261201abcd/sdk.js")
        self.assertEqual(spec.initial_prefix, "gAAAAAD")
        self.assertEqual(spec.enforcement_prefix, "gAAAAAE")
        self.assertEqual(spec.error_prefix, "customErrorPrefix123")
        self.assertEqual(spec.proof_suffix, "~Z")
        self.assertEqual(spec.max_attempts, 250_000)
        self.assertEqual(spec.nonce_index, 4)
        self.assertEqual(spec.elapsed_index, 10)
        self.assertEqual(spec.window_probe_keys, ("ai", "webgpu", "quantum"))
        self.assertEqual(spec.vm_opcodes["resolve"], 99)
        self.assertEqual(spec.vm_opcodes["match_script"], 88)

        with SentinelRuntime(future_sdk_source, user_agent="Mozilla/5.0") as runtime:
            initial = runtime.create_initial_proof()
            self.assertTrue(initial.startswith("gAAAAAD"))
            self.assertTrue(initial.endswith("~Z"))
            dx = encode_vm_program(
                [
                    [2, 20, r"20261201abcd"],
                    [88, 21, 20],
                    [7, 99, 21],
                ],
                initial,
            )
            _, proof, turnstile = runtime.create_enforcement_tokens(
                {
                    "proofofwork": {
                        "required": True,
                        "seed": "future-seed",
                        "difficulty": "f",
                    },
                    "turnstile": {
                        "required": True,
                        "dx": dx,
                    },
                }
            )
            self.assertTrue(proof.startswith("gAAAAAE"))
            self.assertTrue(proof.endswith("~Z"))
            self.assertEqual(_atob(turnstile), "20261201abcd")

    def test_session_observer_token_execution(self):
        with SentinelRuntime(user_agent="Mozilla/5.0") as runtime:
            initial = runtime.create_initial_proof()
            dx = encode_vm_program([[2, 20, "so-snapshot-ok"], [7, 3, 20]], initial)
            token = runtime.create_session_observer_token({"required": True, "snapshot_dx": dx})
            self.assertEqual(_atob(token), "so-snapshot-ok")
            self.assertIsNone(runtime.create_session_observer_token({"required": False}))

    def test_sentinel_runtime_rejects_proof_after_close(self):
        runtime = SentinelRuntime()
        runtime.close()
        with self.assertRaisesRegex(SentinelCompatibilityError, "closed"):
            runtime.create_initial_proof()

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
            "providers.chatgpt.sentinel.proof.time.perf_counter_ns",
            side_effect=[0] + [123_490_000] * 120,
        ):
            proof = generate_enforcement_proof(initial, requirements)

        self.assertTrue(proof.startswith("gAAAAAB"))
        self.assertTrue(proof.endswith("~S"))
        solved_json = json.loads(
            base64.b64decode(proof[len("gAAAAAB") : -len("~S")]).decode("utf-8")
        )
        self.assertEqual(solved_json[9], 123)
        digest = _fnv1a_avalanche("golden-seed" + proof[len("gAAAAAB") : -len("~S")])
        self.assertLessEqual(digest[:6], "0fffff")

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

    def test_client_reuses_one_sentinel_runtime_and_handles_pow_and_turnstile_headers(self):
        client = make_client()
        pow_requirements = {
            "prepare_token": "server-issued-1",
            "token": "sdk-requirements-token",
            "proofofwork": {
                "required": True,
                "seed": "server-seed",
                "difficulty": "ffffffff",
            },
            "turnstile": {"required": False},
        }
        response = Mock()
        response.json.return_value = pow_requirements
        client.request = Mock(return_value=response)

        first = client.create_sentinel_headers()
        runtime = client._sentinel_runtime
        self.assertIsNotNone(runtime)
        self.assertEqual(
            first["openai-sentinel-chat-requirements-prepare-token"],
            "server-issued-1",
        )
        self.assertTrue(first["openai-sentinel-proof-token"].startswith("gAAAAAB"))
        self.assertNotIn("openai-sentinel-turnstile-token", first)

        def prepare_with_turnstile(_method, _url, **kwargs):
            initial_sent = kwargs["json"]["p"]
            stage3 = encode_vm_program(
                [[15, 50, 50], [1, 50, 51], [7, 3, 50]],
                "46.17",
            )
            stage2 = encode_vm_program(
                [
                    [2, 40, "Reflect"],
                    [6, 40, 10, 40],
                    [2, 41, "set"],
                    [6, 41, 40, 41],
                    [2, 42, "Object"],
                    [6, 42, 10, 42],
                    [2, 43, "create"],
                    [6, 43, 42, 43],
                    [2, 44, None],
                    [17, 50, 43, 44],
                    [2, 45, "createElement"],
                    [2, 46, "document"],
                    [6, 46, 10, 46],
                    [24, 45, 46, 45],
                    [2, 47, "canvas"],
                    [17, 48, 45, 47],
                    [2, 49, "getContext"],
                    [13, 60, 24, 49, 48, 49],
                    [2, 52, "k"],
                    [2, 53, "turnstile-solved"],
                    [7, 41, 50, 52, 53],
                    [2, 51, "46.17"],
                    [18, 54],
                    [1, 54, 51],
                    [14, 9, 54],
                ],
                "29.93",
            )
            dx = encode_vm_program(
                [
                    [2, 54, stage3],
                    [2, 55, stage2],
                    [2, 56, "29.93"],
                    [18, 55],
                    [1, 55, 56],
                    [14, 9, 55],
                ],
                initial_sent,
            )
            resp = Mock()
            resp.json.return_value = {
                "prepare_token": "server-issued-2",
                "persona": "chatgpt-paid",
                "arkose": {"required": False},
                "proofofwork": {
                    "required": True,
                    "seed": "server-seed-2",
                    "difficulty": "ffffffff",
                },
                "turnstile": {"required": True, "dx": dx},
                "so": {
                    "required": True,
                    "collector_dx": encode_vm_program([[2, 20, None], [7, 3, 20]], initial_sent),
                    "snapshot_dx": encode_vm_program(
                        [[2, 20, "so-snapshot"], [7, 3, 20]], initial_sent
                    ),
                },
            }
            return resp

        client.request = Mock(side_effect=prepare_with_turnstile)
        second = client.create_sentinel_headers()
        self.assertIs(client._sentinel_runtime, runtime)
        self.assertEqual(
            second["openai-sentinel-chat-requirements-prepare-token"],
            "server-issued-2",
        )
        self.assertTrue(second["openai-sentinel-proof-token"].startswith("gAAAAAB"))
        self.assertEqual(
            _xor_strings(_atob(second["openai-sentinel-turnstile-token"]), "46.17"),
            '{"k":"turnstile-solved"}',
        )

        for unsupported in (
            {"future_required_challenge": {"required": True}},
            {"arkose": {"required": True}},
            {"error": "blocked_by_sentinel"},
        ):
            bad_resp = Mock()
            bad_resp.json.return_value = {
                "prepare_token": "server-issued-3",
                "proofofwork": {
                    "required": True,
                    "seed": "server-seed-3",
                    "difficulty": "ffffffff",
                },
                **unsupported,
            }
            client.request = Mock(return_value=bad_resp)
            with self.assertRaises(SentinelCompatibilityError) as raised:
                client.create_sentinel_headers()
            self.assertFalse(raised.exception.message_submitted)
            self.assertEqual(raised.exception.code, "chatgpt_sentinel_incompatible")

        client.request = Mock(
            side_effect=ChatGPTWebError(
                "ChatGPT returned HTTP 403 for POST /backend-api/sentinel/chat-requirements/prepare"
            )
        )
        with self.assertRaises(SentinelCompatibilityError) as rejected_prepare:
            client.create_sentinel_headers()
        self.assertFalse(rejected_prepare.exception.message_submitted)
        self.assertEqual(rejected_prepare.exception.code, "chatgpt_sentinel_incompatible")

        client.http = Mock()
        client.close()
        self.assertIsNone(client._sentinel_runtime)


if __name__ == "__main__":
    unittest.main()

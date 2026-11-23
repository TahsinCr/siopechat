"""Remote deletion confirmation never treats network errors as absence."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from webaiprovider import RemoteNotFound, WebAIProviderError
from webaiprovider.providers.chatgpt.provider import confirm_deleted
from webaiprovider.providers.gemini.rpc import GeminiRPC


class DeletionVerificationTests(unittest.TestCase):
    def test_only_readback_retries_until_server_confirms_absence(self):
        read = Mock(side_effect=[{"id": "present"}, RemoteNotFound("deleted")])
        with patch("webaiprovider.providers.chatgpt.provider.time.sleep"):
            confirm_deleted(read)
        self.assertEqual(read.call_count, 2)

    def test_local_deletion_cannot_succeed_while_server_still_returns_the_record(self):
        with patch("webaiprovider.providers.chatgpt.provider.time.sleep"):
            with self.assertRaisesRegex(WebAIProviderError, "Sync before retrying"):
                confirm_deleted(Mock(return_value={"id": "present"}))

    def test_network_errors_do_not_count_as_confirmed_deletion(self):
        with self.assertRaisesRegex(WebAIProviderError, "network"):
            confirm_deleted(Mock(side_effect=WebAIProviderError("network")))

    def test_missing_gemini_rpc_reply_cannot_confirm_a_delete(self):
        client = SimpleNamespace(
            batch=Mock(return_value=SimpleNamespace(text="not a valid response"))
        )
        with self.assertRaises(WebAIProviderError):
            GeminiRPC(lambda: client).call("GzXR5e", ["c_fixture"], allow_empty=True)

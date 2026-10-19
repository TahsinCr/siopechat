"""Provider catalogs preserve account entitlements and native thinking selections."""

import copy
import unittest

from providers.chatgpt.discovery import model_catalog
from providers.claude.catalog import selector_catalog
from providers.errors import ProviderError


def selector():
    return {
        "model_selector_config": [
            {
                "id": "chat",
                "models": [
                    {
                        "id": "paid",
                        "name": "Paid",
                        "disabled": True,
                        "disabled_reason": {"type": "upgrade_required"},
                    },
                    {
                        "id": "allowed",
                        "name": "Allowed",
                        "thinking": {
                            "type": "effort_and_mode",
                            "effort_options": [
                                {"id": "low", "name": "Low"},
                                {"id": "medium", "name": "Medium", "recommended": True},
                            ],
                            "mode_options": [{"id": "auto", "name": "Thinking"}],
                        },
                    },
                    {"id": "disabled-by-policy", "disabled_reason": {"type": "policy"}},
                    {"id": "old", "inactive": True},
                ],
            }
        ],
        "model_selector_state": [
            {
                "id": "chat",
                "model": "paid",
                "thinking_by_model": [
                    {"id": "allowed", "thinking": {"effort": "low", "mode": "auto"}}
                ],
            }
        ],
    }


class EntitlementTests(unittest.TestCase):
    def test_unavailable_models_never_enter_the_selectable_catalog(self):
        catalog = selector_catalog(selector())
        self.assertEqual([x["id"] for x in catalog["models"]], ["allowed"])
        self.assertEqual(catalog["default_model"], "allowed")
        model = catalog["models"][0]
        self.assertEqual(
            model["selections"]["medium"], {"effort": "medium", "thinking_mode": "auto"}
        )
        self.assertEqual(next(x["id"] for x in model["efforts"] if x["selected"]), "low")

    def test_two_thinking_axes_remain_selectable_without_inventing_values(self):
        data = selector()
        model = data["model_selector_config"][0]["models"][1]
        model["thinking"]["mode_options"].append({"id": "off", "name": "Off"})
        before = copy.deepcopy(data)
        result = selector_catalog(data)["models"][0]
        self.assertEqual(
            set(result["selections"]), {"low:auto", "low:off", "medium:auto", "medium:off"}
        )
        self.assertEqual(result["selections"]["low:off"], {"effort": "low", "thinking_mode": "off"})
        self.assertEqual(data, before)

    def test_changed_or_empty_authoritative_catalog_does_not_fall_back_to_global_models(self):
        for data in [{}, {"model_selector_config": [{"id": "chat", "models": []}]}]:
            with self.assertRaises(ProviderError):
                selector_catalog(data)

    def test_gpt_disabled_or_account_inaccessible_models_are_filtered(self):
        catalog = model_catalog(
            {
                "models": [
                    {"slug": "live"},
                    {"slug": "locked", "is_available": False},
                    {"slug": "disabled", "disabled": True},
                ]
            }
        )
        self.assertEqual([m.id for m in catalog.models], ["live"])

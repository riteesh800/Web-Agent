import unittest
import os
import sys

suite_directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if suite_directory not in sys.path:
    sys.path.insert(0, suite_directory)

from core_config.environment_loader import ConfigRegistry, VaultManager


class TestSettingsValidation(unittest.TestCase):
    def test_registered_paths(self):
        self.assertTrue(ConfigRegistry.storage_root.exists())
        self.assertTrue(ConfigRegistry.ui_directory.exists())

    def test_secret_lookup_and_fallback(self):
        os.environ["CUSTOM_KEY_PRIMARY"] = "primary_secret"
        self.assertEqual(ConfigRegistry.lookup_secret("CUSTOM_KEY_PRIMARY"), "primary_secret")

        os.environ.pop("CUSTOM_KEY_PRIMARY", None)
        os.environ["CUSTOM_KEY_BACKUP"] = "backup_secret"
        self.assertEqual(ConfigRegistry.lookup_secret("CUSTOM_KEY_PRIMARY", "CUSTOM_KEY_BACKUP"), "backup_secret")

    def test_numeric_parsing(self):
        os.environ["NUMERIC_TEST_VAR"] = "32.5"
        self.assertEqual(ConfigRegistry.read_numeric_setting("NUMERIC_TEST_VAR", 10.0), 32.5)
        self.assertEqual(ConfigRegistry.read_numeric_setting("MISSING_NUMERIC_VAR", 15.0), 15.0)

    def test_vendor_signature_mapping(self):
        self.assertEqual(VaultManager.identify_service_label("sk-or-v1-test")[1], "OpenRouter")
        self.assertEqual(VaultManager.identify_service_label("gsk_test")[1], "Groq")
        self.assertEqual(VaultManager.identify_service_label("AIzaSyTest")[1], "Google Gemini")
        self.assertEqual(VaultManager.identify_service_label("AQ.Test")[1], "Google Gemini")
        self.assertEqual(VaultManager.identify_service_label("sk-test")[1], "OpenAI")

    def test_blank_token_guard(self):
        verdict_ok, service_label, outcome_note = VaultManager.store_secret_token("")
        self.assertFalse(verdict_ok)
        self.assertIn("cannot be blank", outcome_note)


if __name__ == "__main__":
    unittest.main()

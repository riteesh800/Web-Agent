import unittest
import os
import sys

suite_directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if suite_directory not in sys.path:
    sys.path.insert(0, suite_directory)

from agent_core.provider_failover import analyze_provider_exception


class TestFaultDetection(unittest.TestCase):
    def test_rate_boundary_detection(self):
        diagnostic_output = analyze_provider_exception("Error 429: Too Many Requests from api.groq.com")
        self.assertIsNotNone(diagnostic_output)
        self.assertTrue(diagnostic_output["is_limit"])
        self.assertEqual(diagnostic_output["provider_title"], "Groq")
        self.assertEqual(diagnostic_output["category"], "rate_limit")

    def test_gemini_resource_depleted(self):
        diagnostic_output = analyze_provider_exception("Google API call failed: RESOURCE_EXHAUSTED quota exceeded")
        self.assertIsNotNone(diagnostic_output)
        self.assertTrue(diagnostic_output["is_limit"])
        self.assertEqual(diagnostic_output["provider_title"], "Google Gemini")
        self.assertEqual(diagnostic_output["category"], "quota_exhausted")

    def test_pure_gemini_rate_boundary(self):
        diagnostic_output = analyze_provider_exception("Google API call failed: RESOURCE_EXHAUSTED rate limit hit")
        self.assertIsNotNone(diagnostic_output)
        self.assertTrue(diagnostic_output["is_limit"])
        self.assertEqual(diagnostic_output["provider_title"], "Google Gemini")
        self.assertEqual(diagnostic_output["category"], "rate_limit")

    def test_payment_needed_detection(self):
        diagnostic_output = analyze_provider_exception("402 Payment Required: Browser credits exhausted")
        self.assertIsNotNone(diagnostic_output)
        self.assertTrue(diagnostic_output["is_limit"])
        self.assertEqual(diagnostic_output["category"], "credits_exhausted")

    def test_free_tier_denied_detection(self):
        diagnostic_output = analyze_provider_exception("403 PERMISSION_DENIED: Free tier not supported for project")
        self.assertIsNotNone(diagnostic_output)
        self.assertTrue(diagnostic_output["is_limit"])
        self.assertEqual(diagnostic_output["category"], "tier_restricted")

    def test_benign_exception_ignored(self):
        diagnostic_output = analyze_provider_exception("KeyError: missing dict key")
        self.assertIsNone(diagnostic_output)


if __name__ == "__main__":
    unittest.main()

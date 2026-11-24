import os
import sys
import unittest

suite_directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if suite_directory not in sys.path:
    sys.path.insert(0, suite_directory)


def execute_all_verifications():
    loader_engine = unittest.TestLoader()
    suite_collector = unittest.TestSuite()

    from qa_suite.test_settings import TestSettingsValidation
    from qa_suite.test_fault_detection import TestFaultDetection
    from qa_suite.test_failover_engine import TestFailoverEngine
    from qa_suite.test_http_gateway import TestHttpGateway
    from qa_suite.test_history import TestTaskHistory
    from qa_suite.test_verification import TestHumanVerificationFlow

    suite_collector.addTests(loader_engine.loadTestsFromTestCase(TestSettingsValidation))
    suite_collector.addTests(loader_engine.loadTestsFromTestCase(TestFaultDetection))
    suite_collector.addTests(loader_engine.loadTestsFromTestCase(TestFailoverEngine))
    suite_collector.addTests(loader_engine.loadTestsFromTestCase(TestHttpGateway))
    suite_collector.addTests(loader_engine.loadTestsFromTestCase(TestTaskHistory))
    suite_collector.addTests(loader_engine.loadTestsFromTestCase(TestHumanVerificationFlow))

    runner_engine = unittest.TextTestRunner(verbosity=2)
    suite_summary = runner_engine.run(suite_collector)
    return suite_summary.wasSuccessful()


if __name__ == "__main__":
    all_passed = execute_all_verifications()
    sys.exit(0 if all_passed else 1)

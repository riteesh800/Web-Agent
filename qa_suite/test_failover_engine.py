import unittest
import asyncio
import os
import sys
from pydantic_ai.models.test import TestModel

suite_directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if suite_directory not in sys.path:
    sys.path.insert(0, suite_directory)

from agent_core.provider_failover import ResilientModelBridge


class MockEngineStub(TestModel):
    def __init__(self, simulate_failure=False, output_verdict="success_payload"):
        super().__init__()
        self.simulate_failure = simulate_failure
        self.output_verdict = output_verdict
        self.call_tally = 0

    async def request(self, message_sequence, tuning_params, call_parameters):
        self.call_tally += 1
        if self.simulate_failure:
            raise RuntimeError("Simulated Primary 429 Rate Limit")
        return self.output_verdict


class TestFailoverEngine(unittest.TestCase):
    def test_primary_engine_success(self):
        lead_stub = MockEngineStub(simulate_failure=False, output_verdict="primary_verdict")
        sub_stub = MockEngineStub(simulate_failure=False, output_verdict="backup_verdict")

        captured_logs = []
        bridge_adapter = ResilientModelBridge(
            primary_engine=lead_stub,
            secondary_engine=sub_stub,
            turnaround_limit=2.0,
            status_broadcaster=captured_logs.append,
        )

        runtime_loop = asyncio.new_event_loop()
        returned_outcome = runtime_loop.run_until_complete(bridge_adapter.request([], None, None))
        runtime_loop.close()

        self.assertEqual(returned_outcome, "primary_verdict")
        self.assertEqual(bridge_adapter.active_tier, "gemini")
        self.assertEqual(lead_stub.call_tally, 1)
        self.assertEqual(sub_stub.call_tally, 0)

    def test_failover_to_secondary_engine(self):
        lead_stub = MockEngineStub(simulate_failure=True)
        sub_stub = MockEngineStub(simulate_failure=False, output_verdict="backup_verdict")

        captured_logs = []
        bridge_adapter = ResilientModelBridge(
            primary_engine=lead_stub,
            secondary_engine=sub_stub,
            turnaround_limit=2.0,
            status_broadcaster=captured_logs.append,
        )

        runtime_loop = asyncio.new_event_loop()
        returned_outcome = runtime_loop.run_until_complete(bridge_adapter.request([], None, None))
        runtime_loop.close()

        self.assertEqual(returned_outcome, "backup_verdict")
        self.assertEqual(bridge_adapter.active_tier, "groq")
        self.assertEqual(lead_stub.call_tally, 1)
        self.assertEqual(sub_stub.call_tally, 1)
        self.assertTrue(any("fallback" in entry.lower() or "fault" in entry.lower() for entry in captured_logs))


if __name__ == "__main__":
    unittest.main()

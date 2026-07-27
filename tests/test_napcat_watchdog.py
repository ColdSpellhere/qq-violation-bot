from __future__ import annotations

import unittest

from scripts.napcat_watchdog import Metrics, State, decide


HEALTHY = Metrics(
    qq_fd_max=100,
    maps_fd_max=0,
    xvfb_fd_max=40,
    maximum_clients=False,
    websocket_established=True,
)


class WatchdogDecisionTests(unittest.TestCase):
    def test_healthy_metrics_do_not_restart(self) -> None:
        decision = decide(HEALTHY, State(), now_epoch=10_000)
        self.assertFalse(decision.restart)
        self.assertEqual(0, decision.next_state.websocket_failures)

    def test_each_resource_threshold_restarts(self) -> None:
        for metrics in (
            Metrics(1500, 0, 40, False, True),
            Metrics(100, 1000, 40, False, True),
            Metrics(100, 0, 220, False, True),
            Metrics(100, 0, 40, True, True),
        ):
            with self.subTest(metrics=metrics):
                self.assertTrue(decide(metrics, State(), 10_000).restart)

    def test_websocket_requires_two_consecutive_failures(self) -> None:
        down = Metrics(100, 0, 40, False, False)
        first = decide(down, State(), 10_000)
        second = decide(down, first.next_state, 10_300)
        self.assertFalse(first.restart)
        self.assertTrue(second.restart)

    def test_cooldown_suppresses_restart(self) -> None:
        leaking = Metrics(1500, 1000, 220, True, True)
        state = State(last_restart_epoch=9_900)
        decision = decide(leaking, state, 10_000)
        self.assertFalse(decision.restart)
        self.assertTrue(decision.cooldown_active)

    def test_scheduled_restart_is_requested_when_not_in_cooldown(self) -> None:
        decision = decide(HEALTHY, State(), 10_000, scheduled=True)
        self.assertTrue(decision.restart)
        self.assertIn("scheduled", decision.reasons)


if __name__ == "__main__":
    unittest.main()

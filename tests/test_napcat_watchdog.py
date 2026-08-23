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
    def test_runtime_target_is_isolated_per_instance(self) -> None:
        from scripts.napcat_watchdog import target_for_instance

        carrot = target_for_instance("carrot")
        kona = target_for_instance("kona")

        self.assertEqual("napcat@carrot.service", carrot.napcat_unit)
        self.assertEqual("qqbot@carrot.service", carrot.bot_unit)
        self.assertEqual(6199, carrot.port)
        self.assertEqual(6299, kona.port)
        self.assertNotEqual(carrot.state_path, kona.state_path)
        self.assertNotEqual(carrot.lock_path, kona.lock_path)
        with self.assertRaises(ValueError):
            target_for_instance("../kona")

    def test_systemd_watchdog_units_are_instance_scoped(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        service = (root / "deploy/systemd/qqbot-napcat-watchdog@.service").read_text(
            encoding="utf-8"
        )
        daily = (
            root / "deploy/systemd/qqbot-napcat-daily-restart@.service"
        ).read_text(encoding="utf-8")
        self.assertIn("--instance %i", service)
        self.assertIn("--instance %i --scheduled", daily)
        self.assertIn("/opt/qq-bots/instances/%i/current", service)

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

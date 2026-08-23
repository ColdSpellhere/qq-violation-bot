from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstanceHealthTests(unittest.TestCase):
    def test_persisted_state_is_parseable_and_kona_business_is_off(self) -> None:
        from scripts.instance_health import validate_runtime_state

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime_features.json"
            path.write_text(
                json.dumps(
                    {
                        "business_enabled": False,
                        "llm_gateway_business_enabled": False,
                    }
                ),
                encoding="utf-8",
            )
            validate_runtime_state("kona", path)
            path.write_text(
                json.dumps({"business_enabled": True}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "business"):
                validate_runtime_state("kona", path)

    def test_systemd_template_is_instance_scoped(self) -> None:
        source = (ROOT / "deploy/systemd/qqbot@.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("BOT_INSTANCE_ROOT=/opt/qq-bots/instances/%i", source)
        self.assertIn("EnvironmentFile=/opt/qq-bots/instances/%i/.env", source)
        self.assertIn("/opt/qq-bots/instances/%i/current", source)
        self.assertIn("Restart=on-failure", source)


if __name__ == "__main__":
    unittest.main()

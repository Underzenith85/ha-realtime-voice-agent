from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_repository_manifest() -> None:
    manifest = yaml.safe_load((ROOT / "repository.yaml").read_text())
    assert manifest["name"] == "Home Assistant Realtime Voice Agent"


def test_addon_manifest() -> None:
    manifest = yaml.safe_load((ROOT / "realtime_voice/config.yaml").read_text())
    assert manifest["slug"] == "realtime_voice_agent"
    assert manifest["ingress"] is True
    assert manifest["ports"]["8099/tcp"] == 8099
    assert manifest["homeassistant"] == "2026.8.0"

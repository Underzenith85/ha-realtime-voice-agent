from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_repository_manifest() -> None:
    manifest = yaml.safe_load((ROOT / "repository.yaml").read_text())
    assert manifest["name"] == "Home Assistant Realtime Voice Agent"
    assert manifest["url"] == "https://github.com/Underzenith85/ha-realtime-voice-agent"
    assert manifest["maintainer"]


def test_addon_manifest() -> None:
    manifest = yaml.safe_load((ROOT / "realtime_voice/config.yaml").read_text())
    assert manifest["slug"] == "realtime_voice_agent"
    assert manifest["ingress"] is True
    assert manifest["ports"]["8099/tcp"] == 8099
    assert manifest["homeassistant"] == "2026.8.0"
    assert set(manifest["arch"]) == {"amd64", "aarch64"}
    assert (ROOT / "realtime_voice/Dockerfile").is_file()
    assert (ROOT / "realtime_voice/DOCS.md").is_file()
    assert manifest["version"] == "0.10.0"
    assert manifest["options"]["hardware_clients"] == []
    assert (ROOT / "esphome/components/realtime_voice_client/realtime_voice_client.cpp").is_file()
    assert (ROOT / "docs/voice-pe.md").is_file()


def test_service_starts_python_from_root() -> None:
    run_script = (ROOT / "realtime_voice/rootfs/etc/services.d/realtime-voice/run").read_text()
    commands = [line.strip() for line in run_script.splitlines() if line.strip()]
    assert commands[-2:] == ["cd /", "exec python3 -m app"]

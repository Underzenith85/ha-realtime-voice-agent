from pathlib import Path

WEB = Path(__file__).parents[1] / "realtime_voice/rootfs/app/web"


def test_push_to_talk_has_accessible_keyboard_and_pressed_states() -> None:
    html = (WEB / "index.html").read_text()
    javascript = (WEB / "app.js").read_text()

    assert 'aria-pressed="false"' in html
    assert 'role="status"' in html
    assert 'event.code === "Space"' in javascript
    assert 'setAttribute("aria-pressed", "true")' in javascript
    assert 'setAttribute("aria-pressed", "false")' in javascript


def test_capture_and_reconnect_resources_are_deduplicated() -> None:
    javascript = (WEB / "app.js").read_text()

    assert "if (!workletLoaded)" in javascript
    assert "const currentGeneration = ++generation" in javascript
    assert "currentGeneration !== generation" in javascript
    assert "microphoneStream?.getTracks()" in javascript
    assert "Microphone permission was denied" in javascript


def test_route_test_cancel_reset_and_phase_diagnostics_are_present() -> None:
    html = (WEB / "index.html").read_text()
    javascript = (WEB / "app.js").read_text()

    for element_id in ("testRoute", "cancel", "resetConversation", "phaseSummary"):
        assert f'id="{element_id}"' in html
    for phase in ("listening", "thinking", "tool use", "speaking", "reconnecting", "error"):
        assert phase in javascript

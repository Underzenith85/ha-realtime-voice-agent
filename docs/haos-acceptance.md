# Home Assistant OS acceptance record

Use this checklist on a fresh, supported Home Assistant OS system before publishing a
tagged release. Record the HA OS, Core, Supervisor, App, browser, and speaker versions.
Redact tokens, authorization headers, signed media URLs, entity state values, and spoken
content before attaching evidence to issue #2.

## Test record

| Field | Value |
| --- | --- |
| Date and tester | |
| HA OS / Core / Supervisor | |
| Host architecture | |
| App version and commit | |
| Browser and client OS | |
| Speaker integration / model | |
| Result | Pending |

## Fresh installation and startup

- [ ] Start from a system where this custom App repository has not been installed.
- [ ] Add the repository URL from the README and install Realtime Voice Agent.
- [ ] Confirm the Configuration tab renders, save the documented minimum options, and
      start the App without editing files inside its container.
- [ ] Confirm **Open Web UI** loads only through ingress and direct port 8099 rejects
      `/`, `/static/*`, and `/ws` with HTTP 403.
- [ ] Confirm App logs contain no traceback, credential, transcript, raw MCP argument,
      or audio data. Save a redacted startup excerpt.
- [ ] Restart the App and Home Assistant host independently and confirm normal startup.

Evidence:

```text
Paste redacted versions and relevant log lines here or in issue #2.
```

## Home Assistant MCP boundary

- [ ] Install and configure Home Assistant's official MCP Server integration.
- [ ] Expose one harmless readable entity and one test light to Assist; keep a second
      test entity unexposed.
- [ ] Ask the agent to read the exposed entity and operate the exposed light.
- [ ] Ask for the unexposed entity and confirm its identity/state is unavailable and no
      service call targets it.
- [ ] Temporarily disable the MCP integration, confirm the voice session stays usable
      and reports the server unavailable, then re-enable it and use **Refresh tools**.
- [ ] Confirm recovery without restarting the App and save redacted logs.

## Browser audio and lifecycle

- [ ] Use an HTTPS or browser-trusted local HA URL and grant microphone access.
- [ ] Complete one user speech → model → browser speech round trip.
- [ ] Start two browsers and confirm their transcripts, histories, and routes remain
      independent.
- [ ] Interrupt a spoken response with **Cancel response** and a new push-to-talk turn.
- [ ] Disable/re-enable the network or sleep/wake the client; confirm one clean
      reconnect and no duplicate capture/session.
- [ ] Reset the conversation and confirm remembered-turn diagnostics return to zero.

## Generic speaker

- [ ] Set `speaker_base_url` to a trusted-LAN address the speaker can reach.
- [ ] Select a real `media_player`, choose buffered mode, and run **Test output**.
- [ ] Route a model response to it and confirm intelligible audio begins and completes.
- [ ] Start a second response during playback and confirm older playback is stopped.
- [ ] Stop/unavailable the speaker and confirm a structured failure or browser fallback.
- [ ] Record integration, device model, announcement setting, latency, and restoration
      behavior in `docs/speaker-compatibility.md`.

## Privacy and rollback

- [ ] Inspect `/data/routes.json` and `/data/timers.json`: routes/timer metadata may be
      present; credentials, transcripts, raw audio, and MCP arguments must not be.
- [ ] Confirm diagnostics and browser messages contain no OpenAI key, Supervisor token,
      MCP bearer/header value, device credential, or signed URL after it is consumed.
- [ ] Confirm each media URL is single-use and expires if it is not fetched.
- [ ] Export the redacted App configuration and note the currently installed version.
- [ ] Install the previous App version from a local backup or clean reinstall, restore
      compatible options, and confirm startup. Reinstall the candidate afterward.

## Sign-off

All boxes must be checked, with redacted evidence linked from issue #2. Any failure gets
a separate issue and blocks release sign-off. Once issue #2 is accepted, use the release
gate in issue #12; do not tag solely from the automated integration harness.

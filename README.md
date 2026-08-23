# Home Assistant Realtime Voice Agent

An experimental Home Assistant App (formerly add-on) that streams browser microphone audio to
OpenAI `gpt-realtime-2.1`, exposes Home Assistant and selected MCP tools to the model,
and routes reply audio either to the browser or a Home Assistant `media_player`.

This repository is a **custom Home Assistant App Store repository**. It is not a
HACS repository or custom integration. Home Assistant builds and runs the voice gateway
as an App container, which is required for the long-lived Realtime, MCP, and audio services.

## Current capabilities

- Push-to-talk browser client behind Home Assistant ingress.
- Independent concurrent Realtime sessions.
- Home Assistant control through the official MCP Server integration.
- Additional Streamable HTTP and legacy SSE MCP servers with bearer/custom headers.
- Explicit external-tool allowlists and namespaced function names.
- Browser playback or per-client HA speaker routing.
- Compatible buffered MP3 announcements and experimental progressive MP3 playback.
- Short-lived random media URLs; audio and transcripts are not persisted.
- Automatic Realtime reconnect with a bounded, per-client text/tool history (20 turns
  by default), idle expiration, and session diagnostics.
- Persistent client-scoped voice timers and alarms with create, list, cancel, pause, and
  resume tools plus completion audio through the originating client's route.

OAuth MCP servers should initially be configured through Home Assistant's built-in MCP
client integration, which owns the OAuth flow and contributes its tools to Assist. A
pre-issued OAuth access token can also be supplied as an add-on MCP bearer token.

The App automatically discovers MCP APIs managed by Home Assistant and accesses them
through internal credential-free proxy endpoints; remote OAuth tokens never enter the
App. See [OAuth-protected MCP servers](docs/oauth-mcp.md) for onboarding and the current
dynamic-client-registration limitation.

## Requirements

- Home Assistant OS or Home Assistant Supervised with access to the App/Add-on Store.
- Home Assistant 2026.8.0 or newer.
- An OpenAI API key with access to `gpt-realtime-2.1`.
- A browser with microphone access (HTTPS or a browser-trusted local Home Assistant URL).

Home Assistant Container and Home Assistant Core installations do not include the App
Store and cannot install this repository directly.

## Install from the custom App Store repository

[![Add repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FUnderzenith85%2Fha-realtime-voice-agent)

Repository URL:

```text
https://github.com/Underzenith85/ha-realtime-voice-agent
```

1. In Home Assistant, open **Settings → Apps → App store**. On versions that still use
   the former name, open **Settings → Add-ons → Add-on store**.
2. Open the store menu, choose **Repositories**, paste the repository URL above, and
   select **Add**.
3. Refresh the store if necessary, then open and install **Realtime Voice Agent**.
4. Add Home Assistant's official **Model Context Protocol Server** integration and
   enable Home Assistant control.
5. Under **Settings → Voice assistants → Expose**, expose only the entities the agent
   is allowed to read or control.
6. On the App's **Configuration** tab, enter `openai_api_key`. Review the model, voice,
   session limits, speaker URL, and MCP server allowlists before saving.
7. Start the App, enable **Start on boot**, inspect the log for startup errors, and then
   select **Open Web UI**.
8. Grant microphone access and hold the talk button while speaking.

Updates are delivered through the same custom repository. When a newer version appears
in the App Store, review the release notes and select **Update**.

### Troubleshooting installation

- If the repository is rejected, confirm that the URL is the repository root and that
  Home Assistant can reach GitHub.
- If the App does not appear, reload the App Store or restart Home Assistant after adding
  the repository.
- If the install is unavailable, verify the host architecture is `amd64` or `aarch64`.
- If **Open Web UI** is missing, start the App first and check its log.
- This project is not installed through HACS; adding it as a HACS custom repository will
  not work.

## Route replies to Home Assistant speakers

For a LAN speaker, set `speaker_base_url` to an address the physical speaker can reach,
normally `http://<home-assistant-lan-ip>:8099`. Direct requests on port 8099 can access
only random, five-minute audio URLs; the control UI and WebSocket require HA ingress.
Do not publish the port publicly.

After opening the Web UI, select a `media_player` and choose buffered or progressive
playback. Buffered playback is the compatibility-first option. Progressive playback has
lower perceived latency but depends on the player integration accepting a growing MP3
stream. Browser playback requires no direct port exposure.

See the [speaker compatibility matrix](docs/speaker-compatibility.md) for adaptive
fallback, latest-response-wins behavior, and the remaining physical-device checks.

## Additional MCP servers

Each server entry has this shape:

```yaml
name: tasks
url: https://example.internal/mcp
transport: streamable_http
token: optional-bearer-token
headers: []
allowed_tools:
  - list_tasks
  - complete_task
```

An empty external `allowed_tools` list exposes no tools. Home Assistant's own curated
Assist tool set is controlled by HA's exposed-entity configuration.

MCP connections are monitored and reconnect with bounded exponential backoff. An
unavailable optional server does not prevent Home Assistant tools from starting. The
browser's **MCP tools** panel shows credential-free health and schema diagnostics and can
refresh the catalog explicitly. Active Realtime sessions receive changed tools before
their next push-to-talk turn. If Home Assistant's `/api/mcp/assist` endpoint returns a
confirmed 404, the App retries the configured base `/api/mcp` endpoint; authentication
and other failures do not trigger that fallback.

## Session lifecycle

Each browser has an isolated OpenAI Realtime session. `max_sessions` limits concurrent
clients and `idle_timeout_seconds` closes inactive sessions. On an upstream disconnect,
the App reconnects automatically and rebuilds context from input transcriptions,
assistant transcripts, and tool results. `history_turn_limit` bounds that in-memory
context (20 turns by default); captured and generated audio is not retained. A session
runs at most four tool calls concurrently and cancels its reader, encoder, and tool tasks
when the browser disconnects.

MCP calls are schema-validated before dispatch, bounded by per-server timeouts and a
shared concurrency limit, and recorded in redacted audit logs using a hash of arguments.
Media links are single-use as well as short-lived. Per-minute limits bound browser
connection attempts, media fetches, and tool calls. The broker also exposes an optional
policy callback for installations that need to deny or externally approve selected
high-risk tools; no policy callback preserves the default no-confirmation flow.

The browser UI exposes its current route and listening/thinking/tool/speaking phase,
reports first-response latency and structured errors, and supports accessible Space-key
push-to-talk. Output routes can be tested before saving. Explicit cancel and conversation
reset controls are available, and network/sleep reconnects are guarded against duplicate
sessions and capture resources.

## Timers and alarms

Timers are App-owned rather than Home Assistant `timer` helpers: this preserves the
originating browser identity needed for completion routing and natural follow-ups. They
are persisted in `/data/timers.json`, survive App restarts, and are scoped so one client
cannot list or modify another client's timers. Alarms accept an RFC 3339 timestamp with
an explicit timezone. A disconnected client's completed timer is retained and announced
after that client reconnects. If its configured speaker is unavailable, completion PCM
is sent to the browser instead. A full Home Assistant host restart may delay a completion
until the App starts; overdue jobs fire immediately on startup.

## Development

```bash
uv sync
uv run pytest -m "not integration"
uv run pytest -m integration
uv run ruff check .
```

The integration suite starts local fake OpenAI Realtime, Streamable HTTP/SSE MCP, and
Home Assistant API services. It exercises browser and speaker routes, concurrent clients,
tool calls, cancellation, reconnects, and media delivery without paid API access or HA
hardware.

The App requires `ffmpeg` at runtime for generic speaker output; it is included in the
App image.

## Security notes

- OpenAI and MCP credentials remain server-side.
- Tool results are capped before returning to the model.
- There is no per-call confirmation. Entity exposure and MCP allowlists are the safety
  boundary, so review both carefully.
- Progressive playback compatibility and cancellation vary by speaker integration.

# Home Assistant Realtime Voice Agent

An experimental Home Assistant OS add-on that streams browser microphone audio to
OpenAI `gpt-realtime-2.1`, exposes Home Assistant and selected MCP tools to the model,
and routes reply audio either to the browser or a Home Assistant `media_player`.

## Current capabilities

- Push-to-talk browser client behind Home Assistant ingress.
- Independent concurrent Realtime sessions.
- Home Assistant control through the official MCP Server integration.
- Additional Streamable HTTP and legacy SSE MCP servers with bearer/custom headers.
- Explicit external-tool allowlists and namespaced function names.
- Browser playback or per-client HA speaker routing.
- Compatible buffered MP3 announcements and experimental progressive MP3 playback.
- Short-lived random media URLs; audio and transcripts are not persisted.

OAuth MCP servers should initially be configured through Home Assistant's built-in MCP
client integration, which owns the OAuth flow and contributes its tools to Assist. A
pre-issued OAuth access token can also be supplied as an add-on MCP bearer token.

## Install on Home Assistant OS

1. Add this GitHub repository to **Settings → Add-ons → Add-on store → Repositories**.
2. Install **Realtime Voice Agent**.
3. Add the official **Model Context Protocol Server** integration and enable Home
   Assistant control.
4. Expose only the entities the agent should control under **Voice assistants → Expose**.
5. Configure the add-on with an OpenAI API key and start it.
6. Open the add-on Web UI, grant microphone access, and hold the talk button.

For a LAN speaker, set `speaker_base_url` to an address the physical speaker can reach,
normally `http://<home-assistant-lan-ip>:8099`. Direct requests on port 8099 can access
only random, five-minute audio URLs; the control UI and WebSocket require HA ingress.
Do not publish the port publicly.

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

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
```

The add-on requires `ffmpeg` at runtime for generic speaker output.

## Security notes

- OpenAI and MCP credentials remain server-side.
- Tool results are capped before returning to the model.
- There is no per-call confirmation. Entity exposure and MCP allowlists are the safety
  boundary, so review both carefully.
- Progressive playback compatibility and cancellation vary by speaker integration.

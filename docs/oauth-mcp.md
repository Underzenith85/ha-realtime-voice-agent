# OAuth-protected MCP servers

OAuth ownership belongs to Home Assistant, not the Realtime Voice Agent App. This keeps
access and refresh tokens in encrypted-sensitive Home Assistant config-entry data and
out of App options, logs, diagnostics, browser storage, and OpenAI traffic.

## Onboarding

1. In **Settings → Devices & services**, add the built-in **Model Context Protocol**
   integration and enter the remote Streamable HTTP or SSE endpoint.
2. When authentication is required, Home Assistant discovers protected-resource and
   authorization-server metadata and starts its OAuth authorization-code flow with PKCE.
3. If the provider uses a pre-registered client, add its client ID and secret through
   **Application Credentials** when prompted.
4. Ensure Home Assistant's **Model Context Protocol Server** integration is enabled.
5. Refresh tools in the App (or restart it). The App discovers HA LLM APIs whose IDs
   start with `mcp-` and connects to their internal `/api/mcp/<id>` endpoints using its
   Supervisor identity.

Home Assistant refreshes tokens before calls. A rejected refresh or remote HTTP 401
starts Home Assistant's reauthentication flow; the voice App sees only an unavailable
tool/error and never the credential. Removing the HA MCP config entry removes it from the
App at the next tool refresh.

## Dynamic client registration

As of the Home Assistant 2026.8 development line, the built-in MCP flow supports OAuth
metadata discovery, PKCE, Application Credentials, refresh, and reauthentication, but
its “new credentials” step still stops when no client credentials exist. It does not yet
consume an advertised dynamic-registration endpoint. Servers that require RFC 7591
dynamic client registration therefore remain unsupported until Home Assistant adds that
capability (or a separately reviewed companion integration owns it). This limitation is
tracked in issue #4 and is intentionally not worked around by storing tokens in the App.

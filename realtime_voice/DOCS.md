# Realtime Voice Agent

This Home Assistant App (formerly add-on) is installed by adding the parent GitHub
repository as a custom repository under **Settings → Apps → App store → Repositories**.
It is not installed through HACS. See the repository [README](../README.md) for the
complete installation and security guidance, and [architecture](../docs/architecture.md)
for MCP and audio flow diagrams.

The minimum setup is an OpenAI API key plus Home Assistant's official MCP Server
integration. The App automatically authenticates to Home Assistant with its
Supervisor token.

The **MCP tools** panel in the Web UI reports connection and schema health without
showing server URLs, headers, or credentials. Optional servers reconnect automatically
with bounded backoff and can be retried immediately with **Refresh tools**. Catalog
changes are applied to an active voice session before its next turn.

Realtime sessions expire after `idle_timeout_seconds` without browser activity. If the
OpenAI connection drops or expires first, the App reconnects with bounded exponential
backoff and restores up to `history_turn_limit` transcribed turns (20 by default),
including text transcripts and tool results. Audio itself is never retained. The Web
UI's **Session** diagnostics show active session count, age, idle time, reconnects, and
remembered turns. Each session permits at most four simultaneous tool calls.

MCP arguments are validated against the advertised schema before dispatch. Calls have
per-server timeouts plus a shared concurrency limit, and audit logs contain only tool and
server names, timing, outcome, client ID, and an argument hash—not raw arguments. Media
URLs are short-lived and single-use. Browser connections, tool calls, and media requests
have configurable per-minute limits. An installation that needs approval for high-risk
tools can supply the broker policy hook; without one, the existing no-confirmation
behavior remains unchanged.

The Web UI shows the current saved route, active listening/thinking/tool/speaking phase,
first-response latency, and session/tool diagnostics. **Test output** checks a selected
browser or speaker route without saving it. **Cancel response** interrupts playback, and
**Reset conversation** clears retained context. Push-to-talk works by pointer or by
holding Space when focus is not in a form control. Microphone permission failures include
recovery guidance, and reconnects reuse capture resources without opening overlapping
sessions.

For generic speakers, publish port 8099 only to the trusted LAN and set
`speaker_base_url` to the exact address those devices use to reach the add-on.
Buffered mode is recommended. Progressive mode reports request timing and can retry the
same response in buffered mode when Home Assistant rejects the initial playback request.
The latest response targeting an entity stops older playback first. See the repository's
speaker compatibility matrix for device-specific validation status.

Voice timers and alarms are persisted by the App and scoped to the browser/client that
created them. The agent can create, list, cancel, pause, and resume timers, and ordered
list positions support follow-ups such as “cancel the second timer.” Completion speech
uses that client's saved output route. Disconnected completions wait for reconnect;
unavailable speakers fall back to browser audio. Overdue timers fire when the App returns
after a restart.

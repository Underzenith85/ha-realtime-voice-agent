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

For generic speakers, publish port 8099 only to the trusted LAN and set
`speaker_base_url` to the exact address those devices use to reach the add-on.

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

For generic speakers, publish port 8099 only to the trusted LAN and set
`speaker_base_url` to the exact address those devices use to reach the add-on.

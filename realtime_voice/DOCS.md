# Realtime Voice Agent add-on

See the repository [README](../README.md) for installation and security guidance,
and [architecture](../docs/architecture.md) for MCP and audio flow diagrams.

The minimum setup is an OpenAI API key plus Home Assistant's official MCP Server
integration. The add-on automatically authenticates to Home Assistant with its
Supervisor token.

For generic speakers, publish port 8099 only to the trusted LAN and set
`speaker_base_url` to the exact address those devices use to reach the add-on.

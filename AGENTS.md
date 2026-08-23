# AGENTS.md

## Project overview

This repository is a custom Home Assistant App Store repository. Its primary App,
Realtime Voice Agent, streams browser microphone audio to the OpenAI Realtime API,
exposes Home Assistant and explicitly allowed MCP tools to the model, and can route
reply audio to either the browser or a Home Assistant media player.

The App source and manifest live under `realtime_voice/`. It runs as a container on
Home Assistant OS or Home Assistant Supervised; it is not a HACS integration.

## Development expectations

- Keep credentials and Realtime/MCP sessions server-side.
- Preserve the exposed-entity and MCP allowlist security boundaries.
- Run `uv run pytest` and `uv run ruff check .` after Python changes.
- Keep documentation consistent with App behavior and configuration.

## Versioning

The App Store detects updates from the `version` field in
`realtime_voice/config.yaml`. Follow Semantic Versioning for every App release and
never merge a change to the shipped App without an appropriate version bump:

- Patch (`x.y.Z`): backward-compatible bug fixes and internal improvements.
- Minor (`x.Y.0`): backward-compatible features or configuration additions.
- Major (`X.0.0`): breaking behavior, configuration, or compatibility changes.

Reset lower-order components when incrementing a higher-order component. Do not reuse
an already published version, and do not make unrelated version bumps in docs-only
changes that do not alter the shipped App.

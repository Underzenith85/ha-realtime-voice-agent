# Changelog

## 0.10.9

- Replace consecutive Sonos media directly instead of issuing `media_stop` before every
  turn, avoiding stop/play races that clip multi-turn reply audio.

## 0.10.8

- Keep signed speaker media readable for its short TTL and support HTTP byte ranges so
  Sonos retry and buffering requests do not receive `404 Not Found` after the first GET.

## 0.10.7

- Add an `.mp3` suffix to signed speaker media URLs so Sonos identifies the stream's
  MIME type correctly, and include sanitized request details in route-test diagnostics.

## 0.10.6

- Classify signed speaker URLs as `music` for Home Assistant media-player services so
  Sonos accepts normal playback instead of rejecting the MIME type as invalid content.

## 0.10.5

- Report sanitized Home Assistant speaker operation, HTTP status, and response detail in
  App logs and the Web UI while redacting signed media tokens.

## 0.10.4

- Continue Sonos and other speaker playback when Home Assistant rejects a best-effort
  stop for a prior response that has already ended.

## 0.10.3

- Allow media players such as Sonos to probe signed audio URLs with `HEAD` without
  consuming the single-use token before the subsequent audio `GET` request.

## 0.10.2

- Stop reconnecting after terminal OpenAI account and credential errors, preserving the
  actionable upstream error in the Web UI instead of entering an endless retry loop.

## 0.10.1

- Register the Voice PE WebSocket dependency as an ESP-IDF managed component so the
  reference firmware compiles with ESPHome 2026.8.0 and ESP-IDF 5.5.5.
- Compile an ESP32-S3 microphone/resampling-speaker fixture in CI to catch ESPHome
  schema, code generation, managed-component, and C++ API regressions.

## 0.10.0

- Add an authenticated direct WebSocket endpoint for individually provisioned hardware
  clients, with persistent per-device default routes.
- Add a Home Assistant Voice PE ESPHome reference client for 24 kHz PCM capture and
  playback, wake-word activation, silence completion, and button cancellation.
- Document credential generation/revocation, ESPHome provisioning, OTA, stock rollback,
  and the remaining physical-device validation matrix.

## 0.1.1

- Fix App startup when the s6 service is launched outside the filesystem root.

## 0.1.0

- Initial experimental release.

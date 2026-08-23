# Changelog

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

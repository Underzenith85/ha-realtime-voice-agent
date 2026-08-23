# Home Assistant Voice PE developer client

Version 0.10.0 adds a dedicated, authenticated hardware endpoint and a reference
ESPHome external component for Home Assistant Voice Preview Edition. This is a
developer preview until the physical-device checklist in issue #10 is complete; keep
the stock firmware available for rollback.

## Provision a device credential

Generate a different credential for every device. Store the raw value only in ESPHome
`secrets.yaml`; the App stores only its SHA-256 digest.

```bash
TOKEN=$(openssl rand -hex 32)
printf '%s\n' "$TOKEN"
printf '%s' "$TOKEN" | sha256sum
```

Add an entry on the App Configuration tab, using the digest (without the filename or
spaces) as `token_sha256`:

```yaml
hardware_clients:
  - client_id: kitchen-voice-pe
    name: Kitchen Voice PE
    token_sha256: 64-character-sha256-digest
    entity_id: null
    mode: buffered
    announce: true
```

Omit `entity_id` to play the 24 kHz, mono, signed 16-bit PCM response on the Voice PE
itself. Set it to a `media_player` to make that room's persistent default route an HA
speaker instead. Existing saved routes take precedence. To revoke a device, remove its
entry (or replace its digest), save the App configuration, and restart the App.

The direct endpoint is `ws://<home-assistant-lan-address>:8099/device/ws`. It is the
only non-ingress control endpoint and rejects missing, invalid, or mismatched device
credentials before opening an OpenAI session. Do not expose port 8099 to the internet.

## Build the ESPHome client

Start from the official Voice PE ESPHome source matching the firmware release installed
on the device. Copy `esphome/components/realtime_voice_client` beside the device YAML,
merge `esphome/voice-pe.example.yaml`, and copy the two values from
`esphome/secrets.example.yaml` into the private ESPHome `secrets.yaml`.

The component requires the microphone source to be configured as 24 kHz. It requests
one 16-bit channel from `i2s_mics` and sends the App's version-1 PCM stream unchanged.
It writes response PCM to the stock `announcement_resampling_speaker`, which converts
it for the Voice PE's 48 kHz mixer. Disable/remove the stock `voice_assistant` component
and its start/stop automations so it does not contend for the same microphone and
speaker. Preserve the stock hardware, mute, factory-reset, and OTA sections.

The supplied automation maps wake-word detection to a turn and maps the center button
to start/cancel. Local energy detection stops after 900 ms of silence following speech,
with a 15-second safety limit. The component exposes `phase()` values `disconnected`,
`connecting`, `ready`, `listening`, `thinking`, `tool`, `speaking`, and `error`; map
those values into the stock LED control script during hardware validation rather than
replacing the factory-reset and mute indications.

Compile and install the first custom build over USB from ESPHome Device Builder. After
that, normal updates can use ESPHome OTA. Never put the raw token in a public YAML,
repository, log, or issue.

## Rollback

Before flashing, download the stock release matching the device and retain the ESPHome
backup. If the custom image does not boot or audio fails, connect the Voice PE over USB
and use the official Voice PE web installer linked by the upstream firmware repository
to reinstall factory firmware. Re-adopt the device in Home Assistant afterward if
needed. Removing the App credential immediately revokes the abandoned custom image.

Upstream references:

- [Official Voice PE firmware source](https://github.com/esphome/home-assistant-voice-pe)
- [ESPHome microphone source configuration](https://esphome.io/components/microphone/)
- [ESPHome speaker component](https://esphome.io/components/speaker/)

## Physical validation still required

- Confirm 24 kHz microphone capture and audible local response on retail Voice PE.
- Tune silence detection for near/far speech and background noise.
- Map every phase to the stock LED effects without masking mute or reset states.
- Confirm center-button and stop-word interruption during model speech.
- Exercise browser and Voice PE sessions concurrently against a real App.
- Perform OTA upgrade and USB stock-firmware rollback on hardware.

Record results in issue #10. These checks cannot be established by the repository's
containerized CI suite.

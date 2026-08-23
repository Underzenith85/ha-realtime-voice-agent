# Speaker compatibility

Buffered playback is the recommended default because Home Assistant serves a complete
MP3 before asking the target to play it. Progressive playback starts the request earlier,
but support for a growing HTTP response depends on the media-player integration and
device firmware.

| Target | Buffered | Progressive | Announcement / restoration | Status |
| --- | --- | --- | --- | --- |
| Google Cast | Recommended | Test before saving | Device test required | Hardware validation pending |
| Sonos | Recommended | Test before saving | Device test required | HA service acceptance verified; audible playback pending |
| Music Assistant | Recommended | Test before saving | Device test required | Hardware validation pending |

Use **Test output** for each mode. A successful result means Home Assistant accepted the
`play_media` request and shows its request latency; only listening at the device proves
audible playback and restoration. The browser remembers the last saved mode per entity.

If a progressive `play_media` request fails immediately and **Retry progressive failures
in buffered mode** is enabled, the same response is retained as PCM, encoded after
completion, and retried in buffered mode. The UI reports both the failure and fallback.
Failures after a device accepts the request cannot be detected generically by Home
Assistant, so use buffered mode when a progressive test is silent or stalls.

When two sessions target one entity, the newest playback request stops the older one
before starting. Barge-in also stops the target and cancels the progressive encoder and
stream pump. Announcement restoration and volume behavior remain integration-specific;
validate both on the physical target before relying on them.

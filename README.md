# Intercom

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/will-roscoe/intercom/actions/workflows/validate.yaml/badge.svg)](https://github.com/will-roscoe/intercom/actions/workflows/validate.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A Home Assistant integration that turns any set of speakers and phones into an
**intercom / broadcast** system. Type a message, choose which media players to
**speak** it on (with an optional volume) and which **notify** targets to send it
to, and press one button.

It ships with a self-contained Lovelace card, so there is nothing to wire up with
helpers unless you want to.

---

## Features

- **One service, `intercom.broadcast`** — speak a message on media players via
  `tts.speak`, and/or push it to notify targets, in a single call.
- **Bundled Lovelace card** (`custom:intercom-card`) — message box, speaker
  checkboxes, volume slider, notify checkboxes, Broadcast button. Auto-registered;
  no HACS frontend resource to add.
- **Volume that behaves** — optionally set a broadcast volume, then restore each
  speaker's original volume once the message finishes playing.
- **Sonos-correct** — uses `cache: true`, which Sonos requires (without it Sonos
  silently refuses to play HA's on-demand TTS stream).
- **Graceful failure** — offline speakers are skipped and reported via a persistent
  notification and an event; one dead notify target never blocks the others.
- **Works with your own UI too** — the service is card-agnostic, so a helper +
  `button-card` dashboard (or an automation) can drive it just as well.

---

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories**.
2. Add `https://github.com/will-roscoe/intercom` with category **Integration**.
3. Install **Intercom**, then restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → Intercom.**

### Manual

Copy `custom_components/intercom` into your `<config>/custom_components/` directory
and restart Home Assistant, then add the integration from the UI.

---

## Configuration

Setup is via the UI. The config/options flow lets you set defaults, all of which
can be overridden per service call:

| Option | Default | Meaning |
| --- | --- | --- |
| Default TTS engine | `tts.piper` | TTS entity used to speak. |
| Default notification title | `Broadcast` | Title for notify messages. |
| Max seconds to wait for playback | `30` | Cap on waiting for speech to finish before restoring volume. |

---

## The card

Add a **Manual** card and paste:

```yaml
type: custom:intercom-card
title: Intercom
default_volume: 40
players:
  - entity: media_player.sonosroam
    name: Sonos Roam
notify:
  - service: notify.mobile_app_phone
    name: My Phone
  - service: notify.persistent_notification
    name: HA notifications
```

| Card option | Default | Meaning |
| --- | --- | --- |
| `title` | – | Card header. |
| `players` | auto (all `media_player.*`) | Speakers to offer. `entity` + optional `name`. |
| `notify` | – | Notify targets to offer. `service` + optional `name`. |
| `default_volume` | `40` | Initial slider value (0–100). |
| `show_volume` | `true` | Hide the slider with `false`. |
| `volume_step` | `5` | Slider step. |

Offline speakers are shown greyed out with an "offline" tag and are skipped on
broadcast. A status line reports the result of each broadcast.

A full worked example is in [`examples/lovelace-intercom-card.yaml`](examples/lovelace-intercom-card.yaml).

---

## The service

`intercom.broadcast`:

```yaml
action: intercom.broadcast
data:
  message: "Dinner is ready"
  players:
    - media_player.sonosroam
  volume: 40           # optional, 0-100; omit to leave volume unchanged
  notify:
    - notify.mobile_app_phone
    - notify.persistent_notification
  title: "Kitchen"     # optional
  engine: tts.piper    # optional, overrides the configured engine
  restore_volume: true # optional, default true
```

| Field | Required | Description |
| --- | --- | --- |
| `message` | yes | Text to speak and/or notify. |
| `players` | no | Media players to speak on. |
| `volume` | no | 0–100. If set, applied then restored (unless `restore_volume: false`). |
| `notify` | no | Notify service names, e.g. `notify.mobile_app_phone`. |
| `title` | no | Notification title. |
| `engine` | no | TTS engine entity. |
| `restore_volume` | no | Restore original volume afterwards (default `true`). |

The service returns response data and fires an `intercom_broadcast_result` event:

```json
{
  "message": "Dinner is ready",
  "spoke": ["media_player.sonosroam"],
  "offline": [],
  "notified": ["notify.mobile_app_phone"],
  "notify_failed": [],
  "engine_available": true
}
```

### Driving it from your own UI (helpers + button-card)

If you prefer classic helpers and `button-card`, the service still does all the
work — your dashboard just gathers input. See
[`examples/helper-approach/`](examples/helper-approach/): a set of helpers, a thin
script that maps toggles to entity lists and calls `intercom.broadcast`, and a
`button-card` layout.

---

## Limitations

- **LG webOS (and many other) TVs cannot play TTS audio.** The TV entity reports
  `available` but `play_media`/`tts.speak` produces no sound, so it can't be
  reliably auto-detected as "failed". Prefer sending to a TV via its **notify**
  service (an on-screen toast) instead of as a speaker.
- **Notify delivery isn't confirmed.** A failed notify *service call* is reported,
  but Home Assistant can't confirm a push actually reached a phone.
- **Per-speaker TTS errors aren't isolated.** All available speakers are spoken to
  in one `tts.speak` call; offline speakers are reported precisely, but a mid-call
  TTS error is reported at the call level.

---

## Why `cache: true`?

When `cache` is off, Home Assistant hands the speaker a synthesis-on-demand stream
URL with no `Content-Length`. Sonos accepts the play command (so it looks like it
worked) but silently plays nothing. With `cache: true`, Piper renders the whole
clip to a stable cached file the speaker will play. This integration always sets it.

---

## License

[MIT](LICENSE) © Will Roscoe

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
- **Confirmed playback, not assumed playback** — a speaker is only reported as
  `played` when Home Assistant actually saw it start playing. "It said it worked
  but nothing came out" becomes an explicit `unverified` result instead of a
  silent success.
- **Every target is independent** — one `tts.speak` call per speaker, so a Sonos
  that answers *"The command to the player failed."* cannot take the rest of the
  broadcast down with it. Failed targets are retried.
- **Volume that behaves** — optionally set a broadcast volume, then restore each
  speaker's original volume once the message finishes playing. Muted speakers are
  unmuted for the announcement and re-muted afterwards.
- **Sonos-correct** — uses `cache: true`, which Sonos requires (without it Sonos
  silently refuses to play HA's on-demand TTS stream).
- **Loud about failure** — a `critical: true` broadcast retries harder and raises
  an error if anything did not get through, so an emergency automation cannot
  mistake a half-delivered message for a delivered one.
- **Works for the whole household** — results reach the card for non-admin users
  too, via a dedicated websocket subscription rather than the admin-only event
  bus.
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
| Max seconds to wait for playback | `30` | Cap on a single announcement before it is given up on. |
| Max seconds to wait for playback to start | `8` | How long a speaker gets to show *any* sign of playing before the result is reported as `unverified`. Sonos needs several seconds to buffer. |
| Attempts per target | `2` | How many times each speaker or notify target is tried before it is reported as failed. |
| Unmute muted speakers | on | A muted speaker accepts an announcement and plays it silently; unmuting first (and re-muting after) prevents that. Switching it off does not hide the problem — such a speaker is reported `silent`. A `critical` broadcast unmutes regardless. |

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
| `critical` | `false` | Send every broadcast from this card as critical. |

Offline speakers are shown greyed out with an "offline" tag and are skipped on
broadcast. After each broadcast the card lists every target and what happened to
it, and offers a one-tap **Retry** for any speaker that did not produce sound.
Broadcasts sent from elsewhere — another household member's phone, an automation
— show up too, and the last one is replayed when the card loads.

None of that needs admin rights; see [Non-admin users](#non-admin-users).

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
  verify: true         # optional, default true
  critical: false      # optional, default false
  max_attempts: 3      # optional, overrides the configured default
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
| `verify` | no | Confirm playback before reporting a speaker as played (default `true`). |
| `critical` | no | Retry silent speakers and raise an error unless everything was delivered (default `false`). |
| `max_attempts` | no | Attempts per target, 1–5. Overrides the configured default. |

Calling with no players *and* no notify targets is an error — this service never
succeeds at doing nothing.

---

## Knowing whether it worked

Every broadcast returns a response (and fires an `intercom_broadcast_result`
event) that says what happened to each target individually:

```json
{
  "id": "9f3c1a20",
  "message": "Dinner is ready",
  "delivered": true,
  "complete": false,
  "summary": "partial — played on 1/2 speakers, notified 1/1 targets (Study: the player accepted the command but no playback was detected within 8s)",
  "duration": 6.4,
  "players": [
    { "entity_id": "media_player.sonosroam", "name": "Sonos Roam",
      "status": "played", "verified": true, "attempts": 1,
      "error": null, "detail": null, "warnings": [] },
    { "entity_id": "media_player.study", "name": "Study",
      "status": "unverified", "verified": false, "attempts": 1,
      "error": "the player accepted the command but no playback was detected within 8s",
      "detail": null, "warnings": [] }
  ],
  "notify": [
    { "target": "notify.mobile_app_phone", "status": "sent", "attempts": 1, "error": null }
  ],
  "played": ["media_player.sonosroam"],
  "unverified": ["media_player.study"],
  "failed": [], "offline": [], "unsupported": [],
  "notified": ["notify.mobile_app_phone"], "notify_failed": [],
  "errors": ["Study: the player accepted the command but no playback was detected within 8s"],
  "engine_available": true
}
```

### Speaker statuses

| Status | Meaning |
| --- | --- |
| `played` | Home Assistant saw the speaker start playing. This is the only status that means sound came out. |
| `unverified` | The speaker accepted the command but never showed any sign of playing. This is the "reported as sent, nothing heard" case. |
| `silent` | The clip played, but into a muted speaker or one at zero volume, so nobody could have heard it. |
| `failed` | The `tts.speak` call itself raised, e.g. Sonos's *"The command to the player failed."* |
| `offline` | The entity is missing or `unavailable`; nothing was attempted. |
| `unsupported` | The entity exists but cannot play media at all. |
| `sent` | Only with `verify: false` — dispatched, delivery not confirmed. |

`delivered` is true when at least one target definitely got the message.
`complete` is true only when *every* requested target did.

### Non-admin users

Everything above works for any signed-in user, admin or not.

That takes a little care, because Home Assistant will not let a non-admin
subscribe to a custom event. `subscribe_events` checks the event type against a
hardcoded allowlist in `homeassistant/auth/permissions/events.py` and raises
`Unauthorized` otherwise, logging:

```
Refusing to allow Nikki to subscribe to event intercom_broadcast_result
```

There is no hook to extend that allowlist, so a card built on `subscribe_events`
silently never updates for anyone who is not an admin — while filling the log
with that error on every card load.

Rather than widen the event bus, the integration registers one narrow websocket
command of its own, `intercom/subscribe_result` — the same pattern core uses for
`persistent_notification/subscribe`. It is deliberately not admin-gated, and a
subscriber sees intercom broadcast results and nothing else. On subscribe it
replays the most recent broadcast so a freshly loaded card is not blank.

So results reach the card two ways, neither needing admin:

| Path | Covers |
| --- | --- |
| Service response | The broadcast this card just sent. |
| `intercom/subscribe_result` | Broadcasts sent by anyone else, plus the last one on load. |

The `intercom_broadcast_result` bus event is still fired, unchanged, for
automations.

> Any signed-in user who subscribes can read the text of every broadcast. For a
> household intercom that is the point, but it is worth knowing if you share the
> instance more widely.

### Muted and silenced speakers

A muted speaker is the most misleading failure of all: it accepts the
announcement, reports `playing`, finishes on time, and emits nothing. So by
default the integration unmutes a muted speaker for the announcement and
re-mutes it afterwards, leaving it exactly as it was found.

If unmuting is switched off in the options, a muted speaker is reported `silent`
rather than `played` — the message is never quietly counted as delivered. The
same applies to a speaker sitting at zero volume, which unmuting cannot fix.

**`critical: true` overrides both.** It unmutes regardless of the option, and
raises a speaker at zero volume to an audible level (restoring it afterwards
like any other volume change). The speaker somebody muted is exactly the one an
emergency still has to come out of. Pass an explicit `volume:` if you want to
choose the level yourself.

### How playback is confirmed

A state-change listener is armed on each speaker **before** `tts.speak` is
called, then playback counts as confirmed if the player enters a playing state,
switches to different media, or (if it was already playing) picks up a new media
duration. Arming first matters: a short clip can start and finish faster than any
polling loop would notice.

### Sending something that has to get through

```yaml
action: intercom.broadcast
data:
  message: "Smoke detected in the garage. Leave the house now."
  players: [media_player.sonosroam, media_player.kitchen]
  notify: [notify.mobile_app_phone, notify.persistent_notification]
  volume: 80
  critical: true
response_variable: result
```

With `critical: true` the service:

- retries speakers that produced no sound (a non-critical broadcast does not, to
  avoid saying a routine message twice);
- writes a persistent notification listing every target and its failure reason,
  under a broadcast-specific ID so a later broadcast cannot overwrite the
  evidence;
- **raises an error** unless every requested target was delivered, so the calling
  automation fails visibly rather than continuing as if the message landed.

Pair it with `continue_on_error: false` (the default) and an automation-level
fallback:

```yaml
- alias: Emergency broadcast
  sequence:
    - action: intercom.broadcast
      data:
        message: "{{ text }}"
        players: [media_player.sonosroam, media_player.kitchen]
        notify: [notify.mobile_app_phone]
        critical: true
    # only reached if everything above got through
    - action: input_boolean.turn_on
      target: { entity_id: input_boolean.alert_delivered }
```

Every broadcast also logs one line under `custom_components.intercom`, tagged
with the broadcast id, at `INFO` when complete, `WARNING` when partial and
`ERROR` when nothing was delivered.

### Driving it from your own UI (helpers + button-card)

If you prefer classic helpers and `button-card`, the service still does all the
work — your dashboard just gathers input. See
[`examples/helper-approach/`](examples/helper-approach/): a set of helpers, a thin
script that maps toggles to entity lists and calls `intercom.broadcast`, and a
`button-card` layout.

---

## Limitations

- **Confirmation is state-based, not acoustic.** `played` means the player
  reported that it started playing the clip, and that it was neither muted nor
  at zero volume. It cannot catch a speaker whose amplifier is off, whose output
  is routed elsewhere, or that is physically unplugged mid-sentence. It does
  catch the common cases: rejected commands, muted or silenced speakers, and
  players that accept the command and do nothing.
- **LG webOS (and many other) TVs cannot play TTS audio.** They accept the
  command and stay silent, so they are reported as `unverified` rather than
  played. Prefer sending to a TV via its **notify** service (an on-screen toast)
  instead of as a speaker.
- **Notify delivery isn't confirmed.** A failed notify *service call* is reported
  and retried, but Home Assistant cannot confirm a push actually reached a phone.
  Treat `sent` as "handed to the notification platform".
- **A speaker in a group may play on its coordinator.** Multi-room groups are
  reported per entity; the audio may come out of the group instead.

---

## Why `cache: true`?

When `cache` is off, Home Assistant hands the speaker a synthesis-on-demand stream
URL with no `Content-Length`. Sonos accepts the play command (so it looks like it
worked) but silently plays nothing. With `cache: true`, Piper renders the whole
clip to a stable cached file the speaker will play. This integration always sets it.

---

## Development

```bash
pip install pytest pyyaml voluptuous
pytest
```

Home Assistant is not a dependency of this repository; `tests/stubs` provides the
slice of it the integration imports, so the tests drive the real modules without
a full HA install. See [`tests/README.md`](tests/README.md).

### Releasing

`manifest.json` is the source of truth. Bump `version` in a pull request, and
merging it to `main` publishes the release — the workflow creates the tag, zips
`custom_components/intercom`, and attaches it with generated notes. Pushing a
`v*` tag by hand still works and must match the manifest.

Two details worth knowing if you change it:

- The tag is created *by* the release job rather than pushed separately. A tag
  pushed with `GITHUB_TOKEN` does not trigger other workflows, so a
  "tag now, release on the tag" chain would silently never fire.
- The job re-runs lint and tests before publishing, so an automated release
  cannot ship code the checks reject. It also no-ops when a release for the
  current version already exists, making it safe on every push to `main`.

---

## License

[MIT](LICENSE) © Will Roscoe

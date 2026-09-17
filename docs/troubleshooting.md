# Troubleshooting

Each speaker in a broadcast result has a `status`, an `error` or `detail`, and a
`verified_by` naming the check that judged it (see
[How playback is confirmed](../README.md#how-playback-is-confirmed)). Start there.

- [A speaker accepts the broadcast but nothing comes out](#a-speaker-accepts-the-broadcast-but-nothing-comes-out)
- [A broadcast is reported as failed but I heard it](#a-broadcast-is-reported-as-failed-but-i-heard-it)
- [Notifications arrive but the speaker does not](#notifications-arrive-but-the-speaker-does-not)
- [Finding the logs](#finding-the-logs)

---

## A speaker accepts the broadcast but nothing comes out

**Looks like:**

- `failed` with *"the speaker reported it could not play the clip…"*, or
  `unverified` with *"no playback was detected"*;
- music from Music Assistant loads, then drops straight back to paused;
- Music Assistant's log contains lines like

  ```
  ERROR (MainThread) [aiosonos.api] Received unhandled error: { ... 'name': 'playbackError', ...}:
  {'errorCode': 'ERROR_PLAYBACK_FAILED', 'reason': 'ERROR_LOST_CONNECTION',
   'serviceName': '192.168.1.5:8443', ...}
  ```

**Cause: the speaker cannot reach the address it was given for the audio.**

Home Assistant does not send the audio to a speaker. It sends a *link*
(`http://<address>:<port>/api/tts_proxy/<id>.mp3`), and the speaker downloads it
itself. Music Assistant works the same way with its stream server (port `8097`
by default). So "the command was accepted" says nothing about whether the
speaker can open that link.

This commonly happens when the Home Assistant host has **more than one network
interface**, e.g. Ethernet on one subnet and Wi-Fi on another. With no *Local
network* URL configured, Home Assistant builds the link from its primary
interface. That is the one holding the default route with the best metric, and
it is not necessarily the one your speakers share. A wired "backlink" with a
gateway configured is enough to become primary. Music Assistant picks its
stream address the same way.

**Confirm it:**

1. Check the address in the link. Developer tools → Actions →
   `tts.speak`, or look at the `serviceName` in the Sonos error above.
2. Check which subnet the speaker is on (Sonos app → Settings → About, or your
   router's client list).
3. From the Home Assistant host, `ip route` shows which interface holds the
   preferred default route.

If the speaker is on a subnet that cannot route to that address, that is the
problem.

**Fix it** (any one of these):

- Put the speaker on the same network as the address Home Assistant hands out
  (for Sonos, point it at that network's Wi-Fi and consider a DHCP reservation).
- Settings → System → Network → **Home Assistant URL** → set the **Local
  network** URL to an address the speakers can reach. This fixes the TTS links.
- In Music Assistant, set the stream server's **published IP address** to that
  same reachable address. This fixes music and Music Assistant announcements.
- If the other interface is only a point-to-point link, remove its gateway so it
  stops being the primary interface.
- Or route between the two subnets on your router.

**Check the fix:** send a short broadcast. With the Sonos check the result
should be `played` with `verified_by: sonos_clip`. You can also test the path
directly by calling `media_player.play_media` with the TTS link from step 1.

---

## A broadcast is reported as failed but I heard it

**Sonos:** versions before 0.3.0 judged every speaker by its media player state.
HA plays Sonos announcements as audio clips, which never change that state, so
every Sonos broadcast came back `unverified` and the summary said `FAILED`.
Upgrade. The result should then show `verified_by: sonos_clip`.

If you are on 0.3.0 or later and still see `verified_by: state` for a Sonos
speaker, the Sonos check could not be used. The Home Assistant log will contain

```
intercom: cannot follow Sonos clips on media_player.x (<address>: <reason>); falling back to state checks
```

Common reasons:

- **Connection refused / timed out:** Home Assistant cannot reach the speaker on
  port `1443`. That is the same local API HA's Sonos integration uses for
  announcements, so check your firewall or VLAN rules.
- **No player at \<address\> in the household:** HA's Sonos device entry holds a
  stale address (the speaker changed IP). HA picks up the new address when it
  rediscovers the speaker; reloading the Sonos integration or restarting Home
  Assistant forces that.
- **The speaker does not support audio clips:** older Sonos models. The state
  check is the best available for them.

The Sonos check is chosen for entities from HA's **Sonos** integration and for
**Music Assistant** players backed by a Sonos speaker that HA's Sonos
integration also knows about. A Sonos speaker known only to Music Assistant is
judged by state.

**Other players:** `unverified` means the player accepted the command but never
changed state. Some players (many TVs, some cast targets) play announcements
without reporting it. If you have confirmed by ear that a player works, you can
call the service with `verify: false` for it. The result will then say `sent`
rather than `played`.

---

## Notifications arrive but the speaker does not

Notify targets and speakers are handled independently, so this means the
speaker path failed on its own. Read that speaker's `error` in the result, then:

- `offline`: the entity is unavailable in Home Assistant. Fix the integration
  first.
- `failed` with a TTS error: the TTS engine (e.g. Piper) is unavailable or
  failed to synthesise. Check that add-on's log.
- `failed` with *"could not play the clip"*: see
  [the first section](#a-speaker-accepts-the-broadcast-but-nothing-comes-out).
- `silent`: the clip played into a muted or zero-volume speaker.

---

## Finding the logs

Every broadcast is logged under `custom_components.intercom` with a short id,
e.g. `intercom[d92473b7]: FAILED — played on 0/1 speakers (…)`. The same id is
in the result as `id`.

Note that a speaker's own errors, such as the Sonos playback error above, are
reported to whatever asked it to play. They appear in that component's log
rather than in this integration's.

For more detail from this integration:

```yaml
logger:
  logs:
    custom_components.intercom: debug
```

# Tests

A regression suite for the parts of the integration that are easy to get subtly
wrong: whether a broadcast really played, and whether the result tells the truth
about it.

Home Assistant is not a dependency of this repository, and installing it just to
run these would be a heavy price for a custom component. Instead `stubs/`
provides the slice of `homeassistant` the integration actually imports — a state
machine that fires `state_changed` on every write, a service registry whose
handlers can raise, and a bus that really dispatches. The stubs mirror
behaviour read from HA core, not guessed at; where a contract matters
(`websocket_command` stashing `_ws_command`, `connection.subscriptions` being
where `unsubscribe_events` looks) the stub follows core's implementation.

That means the tests import and drive the *real* integration modules. They are
not a substitute for running it in Home Assistant, but they do pin down the
failure modes that prompted this code:

| File | Guards |
| --- | --- |
| `test_broadcast.py` | A player is reported `played` only with evidence of audio. Silent, muted, zero-volume, offline, unsupported, failing and hanging players each get their own honest status. |
| `test_service.py` | The service response, the fired event, the persistent notification, and `critical` escalation. |
| `test_websocket.py` | The non-admin subscription: not admin-gated, replays the last broadcast, torn down by core's generic unsubscribe. |
| `test_packaging.py` | The files hassfest and HACS parse — service schema, manifest, translations, declared dependencies. |

## Running

```bash
pip install pytest pyyaml voluptuous
pytest tests -q
```

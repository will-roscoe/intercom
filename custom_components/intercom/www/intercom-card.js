/**
 * Intercom card
 *
 * A self-contained Lovelace card for the `intercom` integration. Type a message,
 * tick which speakers to speak it on (with an optional volume) and which notify
 * targets to send it to, then hit Broadcast — it calls the `intercom.broadcast`
 * service and reports, per target, what actually happened.
 *
 * Results arrive two ways, neither of which needs admin rights: the service
 * *response* for a broadcast this card sent, and the integration's own
 * `intercom/subscribe_result` websocket command for broadcasts sent by anyone
 * else (a phone, an automation, another household member). Home Assistant
 * refuses to let non-admins subscribe to custom bus events, so an event-only
 * card silently never updates for them.
 *
 * Config:
 *   type: custom:intercom-card
 *   title: Intercom            # optional card header
 *   default_volume: 40         # optional, 0-100; set `show_volume: false` to hide slider
 *   show_volume: true          # optional
 *   show_critical: false       # optional; show a Critical toggle by the button
 *   chime: true                # optional; chime before the message (else the
 *                              # integration setting); a speaker's own `chime` wins
 *   critical: false            # optional; retry harder and fail loudly. With
 *                              # show_critical it is where the toggle starts,
 *                              # otherwise it applies to every broadcast
 *   players:                   # optional; auto-discovers media_player.* if omitted
 *     - entity: media_player.sonosroam
 *       name: Sonos Roam
 *       chime: false           # optional; overrides the card's `chime`
 *       alternates:            # optional; played on instead, in order, when
 *         - media_player.kitchen #  this speaker is offline or cannot play
 *   holds:                     # optional; offers "Hold until" choices
 *     - name: Will is home     #  shown as the choice and as "held until ..."
 *       conditions:            #  as in an automation's `conditions:`
 *         - condition: state
 *           entity_id: person.will
 *           state: home
 *   notify:                    # optional
 *     - service: notify.mobile_app_phone
 *       name: My Phone
 */

// The integration serves this file as `intercom-card.js?v=<manifest version>`
// (frontend.py), so the version is read from that URL rather than kept here as
// a second copy that releases would leave behind.
const VERSION =
  new URL(import.meta.url).searchParams.get("v") || "unknown";

// Per-target statuses returned by intercom.broadcast.
const OK_STATUSES = new Set(["played", "sent", "rerouted"]);
const STATUS_TEXT = {
  played: "played",
  sent: "sent",
  rerouted: "rerouted",
  unverified: "no sound detected",
  silent: "muted — not audible",
  failed: "failed",
  offline: "offline",
  unsupported: "cannot play media",
};

export class IntercomCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = {};
    this._selectedPlayers = new Set();
    this._selectedNotify = new Set();
    this._volume = 40;
    this._critical = false;
    this._built = false;
    this._unsub = null;
    this._unsubHeld = null;
    this._held = [];
    this._subscribed = false;
    this._pending = false;
    this._lastResult = null;
    this._lastResultReplay = false;
    this._statusText = "";
  }

  static getStubConfig() {
    return {
      title: "Intercom",
      default_volume: 40,
      show_critical: false,
      players: [],
      notify: [],
    };
  }

  setConfig(config) {
    this._config = config || {};
    // With show_critical, `critical` is where the toggle starts; without it,
    // it is fixed for every broadcast, as it always was.
    this._critical = !!this._config.critical;
    this._volume =
      typeof this._config.default_volume === "number"
        ? this._config.default_volume
        : 40;
    // Rebuild on next hass assignment / render.
    this._built = false;
    if (this.shadowRoot) this.shadowRoot.innerHTML = "";
    if (this._hass) this._render();
  }

  set hass(hass) {
    const first = this._hass === null;
    this._hass = hass;
    if (!this._built) {
      this._render();
    } else {
      this._updateAvailability();
    }
    if (first) this._subscribe();
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    if (this._hass && !this._unsub) this._subscribe();
  }

  disconnectedCallback() {
    for (const key of ["_unsub", "_unsubHeld"]) {
      if (this[key]) {
        this[key].then((fn) => fn && fn()).catch(() => {});
        this[key] = null;
      }
    }
  }

  getCardSize() {
    const players = this._players().length;
    const notify = this._notify().length;
    return 3 + Math.ceil(players / 2) + Math.ceil(notify / 2);
  }

  // --- config resolution -----------------------------------------------------

  _players() {
    if (Array.isArray(this._config.players) && this._config.players.length) {
      return this._config.players.map((p) =>
        typeof p === "string" ? { entity: p } : p
      );
    }
    // auto-discover media players
    if (!this._hass) return [];
    return Object.keys(this._hass.states)
      .filter((e) => e.startsWith("media_player."))
      .map((entity) => ({ entity }));
  }

  // The card's "hold until" presets: [{name, conditions}].
  _holds() {
    if (!Array.isArray(this._config.holds)) return [];
    return this._config.holds.filter((h) => h && h.conditions);
  }

  // The preset chosen in the "hold until" select, or null for "Send now".
  _chosenHold() {
    if (!this._holdChoice) return null;
    return this._holds()[Number(this._holdChoice)] || null;
  }

  _renderHeld() {
    if (!this._heldEl) return;
    this._heldEl.innerHTML = "";
    for (const item of this._held) {
      const li = document.createElement("li");
      li.className = "held-item";
      const text = document.createElement("span");
      text.className = "held-text";
      text.textContent = `“${item.message}” — held until ${item.label}`;
      if (item.expires_at) text.title = `Expires ${item.expires_at}`;
      const cancel = document.createElement("button");
      cancel.className = "cancel";
      cancel.type = "button";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", () => this._cancelHeld(item.id));
      li.appendChild(text);
      li.appendChild(cancel);
      this._heldEl.appendChild(li);
    }
  }

  async _cancelHeld(id) {
    try {
      await this._hass.callWS({ type: "intercom/cancel_held", held_id: id });
    } catch (err) {
      this._setStatus(`Could not cancel: ${(err && err.message) || err}`, "bad");
    }
  }

  // A speaker's configured alternates, in order; [] when it has none. A single
  // entity may be given as a string, as the service accepts.
  _alternates(entity) {
    const item = this._players().find((p) => p.entity === entity);
    const alts = item && item.alternates;
    if (typeof alts === "string") return [alts];
    return Array.isArray(alts) ? alts : [];
  }

  // The service's `chime` for these speakers: undefined when neither the card
  // nor any speaker sets it (the integration setting decides), true or false
  // when every speaker agrees, otherwise the list of speakers that chime. A
  // speaker's own `chime` overrides the card's.
  _chimeFor(players) {
    const own = (entity) => {
      const item = this._players().find((p) => p.entity === entity);
      return item ? item.chime : undefined;
    };
    if (
      this._config.chime === undefined &&
      players.every((p) => own(p) === undefined)
    )
      return undefined;
    const on = players.filter((p) => (own(p) ?? this._config.chime) === true);
    if (on.length === players.length) return true;
    if (!on.length) return false;
    return on;
  }

  // A speaker's label, using its card config name when it has one.
  _playerLabel(entity) {
    const item = this._players().find((p) => p.entity === entity);
    return this._label(item || { entity }, "entity");
  }

  _notify() {
    if (!Array.isArray(this._config.notify)) return [];
    return this._config.notify.map((n) =>
      typeof n === "string" ? { service: n } : n
    );
  }

  _label(item, key) {
    if (item.name) return item.name;
    const id = item[key];
    const st = this._hass && this._hass.states[id];
    if (st && st.attributes && st.attributes.friendly_name)
      return st.attributes.friendly_name;
    return id.replace(/^(media_player|notify)\./, "").replace(/_/g, " ");
  }

  _isOffline(entity) {
    const st = this._hass && this._hass.states[entity];
    return !st || ["unavailable", "unknown", "none", ""].includes(st.state);
  }

  // Whether a speaker could take a message now: online and able to play media
  // (supported_features PLAY_MEDIA, 512), as the integration checks.
  _canPlay(entity) {
    if (this._isOffline(entity)) return false;
    const features = this._hass.states[entity].attributes.supported_features;
    return ((features || 0) & 512) !== 0;
  }

  // --- rendering -------------------------------------------------------------

  _render() {
    if (!this._hass) return;
    const root = this.shadowRoot;
    root.innerHTML = "";

    const style = document.createElement("style");
    style.textContent = STYLES;
    root.appendChild(style);

    const card = document.createElement("ha-card");
    if (this._config.title) card.setAttribute("header", this._config.title);

    const content = document.createElement("div");
    content.className = "content";

    // message
    const msg = document.createElement("textarea");
    msg.className = "message";
    msg.placeholder = "Type a message to broadcast…";
    msg.rows = 2;
    msg.value = this._message || "";
    msg.addEventListener("input", (e) => {
      this._message = e.target.value;
      this._updateClearButton();
    });
    this._msgEl = msg;

    const clear = document.createElement("button");
    clear.className = "clear";
    clear.type = "button";
    clear.title = "Clear the message";
    clear.setAttribute("aria-label", "Clear the message");
    clear.textContent = "✕";
    clear.addEventListener("click", () => this._clearMessage());
    this._clearEl = clear;

    content.appendChild(this._field("Message", msg, clear));
    this._updateClearButton();

    // volume
    if (this._config.show_volume !== false) {
      const wrap = document.createElement("div");
      wrap.className = "volume";
      const range = document.createElement("input");
      range.type = "range";
      range.min = "0";
      range.max = "100";
      range.step = String(this._config.volume_step || 5);
      range.value = String(this._volume);
      const out = document.createElement("span");
      out.className = "volume-value";
      out.textContent = `${this._volume}%`;
      range.addEventListener("input", (e) => {
        this._volume = Number(e.target.value);
        out.textContent = `${this._volume}%`;
      });
      wrap.appendChild(range);
      wrap.appendChild(out);
      content.appendChild(this._field("Volume", wrap));
    }

    // speakers
    this._playerChips = document.createElement("div");
    this._playerChips.className = "chips";
    for (const p of this._players()) {
      this._playerChips.appendChild(
        this._chip(p.entity, this._label(p, "entity"), this._selectedPlayers, true)
      );
    }
    content.appendChild(this._section("Speak on", this._playerChips));

    // notify
    const notify = this._notify();
    if (notify.length) {
      this._notifyChips = document.createElement("div");
      this._notifyChips.className = "chips";
      for (const n of notify) {
        this._notifyChips.appendChild(
          this._chip(n.service, this._label(n, "service"), this._selectedNotify, false)
        );
      }
      content.appendChild(this._section("Notify", this._notifyChips));
    }

    // "hold until" choice, when the card offers presets
    const holds = this._holds();
    this._holdEl = null;
    if (holds.length) {
      const row = document.createElement("div");
      row.className = "hold-row";
      const caption = document.createElement("span");
      caption.textContent = "Hold until";
      row.appendChild(caption);
      const sel = document.createElement("select");
      sel.className = "hold";
      const now = document.createElement("option");
      now.value = "";
      now.textContent = "Send now";
      sel.appendChild(now);
      holds.forEach((hold, i) => {
        const opt = document.createElement("option");
        opt.value = String(i);
        opt.textContent = hold.name || `Preset ${i + 1}`;
        sel.appendChild(opt);
      });
      sel.value = this._holdChoice || "";
      sel.addEventListener("change", (e) => {
        this._holdChoice = e.target.value;
      });
      row.appendChild(sel);
      this._holdEl = sel;
      content.appendChild(row);
    }

    // broadcast button, with the critical toggle beside it when enabled
    const btn = document.createElement("button");
    btn.className = "broadcast";
    btn.innerHTML = `<span class="mdi">📣</span> Broadcast`;
    btn.addEventListener("click", () => this._broadcast());
    this._btn = btn;

    if (this._config.show_critical) {
      const row = document.createElement("div");
      row.className = "send-row";
      row.appendChild(btn);

      const toggle = document.createElement("button");
      toggle.className = "critical";
      toggle.type = "button";
      toggle.innerHTML = `<span class="mdi">⚠</span> Critical`;
      toggle.title =
        "Retry harder, raise silent speakers, and fail loudly if the " +
        "message does not get through";
      toggle.addEventListener("click", () => {
        this._critical = !this._critical;
        this._updateCriticalButton();
      });
      this._criticalEl = toggle;
      row.appendChild(toggle);
      this._updateCriticalButton();
      content.appendChild(row);
    } else {
      this._criticalEl = null;
      content.appendChild(btn);
    }

    // status
    this._statusEl = document.createElement("div");
    this._statusEl.className = "status";
    content.appendChild(this._statusEl);

    // messages held until their conditions are met, from anyone
    this._heldEl = document.createElement("ul");
    this._heldEl.className = "held";
    content.appendChild(this._heldEl);
    this._renderHeld();

    card.appendChild(content);
    root.appendChild(card);
    this._built = true;
    this._updateAvailability();
    if (this._lastResult)
      this._showResult(this._lastResult, false, this._lastResultReplay);
    else if (this._statusText) this._setStatus(this._statusText);
  }

  _field(label, el, action) {
    const row = document.createElement("div");
    row.className = "field";
    const lbl = document.createElement("label");
    lbl.textContent = label;
    if (action) {
      // Keep the action on the label's own line so it never covers the input.
      const head = document.createElement("div");
      head.className = "field-head";
      head.appendChild(lbl);
      head.appendChild(action);
      row.appendChild(head);
    } else {
      row.appendChild(lbl);
    }
    row.appendChild(el);
    return row;
  }

  _section(label, body) {
    const sec = document.createElement("div");
    sec.className = "section";
    const lbl = document.createElement("label");
    lbl.textContent = label;
    sec.appendChild(lbl);
    sec.appendChild(body);
    return sec;
  }

  _chip(id, label, set, isPlayer) {
    const chip = document.createElement("button");
    chip.className = "chip";
    chip.dataset.id = id;
    chip.dataset.player = isPlayer ? "1" : "0";
    chip.type = "button";

    const text = document.createElement("span");
    text.className = "chip-label";
    text.textContent = label;
    chip.appendChild(text);

    if (set.has(id)) chip.classList.add("selected");
    chip.addEventListener("click", () => {
      if (set.has(id)) {
        set.delete(id);
        chip.classList.remove("selected");
      } else {
        set.add(id);
        chip.classList.add("selected");
      }
    });
    return chip;
  }

  _updateAvailability() {
    if (!this._playerChips) return;
    for (const chip of this._playerChips.querySelectorAll(".chip")) {
      const offline = this._isOffline(chip.dataset.id);
      chip.classList.toggle("offline", offline);
      const standIn = offline
        ? this._alternates(chip.dataset.id).find((a) => this._canPlay(a))
        : undefined;
      chip.title = !offline
        ? ""
        : standIn
          ? `Unavailable, will play on ${this._playerLabel(standIn)}`
          : "Currently unavailable — will be skipped";
    }
  }

  // --- actions ---------------------------------------------------------------

  async _broadcast(overridePlayers, overrideMessage) {
    if (!this._hass) return;
    const message = (overrideMessage || this._message || "").trim();
    if (!message) {
      this._setStatus("Type a message first.", "warn");
      return;
    }
    const players = overridePlayers || [...this._selectedPlayers];
    const notifyTargets = overridePlayers ? [] : [...this._selectedNotify];
    if (!players.length && !notifyTargets.length) {
      this._setStatus("Pick at least one speaker or notify target.", "warn");
      return;
    }

    const data = { message };
    if (players.length) data.players = players;
    const alternates = {};
    for (const entity of players) {
      const list = this._alternates(entity);
      if (list.length) alternates[entity] = list;
    }
    if (Object.keys(alternates).length) data.alternates = alternates;
    const chime = this._chimeFor(players);
    if (chime !== undefined) data.chime = chime;
    const hold = overridePlayers ? null : this._chosenHold();
    if (hold) {
      data.hold = hold.conditions;
      if (hold.name) data.hold_label = hold.name;
    }
    if (notifyTargets.length) data.notify = notifyTargets;
    if (this._config.show_volume !== false && players.length)
      data.volume = this._volume;
    if (this._critical) data.critical = true;

    this._busy(true);
    this._pending = true;
    this._setStatus("Broadcasting… (confirming playback)", "busy");
    try {
      // notifyOnError=false: we render the failure ourselves rather than
      // letting the frontend pop a toast that hides the detail.
      const res = await this._hass.callService(
        "intercom",
        "broadcast",
        data,
        undefined,
        false,
        true
      );
      const result = res && res.response;
      if (result && result.held) {
        // Not delivered yet: it waits, and shows in the held list below.
        this._setStatus(`Held until ${result.label}`, "warn");
        this._clearMessage(false);
        this._holdChoice = "";
        if (this._holdEl) this._holdEl.value = "";
      } else if (result) {
        this._showResult(result, true);
      } else {
        // Older frontends do not return service responses; fall back to the
        // event, or to an honest "we cannot confirm" if we cannot hear it.
        this._setStatus(
          this._subscribed
            ? "Broadcasting… awaiting result"
            : "Sent, but this Home Assistant cannot report the result here.",
          "warn"
        );
      }
    } catch (err) {
      const detail = err && (err.message || err.error || err.code);
      this._setStatus(`Failed: ${detail || err}`, "bad");
    } finally {
      this._pending = false;
      this._busy(false);
    }
  }

  _busy(on) {
    if (this._btn) this._btn.disabled = !!on;
  }

  _clearMessage(focus = true) {
    this._message = "";
    if (this._msgEl) {
      this._msgEl.value = "";
      if (focus) this._msgEl.focus();
    }
    this._updateClearButton();
  }

  _updateClearButton() {
    if (this._clearEl) this._clearEl.hidden = !(this._message || "").length;
  }

  _updateCriticalButton() {
    if (!this._criticalEl) return;
    this._criticalEl.classList.toggle("armed", !!this._critical);
    this._criticalEl.setAttribute("aria-pressed", this._critical ? "true" : "false");
  }

  _subscribe() {
    if (!this._hass || !this._hass.connection || this._unsub) return;
    // intercom/subscribe_result, not subscribe_events: the latter is admin-only
    // for custom event types. The integration replays the last broadcast on
    // subscribe, so the card is not blank on load.
    this._unsub = this._hass.connection
      .subscribeMessage((msg) => this._onPushed(msg), {
        type: "intercom/subscribe_result",
      })
      .then((fn) => {
        this._subscribed = true;
        return fn;
      })
      .catch(() => {
        // Integration not loaded, or older than this card.
        this._subscribed = false;
        return null;
      });
    this._unsubHeld = this._hass.connection
      .subscribeMessage(
        (msg) => {
          this._held = (msg && msg.held) || [];
          this._renderHeld();
        },
        { type: "intercom/subscribe_held" }
      )
      .catch(() => null);
  }

  _onPushed(msg) {
    if (!msg || !msg.result) return;
    // Do not let a replayed or someone else's broadcast overwrite live feedback
    // about the one this card is still waiting on.
    if (this._pending) return;
    this._showResult(msg.result, false, !!msg.replay);
  }

  // --- result rendering ------------------------------------------------------

  _showResult(result, own, replay) {
    if (!result) return;
    this._lastResult = result;
    this._lastResultReplay = !!replay;
    if (!this._statusEl) return;

    const complete = !!result.complete;
    const delivered = !!result.delivered;
    const tone = complete ? "good" : delivered ? "warn" : "bad";

    this._statusEl.innerHTML = "";
    this._statusEl.className = `status ${tone}${replay ? " replay" : ""}`;

    const head = document.createElement("div");
    head.className = "status-head";
    const icon = complete ? "✓" : delivered ? "!" : "✕";
    head.textContent = `${icon} ${replay ? "Last broadcast " : ""}${this._time(
      result
    )} — ${result.summary || (complete ? "delivered" : "not delivered")}`;
    this._statusEl.appendChild(head);

    const rows = document.createElement("ul");
    rows.className = "status-rows";
    const players = result.players || [];
    for (const p of players) {
      // An alternate's own row is shown on the speaker it stood in for.
      if (p.alternate_for) continue;
      if (p.status === "rerouted") {
        const alt = players.find((o) => o.entity_id === p.rerouted_to);
        rows.appendChild(
          this._row(
            `${p.name || p.entity_id} → ${(alt && alt.name) || p.rerouted_to}`,
            alt ? alt.status : p.status,
            p.detail,
            true
          )
        );
        continue;
      }
      rows.appendChild(
        this._row(
          p.name || p.entity_id,
          p.status,
          p.error || p.detail,
          OK_STATUSES.has(p.status)
        )
      );
    }
    for (const n of result.notify || []) {
      rows.appendChild(
        this._row(
          String(n.target).replace(/^notify\./, ""),
          n.status,
          n.error,
          n.status === "sent"
        )
      );
    }
    if (rows.childElementCount) this._statusEl.appendChild(rows);

    // Anything that did not come out of a speaker is worth one tap to retry —
    // but not for a broadcast replayed from before this card loaded.
    const retryable = replay
      ? []
      : players
          .filter((p) => !p.alternate_for && !OK_STATUSES.has(p.status))
          .map((p) => p.entity_id);
    if (retryable.length) {
      const retry = document.createElement("button");
      retry.className = "retry";
      retry.type = "button";
      retry.textContent = `Retry ${retryable.length} speaker${
        retryable.length > 1 ? "s" : ""
      }`;
      retry.addEventListener("click", () =>
        this._broadcast(retryable, result.message)
      );
      this._statusEl.appendChild(retry);
    }

    // Broadcasts fired elsewhere (automations) also land here for admins;
    // only clear the box for a message this card actually sent.
    if (own && complete) this._clearMessage(false);
  }

  _row(label, status, detail, ok) {
    const li = document.createElement("li");
    li.className = ok ? "ok" : "bad";
    const name = document.createElement("span");
    name.className = "row-name";
    name.textContent = label;
    const state = document.createElement("span");
    state.className = "row-state";
    state.textContent = STATUS_TEXT[status] || status;
    li.appendChild(name);
    li.appendChild(state);
    if (detail) li.title = detail;
    return li;
  }

  _time(result) {
    const stamp = result && result.finished_at ? new Date(result.finished_at) : null;
    const when = stamp && !isNaN(stamp.getTime()) ? stamp : new Date();
    return when.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  _setStatus(text, tone) {
    this._statusText = text;
    this._lastResult = null;
    this._lastResultReplay = false;
    if (!this._statusEl) return;
    this._statusEl.className = `status ${tone || ""}`.trim();
    this._statusEl.textContent = text;
  }
}

// --- visual editor -------------------------------------------------------------
//
// The card's edit dialog shows one ha-form. The functions below translate the
// card's YAML to the form's data and back, build the form from what Home
// Assistant has, and refuse a config the form could not show without losing
// part of it. They are exported for tests/card.

/** Card options, and the value the card uses when one is left out. */
export const EDITOR_DEFAULTS = Object.freeze({
  show_volume: true,
  default_volume: 40,
  volume_step: 5,
  show_critical: false,
  critical: false,
  players: [],
  notify: [],
  holds: [],
});

/** Form label of each card option. */
export const EDITOR_LABELS = {
  title: "Title",
  players: "Speakers",
  notify: "Notify targets",
  show_volume: "Show the volume slider",
  default_volume: "Starting volume",
  volume_step: "Volume slider step",
  chime: "Chime",
  show_critical: "Show the Critical toggle",
  critical: "Critical",
  holds: "Hold until",
};

/** Help text under (or, for the lists, above) each card option; as in the README. */
export const EDITOR_HELP = {
  title: "Card header. Leave empty for none.",
  players:
    "Speakers to offer. Leave empty to offer every media player. Each can have its " +
    "own name, chime and alternates: speakers played on instead, in order, when it " +
    "is offline or cannot play media.",
  notify: "Notify targets to offer, such as a phone.",
  show_volume: "Show a slider to set the volume of the message.",
  default_volume: "Where the volume slider starts.",
  volume_step: "How far the slider moves per step.",
  chime:
    "Chime before the message on every speaker from this card. A speaker's own " +
    "chime overrides it. Leave empty to use the integration setting.",
  show_critical: "Show a Critical toggle beside the Broadcast button.",
  critical:
    "Critical messages retry harder and fail loudly. With the toggle shown, this " +
    "is where it starts; without it, every broadcast from this card is critical.",
  holds:
    "Choices for holding a message until conditions are met, such as someone " +
    "getting home. Held messages, from anyone, are listed on the card with a " +
    "Cancel button.",
};

const LIST_OPTIONS = ["players", "notify", "holds"];
const HELP_ROW = "_help";

const CHIME_OPTIONS = [
  { value: "on", label: "Chime" },
  { value: "off", label: "No chime" },
];

const asList = (value) => (Array.isArray(value) ? value : []);
const isObject = (value) =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const sameValue = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// `chime` in the YAML is true, false or absent; in the form "on", "off" or unset.
// Both directions accept either form, so mapping twice changes nothing.
const chimeToForm = (v) => (v === true || v === "on" ? "on" : v === false || v === "off" ? "off" : undefined);
const chimeFromForm = (v) => (v === true || v === "on" ? true : v === false || v === "off" ? false : undefined);

// Set `key`, or remove it when the value is empty.
function setOrDrop(obj, key, value) {
  if (value === undefined || value === null || value === "") delete obj[key];
  else obj[key] = value;
}

/**
 * Throw an Error saying why when the form could not show `config` without
 * losing part of it; Home Assistant then keeps the card in YAML mode and shows
 * the message. Returns nothing for a config the form can show.
 */
export function assertEditable(config) {
  const fail = (why) => {
    throw new Error(`The visual editor cannot show this card: ${why}.`);
  };
  const isChime = (v) => v === undefined || typeof v === "boolean";
  for (const key of LIST_OPTIONS) {
    if (config[key] != null && !Array.isArray(config[key])) fail(`${key} must be a list`);
  }
  if (!isChime(config.chime)) fail("chime must be true or false");
  for (const p of asList(config.players)) {
    if (typeof p === "string") continue;
    if (!isObject(p)) fail("each speaker must be an entity, or an entry with entity");
    const alts = p.alternates;
    if (alts != null && typeof alts !== "string" && !Array.isArray(alts))
      fail(`the alternates of ${p.entity} must be a list`);
    if (!isChime(p.chime)) fail(`the chime of ${p.entity} must be true or false`);
  }
  for (const n of asList(config.notify)) {
    if (typeof n !== "string" && !isObject(n))
      fail("each notify target must be a service, or an entry with service");
  }
  for (const h of asList(config.holds)) {
    if (!isObject(h)) fail("each hold must be an entry with name and conditions");
  }
}

/**
 * Return the form data for a card config: a copy with absent options at their
 * EDITOR_DEFAULTS, shorthand entries in full (`{entity}`, `{service}`,
 * `alternates` as a list) and `chime` as "on"/"off"/unset. Other keys are kept.
 */
export function toFormData(config) {
  const form = { ...config };
  for (const [key, value] of Object.entries(EDITOR_DEFAULTS)) {
    if (form[key] == null) form[key] = Array.isArray(value) ? [] : value;
  }
  form.players = asList(form.players).map((p) => {
    const item = typeof p === "string" ? { entity: p } : { ...p };
    if (typeof item.alternates === "string") item.alternates = [item.alternates];
    setOrDrop(item, "chime", chimeToForm(item.chime));
    return item;
  });
  form.notify = asList(form.notify).map((n) =>
    typeof n === "string" ? { service: n } : { ...n }
  );
  setOrDrop(form, "chime", chimeToForm(form.chime));
  return form;
}

/**
 * Return the card config for edited form data, given the config the form was
 * opened with. Keys the form does not know are kept; `chime` goes back to
 * true/false/absent; empty names, titles and alternates are left out; and an
 * option is left out while it is at its default, unless the original config
 * spelled it out and the edit did not touch it.
 */
export function fromFormData(form, original) {
  const config = { ...original, ...form };
  config.players = asList(config.players).map((p) => {
    const item = { ...p };
    setOrDrop(item, "chime", chimeFromForm(item.chime));
    setOrDrop(item, "name", item.name);
    if (!asList(item.alternates).length) delete item.alternates;
    return item;
  });
  config.notify = asList(config.notify).map((n) => {
    const item = { ...n };
    setOrDrop(item, "name", item.name);
    return item;
  });
  setOrDrop(config, "chime", chimeFromForm(config.chime));
  setOrDrop(config, "title", config.title);
  for (const [key, value] of Object.entries(EDITOR_DEFAULTS)) {
    const untouched = key in original && sameValue(original[key], config[key]);
    if (!untouched && sameValue(config[key], value)) delete config[key];
  }
  return config;
}

/**
 * Return the notify targets to offer, as `{value, label}` sorted by label:
 * every `notify.*` entity (labelled with its friendly name), every notify
 * service except `send_message`, and any `configured` target HA does not offer
 * now, so a removed phone still shows. `hass` may be undefined.
 */
export function notifyOptions(hass, configured = []) {
  const options = new Map();
  const add = (value, label) => {
    if (value && !options.has(value)) options.set(value, label || value);
  };
  for (const [id, st] of Object.entries((hass && hass.states) || {})) {
    if (id.startsWith("notify.")) add(id, st && st.attributes && st.attributes.friendly_name);
  }
  const services = (hass && hass.services && hass.services.notify) || {};
  for (const name of Object.keys(services)) {
    if (name !== "send_message") add(`notify.${name}`);
  }
  for (const value of configured) add(value);
  return [...options]
    .map(([value, label]) => ({ value, label }))
    .sort((a, b) => a.label.localeCompare(b.label));
}

// A line of help text above a list: HA's list control shows no helper of its own.
const helpRow = (name) => ({ type: "constant", name: name + HELP_ROW });

/**
 * Return the ha-form schema for the card's edit dialog: title, speakers,
 * notify targets (offered from `hass`, keeping those in `config`), then
 * collapsed Volume, Chime and critical, and Hold until sections.
 */
export function editorSchema(hass, config = {}) {
  const configured = asList(config && config.notify)
    .map((n) => (typeof n === "string" ? n : n && n.service))
    .filter(Boolean);
  const mediaPlayer = { domain: "media_player" };
  const section = (title, icon, schema) => ({
    type: "expandable",
    name: "",
    flatten: true,
    title,
    icon,
    schema,
  });
  return [
    { name: "title", selector: { text: {} } },
    helpRow("players"),
    {
      name: "players",
      selector: {
        object: {
          multiple: true,
          label_field: "entity",
          description_field: "name",
          fields: {
            entity: {
              label: "Speaker",
              description: "The media player to speak the message on.",
              required: true,
              selector: { entity: { filter: mediaPlayer } },
            },
            name: {
              label: "Name",
              description: "Shown on the card instead of the speaker's own name.",
              selector: { text: {} },
            },
            chime: {
              label: "Chime",
              description: "Overrides the card's chime for this speaker. Leave empty to follow the card.",
              selector: { select: { mode: "dropdown", options: CHIME_OPTIONS } },
            },
            alternates: {
              label: "Alternates",
              description:
                "Speakers to play on instead, in order, when this one is offline or cannot play media.",
              selector: { entity: { multiple: true, reorder: true, filter: mediaPlayer } },
            },
          },
        },
      },
    },
    helpRow("notify"),
    {
      name: "notify",
      selector: {
        object: {
          multiple: true,
          label_field: "service",
          description_field: "name",
          fields: {
            service: {
              label: "Target",
              description: "A notify service or notify entity. Pick one, or type its name.",
              required: true,
              selector: {
                select: {
                  mode: "dropdown",
                  custom_value: true,
                  options: notifyOptions(hass, configured),
                },
              },
            },
            name: {
              label: "Name",
              description: "Shown on the card instead of the target's own name.",
              selector: { text: {} },
            },
          },
        },
      },
    },
    section("Volume", "mdi:volume-high", [
      { name: "show_volume", selector: { boolean: {} } },
      {
        name: "default_volume",
        selector: { number: { min: 0, max: 100, step: 1, mode: "slider", unit_of_measurement: "%" } },
      },
      { name: "volume_step", selector: { number: { min: 1, max: 50, step: 1, mode: "box" } } },
    ]),
    section("Chime and critical", "mdi:bell-ring-outline", [
      { name: "chime", selector: { select: { mode: "dropdown", options: CHIME_OPTIONS } } },
      { name: "show_critical", selector: { boolean: {} } },
      { name: "critical", selector: { boolean: {} } },
    ]),
    section("Hold until", "mdi:timer-pause-outline", [
      helpRow("holds"),
      {
        name: "holds",
        selector: {
          object: {
            multiple: true,
            label_field: "name",
            fields: {
              name: {
                label: "Name",
                description: "Shown as the choice, and as “held until …” on the card.",
                required: true,
                selector: { text: {} },
              },
              conditions: {
                label: "Conditions",
                description: "The message is held until these are met, as in an automation's conditions.",
                required: true,
                selector: { condition: {} },
              },
            },
          },
        },
      },
    ]),
  ];
}

/** ha-form `computeLabel`: an option's label, or a help row's text. */
export function editorLabel(schema) {
  if (schema.type === "constant") return EDITOR_HELP[schema.name.slice(0, -HELP_ROW.length)] || "";
  return EDITOR_LABELS[schema.name] || schema.name;
}

/** ha-form `computeHelper`: help under an option; the lists have a row above instead. */
export function editorHelper(schema) {
  if (LIST_OPTIONS.includes(schema.name)) return "";
  return EDITOR_HELP[schema.name] || "";
}

const STYLES = `
  :host { display: block; }
  .content { padding: 16px; display: flex; flex-direction: column; gap: 14px; }
  .field, .section { display: flex; flex-direction: column; gap: 6px; }
  label {
    font-size: 0.8rem; font-weight: 600; color: var(--secondary-text-color);
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  textarea.message {
    width: 100%; box-sizing: border-box; resize: vertical;
    font: inherit; color: var(--primary-text-color);
    background: var(--secondary-background-color);
    border: 1px solid var(--divider-color); border-radius: 10px; padding: 10px 12px;
  }
  textarea.message:focus { outline: none; border-color: var(--primary-color); }
  .field-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  button.clear {
    border: none; background: none; cursor: pointer; font: inherit;
    line-height: 1; padding: 2px 6px; border-radius: 8px;
    color: var(--secondary-text-color);
  }
  button.clear:hover { color: var(--primary-text-color); background: var(--secondary-background-color); }
  button.clear[hidden] { display: none; }
  .send-row { display: flex; align-items: stretch; gap: 8px; margin-top: 4px; }
  .hold-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; font-size: 0.9em; }
  .hold-row select { flex: 1; padding: 4px; border-radius: 6px; }
  .hold-row span { color: var(--secondary-text-color, #666); }
  .held { list-style: none; padding: 0; margin: 8px 0 0; }
  .held-item { display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 6px 0; border-top: 1px solid var(--divider-color, #e0e0e0); font-size: 0.9em; }
  .held-item .cancel { border: 1px solid var(--divider-color, #ccc); background: none;
    border-radius: 6px; padding: 2px 10px; cursor: pointer; color: inherit; }
  .send-row button.broadcast { flex: 1; margin-top: 0; }
  button.critical {
    flex: none; padding: 0 16px; border-radius: 12px; cursor: pointer;
    font: inherit; font-weight: 700; white-space: nowrap;
    color: var(--secondary-text-color);
    background: var(--secondary-background-color);
    border: 2px solid var(--divider-color);
    transition: border-color 120ms, background 120ms, color 120ms;
  }
  button.critical:hover { border-color: var(--warning-color, #ff9800); }
  button.critical.armed {
    color: var(--warning-color, #ff9800);
    border-color: var(--warning-color, #ff9800);
    background: color-mix(in srgb, var(--warning-color, #ff9800) 16%, var(--card-background-color));
  }
  button.critical .mdi { margin-right: 6px; }
  .volume { display: flex; align-items: center; gap: 12px; }
  .volume input[type="range"] { flex: 1; accent-color: var(--primary-color); }
  .volume-value {
    min-width: 3ch; text-align: right; font-variant-numeric: tabular-nums;
    color: var(--primary-text-color);
  }
  .chips { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 8px; }
  .chip {
    display: flex; align-items: center; justify-content: center; gap: 6px;
    padding: 12px 10px; border-radius: 12px; cursor: pointer;
    font: inherit; text-align: center;
    color: var(--primary-text-color);
    background: var(--secondary-background-color);
    border: 2px solid var(--divider-color);
    transition: border-color 120ms, background 120ms, color 120ms;
  }
  .chip:hover { border-color: var(--primary-color); }
  .chip.selected {
    border-color: var(--primary-color);
    background: color-mix(in srgb, var(--primary-color) 16%, var(--card-background-color));
    color: var(--primary-color);
  }
  .chip.offline { opacity: 0.55; }
  .chip.offline::after {
    content: "offline"; font-size: 0.65rem; text-transform: uppercase;
    color: var(--error-color, #db4437);
  }
  .chip-label { line-height: 1.2; }
  button.broadcast {
    margin-top: 4px; padding: 14px; border: none; border-radius: 12px; cursor: pointer;
    font-size: 1.05rem; font-weight: 700;
    color: var(--text-primary-color, #fff); background: var(--primary-color);
  }
  button.broadcast:disabled { opacity: 0.6; cursor: default; }
  button.broadcast .mdi { margin-right: 6px; }
  .status {
    min-height: 1.2em; font-size: 0.85rem; color: var(--secondary-text-color);
    display: flex; flex-direction: column; gap: 6px;
  }
  .status.busy { font-style: italic; }
  .status.replay { opacity: 0.7; }
  .status-head { font-weight: 600; }
  .status.good .status-head { color: var(--success-color, #43a047); }
  .status.warn .status-head { color: var(--warning-color, #ff9800); }
  .status.bad .status-head { color: var(--error-color, #db4437); }
  .status.warn, .status.bad { color: var(--primary-text-color); }
  .status-rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  .status-rows li {
    display: flex; justify-content: space-between; gap: 10px;
    padding: 2px 0; border-bottom: 1px solid var(--divider-color);
  }
  .status-rows li:last-child { border-bottom: none; }
  .row-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row-state { flex: none; font-variant-numeric: tabular-nums; }
  .status-rows li.ok .row-state { color: var(--success-color, #43a047); }
  .status-rows li.bad .row-state { color: var(--error-color, #db4437); font-weight: 600; }
  button.retry {
    align-self: flex-start; padding: 8px 14px; border-radius: 10px; cursor: pointer;
    font: inherit; font-weight: 600;
    color: var(--primary-color); background: transparent;
    border: 2px solid var(--primary-color);
  }
`;

if (!customElements.get("intercom-card")) {
  customElements.define("intercom-card", IntercomCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "intercom-card")) {
  window.customCards.push({
    type: "intercom-card",
    name: "Intercom Card",
    description:
      "Type a message, pick speakers (with volume) and notify targets, and broadcast.",
    preview: false,
    documentationURL: "https://github.com/will-roscoe/intercom",
  });
}

console.info(`%c INTERCOM-CARD %c v${VERSION} `,
  "color:#fff;background:#03a9f4;font-weight:700;border-radius:4px 0 0 4px;",
  "color:#03a9f4;background:#fff;font-weight:700;border-radius:0 4px 4px 0;");

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
 *   critical: false            # optional; retry harder and fail loudly
 *   players:                   # optional; auto-discovers media_player.* if omitted
 *     - entity: media_player.sonosroam
 *       name: Sonos Roam
 *   notify:                    # optional
 *     - service: notify.mobile_app_phone
 *       name: My Phone
 */

const VERSION = "0.2.0";

// Per-target statuses returned by intercom.broadcast.
const OK_STATUSES = new Set(["played", "sent"]);
const STATUS_TEXT = {
  played: "played",
  sent: "sent",
  unverified: "no sound detected",
  silent: "muted — not audible",
  failed: "failed",
  offline: "offline",
  unsupported: "cannot play media",
};

class IntercomCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = {};
    this._selectedPlayers = new Set();
    this._selectedNotify = new Set();
    this._volume = 40;
    this._built = false;
    this._unsub = null;
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
      players: [],
      notify: [],
    };
  }

  setConfig(config) {
    this._config = config || {};
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
    if (this._unsub) {
      this._unsub.then((fn) => fn && fn()).catch(() => {});
      this._unsub = null;
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
    msg.addEventListener("input", (e) => (this._message = e.target.value));
    this._msgEl = msg;
    content.appendChild(this._field("Message", msg));

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

    // broadcast button
    const btn = document.createElement("button");
    btn.className = "broadcast";
    btn.innerHTML = `<span class="mdi">📣</span> Broadcast`;
    btn.addEventListener("click", () => this._broadcast());
    this._btn = btn;
    content.appendChild(btn);

    // status
    this._statusEl = document.createElement("div");
    this._statusEl.className = "status";
    content.appendChild(this._statusEl);

    card.appendChild(content);
    root.appendChild(card);
    this._built = true;
    this._updateAvailability();
    if (this._lastResult)
      this._showResult(this._lastResult, false, this._lastResultReplay);
    else if (this._statusText) this._setStatus(this._statusText);
  }

  _field(label, el) {
    const row = document.createElement("div");
    row.className = "field";
    const lbl = document.createElement("label");
    lbl.textContent = label;
    row.appendChild(lbl);
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
      chip.title = offline ? "Currently unavailable — will be skipped" : "";
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
    if (notifyTargets.length) data.notify = notifyTargets;
    if (this._config.show_volume !== false && players.length)
      data.volume = this._volume;
    if (this._config.critical) data.critical = true;

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
      if (result) {
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
    for (const p of result.players || []) {
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
      : (result.players || [])
          .filter((p) => !OK_STATUSES.has(p.status))
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
    if (own && complete && this._msgEl) {
      this._message = "";
      this._msgEl.value = "";
    }
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

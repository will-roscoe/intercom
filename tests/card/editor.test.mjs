// The card's visual editor: how card YAML maps to the form and back, the form's
// schema, and the configs the editor refuses. Run with `node --test`; pytest
// runs it through tests/test_card_editor.py.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { card, defined } from "./load-card.mjs";

const {
  assertEditable,
  toFormData,
  fromFormData,
  notifyOptions,
  editorSchema,
  editorLabel,
  editorHelper,
  EDITOR_DEFAULTS,
} = card;

const LISTS = ["players", "notify", "holds"];

const here = path.dirname(fileURLToPath(import.meta.url));
const README = readFileSync(path.join(here, "..", "..", "README.md"), "utf8");

const TYPE = "custom:intercom-card";

const README_EXAMPLE = {
  type: TYPE,
  title: "Intercom",
  default_volume: 40,
  players: [
    {
      entity: "media_player.sonosroam",
      name: "Sonos Roam",
      alternates: ["media_player.kitchen"],
    },
  ],
  notify: [
    { service: "notify.mobile_app_phone", name: "My Phone" },
    { service: "notify.persistent_notification", name: "HA notifications" },
  ],
};

const EXAMPLE_FILE = {
  type: TYPE,
  title: "Intercom",
  default_volume: 40,
  show_critical: true,
  players: [
    { entity: "media_player.sonos_living_room", name: "Living Room" },
    { entity: "media_player.living_room_tv", name: "Living Room TV" },
  ],
  notify: [
    { service: "notify.mobile_app_phone", name: "My Phone" },
    { service: "notify.persistent_notification", name: "HA notifications" },
  ],
  holds: [
    {
      name: "Will is home",
      conditions: [{ condition: "state", entity_id: "person.will", state: "home" }],
    },
  ],
};

const HASS = {
  services: {
    notify: { mobile_app_phone: {}, persistent_notification: {}, send_message: {} },
    media_player: { play_media: {} },
  },
  states: {
    "notify.kitchen_display": {
      state: "unknown",
      attributes: { friendly_name: "Kitchen display" },
    },
    "media_player.kitchen": { state: "idle", attributes: {} },
  },
};

// One visual edit: the form shows the config, the user changes `changes`, and
// the editor writes the result back.
const edit = (config, changes) =>
  fromFormData({ ...toFormData(config), ...changes }, config);

// Every field name in a schema, descending into sections; object selectors'
// item fields are returned under "<list>.<field>".
function fieldNames(schema) {
  const names = [];
  for (const field of schema) {
    if (field.schema) {
      names.push(...fieldNames(field.schema));
      continue;
    }
    if (field.type === "constant") continue; // a line of help text, not an option
    names.push(field.name);
    const fields = field.selector && field.selector.object && field.selector.object.fields;
    if (fields) for (const key of Object.keys(fields)) names.push(`${field.name}.${key}`);
  }
  return names;
}

function findField(schema, name) {
  for (const field of schema) {
    if (field.schema) {
      const found = findField(field.schema, name);
      if (found) return found;
    } else if (field.name === name) {
      return field;
    }
  }
  return undefined;
}

// --- YAML to form ------------------------------------------------------------

test("shorthand entries open as full entries", () => {
  const form = toFormData({
    type: TYPE,
    players: [
      "media_player.roam",
      { entity: "media_player.lounge", alternates: "media_player.kitchen" },
    ],
    notify: ["notify.mobile_app_phone"],
  });
  assert.deepEqual(form.players, [
    { entity: "media_player.roam" },
    { entity: "media_player.lounge", alternates: ["media_player.kitchen"] },
  ]);
  assert.deepEqual(form.notify, [{ service: "notify.mobile_app_phone" }]);
});

test("a hold with a single condition opens as a list of one", () => {
  // The service accepts one condition or a template string where a list is
  // expected (cv.CONDITIONS_SCHEMA); HA's condition editor needs the list.
  const state = { condition: "state", entity_id: "person.will", state: "home" };
  const template = "{{ is_state('person.will', 'home') }}";
  const form = toFormData({
    type: TYPE,
    holds: [
      { name: "One", conditions: state },
      { name: "Template", conditions: template },
      { name: "List", conditions: [state] },
    ],
  });
  assert.deepEqual(
    form.holds.map((h) => h.conditions),
    [[state], [template], [state]]
  );
});

test("absent options show the card's defaults", () => {
  const form = toFormData({ type: TYPE });
  for (const [key, value] of Object.entries(EDITOR_DEFAULTS)) {
    assert.deepEqual(form[key], value, key);
  }
  assert.deepEqual(EDITOR_DEFAULTS, {
    show_volume: true,
    default_volume: 40,
    volume_step: 5,
    show_critical: false,
    critical: false,
    players: [],
    notify: [],
    holds: [],
  });
});

test("chime shows as on, off or unset", () => {
  assert.equal(toFormData({ type: TYPE, chime: true }).chime, "on");
  assert.equal(toFormData({ type: TYPE, chime: false }).chime, "off");
  assert.equal(toFormData({ type: TYPE }).chime, undefined);
  const players = toFormData({
    type: TYPE,
    players: [
      { entity: "media_player.a", chime: true },
      { entity: "media_player.b", chime: false },
      { entity: "media_player.c" },
    ],
  }).players;
  assert.deepEqual(
    players.map((p) => p.chime),
    ["on", "off", undefined]
  );
  assert.ok(!("chime" in players[2]));
});

// --- form to YAML ------------------------------------------------------------

test("a config opened and left alone comes back unchanged", () => {
  for (const config of [README_EXAMPLE, EXAMPLE_FILE, { type: TYPE }]) {
    assert.deepEqual(edit(config, {}), config);
  }
  const stub = { type: TYPE, ...card.IntercomCard.getStubConfig() };
  assert.deepEqual(edit(stub, {}), stub);
});

test("editing one field keeps every other key", () => {
  const config = { ...README_EXAMPLE, mystery: { kept: [1, 2] } };
  assert.deepEqual(edit(config, { title: "Hall" }), { ...config, title: "Hall" });
});

test("defaults are not written until changed", () => {
  assert.deepEqual(edit({ type: TYPE }, { default_volume: 60 }), {
    type: TYPE,
    default_volume: 60,
  });
  assert.deepEqual(edit({ type: TYPE }, { show_volume: false }), {
    type: TYPE,
    show_volume: false,
  });
});

test("turning an option on and off again leaves it out", () => {
  const on = edit({ type: TYPE }, { show_critical: true });
  assert.deepEqual(on, { type: TYPE, show_critical: true });
  assert.deepEqual(edit(on, { show_critical: false }), { type: TYPE });
});

test("an option written out at its default stays while untouched", () => {
  const config = { type: TYPE, show_volume: true, critical: false };
  assert.deepEqual(edit(config, { title: "Hall" }), { ...config, title: "Hall" });
});

test("emptying a list leaves it out", () => {
  const config = { type: TYPE, holds: EXAMPLE_FILE.holds };
  assert.deepEqual(edit(config, { holds: [] }), { type: TYPE });
});

test("chime is written as true, false or nothing", () => {
  assert.equal(edit({ type: TYPE }, { chime: "on" }).chime, true);
  assert.equal(edit({ type: TYPE }, { chime: "off" }).chime, false);
  assert.ok(!("chime" in edit({ type: TYPE, chime: true }, { chime: undefined })));
  assert.ok(!("chime" in edit({ type: TYPE, chime: false }, { chime: "" })));

  const config = { type: TYPE, players: [{ entity: "media_player.a", chime: false }] };
  assert.deepEqual(edit(config, { players: [{ entity: "media_player.a", chime: "on" }] }).players, [
    { entity: "media_player.a", chime: true },
  ]);
  assert.deepEqual(edit(config, { players: [{ entity: "media_player.a" }] }).players, [
    { entity: "media_player.a" },
  ]);
});

test("cleared text is left out", () => {
  assert.deepEqual(edit({ type: TYPE, title: "Hall" }, { title: "" }), { type: TYPE });
  const config = { type: TYPE, players: [{ entity: "media_player.a", name: "A" }] };
  assert.deepEqual(edit(config, { players: [{ entity: "media_player.a", name: "" }] }).players, [
    { entity: "media_player.a" },
  ]);
});

test("shorthand entries are written in full once edited", () => {
  const config = { type: TYPE, players: ["media_player.a"], title: "Old" };
  assert.deepEqual(edit(config, { title: "New" }), {
    type: TYPE,
    players: [{ entity: "media_player.a" }],
    title: "New",
  });
});

// --- notify targets ----------------------------------------------------------

test("notify targets are the services and notify entities HA has", () => {
  const options = notifyOptions(HASS, ["notify.old_phone", "notify.mobile_app_phone"]);
  const values = options.map((o) => o.value);
  assert.deepEqual(new Set(values), new Set([
    "notify.mobile_app_phone",
    "notify.persistent_notification",
    "notify.kitchen_display",
    "notify.old_phone",
  ]));
  assert.equal(values.length, new Set(values).size, "no duplicates");
  assert.ok(!values.includes("notify.send_message"));
  assert.equal(options.find((o) => o.value === "notify.kitchen_display").label, "Kitchen display");
});

test("notify targets work before HA has loaded", () => {
  assert.deepEqual(
    notifyOptions(undefined, ["notify.a"]).map((o) => o.value),
    ["notify.a"]
  );
});

// --- schema ------------------------------------------------------------------

test("the form offers every option in the README table, once", () => {
  const table = README.split("| Card option |")[1].split("\n\n")[0];
  const documented = [...table.matchAll(/^\| `([a-z_]+)` \|/gm)].map((m) => m[1]);
  assert.ok(documented.length >= 10, "README table found");

  const names = fieldNames(editorSchema(HASS, README_EXAMPLE));
  const topLevel = names.filter((n) => !n.includes("."));
  assert.deepEqual([...topLevel].sort(), [...documented].sort());
  assert.equal(topLevel.length, new Set(topLevel).size);
  assert.deepEqual(names.filter((n) => n.includes(".")).sort(), [
    "holds.conditions",
    "holds.name",
    "notify.name",
    "notify.service",
    "players.alternates",
    "players.chime",
    "players.entity",
    "players.name",
  ]);
});

test("every option has a label and help text", () => {
  const schema = editorSchema(HASS, README_EXAMPLE);
  for (const name of fieldNames(schema)) {
    if (name.includes(".")) {
      const [list, key] = name.split(".");
      const item = findField(schema, list).selector.object.fields[key];
      assert.ok(item.label, `${name} label`);
      assert.ok(item.description, `${name} help`);
      continue;
    }
    const field = findField(schema, name);
    if (LISTS.includes(name)) {
      // HA's list control shows no helper, so a heading row with the help sits
      // above it, and the list's own label is left empty so it is not repeated.
      const index = schema.flatMap((f) => f.schema || [f]).indexOf(field);
      const row = schema.flatMap((f) => f.schema || [f])[index - 1];
      assert.equal(row.type, "constant", `${name} heading row`);
      assert.ok(editorLabel(row), `${name} heading`);
      assert.ok(row.value, `${name} help`);
      assert.equal(editorLabel(field), "", `${name} label shown once`);
      assert.equal(editorHelper(field), "", `${name} help shown once`);
    } else {
      assert.ok(editorLabel(field), `${name} label`);
      assert.ok(editorHelper(field), `${name} help`);
    }
  }
});

test("speakers and their alternates are picked from media players", () => {
  const fields = findField(editorSchema(HASS, {}), "players").selector.object.fields;
  assert.deepEqual(fields.entity.selector.entity.filter, { domain: "media_player" });
  assert.equal(fields.entity.required, true);
  assert.deepEqual(fields.alternates.selector.entity.filter, { domain: "media_player" });
  assert.equal(fields.alternates.selector.entity.multiple, true);
  assert.deepEqual(
    fields.chime.selector.select.options.map((o) => o.value),
    ["on", "off"]
  );
});

test("a notify target is a dropdown that also takes a typed one", () => {
  const config = { type: TYPE, notify: [{ service: "notify.old_phone" }] };
  const select = findField(editorSchema(HASS, config), "notify").selector.object.fields.service
    .selector.select;
  assert.equal(select.custom_value, true);
  const values = select.options.map((o) => o.value);
  assert.ok(values.includes("notify.mobile_app_phone"));
  assert.ok(values.includes("notify.old_phone"));
});

test("hold conditions use HA's condition editor", () => {
  const fields = findField(editorSchema(HASS, {}), "holds").selector.object.fields;
  assert.ok("condition" in fields.conditions.selector);
  assert.equal(fields.conditions.required, true);
  assert.equal(fields.name.required, true);
});

// --- configs the editor refuses ----------------------------------------------

test("configs the form can show are accepted", () => {
  for (const config of [
    README_EXAMPLE,
    EXAMPLE_FILE,
    { type: TYPE },
    { type: TYPE, ...card.IntercomCard.getStubConfig() },
    { type: TYPE, players: ["media_player.a"], notify: ["notify.a"], chime: false },
    { type: TYPE, players: [{ entity: "media_player.a", alternates: "media_player.b" }] },
  ]) {
    assert.doesNotThrow(() => assertEditable(config), JSON.stringify(config));
  }
});

test("configs the form would lose part of are refused", () => {
  for (const config of [
    { players: {} },
    { players: "media_player.a" },
    { players: [42] },
    { players: [{ entity: "media_player.a", alternates: 5 }] },
    { players: [{ entity: "media_player.a", chime: "yes" }] },
    { notify: "notify.a" },
    { notify: [7] },
    { holds: {} },
    { holds: ["Will is home"] },
    { chime: "yes" },
  ]) {
    assert.throws(() => assertEditable({ type: TYPE, ...config }), Error, JSON.stringify(config));
  }
});

// --- the editor element ------------------------------------------------------

function openEditor(config, hass = HASS) {
  const el = card.IntercomCard.getConfigElement();
  el.setConfig(config);
  el.hass = hass;
  const form = el.children.find((c) => c.localName === "ha-form");
  return { el, form };
}

test("the card offers its visual editor", () => {
  const el = card.IntercomCard.getConfigElement();
  assert.equal(el.localName, "intercom-card-editor");
  assert.ok(el instanceof defined.get("intercom-card-editor"));
});

test("the card picker shows a live preview", () => {
  const entry = globalThis.customCards.find((c) => c.type === "intercom-card");
  assert.equal(entry.preview, true);
});

test("the editor refuses a config the form cannot show", () => {
  const el = card.IntercomCard.getConfigElement();
  assert.throws(() => el.setConfig({ type: TYPE, players: {} }), /visual editor/);
});

test("the editor shows the config in HA's form", () => {
  const { form } = openEditor({ type: TYPE, chime: true });
  assert.ok(form, "an ha-form");
  assert.equal(form.hass, HASS);
  assert.equal(form.data.chime, "on");
  assert.equal(form.data.show_volume, true);
  assert.deepEqual(form.schema, editorSchema(HASS, { type: TYPE, chime: true }));
  assert.equal(form.computeLabel({ name: "title" }), "Title");
  assert.ok(form.computeHelper({ name: "critical" }));
});

test("an edit in the form becomes one config-changed with the card's YAML", () => {
  const { el, form } = openEditor({ type: TYPE, chime: true, mystery: 1 });
  const seen = [];
  el.addEventListener("config-changed", (ev) => seen.push(ev));
  form.dispatchEvent(
    new CustomEvent("value-changed", {
      detail: { value: { ...form.data, chime: "off", title: "Hall" } },
    })
  );
  assert.equal(seen.length, 1);
  assert.deepEqual(seen[0].detail.config, {
    type: TYPE,
    chime: false,
    mystery: 1,
    title: "Hall",
  });
  assert.equal(seen[0].bubbles, true);
  assert.equal(seen[0].composed, true);
});

test("the form follows the config HA sends back", () => {
  const { el, form } = openEditor({ type: TYPE });
  el.setConfig({ type: TYPE, title: "Hall" });
  assert.equal(form.data.title, "Hall");
});

test("the form is rebuilt only when the notify targets change", () => {
  const { el, form } = openEditor({ type: TYPE });
  const schema = form.schema;
  el.hass = { ...HASS, states: { ...HASS.states, "light.x": { state: "on", attributes: {} } } };
  assert.equal(form.schema, schema, "same schema after an unrelated state change");
  el.hass = {
    ...HASS,
    services: { ...HASS.services, notify: { ...HASS.services.notify, mobile_app_tablet: {} } },
  };
  assert.notEqual(form.schema, schema);
  assert.ok(
    findField(form.schema, "notify").selector.object.fields.service.selector.select.options.some(
      (o) => o.value === "notify.mobile_app_tablet"
    )
  );
});

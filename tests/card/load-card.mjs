// Load the intercom card in Node with just enough browser stand-ins for its
// top level: HTMLElement, customElements, window and document.createElement.
//
// The card is imported from a data: URL so Node treats it as an ES module
// whatever its version or the nearest package.json says about `.js` files.

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const defined = new Map();

export class FakeElement extends EventTarget {
  constructor() {
    super();
    this.children = [];
    this.shadowRoot = null;
  }
  attachShadow() {
    this.shadowRoot = new FakeElement();
    return this.shadowRoot;
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  append(...children) {
    this.children.push(...children);
  }
  setAttribute(name, value) {
    this[name] = value;
  }
}

globalThis.HTMLElement = FakeElement;
globalThis.window = globalThis;
globalThis.customElements = {
  get: (name) => defined.get(name),
  define: (name, cls) => defined.set(name, cls),
};
globalThis.document = {
  createElement(tag) {
    const Cls = defined.get(tag);
    const el = Cls ? new Cls() : new FakeElement();
    el.localName = tag;
    return el;
  },
};

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  path.join(here, "..", "..", "custom_components", "intercom", "www", "intercom-card.js"),
  "utf8"
);
export const card = await import(
  "data:text/javascript;base64," + Buffer.from(source).toString("base64")
);

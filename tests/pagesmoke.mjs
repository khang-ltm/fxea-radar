/* Run the page's inline script under a stub DOM and report the first throw.
 *
 * The page is one file with ~2,000 lines of script; a single error at load time
 * silently stops every handler after it, which has now shipped twice as "the
 * button does nothing". node --check only proves it parses. This proves it runs.
 *
 *   node tests/pagesmoke.mjs
 */
import fs from 'fs';
import path from 'path';

const file = path.join(process.cwd(), 'public', 'index.html');
const html = fs.readFileSync(file, 'utf8');
// the built page has more than one script block - the static shim, then the app
const blocks = [...html.matchAll(/<script(?![^>]*\ssrc=)[^>]*>([\s\S]*?)<\/script>/g)]
  .map(m => m[1]);

const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]));

const node = (id = '') => {
  const el = {
    id,
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    children: [],
    selectedOptions: [],
    value: '',
    textContent: '',
    innerHTML: '',
    hidden: false,
    disabled: false,
    open: false,
    setAttribute() {}, getAttribute: () => null, removeAttribute() {},
    addEventListener() {}, removeEventListener() {},
    appendChild() {}, remove() {}, closest: () => node(), focus() {},
    querySelectorAll: () => [], querySelector: () => null,
    showModal() { el.open = true; }, close() { el.open = false; },
    scrollIntoView() {},
  };
  return el;
};

const missing = [];
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.setTimeout = (fn) => 0;          // no timers: this is a load-time check
globalThis.setInterval = () => 0;
globalThis.clearTimeout = () => {};
globalThis.clearInterval = () => {};
globalThis.document = {
  documentElement: node('html'),
  body: node('body'),
  getElementById(id) {
    if (!ids.has(id)) missing.push(id);
    return node(id);
  },
  querySelector: () => node(),
  querySelectorAll: () => [],
  createElement: () => node(),
  addEventListener() {},
};
globalThis.localStorage = {
  store: new Map(),
  getItem(k) { return this.store.has(k) ? this.store.get(k) : null; },
  setItem(k, v) { this.store.set(k, String(v)); },
  removeItem(k) { this.store.delete(k); },
};
globalThis.location = { search: '', href: 'https://example.test/', reload() {} };
globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: false }), text: async () => '' });
globalThis.EventSource = class { constructor() {} close() {} };
globalThis.alert = () => {};
globalThis.confirm = () => false;
globalThis.prompt = () => null;
globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });
globalThis.requestAnimationFrame = fn => fn();

let failed = false;
try {
  for (const [i, block] of blocks.entries()) {
    try {
      new Function(block)();
    } catch (err) {
      throw new Error(`block ${i + 1}/${blocks.length}: ${err.message}`);
    }
  }
  console.log(`  PASS  all ${blocks.length} script block(s) run without throwing`);
} catch (err) {
  failed = true;
  console.log(`  FAIL  the page script threw at load: ${err.message}`);
  const line = String(err.stack || '').split('\n').find(l => l.includes('anonymous'));
  if (line) console.log(`        ${line.trim()}`);
}

// getElementById on an id the page does not contain returns null in a browser,
// which is how "the button does nothing" happens
const unknown = [...new Set(missing)].filter(id => !['load-more', 'histmorebtn'].includes(id));
if (unknown.length) {
  failed = true;
  console.log(`  FAIL  script asked for ids the page does not have: ${unknown.join(', ')}`);
} else {
  console.log('  PASS  every element the script looks up exists in the page');
}

process.exit(failed ? 1 : 0);

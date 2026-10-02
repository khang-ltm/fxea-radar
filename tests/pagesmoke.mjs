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

// Appended to the page's last script block so the test can call into it.
const PROBE = `
;globalThis.__probe = {
  renderMarket,
  setMarket(d) { MARKET = d; },
  prettyKey, commonLabel, headingLabel, isHeading,
};`;

const node = (id = '') => {
  const el = {
    id,
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    children: [],
    selectedOptions: [],
    value: '',
    checked: false,
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
// One node per id, kept: a browser hands back the same element every time, and
// without that a render writes its HTML into a throwaway object and nothing can
// be asserted about what it produced.
const nodes = new Map();
globalThis.document = {
  documentElement: node('html'),
  body: node('body'),
  getElementById(id) {
    if (!ids.has(id)) missing.push(id);
    if (!nodes.has(id)) nodes.set(id, node(id));
    return nodes.get(id);
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
let probe = null;
try {
  for (const [i, block] of blocks.entries()) {
    try {
      // The page's functions live inside the block's own scope, so a test can
      // only reach them by asking the block to hand them out on its way past.
      const last = i === blocks.length - 1;
      new Function(block + (last ? PROBE : ''))();
    } catch (err) {
      throw new Error(`block ${i + 1}/${blocks.length}: ${err.message}`);
    }
  }
  console.log(`  PASS  all ${blocks.length} script block(s) run without throwing`);
  probe = globalThis.__probe || null;
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

// -- the MQL5 Market tab renders ------------------------------------------
// An EA is ranked on its live signal, so the card has to survive both shapes it
// arrives in: one with a signal, and one without. The unproven case shipped as
// a crash once already, reading .dd_pct off a null signal.
if (probe) {
  const fixture = [
    { id: '1', name: 'Proven EA', url: 'https://mql5.test/1', author: 'A Seller',
      price: 499, rating: 4.5, reviews: 80, score: 9, proven: true, version: '4.91',
      // deliberately free of numbers: an assertion that the growth figure
      // reached the card must not be satisfied by the reason text quoting it
      why: ['+5 grew well against its drawdown', '-1 something'],
      signal: { url: 'https://mql5.test/s/1', growth_pct: 357, dd_pct: 16, trades: 880,
                win_pct: 61.2, profit_factor: 2.53, weeks: 101, deposit_load: 4.8 } },
    { id: '2', name: 'Unproven EA', url: 'https://mql5.test/2', author: 'B Seller',
      free: true, rating: null, reviews: 0, score: 0, proven: false,
      why: ['no live signal - nothing here is measured'], signal: null },
  ];
  try {
    probe.setMarket({ eas: fixture, crawled_at: new Date().toISOString(), proven: 1 });
    probe.renderMarket();
    const html = document.getElementById('mktlist').innerHTML || '';
    const checks = [
      [html.includes('Proven EA'), 'the proven EA is listed'],
      [html.includes('Unproven EA'), 'the unproven EA is listed'],
      [html.includes('unproven'), 'the unproven EA is labelled, not scored badly'],
      [html.includes('<b>357%</b>'), 'the growth figure reaches the card'],
      [html.includes('<b>101</b>'), 'the age reaches the card'],
      [html.includes('<b>2.53</b>'), 'the profit factor reaches the card'],
      [html.includes('v4.91'), 'the version is shown next to the name'],
      [(html.match(/v4\.91/g) || []).length === 1, 'the version is shown once, not per field'],
      [!html.includes('vundefined') && !html.includes('v</span>'),
       'an EA with no version renders no version badge'],
      [html.includes('not read yet'), 'a half-crawled entry says so rather than looking blank'],
      [html.includes('https://mql5.test/s/1'), 'the card links the live signal'],
      [!html.includes('undefined') && !html.includes('NaN'),
       'no field renders as undefined or NaN'],
    ];
    for (const [ok, what] of checks) {
      if (ok) { console.log(`  PASS  ${what}`); }
      else { failed = true; console.log(`  FAIL  ${what}`); }
    }
  } catch (err) {
    failed = true;
    console.log(`  FAIL  the Market tab threw while rendering: ${err.message}`);
  }
} else {
  failed = true;
  console.log('  FAIL  the page no longer exposes renderMarket to the smoke test');
}

// -- EA input names are readable ------------------------------------------
// An EA ships compiled and the chart template gives only `key=value`, so for
// most settings the name is the only evidence of meaning there will ever be.
// These are the shapes MQL5 authors actually write.
if (probe) {
  const cases = [
    ['InpLotCalcMode', 'Lot calculation mode'],
    ['MaxDDPerc', 'Maximum drawdown percent'],
    ['TPPts', 'Take profit points'],
    ['SLDist', 'Stop loss distance'],
    ['InpMagicNumber', 'Magic number'],
    // a one-letter prefix strip used to eat the first M here
    ['MMMode', 'Money management mode'],
    ['i_TF', 'Timeframe'],
    ['inp_RiskPercent', 'Risk percent'],
    ['GridStepPts', 'Grid step points'],
    ['ATRPeriod', 'ATR period'],
  ];
  const bad = cases.filter(([key, want]) => probe.prettyKey(key) !== want);
  if (bad.length) {
    failed = true;
    for (const [key, want] of bad) {
      console.log(`  FAIL  ${key} reads as "${probe.prettyKey(key)}", expected "${want}"`);
    }
  } else {
    console.log(`  PASS  all ${cases.length} input names read as words`);
  }

  // The glossary asserts a convention, so it must only speak when it says
  // something the plain name does not.
  const quiet = [['inp_RiskPercent', 'Risk'], ['DailyLossLimit', 'Daily limit']];
  const loud = quiet.filter(([key]) => probe.commonLabel(key) !== '');
  if (loud.length) {
    failed = true;
    console.log(`  FAIL  the glossary overrides a richer plain name: `
      + loud.map(([k]) => `${k} -> "${probe.commonLabel(k)}"`).join(', '));
  } else {
    console.log('  PASS  the glossary stands down when the name already says it');
  }

  // ...and must still speak when it does.
  const speaks = [['InpMagicNumber', 'Magic number'], ['PropMode', 'Prop firm mode']];
  const mute = speaks.filter(([key, want]) => probe.commonLabel(key) !== want);
  if (mute.length) {
    failed = true;
    console.log(`  FAIL  the glossary went quiet where it was needed: `
      + mute.map(([k, w]) => `${k} gave "${probe.commonLabel(k)}" not "${w}"`).join(', '));
  } else {
    console.log('  PASS  the glossary still names what the key alone cannot');
  }
}

// -- section headers are headers, not text boxes --------------------------
// MQL5 gives an author no way to put a heading in the parameter dialog, so they
// fake one with a dummy input whose value is the title. Every case below was
// taken from a .set file shipped by the channel, written by a different author.
if (probe) {
  const banners = [
    [{ k: '__Risque__', v: '=== Risk Management ===' }, 'Risk Management'],
    [{ k: 'Group5', v: '=== SYSTEM ===' }, 'SYSTEM'],
    [{ k: 'lineRisk', v: '>>> Risk Management' }, 'Risk Management'],
    [{ k: '___1___', v: '========== Lot Size ==========' }, 'Lot Size'],
    [{ k: 'setting_1', v: '--------------------------------' }, ''],
    [{ k: 'blank_2', v: '' }, ''],
    [{ k: 'line_02', v: '' }, ''],
    // straight off the live account: MQL5's own `input group` arrives as a key
    // with no value and no decoration, and Quantum's banners are in the key
    [{ k: 'input group #1', v: '' }, 'Group #1'],
    [{ k: '>>>> GENERAL SETTINGS', v: '' }, 'GENERAL SETTINGS'],
    [{ k: '>>> TRADING DAYS', v: '' }, 'TRADING DAYS'],
  ];
  const wrong = banners.filter(([row, want]) => probe.headingLabel(row) !== want);
  if (wrong.length) {
    failed = true;
    for (const [row, want] of wrong) {
      console.log(`  FAIL  ${row.k}=${row.v} gave `
        + `${JSON.stringify(probe.headingLabel(row))}, expected ${JSON.stringify(want)}`);
    }
  } else {
    console.log(`  PASS  all ${banners.length} fake section headers read as headings`);
  }

  // The old rule was "no value means heading", which hid real settings that
  // happen to be empty strings - they could neither be read nor edited.
  const settings = [
    { k: 'InpCalcPair', v: '' },
    { k: 'SwapProtect', v: '' },
    { k: 'RecoveryTPList', v: '' },
    { k: 'TradeComment', v: '>>> my trades' },   // not a banner: a real string
    { k: 'InpFont', v: 'Trebuchet MS' },         // live: a real string setting
    { k: 'InpNoteName', v: 'Quantum Athena v1.1' },
  ];
  const eaten = settings.filter(row => probe.isHeading(row));
  if (eaten.length) {
    failed = true;
    console.log('  FAIL  real settings swallowed as headings: '
      + eaten.map(r => r.k).join(', '));
  } else {
    console.log('  PASS  an empty or decorated setting is still an editable setting');
  }
}

process.exit(failed ? 1 : 0);

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
  tab: () => TAB,
  prettyKey, commonLabel, headingLabel, isHeading, vendorLabel,
  trimSection, derivedSections, sectionOf,
  renderMt5, sltpOpen, sltpRender, sltpRead, sltpBE, sltpCell, sltpMoney,
  setPositions(p) { MT5_POSITIONS = p; },
  setSltp,
  stubDialog(post, askFn) { managerPost = post; ask = askFn; },
  reportStats, renderReport, rptFillPicker,
  setReport(hist, acct, dd) { RPT_HIST = hist; RPT_ACCT = acct; RPT_DD = dd; RPT_DD_BUSY = false; RPT_ERR = ''; },
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

function runPage() {
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
}

let failed = false;
let probe = null;
try {
  runPage();
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

if (!probe) {
  failed = true;
  console.log('  FAIL  the page script did not hand the smoke test its functions');
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
    // straight off the running EAs: "Min" is three different words here
    ['Inp_NewsFilter_EntryPauseBeforeMin', 'News filter entry pause before minutes'],
    ['Inp_SupportResistanceStrategy_Enable15MinTF',
     'Support resistance strategy enable 15 minute timeframe'],
    ['MinPendingDistance', 'Minimum pending distance'],
    ['Inp_DynamicExit_WindowSec', 'Dynamic exit window seconds'],
    ['Inp_PreventTradingCloseToATH', 'Prevent trading close to ATH'],
    ['LotPer100Balance', 'Lot per 100 balance'],
    ['InpGridDistMultip', 'Grid distance multiplier'],
    // "Multi" was expanded to "multiplier" and made this a multiplier currency
    ['Inp_EnableMultiCurrency', 'Enable multi currency'],
    // weekday names were sentence-cased into "friday"
    ['InpTradingFriday', 'Trading Friday'],
    ['Inp_MondayToThursdayFrom', 'Monday to Thursday from'],
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

// -- option names are offered, never a mapping ----------------------------
// InpPause=1 on two EAs that are both actively trading, so 1 cannot mean
// "paused" - a dropdown asserting 0=no/1=yes would have been confidently wrong.
// Option lists are therefore shown as names and never bound to numbers.
if (probe) {
  const dd = probe.vendorLabel('InpDDMode');
  const pause = probe.vendorLabel('InpPause');
  const checks = [
    [!!dd && Array.isArray(dd.opts) && dd.opts.length === 4,
     'a documented option list is carried'],
    [!!pause && !pause.opts,
     'no option list is invented where the live values contradict it'],
  ];
  for (const [ok, what] of checks) {
    if (ok) console.log(`  PASS  ${what}`);
    else { failed = true; console.log(`  FAIL  ${what}`); }
  }
}

// -- a section does not repeat itself in every label ----------------------
// Wave Rider namespaces 89 inputs, so under a derived heading every label used
// to begin by restating the heading: "Support resistance strategy enabled".
if (probe) {
  const rows = [
    { k: 'Inp_SupportResistanceStrategy_Enabled', v: 'true' },
    { k: 'Inp_SupportResistanceStrategy_TradeMagicNumber', v: '77701' },
    { k: 'Inp_PullbackStrategy_Enabled', v: 'true' },
    { k: 'Inp_PullbackStrategy_TradeMagicNumber', v: '77702' },
    { k: 'Inp_MomentumStrategy_Enabled', v: 'true' },
    { k: 'Inp_MomentumStrategy_TradeMagicNumber', v: '77706' },
  ];
  const secs = probe.derivedSections(rows);
  const checks = [
    [!!secs && secs.has('SupportResistanceStrategy'), 'a namespace becomes a section'],
    // a pair is a section too: the only pairs on the live account were two
    // strategies that looked broken left loose beside five larger ones
    [!!secs && secs.has('MomentumStrategy'), 'a two-input namespace is still a section'],
    [probe.trimSection('Support resistance strategy enabled',
                       'SupportResistanceStrategy') === 'Enabled',
     'the section name is dropped from the label beneath it'],
    [probe.trimSection('Magic number', 'Grid') === 'Magic number',
     'a label that does not start with the section is left alone'],
    [probe.trimSection('Grid', 'Grid') === 'Grid',
     'a label equal to its section keeps its name rather than going blank'],
  ];
  for (const [ok, what] of checks) {
    if (ok) console.log(`  PASS  ${what}`);
    else { failed = true; console.log(`  FAIL  ${what}`); }
  }
}

// -- every real input on the account reads as words -------------------------
// tests/fixtures/live_inputs.json holds the input NAMES of the EAs running on
// the owner's account (values reduced to their kind, except text the EA's
// author wrote). Every one must come out as a heading, help text, or a label
// that is not still the variable name.
if (probe) {
  const fx = JSON.parse(fs.readFileSync(path.join(process.cwd(), 'tests', 'fixtures', 'live_inputs.json'), 'utf8'));
  const raw = [];
  let total = 0;
  for (const ea of fx.eas) {
    const inputs = ea.inputs.map(r => ({ k: r.k, v: r.v !== undefined ? r.v : (r.kind === 'bool' ? 'true' : r.kind === 'int' ? '1' : r.kind === 'float' ? '1.0' : r.kind === 'url' ? 'https://x.test/' : 'x') }));
    const secs = probe.derivedSections(inputs);
    for (const i of inputs) {
      total++;
      if (probe.isHeading(i)) continue;
      const v = probe.vendorLabel(i.k);
      const sec = secs && secs.has(probe.sectionOf(i.k)) ? probe.sectionOf(i.k) : '';
      const label = probe.trimSection(v ? v.label : probe.commonLabel(i.k) || probe.prettyKey(i.k), sec);
      // still raw: an underscore, a camelCase join, or one token so long it can only be
      // words run together ("secondadditionaltrade"). "automatically" is a word.
      if (!label || /undefined|NaN/.test(label) || /_/.test(label) || /[a-z][A-Z]/.test(label)
          || label.split(/\s+/).some(w => w.length >= 16)) raw.push(`${ea.ea}: ${i.k} -> "${label}"`);
    }
  }
  if (raw.length) { failed = true; console.log(`  FAIL  ${raw.length} of ${total} live inputs still read raw:`); raw.slice(0, 10).forEach(r => console.log('        ' + r)); }
  else console.log(`  PASS  all ${total} live inputs read as words`);
}

// -- TP/SL on open positions -----------------------------------------------
// Made-up tickets and prices. contract_size 100 gives money figures that no
// other text on the sheet can contain by accident.
if (probe) {
  const mk = (ticket, type, volume, open, extra = {}) => ({
    ticket, symbol: 'XAUUSD', type, volume, price_open: open, price_current: 4100.5,
    sl: 0, tp: 0, profit: 0, magic: 0, digits: 2, contract_size: 100, ...extra });
  const basket = [mk(9001, 'buy', 0.10, 2000), mk(9002, 'buy', 0.30, 1990),
                  mk(9003, 'buy', 0.10, 1960)];
  const tpOf = (rows, tpText, slText = '') => {
    probe.setPositions(rows);
    probe.sltpOpen(rows.map(p => p.ticket));
    document.getElementById('sltp-tp').value = tpText;
    document.getElementById('sltp-tp-rm').checked = false;
    document.getElementById('sltp-sl').value = slText;
    document.getElementById('sltp-sl-rm').checked = false;
    probe.sltpRender();
    return { bad: document.getElementById('sltp-bad').textContent,
             rows: document.getElementById('sltp-rows').innerHTML,
             go: document.getElementById('sltp-go').disabled };
  };
  try {
    const none = probe.sltpCell(0, 2), nul = probe.sltpCell(null, 2);
    // current price in every fixture is 4100.50
    const wrongBuy = tpOf([mk(9101, 'buy', 0.1, 4100)], '4090');
    const wrongSell = tpOf([mk(9102, 'sell', 0.1, 4100)], '4110');
    const rightBuy = tpOf([mk(9103, 'buy', 0.1, 4100)], '4110');
    const buyTpUnderEntry = tpOf([mk(9107, 'buy', 0.1, 4120)], '4110');
    const buySlOverEntry = tpOf([mk(9108, 'buy', 0.1, 4080)], '', '4090');
    const buySlOverNow = tpOf([mk(9109, 'buy', 0.1, 4080)], '', '4110');
    const sellTpOverEntry = tpOf([mk(9110, 'sell', 0.1, 4080)], '4090');
    const sellSlUnderNow = tpOf([mk(9111, 'sell', 0.1, 4120)], '', '4090');
    const mixed = tpOf([mk(9112, 'buy', 0.1, 4120), mk(9113, 'buy', 0.1, 4120, { price_current: 4200 })], '4150');
    const noCs = tpOf([mk(9104, 'buy', 0.1, 4100, { contract_size: null })], '4110');
    const withCs = tpOf([mk(9105, 'buy', 0.1, 4100, { contract_size: 250 })], '4110');
    probe.renderMt5({ ok: true, account: { login: 1, server: 's', currency: 'USD' },
      terminal: { connected: true, trade_allowed: true }, totals: { positions: 1, orders: 0 },
      positions: [mk(9106, 'buy', 0.1, 4100)], orders: [] });
    const table = document.getElementById('mt5positions').innerHTML || '';
    // one position that already has both: the sheet prefills them, and a field
    // left as prefilled is kept (null), not sent back
    const both = mk(9114, 'buy', 0.1, 4100, { tp: 4120, sl: 4080 });
    probe.setPositions([both]);
    probe.sltpOpen([9114]);
    const preTp = document.getElementById('sltp-tp').value, preSl = document.getElementById('sltp-sl').value;
    const untouched = probe.sltpRead([both]);
    document.getElementById('sltp-tp').value = '4130';
    probe.sltpRender();
    const tpOnly = probe.sltpRead([both]);
    const tpOnlyGo = document.getElementById('sltp-go').disabled;
    const be = probe.sltpBE(basket);
    // result dialog: ticket 9201 was already closed -> listed once as skipped, not failed
    let resultBody = '';
    probe.stubDialog(
      async () => ({ ok: true, results: [{ ticket: 9200, ok: true, tp: 4120, sl: 0 }],
                     failed: [{ ticket: 9201, error: 'position not found' }], already_gone: [9201] }),
      async o => { resultBody = o.body; return true; });
    await probe.setSltp([{ ticket: 9200, tp: 4120, sl: null }, { ticket: 9201, tp: 4120, sl: null }], {});
    const goneLines = resultBody.split('\n').filter(l => l.includes('#9201'));
    const checks = [
      [goneLines.length === 1 && goneLines[0].includes('closed, skipped') && resultBody.includes('1 ok, 0 failed'),
       'an already-closed ticket shows once as "closed, skipped" and is not counted as failed'],
      [none.includes('none') && nul.includes('none') && !none.includes('0.00'),
       'an SL/TP of 0 or null renders "none"'],
      [/<td class="n"><span class="sltpnone">none<\/span><\/td><td class="n"><span class="sltpnone">none<\/span><\/td>/.test(table),
       'the positions table shows "none" for SL and TP of 0, not 0.00'],
      [be === 1986, `break-even of the sample basket is lot-weighted 1986.00 (got ${be})`],
      [preTp === '4120.00' && preSl === '4080.00' && untouched.tp.v === null && untouched.sl.v === null,
       `a single position's TP/SL are prefilled and, left alone, kept (${preTp}/${preSl}`
       + ` -> ${untouched.tp.v}/${untouched.sl.v})`],
      [tpOnly.tp.v === 4130 && tpOnly.sl.v === null && !tpOnlyGo,
       `a TP-only change sends the TP and null for the SL (tp ${tpOnly.tp.v}, sl ${tpOnly.sl.v})`],
      [wrongBuy.bad.includes('TP must be above now 4100.50 for buy') && wrongBuy.go,
       'a buy TP below the current price is blocked and Review is disabled'],
      [wrongSell.bad.includes('TP must be below now 4100.50 for sell') && wrongSell.go,
       'a sell TP above the current price is blocked'],
      [rightBuy.bad === '' && !rightBuy.go, 'a buy TP above the current price is allowed'],
      [buyTpUnderEntry.bad === '' && !buyTpUnderEntry.go,
       'a buy TP under entry but above the current price is allowed (closes at a loss)'],
      [buySlOverEntry.bad === '' && !buySlOverEntry.go,
       'a buy SL over entry but under the current price is allowed (locks profit)'],
      [buySlOverNow.bad.includes('SL must be below now') && buySlOverNow.go,
       'a buy SL above the current price is blocked'],
      [sellTpOverEntry.bad === '' && !sellTpOverEntry.go, 'a sell TP under the current price is allowed'],
      [sellSlUnderNow.bad.includes('SL must be above now') && sellSlUnderNow.go,
       'a sell SL below the current price is blocked'],
      [mixed.bad.includes('#9113') && !mixed.bad.includes('#9112') && mixed.go,
       'in a basket the failing ticket is named, the passing one is not'],
      [probe.sltpMoney(mk(1, 'buy', 0.1, 4100), 4110) === 100,
       'money at target = move x lots x contract size (buy +10 x 0.1 x 100 = 100)'],
      [probe.sltpMoney(mk(1, 'sell', 0.1, 4100), 4090) === 100, 'a sell profits when price falls'],
      [probe.sltpMoney(mk(1, 'buy', 0.1, 4100, { contract_size: null }), 4110) === null,
       'money is unknown, not zero, when the contract size is null'],
      [withCs.rows.includes('(250.00)') && !noCs.rows.includes('250') && noCs.rows.includes('+10.00'),
       'money is hidden when contract_size is null, the price distance still shows'],
    ];
    for (const [ok, what] of checks) {
      if (ok) console.log(`  PASS  ${what}`);
      else { failed = true; console.log(`  FAIL  ${what}`); }
    }
  } catch (err) {
    failed = true;
    console.log(`  FAIL  the TP/SL sheet threw while rendering: ${err.stack || err.message}`);
  }
}

// -- Report tab: one EA since a start day -----------------------------------
// Every number here is made up. Position 9501 closes in two parts (it must
// count once), 9504 closes five minutes after server midnight (next day), 9500
// is before the start, 9505 belongs to another EA on the same symbol.
if (probe) {
  const close = (ticket, magic, comment, at, profit) => ({
    ticket, symbol: 'XAUUSD', type: 'buy', volume: 0.01, price_open: 2000, price_close: 2001,
    magic, comment, closed_at: at + ':00+00:00', profit });
  const hist = {
    ok: true, days: 30,
    by_ea: [{ magic: 11111, ea: 'Test EA Alpha', trades: 3 }, { magic: 11112, ea: 'Test EA Alpha', trades: 1 },
            { magic: 22222, ea: 'Test EA Beta', trades: 1 }],
    closed: [
      close(9504, 11112, 'TA S2 swing', '2026-10-03T00:05', -4),
      close(9503, 11111, 'TA S1 quick', '2026-10-02T23:59', 20),
      close(9505, 22222, 'TB', '2026-10-02T13:00', 100),
      close(9502, 11112, 'TA S2 swing', '2026-10-02T12:00', -8),
      close(9501, 11111, 'TA S1 quick', '2026-10-02T11:00', 5),
      close(9501, 11111, 'TA S1 quick', '2026-10-02T10:00', 10),
      close(9500, 11111, 'TA S1 quick', '2026-09-30T09:00', -50),
    ],
  };
  const acct = { balance: 1000, currency: 'USD' };
  const el = id => document.getElementById(id);
  const show = (ea, from) => {
    probe.setReport(hist, acct, null);
    el('rpt-ea').value = ea;
    probe.rptFillPicker();
    el('rpt-ea').value = ea;
    el('rpt-from').value = from;
    probe.renderReport();
    return { tiles: el('rpttiles').innerHTML, tilesHidden: el('rpttiles').hidden, note: el('rptnote').innerHTML,
             empty: el('rptempty').innerHTML, emptyHidden: el('rptempty').hidden,
             comments: el('rptcomments').innerHTML, bt: el('rptbt').innerHTML, chart: el('rptchart').innerHTML };
  };
  const tile = (html, k, v) => html.includes(`<dt>${k}</dt><dd class="${v[0]}">${v[1]}</dd>`);
  try {
    localStorage.removeItem('radar.report.start.Test EA Alpha');
    localStorage.setItem('radar.report.bt.Test EA Alpha', JSON.stringify({ pf: 3.5 }));
    localStorage.setItem('radar.report.mode', JSON.stringify('demo'));
    const a = show('Test EA Alpha', '2026-10-01');
    localStorage.setItem('radar.report.mode', JSON.stringify('challenge'));
    const ch = show('Test EA Alpha', '2026-10-01');
    localStorage.setItem('radar.report.mode', JSON.stringify('demo'));
    localStorage.setItem('radar.report.start.Test EA Alpha', JSON.stringify(1015));
    const typed = show('Test EA Alpha', '2026-10-01');
    localStorage.removeItem('radar.report.start.Test EA Alpha');
    const b = show('Test EA Beta', '2026-10-01');
    const none = show('Test EA Alpha', '2026-10-20');
    const prompt = show('Test EA Alpha', '');
    const st = probe.reportStats(hist.closed, ['11111', '11112'], '2026-10-01', 877);
    const pcSum = st.perComment.reduce((s, r) => s + r.net, 0);
    const all = [a, ch, typed, b, none, prompt].map(r => Object.values(r).join(' ')).join(' ');
    const checks = [
      [tile(a.tiles, 'Trades', ['', '4']), 'a position closed in two parts counts as one trade (4 trades)'],
      [tile(a.tiles, 'Profit factor', ['', '2.92']), 'profit factor is gross profit / gross loss per position (2.92)'],
      [tile(a.tiles, 'Net profit', ['pos', '+$23.00']), 'net sums only this EA, only since the start (+$23.00)'],
      [tile(a.tiles, 'Win %', ['', '50.0%']), 'win % is winning positions / positions (50.0%)'],
      [tile(a.tiles, 'Worst day 3 Oct', ['neg', '-$4.00']),
       'a close five minutes after server midnight lands on the next server day'],
      [tile(a.tiles, 'Max DD $', ['', '$8.00']) && tile(a.tiles, 'Max DD %', ['', '0.9%']),
       'closed drawdown is peak-to-trough on the closes, % of the peak over the derived start'],
      [a.note.includes('$877.00, derived'), 'the start balance is derived (balance now minus all closes since) and says so'],
      [typed.note.includes('$1,015.00, typed') && tile(typed.tiles, 'Max DD %', ['', '0.8%']),
       'a typed start balance replaces the derived one, says "typed" and moves DD %'],
      [tile(a.tiles, 'DD incl. open', ['', '<span class="rpt__na">n/a</span>']),
       'the open-trade drawdown reads n/a when the agent has not reported it'],
      [tile(b.tiles, 'Trades', ['', '1']) && tile(b.tiles, 'Profit factor', ['', '<span class="rpt__na">n/a</span>'])
        && tile(b.tiles, 'Avg loss', ['', '<span class="rpt__na">n/a</span>']),
       'another EA on the same symbol is kept apart; no losing trade gives PF and avg loss n/a, not 0'],
      [!a.comments.includes('>TB<') && a.comments.includes('TA S1 quick') && a.comments.includes('+$35.00')
        && a.comments.includes('-$12.00') && Math.abs(pcSum - st.net) < 0.005,
       'per-comment rows use the raw comment and sum back to the EA total'],
      [a.bt.includes('value="3.5"') && a.bt.includes('-0.58') && (a.bt.match(/placeholder="not entered" value=""/g) || []).length === 5,
       'the typed backtest PF sits beside the live one with its difference; empty fields read "not entered"'],
      [!a.tiles.includes('Daily loss used') && tile(ch.tiles, 'Daily loss used', ['', '0.5% of 5%'])
        && tile(ch.tiles, 'Max loss used', ['', '0.0% of 10%']) && ch.chart.includes('rpt__limit'),
       'Challenge adds the FTMO daily / max loss tiles and the -10% line; Demo does not'],
      [a.chart.includes('<path d="M0 ') && a.chart.includes('rpt__ddmark'), 'the balance curve and its DD marker are drawn'],
      [!a.tilesHidden && a.emptyHidden, 'with trades the tiles show and the empty card hides'],
      [none.tilesHidden && none.empty.includes('No trades yet') && none.empty.includes('since 20 Oct 2026'),
       'a start date with no trades says so, with no tiles'],
      [prompt.tilesHidden && prompt.empty.includes('Pick an EA and a start date'), 'no start date asks for one'],
      [!/NaN|undefined|Infinity/.test(all), 'no NaN, undefined or Infinity anywhere in the report'],
    ];
    for (const [ok, what] of checks) {
      if (ok) console.log(`  PASS  ${what}`);
      else { failed = true; console.log(`  FAIL  ${what}`); }
    }
  } catch (err) {
    failed = true;
    console.log(`  FAIL  the Report tab threw while rendering: ${err.stack || err.message}`);
  }
}

// -- only the MT5 control panel is left ------------------------------------
// The EA catalog and MQL5 Market tabs were shut down on 2026-10-23. An old link
// (?tab=market, ?tab=eas) or a tab remembered from before must land on the
// account, not on a view that no longer exists. Each case runs the page again
// from scratch with that URL and that remembered tab.
{
  const tabIds = [...ids].filter(id => id.startsWith('tab-')).sort();
  const landOn = (search, stored) => {
    nodes.clear();
    localStorage.store.clear();
    if (stored) localStorage.setItem('fxea_tab', stored);
    location.search = search;
    try {
      runPage();
    } catch (err) {
      return { tab: `threw: ${err.message}`, mt5: false, myea: false, report: false };
    }
    return { tab: globalThis.__probe.tab(), mt5: !nodes.get('view-mt5').hidden,
             myea: !nodes.get('view-myea').hidden, report: !nodes.get('view-report').hidden };
  };
  const fresh = landOn('', null);
  const market = landOn('?tab=market', 'myea');
  const eas = landOn('?tab=eas', 'myea');
  const remembered = landOn('', 'market');
  const myea = landOn('?tab=myea', null);
  const report = landOn('?tab=report', null);
  const reportKept = landOn('', 'report');
  const shown = r => `${r.tab}, mt5 ${r.mt5 ? 'shown' : 'hidden'}, myea ${r.myea ? 'shown' : 'hidden'}`
    + `, report ${r.report ? 'shown' : 'hidden'}`;
  const onMt5 = r => r.tab === 'mt5' && r.mt5 && !r.myea && !r.report;
  const onReport = r => r.tab === 'report' && r.report && !r.mt5 && !r.myea;
  const checks = [
    [JSON.stringify(tabIds) === JSON.stringify(['tab-mt5', 'tab-myea', 'tab-report']),
     `the only tabs are My MT5, My EA and Report (got ${tabIds.join(', ')})`],
    [!ids.has('view-eas') && !ids.has('view-market'), 'the catalog and Market views are gone'],
    [!/Nothing crawled yet|>EA catalog<|>MQL5 Market</.test(html),
     'no catalog or Market tab text is left in the page'],
    [onMt5(fresh), `a first visit lands on My MT5 (${shown(fresh)})`],
    [onMt5(market), `?tab=market lands on My MT5, not the remembered tab (${shown(market)})`],
    [onMt5(eas), `?tab=eas lands on My MT5 (${shown(eas)})`],
    [onMt5(remembered), `a remembered Market tab lands on My MT5 (${shown(remembered)})`],
    [myea.tab === 'myea' && myea.myea && !myea.mt5 && !myea.report, `?tab=myea still opens My EA (${shown(myea)})`],
    [onReport(report), `?tab=report opens the Report tab (${shown(report)})`],
    [onReport(reportKept), `a remembered Report tab opens again (${shown(reportKept)})`],
  ];
  for (const [ok, what] of checks) {
    if (ok) console.log(`  PASS  ${what}`);
    else { failed = true; console.log(`  FAIL  ${what}`); }
  }
}

process.exit(failed ? 1 : 0);

// Run the office for simulated minutes under a stub DOM and report what OPS did.
import fs from 'fs';
const html = fs.readFileSync(new URL('./radar-crew.html', import.meta.url), 'utf8');
let src = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const end = src.lastIndexOf('})();');
src = src.slice(0, end) + ';globalThis.__o = { crew, ops, setTasks: x => { TASKS = x; } };\n' + src.slice(end);

function run(hash) {
  let T = 0, loopFn = null;
  const ctx = new Proxy({}, { get: (o, k) => (k in o ? o[k] : () => ctx), set: (o, k, v) => { o[k] = v; return true; } });
  const node = () => ({ style: { setProperty() {} }, dataset: {}, classList: { add() {}, remove() {}, toggle() {} }, hidden: false,
    textContent: '', innerHTML: '', append() {}, setAttribute() {}, querySelector: () => node(), querySelectorAll: () => [],
    getContext: () => ctx, addEventListener() {}, getBoundingClientRect: () => ({ left: 0, top: 0, width: 320, height: 180 }),
    showModal() {}, close() {}, width: 320, height: 180 });
  const g = globalThis;
  g.window = g; g.performance = { now: () => T }; g.location = { hash };
  g.matchMedia = () => ({ matches: false }); g.requestAnimationFrame = f => { loopFn = f; };
  g.setTimeout = () => 0; g.document = { getElementById: () => node(), createElement: () => node() };
  g.claude = undefined;
  new Function(src)();
  const o = g.__o;
  // one agent has had a task in progress all along
  o.setTasks([{ id: 't1', agent: 'be', status: 'doing', title: 'Find out why execv does nothing' }]);
  const said = new Set();
  for (let i = 0; i < 20 * 60 * 10; i++) {           // twenty simulated minutes at 10 fps
    T += 100; loopFn && loopFn(T);
    if (o.ops.bubble) said.add(o.ops.bubble);
  }
  const be = o.crew.find(c => c.id === 'be');
  return { said: [...said], gone: o.crew.filter(c => c.gone).length, opsGone: o.ops.gone,
           beBreaks: be.breakUntil ? 'had a break' : 'never rested' };
}

console.log('day  :', JSON.stringify(run('#day')));
console.log('night:', JSON.stringify(run('#night')));

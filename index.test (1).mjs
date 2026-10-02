// Run: node --test index.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
const logicSrc = html.split('// <logic>')[1]?.split('// </logic>')[0];
assert.ok(logicSrc, 'logic block markers missing');

// Pull pure logic (and the datasets declared before it) out of the page
const dataSrc = html.match(/const initialProductsRaw[\s\S]*?\n        \];/)[0];
const L = new Function(`${dataSrc}\n${logicSrc}
  return { initialProductsRaw, initialOrdersData, computeInventoryPrediction, computeDeliveryPrediction, sortByRisk, sanitizeHistory, isValidQty };`)();

const base = { id: 'T', currentStock: 10, history: [5, 5, 5, 5, 5, 5] };

test('inventory: flat demand is STABLE with HIGH risk when stock < demand', () => {
  const r = L.computeInventoryPrediction(base);
  assert.equal(r.trend, 'STABLE');
  assert.equal(r.predictedDemand, 15);
  assert.equal(r.risk, 'HIGH');
  assert.equal(r.recommendedRestock, 7); // 15 + ceil(1.5)=2 -> 17 - 10
});
test('inventory: healthy stock is LOW and needs no restock', () => {
  const r = L.computeInventoryPrediction({ ...base, currentStock: 100 });
  assert.equal(r.risk, 'LOW');
  assert.equal(r.recommendedRestock, 0);
});
test('inventory: rising trend detected', () => {
  assert.equal(L.computeInventoryPrediction({ ...base, history: [2, 2, 2, 2, 9, 9, 9] }).trend, 'RISING');
});
test('inventory: all-zero history does not produce NaN', () => {
  const r = L.computeInventoryPrediction({ ...base, history: [0, 0, 0, 0] });
  assert.equal(r.predictedDemand, 0);
  assert.ok(Number.isFinite(Number(r.trendFactor)));
});
test('inventory: bad history is rejected or sanitized', () => {
  assert.throws(() => L.computeInventoryPrediction({ ...base, history: [] }), RangeError);
  assert.throws(() => L.computeInventoryPrediction({ ...base, history: null }), RangeError);
  assert.deepEqual(L.sanitizeHistory([1, 'x', -3, NaN, '4']), [1, 4]);
});
test('every seed product yields finite numbers', () => {
  for (const p of L.initialProductsRaw) {
    const r = L.computeInventoryPrediction(p);
    for (const k of ['predictedDemand', 'totalRequired', 'recommendedRestock']) assert.ok(Number.isFinite(r[k]), `${p.id}.${k}`);
  }
});
test('delivery: prioritizing never makes ETA worse', () => {
  for (const o of L.initialOrdersData) {
    const a = L.computeDeliveryPrediction(o, false), b = L.computeDeliveryPrediction(o, true);
    assert.ok(b.estimatedDeliveryTime <= a.estimatedDeliveryTime, o.id);
  }
});
test('delivery: risk thresholds', () => {
  const far = L.computeDeliveryPrediction({ distance: 8, items: 12, prepTime: 20, storeLoad: 'HIGH', availablePartners: 1 });
  const near = L.computeDeliveryPrediction({ distance: 1, items: 1, prepTime: 5, storeLoad: 'LOW', availablePartners: 5 });
  assert.equal(far.risk, 'HIGH');
  assert.equal(near.risk, 'LOW');
});
test('helpers: sortByRisk is stable-safe and non-mutating; qty validation', () => {
  const input = [{ risk: 'LOW' }, { risk: 'HIGH' }, { risk: 'MEDIUM' }, { risk: 'BOGUS' }];
  assert.deepEqual(L.sortByRisk(input).map(x => x.risk), ['HIGH', 'MEDIUM', 'LOW', 'BOGUS']);
  assert.equal(input[0].risk, 'LOW');
  for (const bad of [0, -1, 1.5, NaN, '5', 1e9, null]) assert.equal(L.isValidQty(bad), false);
  assert.equal(L.isValidQty(25), true);
});

// ---- Static security checks (poor-man's pen test) ----
test('security: no dangerous sinks', () => {
  for (const re of [/dangerouslySetInnerHTML/, /\.innerHTML\s*=/, /document\.write/, /\beval\s*\(/, /new Function/, /localStorage|sessionStorage/, /location\.(hash|search)/])
    assert.ok(!re.test(html), `found ${re}`);
});
test('security: CSP present, locked down', () => {
  const csp = html.match(/Content-Security-Policy"\s+content="([^"]+)"/)?.[1];
  assert.ok(csp, 'CSP missing');
  for (const d of ["default-src 'none'", "connect-src 'none'", "object-src 'none'", "base-uri 'none'", "form-action 'none'"]) assert.ok(csp.includes(d), d);
});
test('security: scripts only from allowlisted hosts with pinned versions', () => {
  const srcs = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map(m => m[1]);
  assert.ok(srcs.length >= 5);
  for (const s of srcs) {
    assert.match(s, /^https:\/\/(cdn\.jsdelivr\.net|cdn\.tailwindcss\.com)(\/|$)/, s);
    if (s.includes('jsdelivr')) assert.match(s, /@\d+\.\d+\.\d+/, `unpinned: ${s}`);
  }
  assert.ok(!html.includes('unpkg.com'));
});
test('security: no inline event-handler attributes or javascript: URLs', () => {
  assert.ok(!/\son(click|error|load)\s*=\s*"/i.test(html));
  assert.ok(!/javascript:/i.test(html));
});
test('a11y: skip link, main landmark, labelled sliders', () => {
  assert.match(html, /class="skip-link"/);
  assert.match(html, /<main id="main"/);
  assert.match(html, /htmlFor="stockout-range"/);
  assert.match(html, /prefers-reduced-motion/);
});

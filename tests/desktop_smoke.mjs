// Renders the Desktop chip with real React against a stub SDK. Not in pytest: needs node, react and react-dom.
//   mkdir smoke && cd smoke && npm i react react-dom && node <repo>/tests/desktop_smoke.mjs <repo>
import { readFileSync, writeFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { resolve } from 'node:path'
import { createRequire } from 'node:module'

const repo = process.argv[2]
writeFileSync('sdk_stub.mjs', 'export const host = { notify: m => globalThis.toasts.push(m) }\n')
writeFileSync('plugin_under_test.mjs', readFileSync(`${repo}/desktop/plugin.js`, 'utf8').replace('@hermes/plugin-sdk', './sdk_stub.mjs'))
globalThis.toasts = []

const { default: plugin } = await import(pathToFileURL(resolve('plugin_under_test.mjs')))
const require = createRequire(resolve('package.json'))
const { renderToString } = require('react-dom/server')
const { jsx } = require('react/jsx-runtime')

const items = []
plugin.register({ register: i => items.push(i), rest: async () => ({}), pet: { visible: { get: () => false } } })
if (plugin.id !== 'obol' || items.length !== 1 || items[0].area !== 'statusBar.right') throw new Error('bad registration')
console.log('registered', items[0].id)
console.log('server render (effects do not run):', JSON.stringify(renderToString(items[0].render())))

// Details and Chart aren't exported: exercise them through a copy that exports them.
writeFileSync('plugin_parts.mjs', readFileSync('plugin_under_test.mjs', 'utf8') + '\nexport { Details, Chip }\n')
const parts = await import(pathToFileURL(resolve('plugin_parts.mjs')))
const data = { state: 'over', at: Date.now() / 1000, rows: [{ title: 'Claude week', state: 'over', used: 40, expected: 130, start: Date.now() / 1000 - 3600, end: Date.now() / 1000 + 3600, out_at: Date.now() / 1000 + 1800 }], chip: '$6.20/$20 · out Fri 14:00', forecast: 'Cascade', lines: ['$6.20 of $20 · ⚠ on track to run out ~Fri 14:00'],
  chart: { title: 'This week', unit: '$', start: 0, end: 100, limit: 20, out_at: 85, actual: [[0, 0], [40, 6.2]],
           expected: [[40, 6.2], [90, 22]], low: [[40, 6.2], [90, 18]], high: [[40, 6.2], [90, 26]] } }
const html = renderToString(jsx(parts.Details, { data }))
for (const want of ['<svg', '<polygon', 'out ', 'resets ', 'Shaded', 'Likely out around', 'before it resets', 'Resets ', 'Hatched', 'Updated', '40% used', 'stroke-dasharray', 'An estimate from Cascade']) if (!html.includes(want)) throw new Error('missing ' + want)
const demo = renderToString(jsx(parts.Details, { data: { state: 'demo', lines: ['x'], rows: [{ title: 'Week', state: 'over', used: 10, expected: 150, start: 1000, end: 2000, now: 1100, out_at: 1800 }] } }))
if (!demo.includes('10% of the time')) throw new Error('demo bar ignored its clock')
const empty = renderToString(jsx(parts.Details, { data: { state: 'none', chip: 'Obol', lines: ['x'] } }))
if (!empty.includes('Type /obol') || !empty.includes('>x<')) throw new Error('bad empty panel')
const plan = renderToString(jsx(parts.Details, { data: { state: 'out', chip: 'x', lines: ['x'], chart: { title: 'w', unit: '%', start: 0, end: 100,
  limit: 100, actual: [[0, 0], [50, 100]], expected: [], out_at: null } } }))
if (!plan.includes('100%') || plan.includes('<polygon')) throw new Error('bad plan chart')
if (!html.includes('Plan percentages')) throw new Error('plan rows must say the percent is the vendor figure')
const budget = renderToString(jsx(parts.Details, { data: { ...data, rows: [{ ...data.rows[0], amount: '$6/$20' }] } }))
if (budget.includes('Plan percentages')) throw new Error('budget rows are not vendor figures')
if (!html.includes('role="dialog"') || !html.includes('aria-label="Obol forecast"') || !html.includes('tabindex="-1"')) throw new Error('panel must be a focusable dialog')
if (!readFileSync('plugin_parts.mjs', 'utf8').includes("e.key !== 'Escape'")) throw new Error('no Escape handling')
const noon = Date.now() / 1000
const vague = renderToString(jsx(parts.Details, { data: { state: 'over', lines: ['x'], rows: [{ title: 'W', state: 'over', used: 70, expected: 120, start: noon - 3600, end: noon + 3600 }] } }))
if (vague.includes('Should last') || !vague.includes('Likely to run out')) throw new Error('over with no time must not say it will last')
const fresh = renderToString(jsx(parts.Details, { data: { state: 'ok', lines: ['x'], rows: [{ title: 'W', state: 'ok', used: 3, expected: null, start: noon - 60, end: noon + 3600 }] } }))
if (!fresh.includes('Too early to say')) throw new Error('no verdict copy on a fresh window')
const paced = renderToString(jsx(parts.Details, { data: { state: 'ok', lines: ['x'], rows: [{ title: 'W', state: 'ok', used: 20, expected: 40, how: 'pace', age: '2h', start: noon - 3600, end: noon + 3600 }] } }))
if (!paced.includes('at your pace so far') || !paced.includes('20% used (2h ago)')) throw new Error('basis or age missing')
if (!html.includes('not how much is used')) throw new Error('bars must say they are time')
if (!html.includes('$6.20 so far') || !html.includes('heading for $22.00')) throw new Error('chart label has no values')
if (html.includes('Dollar figures')) throw new Error('plan rows have no dollars')
if (!budget.includes('Dollar figures')) throw new Error('dollar rows must say what the dollars are')
const flat = renderToString(jsx(parts.Details, { data: { ...data, rows: [], chart: { ...data.chart, expected: [], low: undefined, high: undefined } } }))
if (flat.includes('Dashed')) throw new Error('no forecast, no dashed legend')
const source = readFileSync('plugin_parts.mjs', 'utf8')
if (!source.includes("last !== 'demo'") || !source.includes("last === 'over' && next.state === 'out'")) throw new Error('toast rules')
if (!source.includes("'aria-label': `Obol: ${data.lines?.[0]")) throw new Error('chip needs a spoken name')
console.log('ok')

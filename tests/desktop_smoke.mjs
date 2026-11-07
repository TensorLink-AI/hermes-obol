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
const data = { state: 'over', chip: '$6.20/$20 · out Fri 14:00', forecast: 'Cascade', lines: ['$6.20 of $20 · ⚠ on track to run out ~Fri 14:00'],
  chart: { title: 'This week', unit: '$', start: 0, end: 100, limit: 20, out_at: 85, actual: [[0, 0], [40, 6.2]],
           expected: [[40, 6.2], [90, 22]], low: [[40, 6.2], [90, 18]], high: [[40, 6.2], [90, 26]] } }
const html = renderToString(jsx(parts.Details, { data }))
for (const want of ['<svg', '<polygon', 'out ', 'resets ', 'Shaded', 'on track to run out', 'stroke-dasharray', 'Type /obol']) if (!html.includes(want)) throw new Error('missing ' + want)
renderToString(jsx(parts.Details, { data: { state: 'none', chip: 'Obol', lines: ['x'] } }))
const plan = renderToString(jsx(parts.Details, { data: { state: 'out', chip: 'x', lines: ['x'], chart: { title: 'w', unit: '%', start: 0, end: 100,
  limit: 100, actual: [[0, 0], [50, 100]], expected: [], out_at: null } } }))
if (!plan.includes('100%') || plan.includes('<polygon')) throw new Error('bad plan chart')
console.log('ok')

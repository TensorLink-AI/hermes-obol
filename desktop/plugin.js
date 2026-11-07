// Obol's status-bar chip: when you'll run out, at a glance. Click for the forecast.
import { host } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const POLL_MS = 15_000
const HOT = { over: 'var(--ui-accent)', out: 'var(--ui-accent)', demo: 'var(--ui-accent)' }
const WARN = ['over', 'out']
const UNAVAILABLE = {
  state: 'none',
  chip: 'Obol',
  lines: ["Can't reach Obol's backend. Run: hermes plugins enable obol, then restart the app."]
}

function when(t, span) {
  const d = new Date(t * 1000)
  const time = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  return span > 2 * 86400 ? `${d.toLocaleDateString(undefined, { weekday: 'short' })} ${time}` : time
}

function Chart({ chart }) {
  const W = 280
  const PLOT = 96
  const H = PLOT + 14
  const span = chart.end - chart.start
  const low = chart.low || []
  const high = chart.high || []
  const top = Math.max(chart.limit || 0, ...[...chart.actual, ...chart.expected, ...high].map(p => p[1]), 0.01) * 1.1
  const x = t => ((t - chart.start) / span) * W
  const y = v => PLOT - (v / top) * PLOT
  const coords = points => points.map(p => `${x(p[0]).toFixed(1)} ${y(p[1]).toFixed(1)}`)
  const path = points => points.length ? `M${coords(points).join(' L')}` : ''
  const tertiary = 'var(--ui-text-tertiary)'
  const label = (text, tx, ty, anchor, fill = tertiary) =>
    jsx('text', { x: tx, y: ty, fontSize: 9, textAnchor: anchor, fill, children: text })
  const limit = chart.limit ? (chart.unit === '%' ? `${chart.limit}%` : `$${chart.limit}`) : ''
  const out = chart.out_at ? x(chart.out_at) : null

  return jsxs('svg', {
    viewBox: `0 0 ${W} ${H}`,
    width: W,
    height: H,
    role: 'img',
    'aria-label': `${chart.title || 'Usage'}: so far and where it is heading, against the limit`,
    children: [
      high.length
        ? jsx('polygon', { points: [...coords(high), ...coords(low).reverse()].join(' '), fill: 'var(--ui-accent)', fillOpacity: 0.16 })
        : null,
      chart.limit
        ? jsx('line', { x1: 0, x2: W, y1: y(chart.limit), y2: y(chart.limit), stroke: tertiary, strokeDasharray: '3 3' })
        : null,
      limit ? label(limit, 0, y(chart.limit) - 3, 'start') : null,
      jsx('path', { d: path(chart.expected), fill: 'none', stroke: 'var(--ui-accent)', strokeDasharray: '4 3' }),
      jsx('path', { d: path(chart.actual), fill: 'none', stroke: 'var(--ui-text-secondary)', strokeWidth: 1.5 }),
      out !== null ? jsx('circle', { cx: out, cy: y(chart.limit), r: 3, fill: 'var(--ui-accent)' }) : null,
      out !== null
        ? label(`out ${when(chart.out_at, span)}`, out, y(chart.limit) - 6, out > W / 2 ? 'end' : 'start', 'var(--ui-accent)')
        : null,
      label(when(chart.start, span), 0, H - 2, 'start'),
      label(`resets ${when(chart.end, span)}`, W, H - 2, 'end')
    ]
  })
}

function Details({ data }) {
  return jsxs('div', {
    style: {
      position: 'fixed',
      right: 12,
      bottom: 32,
      zIndex: 50,
      width: 304,
      padding: 12,
      borderRadius: 8,
      background: 'var(--ui-bg-elevated, var(--background))',
      border: '1px solid var(--ui-stroke-secondary)'
    },
    className: 'flex flex-col gap-2 text-xs',
    children: [
      jsx('div', { className: 'font-medium', children: 'Obol' }),
      data.chart ? jsx('div', { className: 'text-(--ui-text-secondary)', children: data.chart.title }) : null,
      data.chart ? jsx(Chart, { chart: data.chart }) : null,
      data.chart
        ? jsx('div', {
            className: 'text-(--ui-text-tertiary)',
            children: `Solid: so far. Dashed: forecast.${data.chart.high ? ' Shaded: how much your past periods varied.' : ''}`
          })
        : null,
      ...(data.lines || []).map((line, i) => jsx('div', { className: 'text-(--ui-text-secondary)', children: line }, i)),
      data.forecast ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: `forecast: ${data.forecast}` }) : null,
      jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'Type /obol for more' })
    ]
  })
}

function Chip({ ctx }) {
  const [data, setData] = useState(null)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    let alive = true
    let last = null
    const load = () =>
      ctx
        .rest('/status')
        .then(next => {
          if (!alive) return
          setData(next)
          if (last && !WARN.includes(last) && WARN.includes(next.state)) {
            host.notify({ kind: 'warning', title: 'Obol', message: next.lines?.[0] || next.chip })
            if (ctx.pet?.visible?.get?.()) ctx.pet.say(next.chip, { id: 'obol', tone: 'wait', ttlMs: 8000 })
          }
          last = next.state
        })
        .catch(() => {
          if (alive) setData(UNAVAILABLE)   // never leave an old number up as if it were current
        })
    load()
    const timer = setInterval(load, POLL_MS)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [ctx])

  if (!data) return null

  return jsxs('span', {
    children: [
      jsx('button', {
        type: 'button',
        title: data.lines?.[0],
        className: 'px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
        style: HOT[data.state] ? { color: HOT[data.state] } : undefined,
        onClick: () => setOpen(!open),
        children: data.chip
      }),
      open ? jsx(Details, { data }) : null
    ]
  })
}

export default {
  id: 'obol',
  name: 'Obol',
  register(ctx) {
    ctx.register({
      id: 'chip',
      area: 'statusBar.right',
      order: 120,
      render: () => jsx(Chip, { ctx })
    })
  }
}

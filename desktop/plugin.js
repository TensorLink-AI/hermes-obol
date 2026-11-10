// Obol's status-bar chip: when you'll run out, at a glance. Click for the forecast.
import { host } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const POLL_MS = 15_000
const STALE_S = 15 * 60
const HOT = { tight: 'var(--ui-yellow)', over: 'var(--ui-accent)', out: 'var(--ui-accent)', demo: 'var(--ui-accent)' }
const WARN = ['over', 'out']
const UNAVAILABLE = {
  state: 'none',
  chip: 'Obol',
  lines: ["Can't reach Obol's backend. If Hermes is running, try: hermes plugins enable obol, then restart the app."]
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
  const value = v => (chart.unit === '%' ? `${Math.round(v)}%` : `$${v.toFixed(2)}`)
  const heading = chart.expected.length > 1 ? `, heading for ${value(chart.expected[chart.expected.length - 1][1])}` : ''
  const sofar = chart.actual.length ? value(chart.actual[chart.actual.length - 1][1]) : ''
  const today = chart.actual.length ? chart.actual[chart.actual.length - 1][0] : undefined

  return jsxs('svg', {
    viewBox: `0 0 ${W} ${H}`,
    style: { display: 'block', width: '100%', height: 'auto' },
    role: 'img',
    'aria-label': `${chart.title || 'Usage'}: ${sofar} so far${heading}${limit ? `, against a limit of ${limit}` : ''}`,
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
        ? label(`out ~${around(chart.out_at, span, today)}`, out, y(chart.limit) - 6, out > W / 2 ? 'end' : 'start', 'var(--ui-accent)')
        : null,
      label(when(chart.start, span), 0, H - 2, 'start'),
      label(`resets ${when(chart.end, span)}`, W, H - 2, 'end')
    ]
  })
}

function length(seconds) {
  const left = Math.max(0, seconds)
  const days = Math.floor(left / 86400)
  const hours = Math.floor((left % 86400) / 3600)
  const minutes = Math.floor((left % 3600) / 60)
  return days ? `${days}d ${hours}h` : hours ? `${hours}h ${minutes}m` : `${minutes}m`
}

// Forecasts are rough: never quote one to the minute. Same rule as the chip's: rounded down, so a warning is early.
function around(t, span, now = Date.now() / 1000) {
  const d = new Date(t * 1000)
  d.setMinutes(t - now < 20 * 3600 ? d.getMinutes() - (d.getMinutes() % 15) : 0, 0, 0)
  const down = d.getTime() / 1000
  return when(down >= now ? down : t, span)
}

const share = (t, row) => Math.max(0, Math.min(100, ((t - row.start) / (row.end - row.start)) * 100))
const nowS = () => Date.now() / 1000

// A runway, not a usage bar: the track is the time until the reset. Filled has passed; hatched is time with nothing left to spend.
function Runway({ row, height = 8, decorative = false }) {
  const passed = share(row.now ?? nowS(), row)   // the demo's week runs on its own clock
  const dryFrom = row.state === 'out' ? passed : row.out_at ? Math.max(passed, share(row.out_at, row)) : null
  const fill = HOT[row.state] || 'var(--ui-text-secondary)'
  const bar = (left, width, extra) => ({ position: 'absolute', top: 0, bottom: 0, left: `${left}%`, width: `${width}%`, ...extra })
  const ending = row.state === 'out' ? ', and it is used up' : row.out_at ? `, likely out around ${around(row.out_at, row.end - row.start, row.now ?? nowS())}` : ''
  return jsxs('div', {
    ...(decorative ? { 'aria-hidden': true } : { role: 'img', 'aria-label': `${Math.round(passed)}% of the time until the reset has passed${ending}` }),
    style: { position: 'relative', height, borderRadius: 2, background: 'var(--ui-stroke-secondary)', overflow: 'hidden' },
    children: [
      jsx('div', { style: bar(0, passed, { background: fill }) }),
      dryFrom === null
        ? null
        : jsx('div', {
            style: bar(dryFrom, 100 - dryFrom, {
              background: 'repeating-linear-gradient(135deg, var(--ui-accent) 0 2px, transparent 2px 4px)'
            })
          })
    ]
  })
}

// The answer the panel exists to give, for the limit closest to running out.
const basis = row => (row.how === 'pace' ? ', at your pace so far' : '')

function verdict(row) {
  const now = row.now ?? nowS()
  const left = row.end - now
  if (row.state === 'out') return { head: 'Used up', sub: `${row.title} is back in ${length(left)}` }
  if (row.out_at) {
    return {
      head: `Likely out around ${around(row.out_at, row.end - row.start, now)}`,
      sub: `${row.title}: about ${length(row.end - row.out_at)} before it resets${basis(row)}`
    }
  }
  if (row.expected == null) return { head: 'Too early to say', sub: `${row.title}: ${Math.round(row.used)}% used. A forecast needs more of this window to pass.` }
  return {
    head: row.state === 'over' ? 'Likely to run out before the reset' : row.state === 'tight' ? 'Close to the limit' : 'Should last until the reset',
    sub: `${row.title}: finishing near ${Math.round(row.expected)}%${basis(row)}`
  }
}

function note(row) {
  const span = row.end - row.start
  if (row.state === 'out') return 'Used up'
  if (row.out_at) return `Likely out ~${around(row.out_at, span, row.now ?? nowS())}`
  return row.expected == null ? '' : `Near ${Math.round(row.expected)}% at reset`
}

function Row({ row, headline }) {
  const hot = HOT[row.state]
  const accent = hot ? { color: hot } : undefined
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('div', {
        className: 'flex items-baseline justify-between gap-2',
        children: [
          jsx('span', { children: row.title }),
          jsx('span', { style: { fontVariantNumeric: 'tabular-nums' }, children: row.amount || `${Math.round(row.used)}% used${row.age ? ` (${row.age} ago)` : ''}` })
        ]
      }),
      jsx(Runway, { row }),
      jsxs('div', {
        className: 'flex items-baseline justify-between gap-2 text-(--ui-text-tertiary)',
        children: [
          jsx('span', { style: accent, children: headline ? '' : note(row) }),
          jsx('span', { children: `Resets ${when(row.end, row.end - row.start)}` })
        ]
      })
    ]
  })
}

function Details({ data, panelRef }) {
  const rows = data.rows || []
  const stale = data.at && nowS() - data.at > STALE_S
  const top = rows[0] ? verdict(rows[0]) : null
  return jsxs('div', {
    ref: panelRef,
    role: 'dialog',
    'aria-label': 'Obol forecast',
    tabIndex: -1,
    style: {
      position: 'fixed',
      right: 12,
      bottom: 32,
      zIndex: 50,
      width: 320,
      maxHeight: 'calc(100vh - 48px)',
      overflowY: 'auto',
      outline: 'none',
      boxSizing: 'border-box',
      padding: 12,
      borderRadius: 6,
      background: 'var(--ui-bg-elevated, var(--background))',
      border: '1px solid var(--ui-stroke-secondary)'
    },
    className: 'flex flex-col gap-3 text-xs',
    children: [
      top
        ? jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              jsx('div', { className: 'font-medium', style: { fontSize: 15, color: HOT[rows[0].state] }, children: top.head }),
              jsx('div', { className: 'text-(--ui-text-secondary)', children: top.sub })
            ]
          })
        : jsx('div', { className: 'font-medium', children: 'Obol' }),
      ...rows.map((row, i) => jsx(Row, { row, headline: i === 0 }, i)),
      rows.length
        ? jsx('div', {
            className: 'text-(--ui-text-tertiary)',
            children: `Bars show the time left until each reset, not how much is used.${rows.some(r => r.out_at || r.state === 'out') ? ' Hatched: out of tokens.' : ''}`
          })
        : null,
      data.chart
        ? jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              rows.length === 1 ? null : jsx('div', { className: 'text-(--ui-text-secondary)', children: data.chart.title }),
              jsx(Chart, { chart: data.chart }),
              jsx('div', {
                className: 'text-(--ui-text-tertiary)',
                children: `Solid: so far.${data.chart.expected.length > 1 ? ' Dashed: forecast.' : ''}${data.chart.high ? ' Shaded: how much your past periods varied.' : ''}`
              })
            ]
          })
        : null,
      ...(rows.length ? (data.demo ? data.lines.slice(0, 1) : []) : data.lines || []).map((line, i) =>
        jsx('div', { className: 'text-(--ui-text-secondary)', children: line }, `line${i}`)
      ),
      rows.some((r) => !r.amount)
        ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: "Plan percentages are the vendor's own figure for the whole account, including use outside Hermes." })
        : null,
      rows.some((r) => r.amount)
        ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: "Dollar figures are Hermes's own use at list price, in US dollars, so your provider's bill can differ." })
        : null,
      jsxs('div', {
        className: 'flex items-baseline justify-between gap-2 text-(--ui-text-tertiary)',
        children: [
          jsx('span', { children: data.forecast ? `An estimate from ${data.forecast}` : 'Type /obol for more' }),
          data.at
            ? jsx('span', { style: { whiteSpace: 'nowrap' }, children: `${stale ? '≈ ' : ''}Updated ${when(data.at, nowS() - data.at > 86400 ? 3 * 86400 : 0)}` })
            : null
        ]
      })
    ]
  })
}

function Chip({ ctx }) {
  const [data, setData] = useState(null)
  const [open, setOpen] = useState(false)
  const button = useRef(null)
  const panel = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    panel.current?.focus()
    const onKey = e => {
      if (e.key !== 'Escape') return
      setOpen(false)
      button.current?.focus()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open])

  useEffect(() => {
    let alive = true
    let last = null
    const load = () =>
      ctx
        .rest('/status')
        .then(next => {
          if (!alive) return
          setData(next)
          // Worse than before; a demo ending isn't news.
          const worse = WARN.includes(next.state) && (!WARN.includes(last) || (last === 'over' && next.state === 'out'))
          if (last && last !== 'demo' && worse) {
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

  const top = data.rows?.[0]
  const hot = HOT[data.state]
  return jsxs('span', {
    children: [
      jsxs('button', {
        ref: button,
        type: 'button',
        title: data.lines?.[0],
        'aria-label': `Obol: ${data.lines?.[0] || data.chip}`,
        'aria-expanded': open,
        className: 'inline-flex items-center gap-1.5 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
        style: hot ? { color: hot } : undefined,
        onClick: () => setOpen(!open),
        children: [
          top ? jsx('span', { style: { display: 'block', width: 28 }, children: jsx(Runway, { row: top, height: 4, decorative: true }) }) : null,
          jsx('span', { children: data.chip })
        ]
      }),
      open ? jsx('div', { style: { position: 'fixed', inset: 0, zIndex: 49 }, onClick: () => setOpen(false) }) : null,
      open ? jsx(Details, { data, panelRef: panel }) : null
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

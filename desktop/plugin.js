// Obol's status-bar chip: when you'll run out, at a glance. Click for the forecast.
import { host } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const POLL_MS = 60_000
const HOT = { over: 'var(--ui-accent)', out: 'var(--ui-accent)' }
const WARN = ['over', 'out']

function Chart({ chart }) {
  const W = 280
  const H = 90
  const all = [...chart.actual, ...chart.expected]
  const top = Math.max(chart.limit || 0, ...all.map(p => p[1]), 0.01) * 1.05
  const x = t => ((t - chart.start) / (chart.end - chart.start)) * W
  const y = v => H - (v / top) * H
  const path = points => points.map((p, i) => `${i ? 'L' : 'M'}${x(p[0]).toFixed(1)} ${y(p[1]).toFixed(1)}`).join(' ')

  return jsxs('svg', {
    viewBox: `0 0 ${W} ${H}`,
    width: W,
    height: H,
    role: 'img',
    'aria-label': 'Spend so far and where it is heading, against the budget',
    children: [
      chart.limit
        ? jsx('line', {
            x1: 0,
            x2: W,
            y1: y(chart.limit),
            y2: y(chart.limit),
            stroke: 'var(--ui-text-tertiary)',
            strokeDasharray: '3 3'
          })
        : null,
      jsx('path', { d: path(chart.expected), fill: 'none', stroke: 'var(--ui-accent)', strokeDasharray: '4 3' }),
      jsx('path', { d: path(chart.actual), fill: 'none', stroke: 'var(--ui-text-secondary)', strokeWidth: 1.5 })
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
      data.chart ? jsx(Chart, { chart: data.chart }) : null,
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
        .catch(() => {})                // the backend is optional: no data just means no chip text yet
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

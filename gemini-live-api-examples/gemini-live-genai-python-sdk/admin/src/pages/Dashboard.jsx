import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import CallLogs, { fmtDate } from '../components/CallLogs.jsx'
import PageHeader from '../components/PageHeader.jsx'

export default function Dashboard() {
  const [s, setS] = useState(null)
  const [active, setActive] = useState(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    api.get('/summary').then(setS).catch((e) => setErr(e.message))
    api.get('/campaigns/active').then((r) => setActive(r.campaign)).catch(() => {})
  }, [])

  const bySource = s?.by_source || {}
  const yesRate = s ? Math.round(((s.unique_yes_rate ?? s.booking_conversion_rate) || 0) * 100) : 0

  return (
    <div className="stack">
      <PageHeader title="Dashboard" sub="Overview of your calling activity" />

      {active && (
        <Link to={`/campaigns/${active.id}`} className="card" style={{ display: 'block', borderColor: 'rgba(16,185,129,0.35)' }}>
          <div className="row-between">
            <div>
              <div className="label" style={{ color: 'var(--green)' }}>
                Current {active.status === 'live' ? 'Live' : 'Scheduled'} Campaign
                {active.status === 'live' && <span className="live-dot" style={{ marginLeft: 8 }} />}
              </div>
              <div style={{ fontSize: '1.1rem', fontWeight: 700, marginTop: 6 }}>{active.name}</div>
              <div className="page-sub">Starts {fmtDate(active.start_at)} · {active.contact_count} contacts</div>
            </div>
            <div style={{ textAlign: 'right' }}>
              <div className="value" style={{ fontSize: '1.4rem' }}>
                {(active.progress?.done || 0)}/{active.contact_count}
              </div>
              <div className="page-sub">completed</div>
            </div>
          </div>
        </Link>
      )}

      {err && <div style={{ color: '#fca5a5' }}>{err}</div>}

      <div className="grid stat-grid">
        <div className="card stat">
          <div className="label">Total Calls</div>
          <div className="value">{s ? s.total_calls : '—'}</div>
          <div className="sub">Inbound {bySource.plivo_inbound || 0} · Browser {bySource.browser || 0}</div>
        </div>
        <div className="card stat">
          <div className="label">Total Minutes</div>
          <div className="value">{s ? s.total_minutes : '—'}</div>
          <div className="sub">{s ? `${s.total_seconds || 0}s across all calls` : ''}</div>
        </div>
        <div className="card stat">
          <div className="label">RSVP Yes-Rate</div>
          <div className="value">{s ? `${yesRate}%` : '—'}</div>
          <div className="sub">{s ? `${(s.unique_yes ?? s.bookings) || 0} coming · ${s.unique_kids || 0} kids confirmed (Cumulative across all campaigns till date)` : ''}</div>
        </div>
      </div>

      <CallLogs title="Call Logs" showSource={false} />
    </div>
  )
}

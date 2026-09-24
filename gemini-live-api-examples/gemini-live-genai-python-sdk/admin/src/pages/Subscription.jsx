import { useEffect, useState } from 'react'
import { api, fmtDur } from '../api.js'
import Modal from '../components/Modal.jsx'
import PageHeader from '../components/PageHeader.jsx'

// The client's plan and how much of it is used. Read-only for the client's admins; the
// service provider (EO_SUPERADMIN_USERS) can override the .env plan from here.
const inr = (n) => new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(Number(n || 0))
const num = (n) => new Intl.NumberFormat('en-IN').format(Number(n || 0))
const LICENCE = {
  valid: ['green', 'Valid'], expiring: ['amber', 'Expiring soon'], expired: ['red', 'Expired'], unset: ['src', 'Not set'],
}

export default function Subscription() {
  const [s, setS] = useState(null)
  const [err, setErr] = useState('')
  const [edit, setEdit] = useState(false)

  const load = () => api.get('/subscription').then((d) => { setS(d); setErr('') }).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])

  if (err && !s) return <div className="stack"><div className="err">{err}</div></div>
  if (!s) return <div className="panel"><div className="muted">Loading…</div></div>

  const { plan, usage } = s
  const pct = usage.pct_used
  const low = usage.minutes_left != null && usage.minutes_included && usage.minutes_left / usage.minutes_included < 0.1
  const [licClass, licLabel] = LICENCE[usage.licence.status] || LICENCE.unset

  return (
    <div className="stack">
      <PageHeader title="Subscription" sub="Your plan, the minutes used so far, and how long the licence runs"
                  actions={s.can_edit && <button className="btn" onClick={() => setEdit(true)}>Edit plan</button>} />
      {err && <div className="err">{err}</div>}

      <div className="grid stat-grid">
        <Stat label="Minutes used" value={num(usage.minutes_used)}
              sub={usage.minutes_included ? `of ${num(usage.minutes_included)} purchased` : 'no minute cap on this plan'} />
        <Stat label="Minutes left" value={usage.minutes_left == null ? '—' : num(usage.minutes_left)}
              sub={pct == null ? 'unlimited' : `${pct}% used`} color={low ? 'var(--red)' : undefined} />
        <Stat label="Amount used" value={`₹${inr(usage.amount_inr)}`}
              sub={usage.rate_inr_per_min ? `at ₹${inr(usage.rate_inr_per_min)} per minute` : 'per-minute rate not set'} />
        <Stat label="Calls this period" value={num(usage.calls)} sub={`${fmtDur(usage.seconds)} of talk time`} />
      </div>

      <div className="grid halves">
        <div className="panel">
          <div className="panel-head"><h3>Usage</h3><span className="muted" style={{ fontSize: '0.78rem' }}>phone calls, each rounded up to the minute</span></div>
          {pct == null ? <div className="muted" style={{ fontSize: '0.85rem' }}>No minute cap is set, so there is nothing to measure against.</div> : (
            <>
              <div className="bar" style={{ height: 12 }}><div className="bar-fill" style={{ width: `${pct}%`, background: low ? 'var(--red)' : undefined }} /></div>
              <div className="row-between" style={{ fontSize: '0.8rem', marginTop: 8 }}>
                <span>{num(usage.minutes_used)} min used</span><span className="muted">{num(usage.minutes_included)} min purchased</span>
              </div>
            </>
          )}
          <div className="muted" style={{ fontSize: '0.76rem', marginTop: 12 }}>
            Counted: outbound campaign calls, inbound calls and "Call me" tests inside the plan period. Browser mic tests are free.
          </div>
        </div>
        <div className="panel">
          <div className="panel-head"><h3>Plan</h3><span className="muted" style={{ fontSize: '0.76rem' }}>{plan.source === 'db' ? 'set by the service provider' : 'from the server configuration'}</span></div>
          <Row label="Plan" value={plan.name || '—'} />
          <Row label="Start date" value={plan.start || '—'} />
          <Row label="End date" value={plan.end ? `${plan.end}${usage.period.days_left != null ? ` · ${usage.period.days_left >= 0 ? `${usage.period.days_left} days left` : 'ended'}` : ''}` : '—'} />
          <Row label="Period" value={<span className={`pill ${usage.period.active ? 'green' : 'amber'}`}>{usage.period.active ? 'Active' : 'Not active'}</span>} />
          <Row label="Licence valid till" value={<span>{plan.licence_valid_till || '—'} <span className={`pill ${licClass}`} style={{ marginLeft: 6 }}>{licLabel}{usage.licence.days_left != null && usage.licence.days_left >= 0 ? ` · ${usage.licence.days_left} days` : ''}</span></span>} />
          <Row label="Rate" value={plan.rate_inr_per_min ? `₹${inr(plan.rate_inr_per_min)} per minute` : '—'} />
        </div>
      </div>

      {edit && <PlanModal plan={plan} onClose={() => setEdit(false)} onDone={(snap) => { setS(snap); setEdit(false) }} />}
    </div>
  )
}

function Stat({ label, value, sub, color }) {
  return (
    <div className="card stat">
      <div className="label">{label}</div>
      <div className="value" style={color ? { color } : undefined}>{value ?? '—'}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  )
}

function Row({ label, value }) {
  return <div style={{ marginBottom: 10 }}><label>{label}</label><div style={{ fontSize: '0.88rem' }}>{value}</div></div>
}

function PlanModal({ plan, onClose, onDone }) {
  const [f, setF] = useState({
    name: plan.name || '', minutes: plan.minutes || '', start: plan.start || '', end: plan.end || '',
    licence_valid_till: plan.licence_valid_till || '', rate_inr_per_min: plan.rate_inr_per_min || '',
  })
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const set = (k, v) => setF((p) => ({ ...p, [k]: v }))

  async function save(body) {
    setBusy(true); setErr('')
    try { onDone(await api.put('/subscription', body)) } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  return (
    <Modal title="Edit plan" sub="Overrides the .env values for everyone who opens this page" onClose={() => !busy && onClose()}
      footer={<>
        {plan.source === 'db' && <button className="btn ghost" disabled={busy} onClick={() => confirm('Drop the override and go back to the .env plan?') && save({ reset: true })}>Back to .env</button>}
        <button className="btn ghost" disabled={busy} onClick={onClose}>Cancel</button>
        <button className="btn" disabled={busy} onClick={() => save(f)}>{busy ? 'Saving…' : 'Save'}</button>
      </>}>
      {err && <div className="err" style={{ marginBottom: 12 }}>{err}</div>}
      <div className="row"><label>Plan name</label><input value={f.name} onChange={(e) => set('name', e.target.value)} placeholder="e.g. Starter 5000" autoFocus /></div>
      <div className="two">
        <div className="row"><label>Minutes purchased</label><input type="number" min="0" step="1" value={f.minutes} onChange={(e) => set('minutes', e.target.value)} /></div>
        <div className="row"><label>₹ per minute</label><input type="number" min="0" step="0.01" value={f.rate_inr_per_min} onChange={(e) => set('rate_inr_per_min', e.target.value)} /></div>
      </div>
      <div className="two">
        <div className="row"><label>Start date</label><input type="date" value={f.start} onChange={(e) => set('start', e.target.value)} /></div>
        <div className="row"><label>End date</label><input type="date" value={f.end} onChange={(e) => set('end', e.target.value)} /></div>
      </div>
      <div className="row"><label>Licence valid till</label><input type="date" value={f.licence_valid_till} onChange={(e) => set('licence_valid_till', e.target.value)} /></div>
    </Modal>
  )
}

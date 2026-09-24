import { useEffect, useState } from 'react'
import { api } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'

// The service provider's page (EO_SUPERADMIN_USERS only): which tabs the client's admins
// see, and clearing the test data before go-live.
const num = (n) => new Intl.NumberFormat('en-IN').format(Number(n || 0))

// Labels for the page keys the server sends back in `pages`.
const PAGE_LABEL = {
  weddings: 'Weddings', campaigns: 'Campaigns', contacts: 'Contacts', scheduler: 'Scheduler',
  agents: 'Agents', 'call-logs': 'Call logs', users: 'Users', settings: 'Settings',
  subscription: 'Subscription',
}

const PARTS = [
  { key: 'calls', label: 'Call logs and recordings',
    what: (c) => `${num(c.calls)} calls and ${num(c.recordings)} recordings. Minutes used on the Subscription page go back to 0.` },
  { key: 'outbound', label: 'Guests and campaigns',
    what: (c) => `${num(c.contacts)} guests and ${num(c.campaigns)} campaigns.` },
  { key: 'weddings', label: 'Weddings and events',
    what: (c) => `${num(c.weddings)} weddings and ${num(c.events)} events, plus any agent copies made for them. The shipped agent scripts are kept.` },
  { key: 'audit', label: 'Audit log',
    what: (c) => `${num(c.audit)} rows. The reset itself becomes the first new row.` },
]

export default function SuperAdmin() {
  const [d, setD] = useState(null)
  const [err, setErr] = useState('')

  const load = () => api.get('/superadmin').then((x) => { setD(x); setErr('') }).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])

  if (err && !d) return <div className="stack"><div className="err">{err}</div></div>
  if (!d) return <div className="panel"><div className="muted">Loading…</div></div>

  return (
    <div className="stack">
      <PageHeader title="Super admin" sub="Only the service provider's accounts see this page. What you set here applies to every admin the client has." />
      {err && <div className="err">{err}</div>}
      <TabsPanel d={d} onSaved={(x) => setD(x)} />
      <ResetPanel d={d} onDone={(x) => setD(x)} />
    </div>
  )
}

function TabsPanel({ d, onSaved }) {
  const baseKey = d.client_hidden_pages.join(',')
  const [hidden, setHidden] = useState(new Set(d.client_hidden_pages))
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => { setHidden(new Set(d.client_hidden_pages)) }, [baseKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const base = new Set(d.client_hidden_pages)
  const dirty = d.pages.some((p) => hidden.has(p) !== base.has(p))
  const toggle = (page, visible) => setHidden((s) => { const n = new Set(s); if (visible) n.delete(page); else n.add(page); return n })

  async function save(body) {
    setBusy(true); setErr(''); setSaved(false)
    try {
      onSaved(await api.put('/superadmin/pages', body))
      setSaved(true); setTimeout(() => setSaved(false), 2500)
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <div><h3>Tabs the client sees</h3>
          <div className="muted" style={{ fontSize: '0.78rem', marginTop: 3 }}>
            Ticked tabs appear in the menu of the client's admins. You always see every tab.
            This curates the menu — anything that must be truly unreachable is blocked on the server as well.
          </div></div>
        {saved && <span className="pill green">Saved ✓</span>}
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th className="no-sort" style={{ width: 70 }}>Visible</th><th className="no-sort">Tab</th><th className="no-sort">Note</th></tr></thead>
          <tbody>
            <tr><td><input type="checkbox" checked disabled /></td><td>Dashboard</td><td className="muted" style={{ fontSize: '0.78rem' }}>Always shown</td></tr>
            {d.pages.map((p) => (
              <tr key={p} style={{ opacity: hidden.has(p) ? 0.6 : 1 }}>
                <td><input type="checkbox" checked={!hidden.has(p)} onChange={(e) => toggle(p, e.target.checked)} /></td>
                <td>{PAGE_LABEL[p] || p}</td>
                <td className="muted" style={{ fontSize: '0.78rem' }}>{hidden.has(p) ? 'Hidden from the client' : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginTop: 12 }}>
        <button className="btn" disabled={busy || !dirty} onClick={() => save({ hidden_pages: [...hidden] })}>{busy ? 'Saving…' : 'Save changes'}</button>
        {dirty && !busy && <button className="btn ghost sm" onClick={() => setHidden(new Set(d.client_hidden_pages))}>Discard</button>}
        {d.source === 'db' && !dirty && <button className="btn ghost sm" disabled={busy} onClick={() => save({ reset: true })}>Back to the .env default</button>}
        <span className="muted" style={{ fontSize: '0.74rem' }}>{d.source === 'db' ? 'Set here.' : 'Coming from EO_HIDDEN_PAGES in .env until you save.'}</span>
      </div>
      {err && <div className="err" style={{ marginTop: 10 }}>{err}</div>}
    </div>
  )
}

function ResetPanel({ d, onDone }) {
  const [parts, setParts] = useState(new Set())
  const [word, setWord] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [result, setResult] = useState(null)
  const c = d.counts
  const can = parts.size > 0 && word === 'DELETE' && !busy && !d.live_calls
  const toggle = (key, on) => setParts((s) => { const n = new Set(s); if (on) n.add(key); else n.delete(key); return n })

  async function run() {
    const names = PARTS.filter((p) => parts.has(p.key)).map((p) => p.label.toLowerCase()).join(', ')
    if (!confirm(`Delete ${names} now? A backup is saved on the server first.`)) return
    setBusy(true); setErr(''); setResult(null)
    try {
      const r = await api.post('/superadmin/reset-data', { confirm: word, parts: [...parts] })
      setResult(r); setWord(''); setParts(new Set()); onDone(r.state)
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="panel" style={{ borderColor: 'rgba(248,113,113,0.45)' }}>
      <div className="panel-head">
        <div><h3 style={{ color: '#fca5a5' }}>Clear test data before go-live</h3>
          <div className="muted" style={{ fontSize: '0.78rem', marginTop: 3 }}>
            Keeps users, the shipped agent scripts, the plan and these settings.
            A copy of the database is saved, and the call files are moved, into <span style={{ fontFamily: 'var(--mono)' }}>{d.backup_root}</span> first.
          </div></div>
      </div>
      <div className="stack" style={{ gap: 10 }}>
        {PARTS.map((p) => (
          <label key={p.key} className="toggle" style={{ display: 'flex', gap: 10, alignItems: 'flex-start', cursor: 'pointer' }}>
            <input type="checkbox" checked={parts.has(p.key)} onChange={(e) => toggle(p.key, e.target.checked)} style={{ width: 'auto', height: 'auto', marginTop: 4 }} />
            <span><b style={{ color: 'var(--text)' }}>{p.label}</b><br /><span className="muted">{p.what(c)}</span></span>
          </label>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginTop: 16 }}>
        <input value={word} onChange={(e) => setWord(e.target.value)} placeholder="Type DELETE" style={{ width: 160 }} />
        <button className="btn danger" disabled={!can} onClick={run}>{busy ? 'Deleting…' : 'Delete selected data'}</button>
        {!!d.live_calls && <span style={{ color: 'var(--amber)', fontSize: '0.8rem' }}>A call is on the line. Wait until it ends.</span>}
      </div>
      {err && <div className="err" style={{ marginTop: 10 }}>{err}</div>}
      {result && (
        <div className="card" style={{ marginTop: 14, padding: 12, fontSize: '0.82rem' }}>
          <b>Done.</b> Deleted: {result.parts.join(', ')}. Now {num(result.after.weddings)} weddings, {num(result.after.contacts)} guests, {num(result.after.calls)} calls.
          <div className="muted" style={{ marginTop: 4 }}>Backup: <span style={{ fontFamily: 'var(--mono)' }}>{result.backup}</span></div>
        </div>
      )}
    </div>
  )
}

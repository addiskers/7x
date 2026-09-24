import { useEffect, useMemo, useState } from 'react'
import { api, qs } from '../api.js'
import { fmtDate } from '../components/CallLogs.jsx'
import PageHeader from '../components/PageHeader.jsx'
import { IconSearch } from '../components/icons.jsx'

const PAGE = 50

// Colour by what the row means: red = something failed or was destroyed, green = something
// was created, amber = a configuration or plan change.
function pillClass(action) {
  if (action.includes('failed') || action.includes('deleted') || action === 'data_reset') return 'red'
  if (action.includes('created') || action.includes('duplicated')) return 'green'
  if (action.includes('updated') || action.includes('cancelled') || action.includes('reset')) return 'amber'
  return 'src'
}

// Wait until the user stops typing before searching, so every keystroke is not a request.
function useDebounced(value, ms) {
  const [v, setV] = useState(value)
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t) }, [value, ms])
  return v
}

export default function AuditLog() {
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [actions, setActions] = useState([])
  const [err, setErr] = useState('')
  const [q, setQ] = useState('')
  const [loading, setLoading] = useState(false)
  const [action, setAction] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [page, setPage] = useState(0)

  useEffect(() => { api.get('/audit/actions').then((d) => setActions(d.items || [])).catch(() => {}) }, [])
  const dq = useDebounced(q, 300)
  const filters = useMemo(() => ({ q: dq, action, from, to, limit: PAGE, offset: page * PAGE }), [dq, action, from, to, page])
  useEffect(() => {
    setLoading(true)
    api.get(`/audit${qs(filters)}`).then((d) => { setItems(d.items || []); setTotal(d.total || 0); setErr('') })
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false))
  }, [filters])

  return (
    <div className="stack">
      <PageHeader title="Audit log" sub="Who did what: logins, user changes, weddings, campaigns, agent edits and data resets" />
      <div className="toolbar">
        <div className="search"><span className="ic"><IconSearch /></span>
          <input placeholder="User, target, detail…" value={q} onChange={(e) => { setPage(0); setQ(e.target.value) }} /></div>
        <select value={action} onChange={(e) => { setPage(0); setAction(e.target.value) }}>
          <option value="">All actions</option>
          {actions.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <input type="date" value={from} onChange={(e) => { setPage(0); setFrom(e.target.value) }} />
        <input type="date" value={to} onChange={(e) => { setPage(0); setTo(e.target.value) }} />
        {loading && <span className="muted" style={{ fontSize: '0.78rem' }}>Searching…</span>}
      </div>
      {err && <div className="err">{err}</div>}
      <div className="table-wrap" style={{ opacity: loading ? 0.6 : 1, transition: 'opacity .15s' }}>
        <table>
          <thead><tr><th className="no-sort">When</th><th className="no-sort">User</th><th className="no-sort">Action</th><th className="no-sort">Target</th><th className="no-sort">Detail</th><th className="no-sort">IP</th></tr></thead>
          <tbody>
            {!items.length ? <tr><td colSpan={6} className="empty">Nothing recorded yet.</td></tr> : items.map((a) => (
              <tr key={a.id}>
                <td style={{ whiteSpace: 'nowrap' }}>{fmtDate(a.created_at)}</td>
                <td>{a.username || <span className="muted">—</span>}</td>
                <td><span className={`pill ${pillClass(a.action)}`}>{a.action}</span></td>
                <td style={{ fontFamily: 'var(--mono)', fontSize: '0.78rem' }}>{a.target || '—'}</td>
                <td className="muted" style={{ fontSize: '0.76rem', maxWidth: 360, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={a.detail}>{a.detail || '—'}</td>
                <td className="muted" style={{ fontSize: '0.76rem' }}>{a.ip || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="pager">
        <span>{total} total</span>
        <button disabled={page === 0} onClick={() => setPage((p) => p - 1)}>Prev</button>
        <span>Page {page + 1}</span>
        <button disabled={(page + 1) * PAGE >= total} onClick={() => setPage((p) => p + 1)}>Next</button>
      </div>
    </div>
  )
}

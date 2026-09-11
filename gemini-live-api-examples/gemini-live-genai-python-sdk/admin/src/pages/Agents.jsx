import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import PageHeader from '../components/PageHeader.jsx'
import { useWedding } from '../wedding.jsx'

const KIND_PILL = { reminder: 'green', logistics: 'blue', custom: 'amber' }

export default function Agents() {
  const navigate = useNavigate()
  const { weddingId, wedding } = useWedding()
  const [items, setItems] = useState([])
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const r = await api.get('/agents' + (weddingId ? `?wedding_id=${weddingId}` : ''))
      setItems(r.items || [])
    } catch (e) { setErr(e.message) }
  }, [weddingId])

  useEffect(() => { load() }, [load])

  async function duplicate(a) {
    if (!weddingId) { alert('Select a wedding first.'); return }
    setBusy(true)
    try {
      const copy = await api.post(`/agents/${a.id}/duplicate`, {
        wedding_id: weddingId, name: `${a.name} — ${wedding?.name || 'custom'}`,
      })
      navigate(`/agents/${copy.id}`)
    } catch (e) { alert(e.message) } finally { setBusy(false) }
  }

  async function remove(a) {
    if (!confirm(`Delete "${a.name}"?`)) return
    try { await api.del(`/agents/${a.id}`); load() } catch (e) { alert(e.message) }
  }

  const shipped = items.filter((a) => !a.wedding_id)
  const custom = items.filter((a) => a.wedding_id)

  return (
    <div className="stack">
      <PageHeader
        title="Agents"
        sub="What the agent says on a call. Edit the script, then test it before you dial anyone."
      />
      {err && <div className="err">{err}</div>}

      {!!custom.length && (
        <Section title={`Your agents${wedding ? ` — ${wedding.name}` : ''}`}
                 items={custom} onOpen={(a) => navigate(`/agents/${a.id}`)}
                 onDuplicate={duplicate} onDelete={remove} busy={busy} />
      )}

      <Section
        title="Shipped templates"
        sub="Ready to use as they are. Duplicate one to change its wording for this wedding — the original stays untouched for everyone else."
        items={shipped} onOpen={(a) => navigate(`/agents/${a.id}`)}
        onDuplicate={duplicate} busy={busy}
      />
    </div>
  )
}

function Section({ title, sub, items, onOpen, onDuplicate, onDelete, busy }) {
  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <h3>{title}</h3>
          {sub && <div className="muted" style={{ fontSize: '0.8rem', marginTop: 3, maxWidth: 640 }}>{sub}</div>}
        </div>
      </div>
      <div style={{ display: 'grid', gap: 12, gridTemplateColumns: 'repeat(auto-fill,minmax(300px,1fr))', padding: 4 }}>
        {items.map((a) => (
          <div key={a.id} className="card" style={{ padding: 16 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
              <h4 style={{ margin: 0 }}>{a.name}</h4>
              <span className={`pill ${KIND_PILL[a.kind] || 'amber'}`}>{a.kind}</span>
            </div>
            <p className="muted" style={{ fontSize: '0.82rem', margin: '8px 0 0', minHeight: 34 }}>
              {a.description || 'No description'}
            </p>
            <div className="muted" style={{ fontSize: '0.76rem', marginTop: 10 }}>
              {a.requires_event ? 'Calls about one event' : 'Not tied to an event'}
              {a.listen_seconds ? ` · listens ${a.listen_seconds}s after` : ''}
            </div>
            <div style={{ display: 'flex', gap: 8, marginTop: 14, flexWrap: 'wrap' }}>
              <button className="btn sm" onClick={() => onOpen(a)}>
                {a.wedding_id ? 'Edit & Test' : 'View & Test'}
              </button>
              <button className="btn ghost sm" disabled={busy} onClick={() => onDuplicate(a)}>Duplicate</button>
              {onDelete && a.wedding_id && (
                <button className="btn danger sm" onClick={() => onDelete(a)}>Delete</button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

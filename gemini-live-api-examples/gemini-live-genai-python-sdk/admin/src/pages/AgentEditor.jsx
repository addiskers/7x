import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import AgentTestPanel from '../components/AgentTestPanel.jsx'
import PageHeader from '../components/PageHeader.jsx'
import PlaceholderPalette from '../components/PlaceholderPalette.jsx'
import { useWedding } from '../wedding.jsx'

export default function AgentEditor() {
  const { id } = useParams()
  const navigate = useNavigate()
  const { weddingId, wedding, refresh } = useWedding()
  const [agent, setAgent] = useState(null)
  const [known, setKnown] = useState([])
  const [events, setEvents] = useState([])
  const [guests, setGuests] = useState([])
  const [prompt, setPrompt] = useState('')
  const [trigger, setTrigger] = useState('')
  const [name, setName] = useState('')
  const [listen, setListen] = useState(0)
  const [err, setErr] = useState('')
  const [saved, setSaved] = useState(false)
  const [busy, setBusy] = useState(false)
  const promptRef = useRef(null)

  const readOnly = agent && !agent.wedding_id      // shipped templates are duplicate-only
  const dirty = agent && (prompt !== agent.prompt_template || trigger !== (agent.trigger_template || '')
                          || name !== agent.name || Number(listen) !== Number(agent.listen_seconds || 0))

  const load = useCallback(async () => {
    try {
      const [a, list] = await Promise.all([
        api.get(`/agents/${id}`),
        api.get('/agents' + (weddingId ? `?wedding_id=${weddingId}` : '')),
      ])
      setAgent(a); setKnown(list.placeholders || [])
      setPrompt(a.prompt_template || ''); setTrigger(a.trigger_template || '')
      setName(a.name || ''); setListen(a.listen_seconds || 0)
      if (weddingId) {
        const [ev, gs] = await Promise.all([
          api.get(`/weddings/${weddingId}/events`),
          api.get('/contacts?limit=200'),
        ])
        setEvents(ev.items || [])
        setGuests(gs.items || [])
      }
    } catch (e) { setErr(e.message) }
  }, [id, weddingId])

  useEffect(() => { load() }, [load])

  // Insert at the cursor rather than appending — the operator is usually mid-sentence.
  function insert(text) {
    const el = promptRef.current
    if (!el) { setPrompt((p) => p + text); return }
    const { selectionStart: s, selectionEnd: e } = el
    setPrompt((p) => p.slice(0, s) + text + p.slice(e))
    requestAnimationFrame(() => {
      el.focus()
      el.setSelectionRange(s + text.length, s + text.length)
    })
  }

  async function save() {
    setErr(''); setBusy(true); setSaved(false)
    try {
      const updated = await api.patch(`/agents/${agent.id}`, {
        name, prompt_template: prompt, trigger_template: trigger,
        listen_seconds: Number(listen) || 0,
      })
      setAgent(updated); setSaved(true)
      setTimeout(() => setSaved(false), 2500)
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  async function duplicate() {
    if (!weddingId) { alert('Select a wedding first.'); return }
    setBusy(true)
    try {
      const copy = await api.post(`/agents/${agent.id}/duplicate`, {
        wedding_id: weddingId, name: `${agent.name} — ${wedding?.name || 'custom'}`,
      })
      await refresh()
      navigate(`/agents/${copy.id}`)
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  if (err && !agent) return <div className="stack"><div className="err">{err}</div></div>
  if (!agent) return <div className="panel"><div className="muted">Loading…</div></div>

  return (
    <div className="stack">
      <PageHeader
        title={agent.name}
        sub={agent.description}
        back={<Link to="/agents" className="muted" style={{ fontSize: '0.82rem' }}>← All agents</Link>}
        actions={readOnly
          ? <button className="btn" disabled={busy} onClick={duplicate}>Duplicate to edit</button>
          : <>
              {saved && <span className="pill green" style={{ marginRight: 8 }}>Saved</span>}
              <button className="btn" disabled={busy || !dirty} onClick={save}>
                {busy ? 'Saving…' : 'Save changes'}
              </button>
            </>}
      />

      {readOnly && (
        <div style={{ background: 'var(--blue-soft)', border: '1px solid var(--blue)',
                      borderRadius: 'var(--radius-sm)', padding: '10px 12px', fontSize: '0.84rem' }}>
          This is a shipped template, shared by every wedding. Duplicate it to change the
          wording for {wedding?.name || 'this wedding'} — the original stays as it is.
        </div>
      )}
      {err && <div className="err">{err}</div>}

      <div style={{ display: 'grid', gap: 16, gridTemplateColumns: 'minmax(0,1fr) 280px', alignItems: 'start' }}>
        <div className="panel">
          <div className="panel-head"><h3>What it says</h3></div>

          {!readOnly && (
            <div className="two">
              <div><label>Agent name</label>
                <input value={name} onChange={(e) => setName(e.target.value)} /></div>
              <div><label>Listen after the message (seconds)</label>
                <input type="number" min="0" max="120" value={listen}
                       onChange={(e) => setListen(e.target.value)} />
                <div className="muted" style={{ fontSize: '0.76rem', marginTop: 3 }}>
                  0 keeps the standard window. A short value suits announce-and-go reminders.
                </div>
              </div>
            </div>
          )}

          <div className="row" style={{ marginTop: 14 }}>
            <label>The script</label>
            <textarea
              ref={promptRef} rows={26} value={prompt} readOnly={readOnly}
              onChange={(e) => setPrompt(e.target.value)}
              style={{ fontFamily: 'var(--mono)', fontSize: '0.79rem', lineHeight: 1.6, width: '100%' }}
            />
          </div>

          <div className="row">
            <label>Opening line</label>
            <textarea rows={3} value={trigger} readOnly={readOnly}
                      onChange={(e) => setTrigger(e.target.value)}
                      style={{ fontFamily: 'var(--mono)', fontSize: '0.79rem', width: '100%' }} />
            <div className="muted" style={{ fontSize: '0.76rem', marginTop: 3 }}>
              What makes the agent speak first. Leave it blank for a standard greeting.
            </div>
          </div>
        </div>

        <div className="panel" style={{ position: 'sticky', top: 16 }}>
          <div className="panel-head"><h3>Placeholders</h3></div>
          <PlaceholderPalette known={known} onInsert={readOnly ? () => {} : insert} />
        </div>
      </div>

      <AgentTestPanel agent={agent} events={events} guests={guests} weddingId={weddingId} />
    </div>
  )
}

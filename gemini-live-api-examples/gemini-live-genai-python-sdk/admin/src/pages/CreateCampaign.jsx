import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import ContactUpload from '../components/ContactUpload.jsx'
import ContactsTable from '../components/ContactsTable.jsx'
import Modal from '../components/Modal.jsx'
import PageHeader from '../components/PageHeader.jsx'
import { useWedding } from '../wedding.jsx'

const pad = (n) => String(n).padStart(2, '0')
function todayStr() { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` }
function nowTimeStr() { const d = new Date(); return `${pad(d.getHours())}:${pad(d.getMinutes())}` }

const blockDecimalKeys = (e) => { if (['.', ',', 'e', 'E', '+', '-'].includes(e.key)) e.preventDefault() }
function toWhole(v) {
  if (v === '') return ''
  const n = Math.floor(Number(v))
  return Number.isFinite(n) ? String(Math.max(0, n)) : ''
}

export default function CreateCampaign() {
  const navigate = useNavigate()
  const { weddingId, wedding } = useWedding()
  const [refreshKey, setRefreshKey] = useState(0)
  const [selected, setSelected] = useState(new Set())
  const [total, setTotal] = useState(0)

  const [agents, setAgents] = useState([])
  const [events, setEvents] = useState([])
  const [agentId, setAgentId] = useState('')
  const [eventId, setEventId] = useState('')

  const [showStart, setShowStart] = useState(false)
  const [name, setName] = useState('')
  const [startDate, setStartDate] = useState(todayStr())
  const [startTime, setStartTime] = useState(nowTimeStr())
  const [delayH, setDelayH] = useState(4)
  const [maxDay, setMaxDay] = useState(3)
  const [days, setDays] = useState(1)
  const [callStart, setCallStart] = useState('09:00')
  const [callEnd, setCallEnd] = useState('21:00')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [preflight, setPreflight] = useState(null)
  const [ackFatigue, setAckFatigue] = useState(false)

  const [showAdd, setShowAdd] = useState(false)
  const [addName, setAddName] = useState('')
  const [addPhone, setAddPhone] = useState('')
  const [addErr, setAddErr] = useState('')
  const [addBusy, setAddBusy] = useState(false)

  const refresh = () => setRefreshKey((k) => k + 1)
  function toggle(id) { setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n }) }
  function toggleMany(ids, checked) { setSelected((s) => { const n = new Set(s); ids.forEach((id) => checked ? n.add(id) : n.delete(id)); return n }) }

  useEffect(() => {
    (async () => {
      try {
        const a = await api.get('/agents' + (weddingId ? `?wedding_id=${weddingId}` : ''))
        setAgents((a.items || []).filter((x) => x.active))
        if (weddingId) {
          const e = await api.get(`/weddings/${weddingId}/events`)
          setEvents(e.items || [])
        } else setEvents([])
      } catch { /* the picker just stays empty */ }
    })()
  }, [weddingId])

  const agent = useMemo(() => agents.find((a) => String(a.id) === agentId), [agents, agentId])
  const event = useMemo(() => events.find((e) => String(e.id) === eventId), [events, eventId])
  const needsEvent = agent?.requires_event && !eventId

  // Picking an event pre-selects its audience — still editable, it just saves the clicking.
  useEffect(() => {
    if (!event || !weddingId) return
    (async () => {
      try {
        const r = await api.post('/campaigns/preflight', {
          name: name || 'preview', start_at: new Date(Date.now() + 300000).toISOString(),
          agent_id: Number(agentId), event_id: Number(eventId), wedding_id: weddingId,
        })
        setSelected(new Set((r.audience?.guests || []).map((g) => g.id)))
      } catch { /* leave the selection alone */ }
    })()
  }, [eventId, agentId]) // eslint-disable-line react-hooks/exhaustive-deps

  const minTime = startDate === todayStr() ? nowTimeStr() : undefined

  function buildBody() {
    const start = new Date(`${startDate}T${startTime}`)
    return {
      name: name.trim(), contact_ids: [...selected], start_at: start.toISOString(),
      agent_id: Number(agentId), event_id: eventId ? Number(eventId) : null,
      wedding_id: weddingId,
      callback_delay_hours: Number(delayH), callback_max_per_day: Number(maxDay),
      callback_days: Number(days), call_start: callStart, call_end: callEnd,
    }
  }

  async function openConfirm() {
    setErr(''); setAckFatigue(false); setPreflight(null); setShowStart(true)
    if (!name.trim()) setName(event ? `${event.name} — reminder` : '')
    try {
      setPreflight(await api.post('/campaigns/preflight', { ...buildBody(), name: name || 'preview' }))
    } catch (e) { setErr(e.message) }
  }

  async function startCampaign() {
    setErr(''); setBusy(true)
    try {
      if (!name.trim()) throw new Error('Campaign name is required')
      if (!agentId) throw new Error('Pick an agent')
      if (needsEvent) throw new Error('This agent calls about a specific event — pick one')
      if (!startDate || !startTime) throw new Error('Start date and time are required')
      const start = new Date(`${startDate}T${startTime}`)
      if (start.getTime() < Date.now() - 60000) throw new Error('Start time is in the past — pick the current time or later')
      const dH = Number(delayH), mD = Number(maxDay), dY = Number(days)
      if (!Number.isInteger(dH) || dH < 0) throw new Error('Call-back hours must be a whole number (0 or more)')
      if (!Number.isInteger(mD) || mD < 1 || mD > 10) throw new Error('Attempts per day must be a whole number between 1 and 10')
      if (!Number.isInteger(dY) || dY < 1 || dY > 10) throw new Error('Call-back days must be a whole number between 1 and 10')
      if (!callStart || !callEnd) throw new Error('Set the calling hours (start and end time)')
      const c = await api.post('/campaigns', { ...buildBody(), acknowledge_fatigue: ackFatigue })
      navigate(`/campaigns/${c.id}`)
    } catch (e) {
      // The server re-checks fatigue, so an API client can't skip the warning.
      const detail = e.detail || {}
      if (detail.error === 'fatigue_warning') {
        setPreflight((p) => ({ ...(p || {}), fatigue: detail.fatigue }))
        setErr(detail.message)
      } else setErr(e.message)
    } finally { setBusy(false) }
  }

  async function addContact() {
    setAddBusy(true); setAddErr('')
    try {
      await api.post('/contacts', { name: addName, phone: addPhone, wedding_id: weddingId })
      setShowAdd(false); setAddName(''); setAddPhone(''); refresh()
    } catch (e) { setAddErr(e.message) } finally { setAddBusy(false) }
  }

  async function deleteSelected() {
    if (!selected.size) return
    if (!confirm(`Delete ${selected.size} guest(s)? They won't be available for any campaign.`)) return
    await api.post('/contacts/delete', { ids: [...selected] })
    setSelected(new Set()); refresh()
  }

  if (!weddingId) {
    return (
      <div className="stack">
        <PageHeader title="Create Campaign" />
        <div className="panel" style={{ textAlign: 'center', padding: 40 }}>
          <h3 style={{ marginTop: 0 }}>Pick a wedding first</h3>
          <p className="muted">A campaign belongs to one wedding's guest list.</p>
          <Link className="btn" to="/weddings">Go to Weddings</Link>
        </div>
      </div>
    )
  }

  const fatigue = preflight?.fatigue
  const warnings = preflight?.prompt_warnings?.missing || []

  return (
    <div className="stack" style={{ paddingBottom: 72 }}>
      <PageHeader title="Create Campaign"
                  sub={`Who to call, what to say, and when — for ${wedding?.name || 'this wedding'}`} />

      <div className="panel">
        <div className="panel-head"><h3>1. What is this call?</h3></div>
        <div className="two">
          <div>
            <label>Agent</label>
            <select value={agentId} onChange={(e) => setAgentId(e.target.value)}>
              <option value="">Select an agent…</option>
              {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
            {agent && <div className="muted" style={{ fontSize: '0.78rem', marginTop: 4 }}>
              {agent.description}
            </div>}
          </div>
          <div>
            <label>Event {agent?.requires_event && <span style={{ color: 'var(--red)' }}>*</span>}</label>
            <select value={eventId} onChange={(e) => setEventId(e.target.value)}
                    disabled={!agentId}>
              <option value="">{agent && !agent.requires_event
                ? 'No event — a logistics call' : 'Select an event…'}</option>
              {events.map((ev) => (
                <option key={ev.id} value={ev.id}>
                  {ev.name}{ev.event_date ? ` · ${ev.event_date}` : ''}
                </option>
              ))}
            </select>
            {event && <div className="muted" style={{ fontSize: '0.78rem', marginTop: 4 }}>
              {event.venue || 'No venue set'} · for {event.audience === 'all' ? 'everyone'
                : event.audience === 'groom' ? "the groom's side" : "the bride's side"}
            </div>}
          </div>
        </div>
        {!events.length && (
          <div className="muted" style={{ fontSize: '0.8rem', marginTop: 10 }}>
            This wedding has no events yet. <Link to={`/weddings/${weddingId}`}>Add one</Link> so
            the agent knows what it's calling about.
          </div>
        )}
      </div>

      <ContactUpload step={2} onImported={refresh} weddingId={weddingId} />

      <div className="panel">
        <div className="panel-head">
          <div>
            <h3>3. Guests</h3>
            {event && <div className="muted" style={{ fontSize: '0.8rem', marginTop: 3 }}>
              Pre-selected for {event.name}. Add or remove anyone before you start.
            </div>}
          </div>
          <div style={{ display: 'flex', gap: 10 }}>
            {selected.size > 0 && <button className="btn danger sm" onClick={deleteSelected}>Delete ({selected.size})</button>}
            <button className="btn ghost sm" onClick={() => { setAddErr(''); setShowAdd(true) }}>+ Add Guest</button>
          </div>
        </div>
        <ContactsTable
          selectable selected={selected} onToggle={toggle} onToggleMany={toggleMany}
          onTotal={setTotal} refreshKey={refreshKey}
        />
      </div>

      <div className="fixed-bar">
        <span className="muted"><b style={{ color: 'var(--text)' }}>{selected.size}</b> of {total} selected</span>
        <button className="btn" disabled={selected.size === 0 || !agentId || needsEvent} onClick={openConfirm}>
          Start Campaign
        </button>
      </div>

      {showStart && (
        <Modal
          title="Start Campaign" width={620}
          onClose={() => !busy && setShowStart(false)}
          footer={<>
            <button className="btn ghost" disabled={busy} onClick={() => setShowStart(false)}>Cancel</button>
            <button className="btn" disabled={busy || (fatigue?.count > 0 && !ackFatigue)} onClick={startCampaign}>
              {busy ? 'Starting…' : 'Start Campaign'}
            </button>
          </>}
        >
          {err && <div className="err" style={{ marginBottom: 12 }}>{err}</div>}

          {!!warnings.length && (
            <div style={{ background: 'var(--amber-soft)', border: '1px solid var(--amber)',
                          borderRadius: 'var(--radius-sm)', padding: '10px 12px', marginBottom: 12 }}>
              <div style={{ fontWeight: 600, fontSize: '0.85rem', color: 'var(--amber)' }}>
                Some details are missing
              </div>
              <div className="muted" style={{ fontSize: '0.79rem', marginTop: 3 }}>
                {warnings.join(', ')} — the agent will simply leave those out. Fill them in on
                the event if guests should hear them.
              </div>
            </div>
          )}

          {fatigue?.count > 0 && (
            <div style={{ background: 'var(--amber-soft)', border: '1px solid var(--amber)',
                          borderRadius: 'var(--radius-sm)', padding: '10px 12px', marginBottom: 12 }}>
              <div style={{ fontWeight: 600, fontSize: '0.85rem', color: 'var(--amber)' }}>
                {fatigue.count} of these guests were called in the last {fatigue.hours} hours
              </div>
              <div className="muted" style={{ fontSize: '0.79rem', margin: '3px 0 8px' }}>
                {fatigue.guests.slice(0, 5).map((g) => g.name || g.phone).join(', ')}
                {fatigue.count > 5 ? ` and ${fatigue.count - 5} more` : ''}.
                That may well be intentional on a wedding day.
              </div>
              <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: '0.82rem' }}>
                <input type="checkbox" checked={ackFatigue} onChange={(e) => setAckFatigue(e.target.checked)} />
                Call them anyway
              </label>
            </div>
          )}

          <div className="row">
            <label>Campaign Name</label>
            <input value={name} onChange={(e) => setName(e.target.value)}
                   placeholder="Saanth reminder" autoFocus />
          </div>
          <div className="row">
            <label>Calling</label>
            <input readOnly style={{ opacity: 0.7 }}
                   value={`${selected.size} guests · ${agent?.name || ''}${event ? ` · ${event.name}` : ''}`} />
          </div>
          <div className="two">
            <div><label>Start Date</label><input type="date" min={todayStr()} value={startDate} onChange={(e) => setStartDate(e.target.value)} /></div>
            <div><label>Start Time</label><input type="time" min={minTime} value={startTime} onChange={(e) => setStartTime(e.target.value)} /></div>
          </div>
          <div className="row" style={{ marginTop: 14 }}>
            <label>Call back if no answer (hours)</label>
            <input type="number" min="0" step="1" inputMode="numeric" value={delayH}
                   onKeyDown={blockDecimalKeys} onChange={(e) => setDelayH(toWhole(e.target.value))} />
          </div>
          <div className="two">
            <div><label>Attempts per day (1–10)</label>
              <input type="number" min="1" max="10" step="1" inputMode="numeric" value={maxDay}
                     onKeyDown={blockDecimalKeys} onChange={(e) => setMaxDay(toWhole(e.target.value))} /></div>
            <div><label>For how many days (1–10)</label>
              <input type="number" min="1" max="10" step="1" inputMode="numeric" value={days}
                     onKeyDown={blockDecimalKeys} onChange={(e) => setDays(toWhole(e.target.value))} /></div>
          </div>
          <div className="two" style={{ marginTop: 14 }}>
            <div><label>Call only after (IST)</label>
              <input type="time" value={callStart} onChange={(e) => setCallStart(e.target.value)} /></div>
            <div><label>…and before (IST)</label>
              <input type="time" value={callEnd} onChange={(e) => setCallEnd(e.target.value)} /></div>
          </div>
          <div className="row" style={{ marginTop: 6, fontSize: '0.78rem', color: 'var(--muted)' }}>
            No calls are placed outside these hours.
          </div>
        </Modal>
      )}

      {showAdd && (
        <Modal
          title="Add Guest" sub="Add a single guest to this wedding"
          onClose={() => !addBusy && setShowAdd(false)}
          footer={<>
            <button className="btn ghost" disabled={addBusy} onClick={() => setShowAdd(false)}>Cancel</button>
            <button className="btn" disabled={addBusy || !addPhone} onClick={addContact}>{addBusy ? 'Adding…' : 'Add'}</button>
          </>}
        >
          {addErr && <div className="err" style={{ marginBottom: 10 }}>{addErr}</div>}
          <div className="row"><label>Name</label><input value={addName} onChange={(e) => setAddName(e.target.value)} autoFocus /></div>
          <div className="row"><label>Phone</label><input value={addPhone} onChange={(e) => setAddPhone(e.target.value)} placeholder="9876543210 or +9198…" /></div>
        </Modal>
      )}
    </div>
  )
}

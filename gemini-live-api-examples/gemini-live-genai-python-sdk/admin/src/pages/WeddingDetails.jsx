import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import EventForm from '../components/EventForm.jsx'
import PageHeader from '../components/PageHeader.jsx'
import { useWedding } from '../wedding.jsx'

const AUDIENCE_LABEL = { all: 'Everyone', groom: "Groom's side", bride: "Bride's side" }
const AUDIENCE_PILL = { all: 'blue', groom: 'green', bride: 'amber' }

// "19:00" -> "7:00 PM". The agent speaks these as words; the grid shows them plainly.
function prettyTime(t) {
  const m = /^(\d{1,2}):(\d{2})/.exec(String(t || ''))
  if (!m) return t || '—'
  const h = Number(m[1]); const suffix = h < 12 ? 'AM' : 'PM'
  return `${((h + 11) % 12) + 1}:${m[2]} ${suffix}`
}

function prettyDate(d) {
  if (!d) return 'No date'
  const dt = new Date(`${d}T00:00:00`)
  if (Number.isNaN(dt.getTime())) return d
  return dt.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })
}

export default function WeddingDetails() {
  const { id } = useParams()
  const navigate = useNavigate()
  const { setWeddingId, refresh: refreshWeddings } = useWedding()
  const [wedding, setWedding] = useState(null)
  const [events, setEvents] = useState([])
  const [err, setErr] = useState('')
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState(null)

  const load = useCallback(async () => {
    try {
      const w = await api.get(`/weddings/${id}`)
      setWedding(w)
      setEvents(w.events || [])
      setWeddingId(Number(id))       // opening a wedding selects it everywhere
    } catch (e) { setErr(e.message) }
  }, [id]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { load() }, [load])

  async function removeEvent(ev) {
    if (!confirm(`Delete "${ev.name}"?`)) return
    try {
      await api.del(`/events/${ev.id}`)
      await load(); await refreshWeddings()
    } catch (e) { alert(e.message) }
  }

  if (err) return <div className="stack"><div className="err">{err}</div></div>
  if (!wedding) return <div className="panel"><div className="muted">Loading…</div></div>

  // Group by day so the schedule reads the way the client wrote it.
  const byDate = events.reduce((acc, e) => {
    const key = e.event_date || ''
    ;(acc[key] = acc[key] || []).push(e)
    return acc
  }, {})
  const dates = Object.keys(byDate).sort()

  return (
    <div className="stack">
      <PageHeader
        title={wedding.name}
        sub={[wedding.groom_name, wedding.bride_name].filter(Boolean).join(' & ')
             + (wedding.city ? ` · ${wedding.city}` : '')}
        back={<Link to="/weddings" className="muted" style={{ fontSize: '0.82rem' }}>← All weddings</Link>}
        actions={<>
          <button className="btn ghost" onClick={() => navigate('/create-campaign')}>Create Campaign</button>
          <button className="btn" onClick={() => { setEditing(null); setShowForm(true) }}>+ Add Event</button>
        </>}
      />

      <div style={{ display: 'grid', gap: 12, gridTemplateColumns: 'repeat(auto-fit,minmax(160px,1fr))' }}>
        <Stat label="Events" value={events.length} />
        <Stat label="Guests" value={wedding.guest_count ?? 0} />
        <Stat label="Hospitality team" value={wedding.hospitality_team || '—'} small />
        <Stat label="Contact" value={wedding.contact_phone || '—'} small />
      </div>

      {!events.length && (
        <div className="panel" style={{ textAlign: 'center', padding: 36 }}>
          <h3 style={{ marginTop: 0 }}>No events yet</h3>
          <p className="muted" style={{ maxWidth: 440, margin: '0 auto 16px' }}>
            Add each function — its time, venue, and who it's for. A campaign then picks one
            event and calls exactly the guests invited to it.
          </p>
          <button className="btn" onClick={() => { setEditing(null); setShowForm(true) }}>+ Add Event</button>
        </div>
      )}

      {dates.map((d) => (
        <div className="panel" key={d || 'undated'}>
          <div className="panel-head"><h3>{prettyDate(d)}</h3></div>
          <div className="table-scroll">
          <table className="grid">
            <thead>
              <tr><th>Function</th><th>Time</th><th>Venue</th><th>Who</th><th style={{ width: 150 }}></th></tr>
            </thead>
            <tbody>
              {byDate[d].map((ev) => (
                <tr key={ev.id}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{ev.name}</div>
                    {ev.dress_code && <div className="muted" style={{ fontSize: '0.78rem' }}>{ev.dress_code}</div>}
                  </td>
                  <td>{prettyTime(ev.start_time)}</td>
                  <td>{ev.venue || <span className="muted">Not set</span>}</td>
                  <td><span className={`pill ${AUDIENCE_PILL[ev.audience] || 'blue'}`}>
                    {AUDIENCE_LABEL[ev.audience] || ev.audience}</span></td>
                  <td style={{ textAlign: 'right' }}>
                    <button className="btn ghost sm" onClick={() => { setEditing(ev); setShowForm(true) }}>Edit</button>
                    <button className="btn danger sm" style={{ marginLeft: 6 }} onClick={() => removeEvent(ev)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </div>
      ))}

      {showForm && (
        <EventForm
          weddingId={wedding.id}
          event={editing}
          onClose={() => setShowForm(false)}
          onSaved={async () => { setShowForm(false); await load(); await refreshWeddings() }}
        />
      )}
    </div>
  )
}

function Stat({ label, value, small }) {
  return (
    <div className="card" style={{ padding: 14 }}>
      <div className="muted" style={{ fontSize: '0.74rem', textTransform: 'uppercase', letterSpacing: '.04em' }}>{label}</div>
      <div style={{ fontSize: small ? '0.92rem' : '1.5rem', fontWeight: 600, marginTop: 4 }}>{value}</div>
    </div>
  )
}

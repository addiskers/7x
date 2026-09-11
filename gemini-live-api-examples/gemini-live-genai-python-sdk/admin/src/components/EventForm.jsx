import { useState } from 'react'
import { api } from '../api.js'
import Modal from './Modal.jsx'

const AUDIENCES = [
  ['all', 'Everyone', 'Every guest at the wedding'],
  ['groom', "Groom's side", "Groom-side guests, plus anyone marked 'both' or unspecified"],
  ['bride', "Bride's side", "Bride-side guests, plus anyone marked 'both' or unspecified"],
]

const blank = {
  name: '', event_date: '', start_time: '', end_time: '', venue: '', venue_address: '',
  dress_code: '', audience: 'all', announcement: '',
}

export default function EventForm({ weddingId, event, onClose, onSaved }) {
  const [form, setForm] = useState(
    event ? { ...blank, ...Object.fromEntries(Object.keys(blank).map((k) => [k, event[k] ?? ''])) } : blank,
  )
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))

  async function save() {
    setErr(''); setBusy(true)
    try {
      if (!form.name.trim()) throw new Error('Event name is required')
      if (event) await api.patch(`/events/${event.id}`, form)
      else await api.post('/events', { ...form, wedding_id: weddingId })
      onSaved()
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  return (
    <Modal
      title={event ? 'Edit Event' : 'Add Event'}
      sub="The agent reads the time and venue aloud, so write them exactly as guests should hear them"
      width={620}
      onClose={() => !busy && onClose()}
      footer={<>
        <button className="btn ghost" disabled={busy} onClick={onClose}>Cancel</button>
        <button className="btn" disabled={busy} onClick={save}>{busy ? 'Saving…' : 'Save Event'}</button>
      </>}
    >
      {err && <div className="err" style={{ marginBottom: 12 }}>{err}</div>}

      <div className="row">
        <label>Function name</label>
        <input value={form.name} onChange={set('name')} autoFocus
               placeholder="Mehendi &amp; Haldi followed by Lunch" />
      </div>

      <div className="two">
        <div><label>Date</label><input type="date" value={form.event_date} onChange={set('event_date')} /></div>
        <div><label>Starts at</label><input type="time" value={form.start_time} onChange={set('start_time')} /></div>
      </div>

      <div className="row">
        <label>Venue</label>
        <input value={form.venue} onChange={set('venue')} placeholder="Ivory Garden &amp; Pool" />
      </div>

      <div className="row">
        <label>Dress code <span className="muted">(optional)</span></label>
        <input value={form.dress_code} onChange={set('dress_code')} placeholder="Indian formals" />
        <div className="muted" style={{ fontSize: '0.76rem', marginTop: 3 }}>
          Left blank, the agent simply doesn't mention it.
        </div>
      </div>

      <div className="row">
        <label>Who is invited</label>
        <div style={{ display: 'grid', gap: 6, marginTop: 4 }}>
          {AUDIENCES.map(([value, label, hint]) => (
            <label key={value} style={{
              display: 'flex', gap: 10, alignItems: 'flex-start', padding: '9px 11px',
              border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)', cursor: 'pointer',
              background: form.audience === value ? 'var(--green-soft)' : 'transparent',
              borderColor: form.audience === value ? 'var(--green-line)' : 'var(--border)',
            }}>
              <input type="radio" name="audience" value={value} checked={form.audience === value}
                     onChange={set('audience')} style={{ marginTop: 3 }} />
              <span>
                <span style={{ fontWeight: 600 }}>{label}</span>
                <span className="muted" style={{ display: 'block', fontSize: '0.78rem' }}>{hint}</span>
              </span>
            </label>
          ))}
        </div>
        <div className="muted" style={{ fontSize: '0.76rem', marginTop: 6 }}>
          A campaign for this event pre-selects these guests — you can still add or remove
          individuals before it starts.
        </div>
      </div>

      <div className="row">
        <label>Anything else to convey <span className="muted">(optional)</span></label>
        <textarea rows={2} value={form.announcement} onChange={set('announcement')}
                  placeholder="Lunch follows the ritual." />
      </div>
    </Modal>
  )
}

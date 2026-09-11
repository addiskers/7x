import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import Modal from '../components/Modal.jsx'
import PageHeader from '../components/PageHeader.jsx'
import { useWedding } from '../wedding.jsx'

// Fields the agent speaks aloud. Grouped so it's obvious which ones reach a guest's ear.
const SPOKEN = [
  ['hospitality_team', 'Hospitality team name', '"calling from …" — say it the way it should sound'],
  ['placard_text', 'Placard text', 'What the pickup team holds at the airport gate'],
  ['contact_name', 'Contact person', 'Who guests are told to call'],
  ['contact_phone', 'Contact number', 'Read out digit by digit on the call'],
]

const DETAILS = [
  ['groom_name', 'Groom'], ['bride_name', 'Bride'],
  ['groom_side_family', "Groom's family"], ['bride_side_family', "Bride's family"],
  ['city', 'City'],
]

const blank = {
  name: '', groom_name: '', bride_name: '', groom_side_family: '', bride_side_family: '',
  city: '', start_date: '', end_date: '', hospitality_team: '', placard_text: '',
  contact_name: '', contact_phone: '',
}

export default function Weddings() {
  const navigate = useNavigate()
  const { weddings, weddingId, setWeddingId, refresh, loading } = useWedding()
  const [show, setShow] = useState(false)
  const [editing, setEditing] = useState(null)
  const [form, setForm] = useState(blank)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  function openNew() {
    setEditing(null); setForm(blank); setErr(''); setShow(true)
  }
  function openEdit(w) {
    setEditing(w)
    setForm({ ...blank, ...Object.fromEntries(Object.keys(blank).map((k) => [k, w[k] ?? ''])) })
    setErr(''); setShow(true)
  }
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))

  async function save() {
    setErr(''); setBusy(true)
    try {
      if (!form.name.trim()) throw new Error('Wedding name is required')
      const saved = editing
        ? await api.patch(`/weddings/${editing.id}`, form)
        : await api.post('/weddings', form)
      setShow(false)
      const items = await refresh()
      if (!editing) setWeddingId(saved.id)
      if (!editing && items) navigate(`/weddings/${saved.id}`)
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  async function remove(w) {
    if (!confirm(`Delete "${w.name}"? Its events, guests and agents go with it.`)) return
    try {
      await api.del(`/weddings/${w.id}`)
      await refresh()
    } catch (e) { alert(e.message) }
  }

  return (
    <div className="stack">
      <PageHeader
        title="Weddings"
        sub="Each wedding holds its own events, guests and agents"
        actions={<button className="btn" onClick={openNew}>+ New Wedding</button>}
      />

      {loading && <div className="panel"><div className="muted">Loading…</div></div>}

      {!loading && !weddings.length && (
        <div className="panel" style={{ textAlign: 'center', padding: 40 }}>
          <h3 style={{ marginTop: 0 }}>No weddings yet</h3>
          <p className="muted" style={{ maxWidth: 460, margin: '0 auto 18px' }}>
            Start by creating the wedding. You'll then add its events (Mehendi, Saanth,
            the ceremony) and upload the guest list.
          </p>
          <button className="btn" onClick={openNew}>+ New Wedding</button>
        </div>
      )}

      <div style={{ display: 'grid', gap: 14, gridTemplateColumns: 'repeat(auto-fill,minmax(320px,1fr))' }}>
        {weddings.map((w) => (
          <div key={w.id} className="panel" style={{
            padding: 18, cursor: 'pointer',
            borderColor: w.id === weddingId ? 'var(--green-line)' : undefined,
          }} onClick={() => navigate(`/weddings/${w.id}`)}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
              <div>
                <h3 style={{ margin: 0 }}>{w.name}</h3>
                <div className="muted" style={{ fontSize: '0.82rem', marginTop: 3 }}>
                  {[w.groom_name, w.bride_name].filter(Boolean).join(' & ') || '—'}
                  {w.city ? ` · ${w.city}` : ''}
                </div>
              </div>
              {w.id === weddingId && <span className="pill green">Selected</span>}
            </div>
            <div className="muted" style={{ fontSize: '0.8rem', marginTop: 12 }}>
              {w.event_count || 0} event{w.event_count === 1 ? '' : 's'}
              {w.start_date ? ` · from ${w.start_date}` : ''}
            </div>
            <div style={{ display: 'flex', gap: 8, marginTop: 14 }} onClick={(e) => e.stopPropagation()}>
              {w.id !== weddingId && (
                <button className="btn ghost sm" onClick={() => setWeddingId(w.id)}>Select</button>
              )}
              <button className="btn ghost sm" onClick={() => openEdit(w)}>Edit</button>
              <button className="btn danger sm" onClick={() => remove(w)}>Delete</button>
            </div>
          </div>
        ))}
      </div>

      {show && (
        <Modal
          title={editing ? 'Edit Wedding' : 'New Wedding'}
          sub="The spoken fields below are read out on calls — write them the way they should sound"
          width={680}
          onClose={() => !busy && setShow(false)}
          footer={<>
            <button className="btn ghost" disabled={busy} onClick={() => setShow(false)}>Cancel</button>
            <button className="btn" disabled={busy} onClick={save}>{busy ? 'Saving…' : 'Save'}</button>
          </>}
        >
          {err && <div className="err" style={{ marginBottom: 12 }}>{err}</div>}
          <div className="row">
            <label>Wedding name</label>
            <input value={form.name} onChange={set('name')} placeholder="Anant & Manya" autoFocus />
          </div>
          <div className="two">
            {DETAILS.slice(0, 2).map(([k, label]) => (
              <div key={k}><label>{label}</label><input value={form[k]} onChange={set(k)} /></div>
            ))}
          </div>
          <div className="two">
            {DETAILS.slice(2, 4).map(([k, label]) => (
              <div key={k}>
                <label>{label}</label>
                <input value={form[k]} onChange={set(k)} placeholder={k.startsWith('groom') ? 'Kapoor Family' : 'Chopra Family'} />
              </div>
            ))}
          </div>
          <div className="two">
            <div><label>City</label><input value={form.city} onChange={set('city')} /></div>
            <div />
          </div>
          <div className="two">
            <div><label>First function</label><input type="date" value={form.start_date} onChange={set('start_date')} /></div>
            <div><label>Last function</label><input type="date" value={form.end_date} onChange={set('end_date')} /></div>
          </div>

          <div style={{ margin: '18px 0 8px', fontSize: '0.78rem', letterSpacing: '.04em',
                        textTransform: 'uppercase', color: 'var(--secondary)' }}>
            Spoken on calls
          </div>
          {SPOKEN.map(([k, label, hint]) => (
            <div className="row" key={k}>
              <label>{label}</label>
              <input value={form[k]} onChange={set(k)} />
              <div className="muted" style={{ fontSize: '0.76rem', marginTop: 3 }}>{hint}</div>
            </div>
          ))}
        </Modal>
      )}
    </div>
  )
}

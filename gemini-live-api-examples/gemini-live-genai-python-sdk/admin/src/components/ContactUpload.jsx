import { useRef, useState } from 'react'
import { uploadFile, downloadFile } from '../api.js'
import { IconDownload } from './icons.jsx'

// "Upload Guests" card — drag/drop or click, plus a Download Sample button.
export default function ContactUpload({ onImported, step, weddingId, weddingPicker }) {
  const inputRef = useRef(null)
  const [drag, setDrag] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const [err, setErr] = useState('')

  async function handleFile(file) {
    if (!file) return
    if (!weddingId) { setErr('Pick the wedding this guest list belongs to first.'); return }
    setBusy(true); setErr(''); setMsg(null)
    try {
      const r = await uploadFile('/contacts/import', file, { wedding_id: weddingId })
      setMsg(r)
      onImported?.(r)
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  function onDrop(e) {
    e.preventDefault(); setDrag(false)
    const f = e.dataTransfer.files?.[0]
    if (f) handleFile(f)
  }

  async function sample() {
    try { await downloadFile('/contacts/template', 'contacts_template.xlsx') }
    catch (e) { setErr(e.message) }
  }

  return (
    <div className="card">
      <div className="panel-head">
        <h3>{step ? `${step}. ` : ''}Upload Contacts</h3>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          {weddingPicker}
          <button className="btn ghost sm" onClick={sample} style={{ display: 'flex', gap: 6 }}><IconDownload /> Download Sample</button>
        </div>
      </div>
      <div
        className={`dropzone ${drag ? 'drag' : ''}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setDrag(true) }}
        onDragLeave={() => setDrag(false)}
        onDrop={onDrop}
      >
        <input ref={inputRef} type="file" accept=".xls,.xlsx,.csv" hidden
               onChange={(e) => handleFile(e.target.files?.[0])} />
        <div className="big">{busy ? 'Uploading…' : 'Drop your .xlsx / .csv here'}</div>
        <div className="hint">or click to browse — columns: Name, Phone</div>
      </div>
      {err && <div style={{ color: '#fca5a5', fontSize: '0.82rem', marginTop: 10 }}>{err}</div>}
      {msg && (
        <div style={{ fontSize: '0.82rem', marginTop: 12, color: 'var(--secondary)' }}>
          Read {msg.rows_read} · <b style={{ color: 'var(--green)' }}>{msg.added} added</b> · {msg.updated} updated
          {msg.invalid ? ` · ${msg.invalid} invalid` : ''}{msg.rejected ? ` · ${msg.rejected} rejected` : ''}
        </div>
      )}
    </div>
  )
}

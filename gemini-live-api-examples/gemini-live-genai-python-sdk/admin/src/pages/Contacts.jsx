import { useEffect, useState } from 'react'
import { api } from '../api.js'
import ContactUpload from '../components/ContactUpload.jsx'
import ContactsTable from '../components/ContactsTable.jsx'
import Modal from '../components/Modal.jsx'
import PageHeader from '../components/PageHeader.jsx'
import { useWedding } from '../wedding.jsx'

// Guests belong to a wedding. This page used to upload and add guests with NO wedding, so
// they were filed under "no wedding" (id 0) and the list — scoped to the selected wedding —
// showed nothing: "1 updated … No guests yet". Every write here now names its wedding,
// chosen in the popup / above the drop zone, defaulting to the wedding selected at the top.
export default function Contacts() {
  const { weddings, weddingId: globalWeddingId } = useWedding()
  const [weddingId, setWeddingId] = useState(globalWeddingId || '')
  const [refreshKey, setRefreshKey] = useState(0)
  const [selected, setSelected] = useState(new Set())
  const [showAdd, setShowAdd] = useState(false)
  const [addName, setAddName] = useState('')
  const [addPhone, setAddPhone] = useState('')
  const [addErr, setAddErr] = useState('')
  const [busy, setBusy] = useState(false)

  // Follow the top-bar wedding until the operator picks a different one here.
  useEffect(() => { if (globalWeddingId) setWeddingId(globalWeddingId) }, [globalWeddingId])

  const wedding = weddings.find((w) => w.id === Number(weddingId))
  const refresh = () => { setSelected(new Set()); setRefreshKey((k) => k + 1) }

  function toggle(id) {
    setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  }
  function toggleMany(ids, checked) {
    setSelected((s) => { const n = new Set(s); ids.forEach((id) => checked ? n.add(id) : n.delete(id)); return n })
  }

  async function addContact() {
    if (!weddingId) { setAddErr('Pick the wedding this guest belongs to.'); return }
    setBusy(true); setAddErr('')
    try {
      await api.post('/contacts', { name: addName, phone: addPhone, wedding_id: Number(weddingId) })
      setShowAdd(false); setAddName(''); setAddPhone(''); refresh()
    } catch (e) { setAddErr(e.message) } finally { setBusy(false) }
  }

  async function deleteSelected() {
    if (!selected.size) return
    if (!confirm(`Delete ${selected.size} contact(s)?`)) return
    await api.post('/contacts/delete', { ids: [...selected] })
    refresh()
  }

  const weddingPicker = (
    <select value={weddingId || ''} onChange={(e) => setWeddingId(e.target.value)}>
      <option value="">Select a wedding…</option>
      {weddings.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
    </select>
  )

  return (
    <div className="stack">
      <PageHeader
        title="Contacts"
        sub={wedding ? `Guest list for ${wedding.name}` : 'Guest lists, one per wedding'}
        actions={<>
          {selected.size > 0 && <button className="btn danger" onClick={deleteSelected}>Delete ({selected.size})</button>}
          <button className="btn" onClick={() => setShowAdd(true)}>+ Add Contact</button>
        </>}
      />

      <ContactUpload onImported={refresh} weddingId={weddingId ? Number(weddingId) : null}
                     weddingPicker={weddingPicker} />

      <div className="panel">
        <ContactsTable
          selectable
          selected={selected}
          onToggle={toggle}
          onToggleMany={toggleMany}
          refreshKey={refreshKey}
          weddingId={weddingId ? Number(weddingId) : null}
        />
      </div>

      {showAdd && (
        <Modal
          title="Add Contact"
          sub="A single guest, added to one wedding's list"
          onClose={() => setShowAdd(false)}
          footer={<>
            <button className="btn ghost" onClick={() => setShowAdd(false)}>Cancel</button>
            <button className="btn" disabled={busy || !addPhone || !weddingId} onClick={addContact}>{busy ? 'Adding…' : 'Add'}</button>
          </>}
        >
          {addErr && <div className="err" style={{ color: '#fca5a5', marginBottom: 10 }}>{addErr}</div>}
          <div className="row"><label>Wedding</label>{weddingPicker}</div>
          <div className="row"><label>Name</label><input value={addName} onChange={(e) => setAddName(e.target.value)} autoFocus /></div>
          <div className="row"><label>Phone</label><input value={addPhone} onChange={(e) => setAddPhone(e.target.value)} placeholder="9876543210 or +9198…" /></div>
        </Modal>
      )}
    </div>
  )
}

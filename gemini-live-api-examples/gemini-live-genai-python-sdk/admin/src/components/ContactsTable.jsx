import { useEffect, useMemo, useState } from 'react'
import { api, qs } from '../api.js'
import { IconSearch } from './icons.jsx'
import RemarkCell from './RemarkCell.jsx'
import { useWedding } from '../wedding.jsx'

const PAGE = 25
const SIDE_PILL = { groom: 'green', bride: 'amber', both: 'blue' }

// Reusable contacts table with search / source / status / date filters, sortable
// headers, pagination, and optional row selection. Selection is controlled by the
// parent (a Set of ids) so Create-Campaign can read the chosen subset.
export default function ContactsTable({
  selectable = false,
  selected,            // Set<number>
  onToggle,            // (id) => void
  onToggleMany,        // (ids, checked) => void
  refreshKey = 0,
  onTotal,             // (total) => void
}) {
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  const { weddingId } = useWedding()
  const [q, setQ] = useState('')
  const [source, setSource] = useState('')
  const [status, setStatus] = useState('')
  const [side, setSide] = useState('')
  const [sort, setSort] = useState({ key: 'created_at', dir: 'desc' })
  const [page, setPage] = useState(0)

  // Guests belong to a wedding, so the grid is always scoped to the selected one.
  const filters = useMemo(() => ({
    q, source, status, side, wedding_id: weddingId || '',
    sort: sort.key, dir: sort.dir, limit: PAGE, offset: page * PAGE,
  }), [q, source, status, side, weddingId, sort, page])

  useEffect(() => {
    let cancel = false
    setLoading(true)
    api.get(`/contacts${qs(filters)}`)
      .then((d) => { if (!cancel) { setItems(d.items || []); setTotal(d.total || 0); onTotal?.(d.total || 0); setErr('') } })
      .catch((e) => { if (!cancel) setErr(e.message) })
      .finally(() => { if (!cancel) setLoading(false) })
    return () => { cancel = true }
  }, [filters, refreshKey])

  function th(key, label, extra = '') {
    const active = sort.key === key
    return (
      <th className={extra} onClick={() => { setPage(0); setSort((s) => ({ key, dir: s.key === key && s.dir === 'asc' ? 'desc' : 'asc' })) }}>
        {label}{active && <span className="arrow">{sort.dir === 'asc' ? '▴' : '▾'}</span>}
      </th>
    )
  }

  function reset() { setQ(''); setSource(''); setStatus(''); setSide(''); setPage(0) }

  const pageIds = items.map((c) => c.id)
  const allOnPageSelected = selectable && pageIds.length > 0 && pageIds.every((id) => selected?.has(id))

  return (
    <div>
      <div className="toolbar">
        <div className="search">
          <span className="ic"><IconSearch /></span>
          <input placeholder="Search name / phone…" value={q} onChange={(e) => { setPage(0); setQ(e.target.value) }} />
        </div>
        <select value={source} onChange={(e) => { setPage(0); setSource(e.target.value) }}>
          <option value="">All Sources</option>
          <option value="upload">Upload</option>
          <option value="manual">Manual</option>
        </select>
        <select value={status} onChange={(e) => { setPage(0); setStatus(e.target.value) }}>
          <option value="">All Status</option>
          <option value="valid">Valid</option>
          <option value="invalid">Invalid</option>
        </select>
        <select value={side} onChange={(e) => { setPage(0); setSide(e.target.value) }}>
          <option value="">Both sides</option>
          <option value="groom">Groom's side</option>
          <option value="bride">Bride's side</option>
          <option value="both">Both families</option>
        </select>
        <button className="btn ghost sm" onClick={reset}>Reset</button>
        {selectable && selected?.size > 0 && (
          <span className="muted" style={{ marginLeft: 'auto' }}>{selected.size} of {total} selected</span>
        )}
      </div>

      {err && <div style={{ color: '#fca5a5', fontSize: '0.8rem', marginBottom: 10 }}>{err}</div>}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {selectable && (
                <th className="no-sort" style={{ width: 36 }}>
                  <input type="checkbox" checked={allOnPageSelected} onChange={(e) => onToggleMany?.(pageIds, e.target.checked)} />
                </th>
              )}
              {th('name', 'Name')}
              {th('phone', 'Phone Number')}
              {th('side', 'Side')}
              <th className="no-sort">Travel</th>
              <th className="no-sort">Stay</th>
              {th('status', 'Status')}
              <th className="no-sort">Remark</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={selectable ? 8 : 7} className="empty">Loading…</td></tr>
            ) : items.length === 0 ? (
              <tr><td colSpan={selectable ? 8 : 7} className="empty">
                No guests yet. Upload the guest list or add someone.
              </td></tr>
            ) : items.map((c) => (
              <tr key={c.id} className={selectable ? 'clickable' : ''} onClick={selectable ? () => onToggle?.(c.id) : undefined}>
                {selectable && (
                  <td onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" checked={selected?.has(c.id) || false} onChange={() => onToggle?.(c.id)} />
                  </td>
                )}
                <td>{c.name || <span className="muted">—</span>}</td>
                <td style={{ fontFamily: 'var(--mono)' }}>{c.phone}</td>
                <td>
                  {c.side
                    ? <span className={`pill ${SIDE_PILL[c.side] || 'src'}`}>{c.side}</span>
                    : <span className="muted">—</span>}
                </td>
                <td className="muted" style={{ fontSize: '0.8rem' }}>
                  {[c.transport_mode, c.transport_number].filter(Boolean).join(' ') || '—'}
                  {c.arrival_at ? <div style={{ fontSize: '0.75rem' }}>{c.arrival_at}</div> : null}
                </td>
                <td className="muted" style={{ fontSize: '0.8rem' }}>
                  {c.hotel || '—'}{c.room_number ? ` · ${c.room_number}` : ''}
                </td>
                <td><span className={`pill ${c.status}`}>{c.status}</span></td>
                <td onClick={(e) => e.stopPropagation()}>
                  <RemarkCell value={c.remark} onSave={(v) => api.patch(`/contacts/${c.id}/remark`, { remark: v })} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="pager">
        <span>{total} total</span>
        <button disabled={page === 0} onClick={() => setPage((p) => p - 1)}>Prev</button>
        <span>Page {page + 1} / {Math.max(1, Math.ceil(total / PAGE))}</span>
        <button disabled={(page + 1) * PAGE >= total} onClick={() => setPage((p) => p + 1)}>Next</button>
      </div>
    </div>
  )
}

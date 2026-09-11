// Click a placeholder to drop it into the prompt at the cursor. The list comes from the
// API (prompt_render.KNOWN_PLACEHOLDERS), so the palette and the save-time validator can
// never disagree about what is spellable.
const GROUPS = [
  ['The event', ['event_name', 'event_time', 'event_date_spoken', 'event_end_time', 'venue',
                 'venue_address', 'dress_code', 'announcement', 'when_phrase', 'days_until']],
  ['The guest', ['guest_name', 'guest_full_name', 'side_phrase', 'side', 'dietary', 'hotel',
                 'room_number', 'guest_count', 'transport_mode', 'transport_number',
                 'flight_number', 'train_number', 'arrival_time', 'departure_time']],
  ['The wedding', ['hospitality_team', 'placard_text', 'contact_name', 'contact_phone',
                   'groom_name', 'bride_name', 'groom_side_family', 'bride_side_family',
                   'wedding_name', 'wedding_city']],
  ['Today', ['today_spoken', 'now_time']],
]

export default function PlaceholderPalette({ known, onInsert }) {
  const allowed = new Set(known || [])
  return (
    <div>
      <div className="muted" style={{ fontSize: '0.78rem', marginBottom: 10 }}>
        Click to insert. Anything with no value on the day is left out of the sentence
        rather than spoken as a gap.
      </div>
      {GROUPS.map(([label, keys]) => {
        const usable = keys.filter((k) => allowed.has(k))
        if (!usable.length) return null
        return (
          <div key={label} style={{ marginBottom: 12 }}>
            <div style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '.05em',
                          color: 'var(--secondary)', marginBottom: 5 }}>{label}</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
              {usable.map((k) => (
                <button key={k} type="button" className="btn ghost sm"
                        style={{ fontFamily: 'var(--mono)', fontSize: '0.72rem', padding: '3px 7px' }}
                        onClick={() => onInsert(`{${k}}`)} title={`Insert {${k}}`}>
                  {k}
                </button>
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}

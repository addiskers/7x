import { createContext, useContext, useCallback, useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import { useAuth } from './auth.jsx'

// The wedding every other screen is scoped to. Events, guests and campaigns all belong to
// one wedding, so the choice lives here rather than being passed down through every page.
// Persisted so a reload doesn't dump the operator back to "no wedding selected".
const KEY = 'sevenx_wedding_id'
const WeddingCtx = createContext(null)

export function WeddingProvider({ children }) {
  const { user } = useAuth()
  const [weddings, setWeddings] = useState([])
  const [weddingId, setWeddingId] = useState(() => {
    const raw = localStorage.getItem(KEY)
    return raw ? Number(raw) : null
  })
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    if (!user) { setWeddings([]); setLoading(false); return }
    setLoading(true)
    try {
      const r = await api.get('/weddings')
      setWeddings(r.items || [])
      return r.items || []
    } catch {
      setWeddings([])
      return []
    } finally { setLoading(false) }
  }, [user])

  useEffect(() => { refresh() }, [refresh])

  // Keep the selection valid: fall back to the first wedding if the stored one is gone.
  useEffect(() => {
    if (loading) return
    if (!weddings.length) { setWeddingId(null); return }
    if (!weddings.some((w) => w.id === weddingId)) setWeddingId(weddings[0].id)
  }, [weddings, loading]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (weddingId) localStorage.setItem(KEY, String(weddingId))
    else localStorage.removeItem(KEY)
  }, [weddingId])

  const wedding = useMemo(
    () => weddings.find((w) => w.id === weddingId) || null,
    [weddings, weddingId],
  )

  const value = useMemo(
    () => ({ weddings, wedding, weddingId, setWeddingId, refresh, loading }),
    [weddings, wedding, weddingId, refresh, loading],
  )
  return <WeddingCtx.Provider value={value}>{children}</WeddingCtx.Provider>
}

export function useWedding() {
  return useContext(WeddingCtx) || {
    weddings: [], wedding: null, weddingId: null,
    setWeddingId: () => {}, refresh: () => {}, loading: false,
  }
}

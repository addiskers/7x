import { createContext, useContext, useEffect, useState } from 'react'
import { api, getToken, setToken } from './api.js'

const AuthCtx = createContext(null)

// Server-side menu config, sent with /login and /me: which tabs this user may not see, and
// (for the super admin) which tabs the CLIENT may not see, so the sidebar can tag them.
const NO_UI = { hidden_pages: [], client_hidden_pages: [], superadmin: false }

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [ui, setUi] = useState(NO_UI)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    let cancelled = false
    async function boot() {
      if (!getToken()) { setReady(true); return }
      try {
        const r = await api.get('/me')
        if (!cancelled) { setUser(r.user); setUi(r.ui || NO_UI) }
      } catch {
        setToken('')
      } finally {
        if (!cancelled) setReady(true)
      }
    }
    boot()
    return () => { cancelled = true }
  }, [])

  async function login(username, password) {
    const r = await api.post('/login', { username, password })
    setToken(r.token)
    setUser(r.user)
    setUi(r.ui || NO_UI)
    return r.user
  }

  function logout() {
    setToken('')
    setUser(null)
    setUi(NO_UI)
  }

  // Re-read /me after the super admin changes the client's menu, so the sidebar follows at once.
  async function refresh() {
    try { const r = await api.get('/me'); setUser(r.user); setUi(r.ui || NO_UI) } catch { /* keep what we have */ }
  }

  const hidden = new Set(ui?.hidden_pages || [])
  const clientHidden = new Set(ui?.client_hidden_pages || [])
  return (
    <AuthCtx.Provider value={{
      user, ready, login, logout, refresh, ui,
      isAdmin: user?.role === 'eo_admin',
      isSuperadmin: !!ui?.superadmin,
      clientHidden,
      isHidden: (page) => !!page && hidden.has(page),
    }}>
      {children}
    </AuthCtx.Provider>
  )
}

export function useAuth() {
  return useContext(AuthCtx)
}

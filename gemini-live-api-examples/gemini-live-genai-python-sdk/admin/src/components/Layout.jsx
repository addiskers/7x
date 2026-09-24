import { useEffect, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import { useWedding } from '../wedding.jsx'
import {
  IconDashboard, IconCampaigns, IconClock, IconUsers, IconSettings, IconUser,
  IconLogout, IconRings, IconAgent, IconLogs,
} from './icons.jsx'

// Call Logs lives on the Dashboard; guests live inside Create Campaign, which is itself
// reached from Campaigns ("New campaign") rather than having its own sidebar entry.
//
// `page` is the key the server uses in UI_PAGES / hidden_pages, so the super admin's
// show-hide toggles drive this menu. Entries without a `page` can never be hidden.
// Superadmin (role eo_admin) sees everything; Admin (eo_agent) is the CLIENT-facing role.
// Note the UI labels invert the code names: eo_admin renders as "Superadmin".
export const ADMIN_NAV = [
  { to: '/', label: 'Dashboard', icon: IconDashboard, end: true },
  { to: '/weddings', label: 'Weddings', icon: IconRings, page: 'weddings' },
  { to: '/agents', label: 'Agents', icon: IconAgent, page: 'agents', adminOnly: true },
  { to: '/campaigns', label: 'Campaigns', icon: IconCampaigns, page: 'campaigns' },
  { to: '/scheduler', label: 'Scheduler', icon: IconClock, page: 'scheduler' },
  { to: '/subscription', label: 'Subscription', icon: IconLogs, page: 'subscription' },
  { to: '/users', label: 'Users', icon: IconUsers, page: 'users', adminOnly: true },
  { to: '/settings', label: 'Settings', icon: IconSettings, page: 'settings', adminOnly: true },
  { to: '/superadmin', label: 'Super admin', icon: IconSettings, superOnly: true },
  { to: '/profile', label: 'My Profile', icon: IconUser, agentOnly: true },
]

function initials(name, username) {
  const s = (name || username || '7x').trim()
  const parts = s.split(/\s+/)
  return ((parts[0]?.[0] || '') + (parts[1]?.[0] || '')).toUpperCase() || '7X'
}

export default function Layout() {
  const { user, isAdmin, isSuperadmin, logout } = useAuth()
  const { weddings, weddingId, setWeddingId } = useWedding()
  const navigate = useNavigate()
  const [hidden, setHidden] = useState([])

  // Which tabs the client may see. The super admin sets this; until it loads we show
  // nothing extra rather than flashing a tab the client is not meant to have.
  useEffect(() => {
    if (isSuperadmin) { setHidden([]); return }
    let alive = true
    api.get('/ui-pages').then((r) => { if (alive) setHidden(r.hidden_pages || []) }).catch(() => {})
    return () => { alive = false }
  }, [isSuperadmin])

  const nav = ADMIN_NAV.filter((n) => {
    if (n.superOnly) return isSuperadmin
    if (n.adminOnly) return isAdmin
    if (n.agentOnly) return !isAdmin
    if (n.page && !isSuperadmin && hidden.includes(n.page)) return false
    return true
  })

  function doLogout() {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="logo">7x</div>
          <div>
            <div className="name">7x</div>
            <div className="sub">Event Calling</div>
          </div>
        </div>
        <nav className="nav">
          {nav.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => (isActive ? 'active' : '')}>
              <n.icon />
              <span>{n.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <button className="btn ghost" style={{ width: '100%', display: 'flex', gap: 8, justifyContent: 'center' }} onClick={doLogout}>
            <IconLogout /> Sign out
          </button>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div id="topbar-title" />
          <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginLeft: 'auto' }}>
            {/* Which wedding everything else is scoped to. */}
            {weddings.length > 0 && (
              <select
                value={weddingId || ''}
                onChange={(e) => setWeddingId(Number(e.target.value))}
                style={{ minWidth: 190, height: 34, fontSize: '0.83rem' }}
                title="The wedding every screen is scoped to"
              >
                {weddings.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
              </select>
            )}
            <div className="userchip">
              <div style={{ textAlign: 'right' }}>
                <div style={{ fontSize: '0.82rem', fontWeight: 600 }}>{user?.name || user?.username}</div>
                <div className="page-sub">{isAdmin ? 'Superadmin' : 'Admin'}</div>
              </div>
              <div className="avatar">{initials(user?.name, user?.username)}</div>
            </div>
          </div>
        </header>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}

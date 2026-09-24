import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth.jsx'
import { useWedding } from '../wedding.jsx'
import {
  IconDashboard, IconCampaigns, IconClock, IconUsers, IconSettings, IconUser, IconContacts,
  IconLogout, IconRings, IconAgent, IconLogs, IconAudit, IconAlert,
} from './icons.jsx'

// `page` is the key the super admin (or EO_HIDDEN_PAGES) uses to take an entry off the
// client's menu; the super admin sees every entry, with a "hidden" tag on the ones the client
// does not get. `adminOnly` entries also need the eo_admin role (their endpoints refuse
// anyone else), `superOnly` exist only for EO_SUPERADMIN_USERS, `agentOnly` only for eo_agent.
// Create Campaign has no entry of its own: it opens from Campaigns ("+ Create Campaign").
export const ADMIN_NAV = [
  { to: '/', label: 'Dashboard', icon: IconDashboard, end: true },
  { to: '/weddings', label: 'Weddings', icon: IconRings, page: 'weddings' },
  { to: '/campaigns', label: 'Campaigns', icon: IconCampaigns, page: 'campaigns' },
  { to: '/contacts', label: 'Contacts', icon: IconContacts, page: 'contacts' },
  { to: '/scheduler', label: 'Scheduler', icon: IconClock, page: 'scheduler' },
  { to: '/agents', label: 'Agents', icon: IconAgent, page: 'agents', adminOnly: true },
  { to: '/call-logs', label: 'Call Logs', icon: IconLogs, page: 'call-logs' },
  { to: '/users', label: 'Users', icon: IconUsers, page: 'users', adminOnly: true },
  { to: '/settings', label: 'Settings', icon: IconSettings, page: 'settings', adminOnly: true },
  { to: '/audit', label: 'Audit Log', icon: IconAudit, page: 'audit', adminOnly: true },
  { to: '/subscription', label: 'Subscription', icon: IconClock, page: 'subscription' },
  { to: '/superadmin', label: 'Super admin', icon: IconAlert, superOnly: true },
  { to: '/profile', label: 'My Profile', icon: IconUser, agentOnly: true },
]

function initials(name, username) {
  const s = (name || username || '7x').trim()
  const parts = s.split(/\s+/)
  return ((parts[0]?.[0] || '') + (parts[1]?.[0] || '')).toUpperCase() || '7X'
}

export default function Layout() {
  const { user, isAdmin, isSuperadmin, isHidden, clientHidden, logout } = useAuth()
  const { weddings, weddingId, setWeddingId } = useWedding()
  const navigate = useNavigate()
  const location = useLocation()
  const [open, setOpen] = useState(false)          // the drawer on small screens

  useEffect(() => { setOpen(false) }, [location.pathname])   // navigating closes the drawer

  const nav = ADMIN_NAV.filter((n) => {
    if (n.superOnly) return isSuperadmin
    if (n.adminOnly && !isAdmin) return false
    if (n.agentOnly) return !isAdmin
    return !isHidden(n.page)
  })

  function doLogout() {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="shell">
      {open && <div className="drawer-backdrop" onClick={() => setOpen(false)} />}
      <aside className={`sidebar${open ? ' open' : ''}`}>
        <div className="brand">
          <div className="logo">7x</div>
          <div>
            <div className="name">7x</div>
            <div className="sub">Event Calling</div>
          </div>
        </div>
        <nav className="nav">
          {nav.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => (isActive ? 'active' : '')} onClick={() => setOpen(false)}>
              <n.icon />
              <span>{n.label}</span>
              {isSuperadmin && n.page && clientHidden.has(n.page) && (
                <span className="nav-tag" title="The client's admins do not see this page">hidden</span>
              )}
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
          <button className="menu-btn" aria-label="Menu" onClick={() => setOpen((o) => !o)}>☰</button>
          <div id="topbar-title" />
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginLeft: 'auto', minWidth: 0 }}>
            {/* Which wedding everything else is scoped to. */}
            {weddings.length > 0 && (
              <select
                className="wedding-picker"
                value={weddingId || ''}
                onChange={(e) => setWeddingId(Number(e.target.value))}
                title="The wedding every screen is scoped to"
              >
                {weddings.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
              </select>
            )}
            <div className="userchip">
              <div className="who" style={{ textAlign: 'right' }}>
                <div style={{ fontSize: '0.82rem', fontWeight: 600 }}>{user?.name || user?.username}</div>
                <div className="page-sub">{isSuperadmin ? 'Super admin' : isAdmin ? 'Admin' : 'Staff'}</div>
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

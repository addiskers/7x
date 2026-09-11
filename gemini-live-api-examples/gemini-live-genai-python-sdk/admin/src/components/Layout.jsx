import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth.jsx'
import { useWedding } from '../wedding.jsx'
import {
  IconDashboard, IconPlus, IconCampaigns, IconClock, IconUsers, IconSettings, IconUser,
  IconLogout, IconRings, IconAgent,
} from './icons.jsx'

// Call Logs lives on the Dashboard; guests live inside Create Campaign.
const FULL_NAV = [
  { to: '/', label: 'Dashboard', icon: IconDashboard, end: true },
  { to: '/weddings', label: 'Weddings', icon: IconRings },
  { to: '/agents', label: 'Agents', icon: IconAgent },
  { to: '/create-campaign', label: 'Create Campaign', icon: IconPlus },
  { to: '/campaigns', label: 'Campaigns', icon: IconCampaigns },
  { to: '/scheduler', label: 'Scheduler', icon: IconClock },
  { to: '/users', label: 'Users', icon: IconUsers, adminOnly: true },
  { to: '/settings', label: 'Settings', icon: IconSettings, adminOnly: true },
]

const AGENT_NAV = [
  { to: '/', label: 'Dashboard', icon: IconDashboard, end: true },
  { to: '/weddings', label: 'Weddings', icon: IconRings },
  { to: '/agents', label: 'Agents', icon: IconAgent },
  { to: '/create-campaign', label: 'Create Campaign', icon: IconPlus },
  { to: '/campaigns', label: 'Campaigns', icon: IconCampaigns },
  { to: '/scheduler', label: 'Scheduler', icon: IconClock },
  { to: '/profile', label: 'My Profile', icon: IconUser },
]

function initials(name, username) {
  const s = (name || username || '7x').trim()
  const parts = s.split(/\s+/)
  return ((parts[0]?.[0] || '') + (parts[1]?.[0] || '')).toUpperCase() || '7X'
}

export default function Layout() {
  const { user, isAdmin, logout } = useAuth()
  const { weddings, weddingId, setWeddingId } = useWedding()
  const navigate = useNavigate()
  const nav = isAdmin ? FULL_NAV : AGENT_NAV

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

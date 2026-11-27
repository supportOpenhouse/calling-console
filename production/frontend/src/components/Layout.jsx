import { useEffect, useState } from 'react';
import { NavLink, Outlet, useMatch, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext.jsx';
import { useTheme } from '../contexts/ThemeContext.jsx';
import { homeDept, rememberDept, userDepts } from '../utils/format.js';
import {
  IconTasks, IconLogs, IconPipeline, IconFollowUp, IconDialer, IconPhone, IconTicketStub,
  IconUsers, IconSettings, IconLeads, IconReport, IconQualified, IconBook, IconSun, IconMoon, IconMenu, IconLogout, IconChevron,
} from './icons.jsx';
import { TitleSlot } from './ui.jsx';
import LiveNotices from './LiveNotices.jsx';

function initials(name, email) {
  const s = (name || (email || '').split('@')[0] || '').trim();
  const parts = s.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return s.slice(0, 2).toUpperCase() || '?';
}

const cap = (s) => s[0].toUpperCase() + s.slice(1);


export default function Layout() {
  const { user, logout } = useAuth();
  const { theme, toggle } = useTheme();
  const nav = useNavigate();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('cc_sidebar_collapsed') === '1');

  const depts = userDepts(user);
  const deptMatch = useMatch('/d/:dept/*');
  // Campaign / call detail and global routes sit outside /d/:dept: they keep the last-chosen department, which is
  // remembered in this browser (and is where "/" lands).
  const routeDept = deptMatch?.params.dept;
  const inDept = Boolean(routeDept && depts.includes(routeDept));
  useEffect(() => { if (inDept) rememberDept(routeDept); }, [inDept, routeDept]);
  const dept = inDept ? routeDept : homeDept(user);

  const isOwner = user.role === 'ADMIN';
  const canManage = isOwner || user.role === 'HEAD';

  const [titleSlot, setTitleSlot] = useState(null); // every page's PageHead portals its title here

  function toggleCollapse() {
    setCollapsed((c) => { const n = !c; localStorage.setItem('cc_sidebar_collapsed', n ? '1' : '0'); return n; });
  }

  const navItem = ({ to, label, Icon, end }) => (
    <NavLink key={to} to={to} end={end}
      className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
      onClick={() => setMobileOpen(false)} title={collapsed ? label : undefined}>
      <span className="nav-ic"><Icon /></span>
      <span className="nav-label">{label}</span>
    </NavLink>
  );

  const d = `/d/${dept}`;

  return (
    <div className={`app-shell ${collapsed ? 'collapsed' : ''}`}>
      <aside className={`sidebar ${mobileOpen ? 'open' : ''}`}>
        <div className="sidebar-brand">
          <img className="brand-mark" src="/openhouse-logo.png" alt="Openhouse" />
          <img className="brand-logo" src="/oh_full_logo.png" alt="Openhouse · Calling Console" />
        </div>

        <button className="sidebar-collapse-btn" onClick={toggleCollapse} title={collapsed ? 'Expand' : 'Collapse'} aria-label="Toggle sidebar">
          <span className="scb-chev"><IconChevron size={16} /></span>
          <span className="scb-label">Collapse</span>
        </button>

        <nav className="sidebar-nav">
          <div className="sidebar-section-label">Department</div>
          <div className="dept-switch" style={{ display: 'flex', gap: 6, padding: '0 4px 8px' }}>
            {depts.map((x) => (
              <button key={x} type="button" className={`pill ${x === dept ? 'pill-on' : ''}`}
                onClick={() => { nav(`/d/${x}`); setMobileOpen(false); }}>{cap(x)}</button>
            ))}
          </div>
          {navItem({ to: d, label: 'Dashboard', Icon: IconReport, end: true })}
          {navItem({ to: `${d}/use-cases`, label: 'Use cases', Icon: IconTasks })}
          {navItem({ to: `${d}/datasets`, label: 'Datasets', Icon: IconLogs })}
          {navItem({ to: `${d}/workflows`, label: 'Workflows', Icon: IconPipeline })}
          {navItem({ to: `${d}/schedule`, label: 'Schedule', Icon: IconFollowUp })}
          {navItem({ to: `${d}/campaigns`, label: 'Campaigns', Icon: IconDialer })}
          {navItem({ to: `${d}/calls`, label: 'Calls', Icon: IconPhone })}
          {navItem({ to: `${d}/handoffs`, label: 'Hand-offs', Icon: IconTicketStub })}
          {canManage && navItem({ to: `${d}/team`, label: 'Team', Icon: IconUsers })}
          {canManage && navItem({ to: `${d}/settings`, label: 'Settings', Icon: IconSettings })}

          <div className="sidebar-section-label">Global</div>
          {navItem({ to: '/agents', label: 'Agents', Icon: IconLeads })}
          {navItem({ to: '/knowledge', label: 'Knowledge base', Icon: IconBook })}
          {isOwner && navItem({ to: '/team', label: 'All users', Icon: IconUsers })}
          {isOwner && navItem({ to: '/settings', label: 'Integrations', Icon: IconSettings })}
          {isOwner && navItem({ to: '/audit', label: 'Audit trail', Icon: IconQualified })}
        </nav>

        <div className="sidebar-foot">
          <div className="sidebar-user">
            <span className="avatar">{initials(user.name, user.email)}</span>
            <div className="su-text">
              <div className="su-name">{user.name || user.email}</div>
              <div className="su-role">{user.role}</div>
            </div>
          </div>
        </div>
      </aside>

      {mobileOpen && <div className="modal-backdrop" style={{ zIndex: 400 }} onClick={() => setMobileOpen(false)} />}
      <div className="main-col">
        <header className="topbar">
          <button className="icon-btn topbar-menu" onClick={() => setMobileOpen(true)} aria-label="Menu"><IconMenu /></button>
          <h1 ref={setTitleSlot} />
          <div className="topbar-spacer" />
          <button className="icon-btn" onClick={toggle} aria-label="Toggle theme" title={theme === 'dark' ? 'Switch to light' : 'Switch to dark'}>
            {theme === 'dark' ? <IconSun /> : <IconMoon />}
          </button>
          <button className="icon-btn" onClick={logout} aria-label="Logout" title="Logout"><IconLogout /></button>
        </header>
        <main className="main"><TitleSlot.Provider value={titleSlot}><Outlet /></TitleSlot.Provider></main>
      </div>
      <LiveNotices dept={dept} />
    </div>
  );
}

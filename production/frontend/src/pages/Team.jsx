import { useState } from 'react';
import { Navigate, useParams } from 'react-router-dom';
import { api } from '../api/client.js';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { canAdminDept } from '../utils/format.js';
import { toast } from '../utils/toast.js';
import { PageHead, PageState, Card, Table, Modal } from '../components/ui.jsx';

const ROLE_LABEL = { ADMIN: 'Product owner', HEAD: 'Department head', MANAGER: 'Manager', VIEWER: 'Viewer' };
const vLabel = (v) => (v === 'BOTH' ? 'Both' : v.toLowerCase());

// Used for /d/:dept/team (dept head or owner) and /team (owner, all users).
export default function Team() {
  const { dept } = useParams();
  const { user } = useAuth();
  if (dept ? !canAdminDept(user, dept) : user.role !== 'ADMIN') return <Navigate to={dept ? `/d/${dept}` : '/'} replace />;
  return <TeamFor key={dept ?? 'all'} dept={dept} user={user} />;
}

function TeamFor({ dept, user }) {
  const isOwner = user.role === 'ADMIN';
  const usersQ = useFetch(dept ? `/api/departments/${dept}/team` : '/api/users');
  // Use cases to name and pick restrictions: this department's, or both for the owner view.
  const ucA = useFetch(`/api/departments/${dept ?? 'supply'}/use-cases`);
  const ucB = useFetch(dept ? null : '/api/departments/demand/use-cases');
  const [editing, setEditing] = useState(null); // member, or {} for a new one

  const ucFetches = dept ? [ucA] : [ucA, ucB];
  const ucs = ucFetches.every((q) => q.data) ? ucFetches.flatMap((q) => q.data.items) : null;
  const error = usersQ.error || ucFetches.find((q) => q.error)?.error;
  const ready = usersQ.data && ucs;
  const done = () => { setEditing(null); usersQ.reload(); };

  // Mirrors the backend's check_user_edit: nobody edits themselves; a head can't touch owners/heads
  // or BOTH-department people other than viewers (who they may claim).
  const editable = (m) => m.id !== user.id
    && (isOwner || (!['ADMIN', 'HEAD'].includes(m.role) && (m.vertical !== 'BOTH' || m.role === 'VIEWER')));

  const cols = [
    { key: 'name', label: 'Person', render: (m) => (
      <>{m.name || '—'}<div className="muted">{m.email}{m.title ? ` · ${m.title}` : ''}{m.id === user.id ? ' (you)' : ''}</div></>) },
    { key: 'role', label: 'Role', render: (m) => ROLE_LABEL[m.role] },
    { key: 'vertical', label: 'Department', render: (m) => vLabel(m.vertical) },
    { key: 'use_case_ids', label: 'Use cases', render: (m) => (['ADMIN', 'HEAD'].includes(m.role) ? 'all'
      : m.use_case_ids.length ? m.use_case_ids.map((id) => ucs.find((u) => u.id === id)?.name ?? '?').join(', ') : 'all in department') },
    { key: 'active', label: 'Active', render: (m) => (m.active ? 'yes' : 'no') },
    { key: 'edit', label: '', render: (m) => editable(m) && <button type="button" className="btn-edit" onClick={() => setEditing(m)}>Edit</button> },
  ];

  return (
    <>
      <PageHead title={dept ? 'Team' : 'All users'}
        actions={<button type="button" className="btn-primary" onClick={() => setEditing({})}>Add person</button>} />
      <PageState loading={!ready && !error} error={error} onRetry={() => { usersQ.reload(); ucFetches.forEach((q) => q.reload()); }} />
      {ready && <Card><Table cols={cols} rows={usersQ.data.items} /></Card>}
      {editing && ucs && <MemberForm m={editing.id ? editing : null} isOwner={isOwner} dept={dept} ucs={ucs}
        onClose={() => setEditing(null)} onSaved={done} />}
    </>
  );
}

function MemberForm({ m, isOwner, dept, ucs, onClose, onSaved }) {
  const roles = isOwner ? ['ADMIN', 'HEAD', 'MANAGER', 'VIEWER'] : ['MANAGER', 'VIEWER'];
  const verticals = isOwner ? ['SUPPLY', 'DEMAND', 'BOTH'] : [dept.toUpperCase()];
  const [v, setV] = useState(() => ({
    email: m?.email ?? '', role: m?.role ?? 'MANAGER', vertical: m?.vertical ?? verticals[0],
    use_case_ids: m?.use_case_ids ?? [], title: m?.title ?? '', active: m?.active ?? true,
  }));
  const [busy, setBusy] = useState(false);
  const set = (k, x) => setV((p) => ({ ...p, [k]: x }));
  const restrictable = v.role === 'MANAGER' || v.role === 'VIEWER';
  const options = ucs.filter((u) => v.vertical === 'BOTH' || u.department === v.vertical);
  const toggle = (id) => set('use_case_ids', v.use_case_ids.includes(id) ? v.use_case_ids.filter((x) => x !== id) : [...v.use_case_ids, id]);

  async function save() {
    setBusy(true);
    try {
      const body = { role: v.role, vertical: v.vertical, use_case_ids: restrictable ? v.use_case_ids : [], title: v.title || null, active: v.active };
      if (m) await api.put(`/api/users/${m.id}`, body, { silent: true });
      else await api.post('/api/users', { ...body, email: v.email }, { silent: true });
      toast(m ? 'Saved' : 'Person added', 'success');
      onSaved();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open onClose={onClose} title={m ? `Edit ${m.email}` : 'Add a person'}>
      <div className="form-grid">
        <div className="form-wide-2"><label>Email (@openhouse.in)</label><input value={v.email} disabled={!!m} onChange={(e) => set('email', e.target.value)} placeholder="name@openhouse.in" /></div>
        <div>
          <label>Role</label>
          <select value={v.role} onChange={(e) => set('role', e.target.value)}>
            {roles.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
          </select>
        </div>
        <div>
          <label>Department</label>
          <select value={v.vertical} onChange={(e) => setV((p) => ({ ...p, vertical: e.target.value, use_case_ids: [] }))}>
            {verticals.map((d) => <option key={d} value={d}>{vLabel(d)}</option>)}
          </select>
        </div>
        <div className="form-wide-2"><label>Title (optional)</label><input value={v.title} onChange={(e) => set('title', e.target.value)} placeholder="Supply Head, RM, Growth" /></div>
        <div className="form-wide-2">
          <label>Limit to use cases {restrictable ? '(none selected = all in the department)' : '(heads and owners see all)'}</label>
          <div className="city-pills">
            {options.map((u) => (
              <button type="button" key={u.id} disabled={!restrictable} onClick={() => toggle(u.id)}
                className={`pill ${v.use_case_ids.includes(u.id) ? 'pill-on' : ''}`}>{u.name}</button>
            ))}
          </div>
        </div>
        <div className="form-wide-2"><label><input type="checkbox" checked={v.active} onChange={(e) => set('active', e.target.checked)} /> Active</label></div>
      </div>
      <div style={{ display: 'flex', gap: 8, marginTop: 18 }}>
        <button type="button" className="btn-primary" disabled={busy || !v.email.trim()} onClick={save}>{m ? 'Save' : 'Add'}</button>
        <button type="button" className="btn-ghost" onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  );
}

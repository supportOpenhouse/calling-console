import { useState } from 'react';
import { Link, Navigate, useParams } from 'react-router-dom';
import { api } from '../api/client.js';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { HANDOFF_STATUSES, canAdminDept, canManageDept, fmtIST, userDepts } from '../utils/format.js';
import { PageHead, PageState, Card, Empty, TagBadge } from '../components/ui.jsx';

const label = (s) => s.toLowerCase().replace(/_/g, ' ');

export default function Handoffs() {
  const { dept } = useParams();
  const { user } = useAuth();
  if (!userDepts(user).includes(dept)) return <Navigate to="/" replace />;
  return <HandoffsFor key={dept} dept={dept} user={user} />;
}

function HandoffsFor({ dept, user }) {
  const manage = canManageDept(user, dept);
  const admin = canAdminDept(user, dept);
  const [status, setStatus] = useState('');
  const [ucId, setUcId] = useState('');
  const q = useFetch(`/api/departments/${dept}/handoffs?${new URLSearchParams({ ...(status && { status }), ...(ucId && { use_case_id: ucId }) })}`);
  const ucsQ = useFetch(`/api/departments/${dept}/use-cases`);
  // The team list is admin-only on the backend; managers get "assign to me" instead.
  const teamQ = useFetch(admin ? `/api/departments/${dept}/team` : null);
  const items = q.data?.items ?? [];

  return (
    <>
      <PageHead title="Hand-offs" />
      <div className="toolbar">
        {[['', 'All'], ...HANDOFF_STATUSES.map((s) => [s, label(s)])].map(([k, l]) => (
          <button key={k} type="button" className={k === status ? 'btn-primary' : 'btn-ghost'} onClick={() => setStatus(k)}>{l}</button>
        ))}
        <select value={ucId} onChange={(e) => setUcId(e.target.value)} aria-label="Use case">
          <option value="">All use cases</option>
          {(ucsQ.data?.items ?? []).map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
        </select>
      </div>
      <Card>
        <PageState loading={q.loading} error={q.error} onRetry={q.reload} />
        {q.data && !items.length && <Empty>No hand-offs{status ? ' with this status' : ''}.</Empty>}
        {items.length > 0 && (
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Created</th><th>Lead</th><th>Use case</th><th>Tag</th><th>Summary</th><th>Status / assignee / notes</th><th>Call</th></tr></thead>
              <tbody>
                {items.map((h) => (
                  <tr key={`${h.id}:${h.updated_at}`}>
                    <td style={{ whiteSpace: 'nowrap' }}>{fmtIST(h.created_at)}</td>
                    <td>{h.name || '—'}<div className="muted"><code>{h.phone}</code></div></td>
                    <td>{h.use_case_name ?? '—'}</td>
                    <td><TagBadge tag={h.tag} /></td>
                    <td style={{ maxWidth: 280 }}>{h.summary || '—'}</td>
                    <td>
                      {manage
                        ? <HandoffControls h={h} me={user} team={admin ? teamQ : null} onSaved={q.reload} />
                        : <ReadOnly h={h} />}
                    </td>
                    <td>{h.call_id ? <Link to={`/calls/${h.call_id}`}>open</Link> : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}

function ReadOnly({ h }) {
  return <div><span className="badge">{label(h.status)}</span>{h.rm_notes && <div className="muted">{h.rm_notes}</div>}</div>;
}

function HandoffControls({ h, me, team, onSaved }) {
  const [busy, setBusy] = useState(false);
  const [notes, setNotes] = useState(h.rm_notes ?? '');

  async function save(patch) {
    setBusy(true);
    try { await api.patch(`/api/handoffs/${h.id}`, patch, { toast: false }); onSaved(); } catch { /* api client already toasted */ } finally { setBusy(false); }
  }

  const people = team?.data?.items;
  let assignee;
  if (team) {
    assignee = team.error ? <span className="muted">Couldn’t load team</span> : (
      <select value={h.assigned_to_id ?? ''} disabled={busy || !people} onChange={(e) => save({ assigned_to_id: e.target.value || null })} aria-label="Assignee">
        <option value="">unassigned</option>
        {(people ?? []).filter((u) => u.active).map((u) => <option key={u.id} value={u.id}>{u.name || u.email}</option>)}
      </select>
    );
  } else {
    assignee = (
      <>
        {h.assigned_to_id === me.id && <span className="badge badge-green">me</span>}
        {h.assigned_to_id && h.assigned_to_id !== me.id && <span className="badge">assigned</span>}
        {h.assigned_to_id !== me.id && <button type="button" className="btn-ghost" disabled={busy} onClick={() => save({ assigned_to_id: me.id })}>Assign to me</button>}
        {h.assigned_to_id && <button type="button" className="btn-ghost" disabled={busy} onClick={() => save({ assigned_to_id: null })}>Unassign</button>}
      </>
    );
  }

  return (
    <div className="ho-ctl">
      <select value={h.status} disabled={busy} onChange={(e) => save({ status: e.target.value })} aria-label="Status">
        {HANDOFF_STATUSES.map((s) => <option key={s} value={s}>{label(s)}</option>)}
      </select>
      {assignee}
      <input value={notes} disabled={busy} placeholder="RM notes" aria-label="RM notes" style={{ minWidth: 160 }}
        onChange={(e) => setNotes(e.target.value)} onBlur={() => { if (notes !== (h.rm_notes ?? '')) save({ rm_notes: notes }); }} />
    </div>
  );
}

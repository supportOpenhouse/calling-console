import { useState } from 'react';
import { Navigate, useSearchParams } from 'react-router-dom';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { fmtIST } from '../utils/format.js';
import { PageHead, PageState, Card, Table } from '../components/ui.jsx';

const PER = 50;
const FILTER_KEYS = ['user_id', 'action', 'entity', 'from', 'to', 'q'];

export default function Audit() {
  const { user } = useAuth();
  if (user.role !== 'ADMIN') return <Navigate to="/" replace />;
  return <AuditPage />;
}

function AuditPage() {
  const [sp, setSp] = useSearchParams();
  const f = Object.fromEntries(FILTER_KEYS.map((k) => [k, sp.get(k) ?? '']));
  const page = Math.max(1, Number(sp.get('page')) || 1);
  const [qInput, setQInput] = useState(f.q);
  const qs = new URLSearchParams([...FILTER_KEYS.filter((k) => f[k]).map((k) => [k, f[k]]), ['page', page]]);
  const q = useFetch(`/api/settings/audit?${qs}`);

  function setFilter(k, v) {
    const next = new URLSearchParams(sp);
    if (v) next.set(k, v); else next.delete(k);
    next.delete('page');
    setSp(next);
  }
  function setPage(p) {
    const next = new URLSearchParams(sp);
    next.set('page', p);
    setSp(next);
  }

  const d = q.data;
  const cols = [
    { key: 'created_at', label: 'When', render: (a) => fmtIST(a.created_at, true) },
    { key: 'user_email', label: 'User', render: (a) => a.user_email || 'system' },
    { key: 'action', label: 'Action' },
    { key: 'entity', label: 'Record', render: (a) => (a.entity ? <>{a.entity} <span className="muted mono">{a.entity_id}</span></> : '') },
    { key: 'meta', label: 'Detail', render: (a) => <span className="muted">{a.meta ? JSON.stringify(a.meta).slice(0, 200) : ''}</span> },
  ];
  return (
    <>
      <PageHead title="Audit trail" sub={d ? `${d.total.toLocaleString('en-IN')} entries` : ' '} />
      <div className="toolbar">
        <select value={f.user_id} onChange={(e) => setFilter('user_id', e.target.value)} aria-label="User">
          <option value="">All users</option>
          {d?.has_system && <option value="system">system</option>}
          {(d?.users ?? []).map((u) => <option key={u.id} value={u.id}>{u.email}</option>)}
        </select>
        <select value={f.action} onChange={(e) => setFilter('action', e.target.value)} aria-label="Action">
          <option value="">All actions</option>
          {(d?.actions ?? []).map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <select value={f.entity} onChange={(e) => setFilter('entity', e.target.value)} aria-label="Record type">
          <option value="">All records</option>
          {(d?.entities ?? []).map((x) => <option key={x} value={x}>{x}</option>)}
        </select>
        <input type="date" value={f.from} onChange={(e) => setFilter('from', e.target.value)} aria-label="From date" />
        <input type="date" value={f.to} onChange={(e) => setFilter('to', e.target.value)} aria-label="To date" />
        <form onSubmit={(e) => { e.preventDefault(); setFilter('q', qInput.trim()); }}>
          <input type="search" value={qInput} onChange={(e) => setQInput(e.target.value)} placeholder="record id / detail" />
        </form>
      </div>
      <Card>
        <PageState loading={q.loading} error={q.error} onRetry={q.reload} />
        {d && (d.items.length ? <Table cols={cols} rows={d.items} /> : <div className="muted">No entries match.</div>)}
        {d && d.total > PER && (
          <div className="pager">
            <span>Page {page} of {Math.ceil(d.total / PER)}</span>
            <span>
              <button type="button" className="btn-ghost" disabled={page <= 1} onClick={() => setPage(page - 1)}>← Prev</button>{' '}
              <button type="button" className="btn-ghost" disabled={page * PER >= d.total} onClick={() => setPage(page + 1)}>Next →</button>
            </span>
          </div>
        )}
      </Card>
    </>
  );
}

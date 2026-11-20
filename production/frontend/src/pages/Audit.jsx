import { useState } from 'react';
import { Navigate } from 'react-router-dom';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { fmtIST } from '../utils/format.js';
import { PageHead, PageState, Card, Table } from '../components/ui.jsx';

const PER = 50;

export default function Audit() {
  const { user } = useAuth();
  if (user.role !== 'ADMIN') return <Navigate to="/" replace />;
  return <AuditPage />;
}

function AuditPage() {
  const [page, setPage] = useState(1);
  const q = useFetch(`/api/settings/audit?page=${page}`);
  const cols = [
    { key: 'created_at', label: 'When', render: (a) => fmtIST(a.created_at, true) },
    { key: 'user_email', label: 'User', render: (a) => a.user_email || 'system' },
    { key: 'action', label: 'Action' },
    { key: 'meta', label: 'Detail', render: (a) => <span className="muted">{a.meta ? JSON.stringify(a.meta).slice(0, 200) : ''}</span> },
  ];
  return (
    <>
      <PageHead title="Audit trail" sub={q.data ? `${q.data.total.toLocaleString('en-IN')} entries` : ' '} />
      <Card>
        <PageState loading={q.loading} error={q.error} onRetry={q.reload} />
        {q.data && (q.data.items.length ? <Table cols={cols} rows={q.data.items} /> : <div className="muted">Nothing yet.</div>)}
        {q.data && q.data.total > PER && (
          <div className="pager">
            <span>Page {page} of {Math.ceil(q.data.total / PER)}</span>
            <span>
              <button type="button" className="btn-ghost" disabled={page <= 1} onClick={() => setPage(page - 1)}>← Prev</button>{' '}
              <button type="button" className="btn-ghost" disabled={page * PER >= q.data.total} onClick={() => setPage(page + 1)}>Next →</button>
            </span>
          </div>
        )}
      </Card>
    </>
  );
}

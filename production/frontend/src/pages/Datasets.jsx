import { Link, Navigate, useParams } from 'react-router-dom';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { canManageDept, fmtIST, userDepts } from '../utils/format.js';
import { PageHead, PageState, Card, Empty, Table } from '../components/ui.jsx';

export default function Datasets() {
  const { dept } = useParams();
  const { user } = useAuth();
  if (!userDepts(user).includes(dept)) return <Navigate to="/" replace />;
  return <DatasetsFor key={dept} dept={dept} manage={canManageDept(user, dept)} />;
}

function DatasetsFor({ dept, manage }) {
  const q = useFetch(`/api/departments/${dept}/datasets`);
  const ucs = useFetch(`/api/departments/${dept}/use-cases`);
  const ready = q.data && ucs.data;
  const ucName = (id) => (id === null ? 'shared' : ucs.data.items.find((u) => u.id === id)?.name ?? '—');
  const cols = [
    { key: 'name', label: 'Name', render: (d) => <Link to={`/d/${dept}/datasets/${d.id}`}>{d.name}</Link> },
    { key: 'use_case_id', label: 'Use case', render: (d) => ucName(d.use_case_id) },
    { key: 'source', label: 'Source', render: (d) => <span className="badge">{d.source.toLowerCase()}</span> },
    { key: 'row_count', label: 'Rows', align: 'right' },
    { key: 'never_called', label: 'Never called', align: 'right' },
    { key: 'dnc', label: 'DNC', align: 'right' },
    { key: 'last_synced_at', label: 'Updated', render: (d) => fmtIST(d.last_synced_at ?? d.updated_at) },
  ];
  return (
    <>
      <PageHead title="Datasets"
        actions={manage && <Link to={`/d/${dept}/datasets/new`} className="btn-primary">New dataset</Link>} />
      <PageState loading={!ready && !q.error && !ucs.error} error={q.error || ucs.error}
        onRetry={() => { q.reload(); ucs.reload(); }} />
      {ready && !q.data.items.length && <Empty>No datasets yet.{manage ? ' Create one to get started.' : ''}</Empty>}
      {ready && q.data.items.length > 0 && <Card><Table cols={cols} rows={q.data.items} /></Card>}
    </>
  );
}

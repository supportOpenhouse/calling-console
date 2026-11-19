import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client.js';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { fmtIST } from '../utils/format.js';
import { toast } from '../utils/toast.js';
import { PageHead, PageState, Card, Table, Empty } from '../components/ui.jsx';

export default function Agents() {
  const { user } = useAuth();
  const q = useFetch('/api/agents');
  const [busy, setBusy] = useState(false);

  async function refresh() {
    setBusy(true);
    try {
      const r = await api.post('/api/agents/refresh', undefined, { silent: true });
      toast(`${r.count} agents synced`, 'success');
      q.reload();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  const cols = [
    { key: 'name', label: 'Agent', render: (a) => <Link to={`/agents/${a.id}`}>{a.name}</Link> },
    { key: 'vertical', label: 'Department', render: (a) => (a.vertical === 'BOTH' ? 'Both' : a.vertical.toLowerCase()) },
    { key: 'caller_name', label: 'Caller name', render: (a) => a.caller_name || '—' },
    { key: 'default_from_number', label: 'Default number', render: (a) => a.default_from_number || '—' },
    { key: 'variables', label: 'Variables', render: (a) => a.variables.join(', ') || '—' },
    { key: 'last_synced_at', label: 'Synced', render: (a) => fmtIST(a.last_synced_at) },
  ];

  return (
    <>
      <PageHead title="Agents"
        actions={user.role === 'ADMIN' && <button type="button" className="btn-primary" disabled={busy} onClick={refresh}>Refresh from Bolna</button>} />
      <PageState loading={q.loading} error={q.error} onRetry={q.reload} />
      {q.data && (q.data.items.length
        ? <Card><Table cols={cols} rows={q.data.items} /></Card>
        : <Empty>No agents yet.</Empty>)}
    </>
  );
}

import { useEffect, useState } from 'react';
import { Navigate } from 'react-router-dom';
import { api } from '../api/client.js';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { fmtIST } from '../utils/format.js';
import { toast } from '../utils/toast.js';
import { PageHead, PageState, Card, Table } from '../components/ui.jsx';

const flag = (on) => <span className={`badge ${on ? 'badge-green' : ''}`}>{on ? 'configured' : 'not configured'}</span>;

export default function Integrations() {
  const { user } = useAuth();
  if (user.role !== 'ADMIN') return <Navigate to="/" replace />;
  return <IntegrationsPage />;
}

function IntegrationsPage() {
  const q = useFetch('/api/settings/integrations');
  const [syncing, setSyncing] = useState(false);
  const [numbersTick, setNumbersTick] = useState(0);

  async function syncNow() {
    setSyncing(true);
    try {
      const r = await api.post('/api/sync', {}, { silent: true });
      if (r.errors.length) toast(`Sync finished with ${r.errors.length} error(s)`, 'error');
      else toast('Sync finished', 'success');
      q.reload();
      setNumbersTick((t) => t + 1);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setSyncing(false);
    }
  }

  if (!q.data) return <><PageHead title="Integrations" /><PageState loading={q.loading} error={q.error} onRetry={q.reload} /></>;
  const { bolna, r2, di, interakt, ingest, sync } = q.data;
  const r = sync.last_report;
  const failures = di.failures ?? [];

  return (
    <>
      <PageHead title="Integrations" />
      <Card title="Integrations">
        <dl className="kv">
          <dt>Bolna</dt><dd>{flag(bolna.configured)}</dd>
          <dt>Recording archive (R2)</dt><dd>{flag(r2.configured)}</dd>
          <dt>Direct Inventory push</dt><dd>{flag(di.configured)}</dd>
          <dt>WhatsApp (Interakt)</dt><dd>{flag(interakt.configured)}</dd>
          <dt>Lead ingest</dt><dd>{flag(ingest.configured)}{' '}
            <code>{ingest.path}</code>
            <div className="muted">The full URL is this backend’s host plus the path, with an <code>Authorization: Bearer INGEST_TOKEN</code> header.</div></dd>
        </dl>
      </Card>
      <Card title="Sync" action={<button type="button" className="btn-primary" disabled={syncing} onClick={syncNow}>Sync now</button>}>
        <dl className="kv">
          <dt>Background sync</dt><dd>{sync.enabled ? 'on' : 'off'}</dd>
          <dt>Last full sync</dt><dd>{sync.last_full_sync_date ?? 'never'}</dd>
          <dt>Last sync</dt>
          <dd>{r ? `${fmtIST(r.finished_at ?? r.started_at, true)} · ${r.calls_upserted} calls · ${r.recordings_archived} recordings · ${r.redial_rounds} redial rounds · ${r.di_pushed} pushed to DI · ${r.errors.length} errors` : 'never'}</dd>
          {r && r.errors.length > 0 && <><dt>Errors</dt><dd><ul>{r.errors.map((e, i) => <li key={i} className="tone-red">{e}</li>)}</ul></dd></>}
          {r?.window_stops?.length > 0 && <><dt>Stopped at window end</dt><dd><ul>{r.window_stops.map((e, i) => <li key={i}>{e}</li>)}</ul></dd></>}
        </dl>
      </Card>
      <Card title="Direct Inventory push failures">
        {failures.length
          ? <Table cols={[
            { key: 'call_id', label: 'Call', render: (f) => <span className="mono">{f.call_id}</span> },
            { key: 'error', label: 'Error' },
            { key: 'at', label: 'When', render: (f) => fmtIST(f.at, true) },
          ]} rows={failures.map((f) => ({ ...f, id: f.call_id }))} />
          : <div className="muted">None.</div>}
      </Card>
      <Numbers tick={numbersTick} />
    </>
  );
}

function Numbers({ tick }) {
  const q = useFetch('/api/numbers');
  useEffect(() => { if (tick) q.reload(); }, [tick]); // eslint-disable-line react-hooks/exhaustive-deps
  const [busy, setBusy] = useState(false);
  const [labels, setLabels] = useState({});

  async function refresh() {
    setBusy(true);
    try {
      const r = await api.post('/api/numbers/refresh', undefined, { silent: true });
      toast(`${r.count} numbers synced`, 'success');
      q.reload();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }
  async function patch(n, body) {
    try {
      await api.put(`/api/numbers/${n.id}`, body, { silent: true });
      toast('Saved', 'success');
      setLabels((p) => { const { [n.id]: _, ...rest } = p; return rest; });
      q.reload();
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  const cols = [
    { key: 'number', label: 'Number' },
    { key: 'provider', label: 'Provider', render: (n) => n.provider || '—' },
    { key: 'label', label: 'Label', render: (n) => (
      <input value={labels[n.id] ?? n.label ?? ''} placeholder="label" aria-label={`Label for ${n.number}`}
        onChange={(e) => setLabels((p) => ({ ...p, [n.id]: e.target.value }))}
        onBlur={() => { if (n.id in labels && labels[n.id] !== (n.label ?? '')) patch(n, { label: labels[n.id] }); }} />) },
    { key: 'active', label: 'Active', render: (n) => (
      <input type="checkbox" checked={n.active} aria-label={`Active ${n.number}`} onChange={(e) => patch(n, { active: e.target.checked })} />) },
  ];
  return (
    <Card title="Calling numbers" action={<button type="button" className="btn-ghost" disabled={busy} onClick={refresh}>Refresh from Bolna</button>}>
      <PageState loading={q.loading} error={q.error} onRetry={q.reload} />
      {q.data && (q.data.items.length ? <Table cols={cols} rows={q.data.items} /> : <div className="muted">No numbers yet.</div>)}
    </Card>
  );
}

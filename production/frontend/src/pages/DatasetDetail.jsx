import { useState } from 'react';
import { Link, Navigate, useNavigate, useParams } from 'react-router-dom';
import { api, upload } from '../api/client.js';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { toast } from '../utils/toast.js';
import { canAdminDept, canManageDept, describeTrigger, fmtImport, fmtIST, pct, queuedWarning, uploadSizeError, userDepts } from '../utils/format.js';
import { PageHead, PageState, Card, Stat, Bars, Empty, OutcomeBadge, TagBadge } from '../components/ui.jsx';

const FILTERS = [['', 'All'], ['never', 'Never called'], ['connected', 'Connected'], ['dnc', 'DNC']];
const PER = 50;

export default function DatasetDetail() {
  const { dept, id } = useParams();
  const { user } = useAuth();
  if (!userDepts(user).includes(dept)) return <Navigate to="/" replace />;
  return <DatasetDetailFor key={id} dept={dept} id={id} manage={canManageDept(user, dept)} admin={canAdminDept(user, dept)} />;
}

function DatasetDetailFor({ dept, id, manage, admin }) {
  const nav = useNavigate();
  const dsQ = useFetch(`/api/datasets/${id}`);
  const facetsQ = useFetch(`/api/datasets/${id}/facets`);
  const ucsQ = useFetch(`/api/departments/${dept}/use-cases`);
  const wfQ = useFetch(`/api/departments/${dept}/workflows`);
  const [f, setF] = useState('');
  const [q, setQ] = useState('');
  const [qInput, setQInput] = useState('');
  const [page, setPage] = useState(1);
  const rowsQ = useFetch(`/api/datasets/${id}/rows?${new URLSearchParams({ f, q, page })}`);
  const [busy, setBusy] = useState(false);

  const reloadAll = () => { dsQ.reload(); facetsQ.reload(); rowsQ.reload(); wfQ.reload(); };

  async function run(fn, ok) {
    setBusy(true);
    try {
      const r = await fn();
      toast(ok(r), 'success');
      reloadAll();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  function append(e) {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    const tooBig = uploadSizeError(file);
    if (tooBig) { toast(tooBig, 'error'); return; }
    const fd = new FormData();
    fd.append('file', file);
    run(() => upload(`/api/datasets/${id}/append`, fd), fmtImport);
  }

  const pull = () => run(() => api.post(`/api/datasets/${id}/pull`, {}, { silent: true }), fmtImport);

  async function remove() {
    if (!window.confirm('Delete this dataset and all its rows? Past calls are kept.')) return;
    setBusy(true);
    try {
      await api.del(`/api/datasets/${id}`, { toast: 'Dataset deleted' });
      nav(`/d/${dept}/datasets`);
    } catch {
      setBusy(false); // api.del already toasted the failure
    }
  }

  async function toggleDnc(row) {
    const path = `/api/dnc/${encodeURIComponent(row.phone)}`;
    try {
      if (row.dnc) {
        await api.del(path, { silent: true });
        toast(`${row.phone} removed from the DNC list`, 'success');
      } else {
        const warning = queuedWarning(await api.put(path, {}, { silent: true }));
        toast(warning ? `${row.phone} marked DNC. ${warning}` : `${row.phone} marked DNC`, warning ? 'error' : 'success');
      }
      dsQ.reload();
      rowsQ.reload();
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  function setFilter(k) { setF(k); setPage(1); }
  function search(e) { e.preventDefault(); setQ(qInput.trim()); setPage(1); }

  const head = dsQ.data && facetsQ.data;
  if (!head) {
    return (
      <>
        <PageHead title="Dataset" actions={<Link to={`/d/${dept}/datasets`} className="btn-ghost">← Datasets</Link>} />
        <PageState loading={!dsQ.error && !facetsQ.error} error={dsQ.error || facetsQ.error}
          onRetry={() => { dsQ.reload(); facetsQ.reload(); }} />
      </>
    );
  }
  const { dataset: ds, stats } = dsQ.data;
  const facets = facetsQ.data;
  const ucName = ds.use_case_id === null ? 'shared' : ucsQ.data?.items.find((u) => u.id === ds.use_case_id)?.name ?? '—';
  const bars = (list, n) => list.slice(0, n).map((s) => ({ label: s.v, n: s.n }));
  const extraCols = ds.columns
    .filter((c) => !['phone', 'mobile', 'name', 'city', 'society', 'segment'].some((k) => c.toLowerCase().includes(k)))
    .slice(0, 4);
  const usedBy = (wfQ.data?.items ?? []).filter((w) => w.dataset_id === id);
  const rows = rowsQ.data?.items ?? [];
  const total = rowsQ.data?.total ?? 0;

  return (
    <>
      <PageHead title={ds.name}
        sub={`${ucName} · ${ds.source.toLowerCase()}${ds.description ? ` · ${ds.description}` : ''} · updated ${fmtIST(ds.last_synced_at ?? ds.updated_at)}`}
        actions={<>
          <Link to={`/d/${dept}/datasets`} className="btn-ghost">← Datasets</Link>
          {manage && <Link to={`/d/${dept}/workflows/new?dataset_id=${ds.id}`} className="btn-primary">New workflow on this data</Link>}
        </>} />
      <div className="grid-6">
        <Stat label="Rows" value={stats.total} />
        <Stat label="Never called" value={stats.never} tone="brand" />
        <Stat label="Last outcome connected" value={stats.connected} hint={pct(stats.connected, stats.total)} tone="green" />
        <Stat label="Carrier blocked" value={stats.blocked} tone="red" />
        <Stat label="DNC" value={stats.dnc} />
        <Stat label="Total attempts" value={stats.attempts} />
      </div>
      <div className="mt grid-3" style={{ gridTemplateColumns: 'repeat(5, 1fr)' }}>
        <Card title="Segments"><Bars items={bars(facets.segments, 10)} /></Card>
        <Card title="Cities"><Bars items={bars(facets.cities, 10)} /></Card>
        <Card title="Societies (top)"><Bars items={bars(facets.societies, 10)} /></Card>
        <Card title="Used by">
          {wfQ.error ? <div className="muted">Couldn’t load workflows.</div>
            : !wfQ.data ? <div className="muted">Loading…</div>
              : usedBy.length ? usedBy.map((w) => (
                <div key={w.id}><Link to={`/d/${dept}/workflows/${w.id}`}>{w.name}</Link> <span className="muted">· {describeTrigger(w.trigger)}{w.active ? '' : ' · off'}</span></div>
              )) : <div className="muted">No workflows use this dataset.</div>}
        </Card>
        <Card title="Manage">
          {manage ? (
            <div style={{ display: 'grid', gap: 8 }}>
              <label className="btn-ghost" style={{ marginBottom: 0, cursor: 'pointer', textAlign: 'center' }}>
                Append file
                <input type="file" accept=".csv,.xlsx,.xls" disabled={busy} onChange={append} style={{ display: 'none' }} />
              </label>
              {ds.source !== 'UPLOAD' && <button type="button" className="btn-primary" disabled={busy} onClick={pull}>Pull again from source</button>}
              {admin && <button type="button" className="btn-danger" disabled={busy} onClick={remove}>Delete dataset</button>}
              {busy && <div className="muted">Working…</div>}
            </div>
          ) : <div className="muted">View only.</div>}
        </Card>
      </div>
      <div className="mt">
        <Card title={`Leads · ${total}`} action={
          <div className="toolbar" style={{ marginBottom: 0 }}>
            {FILTERS.map(([k, label]) => (
              <button key={k} type="button" className={k === f ? 'btn-primary' : 'btn-ghost'} onClick={() => setFilter(k)}>{label}</button>
            ))}
            <form onSubmit={search}>
              <input type="search" value={qInput} onChange={(e) => setQInput(e.target.value)} placeholder="phone / name / society" />
            </form>
          </div>}>
          <PageState loading={rowsQ.loading} error={rowsQ.error} onRetry={rowsQ.reload} />
          {rowsQ.data && !rows.length && <Empty>No rows match.</Empty>}
          {rows.length > 0 && (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Phone</th><th>Name</th><th>Society</th><th>City</th><th>Segment</th>
                    {extraCols.map((c) => <th key={c}>{c}</th>)}
                    <th className="num">Tries</th><th>Last</th><th>Outcome</th><th>Tag</th><th>DNC</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((l) => (
                    <tr key={l.id} className={l.dnc ? 'row-dnc' : ''}>
                      <td><code>{l.phone}</code></td>
                      <td>{l.name || '—'}</td><td>{l.society || '—'}</td><td>{l.city || '—'}</td><td>{l.segment || '—'}</td>
                      {extraCols.map((c) => <td key={c} className="muted">{l.data[c] ?? ''}</td>)}
                      <td className="num">{l.total_attempts}</td>
                      <td className="muted">{fmtIST(l.last_called_at)}</td>
                      <td>{l.last_outcome ? <OutcomeBadge o={l.last_outcome} /> : '—'}</td>
                      <td><TagBadge tag={l.last_tag} /></td>
                      <td>
                        {/* a manager may block a number; only a head or the owner may unblock it */}
                        {manage && (admin || !l.dnc)
                          ? <input type="checkbox" checked={l.dnc} onChange={() => toggleDnc(l)} title="Do not call" aria-label={`Do not call ${l.phone}`} />
                          : l.dnc ? 'yes' : ''}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {total > PER && (
            <div className="pager">
              <span>Page {page} of {Math.ceil(total / PER)}</span>
              <span>
                <button type="button" className="btn-ghost" disabled={page <= 1} onClick={() => setPage(page - 1)}>← Prev</button>{' '}
                <button type="button" className="btn-ghost" disabled={page * PER >= total} onClick={() => setPage(page + 1)}>Next →</button>
              </span>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}

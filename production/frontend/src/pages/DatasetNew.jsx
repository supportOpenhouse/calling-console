import { useState } from 'react';
import { Link, Navigate, useNavigate, useParams } from 'react-router-dom';
import { upload } from '../api/client.js';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { toast } from '../utils/toast.js';
import { canManageDept, fmtImport, uploadSizeError } from '../utils/format.js';
import { PageHead, PageState, Card } from '../components/ui.jsx';

const PULL_FIELDS = ['url', 'method', 'token', 'path', 'phone_field', 'name_field'];

export default function DatasetNew() {
  const { dept } = useParams();
  const { user } = useAuth();
  if (!canManageDept(user, dept)) return <Navigate to={`/d/${dept}/datasets`} replace />;
  return <DatasetNewFor key={dept} dept={dept} />;
}

function DatasetNewFor({ dept }) {
  const nav = useNavigate();
  const ucs = useFetch(`/api/departments/${dept}/use-cases`);
  const [v, setV] = useState({ name: '', use_case_id: '', description: '', source: 'UPLOAD', url: '', method: 'GET', token: '', path: '', phone_field: '', name_field: '' });
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k, x) => setV((p) => ({ ...p, [k]: x }));
  const pull = v.source !== 'UPLOAD';

  async function submit(e) {
    e.preventDefault();
    const tooBig = uploadSizeError(file);
    if (tooBig) { toast(tooBig, 'error'); return; }
    const fd = new FormData();
    for (const k of ['name', 'use_case_id', 'description', 'source']) fd.append(k, v[k]);
    if (pull) for (const k of PULL_FIELDS) fd.append(k, v[k]);
    if (file) fd.append('file', file);
    setBusy(true);
    try {
      const r = await upload(`/api/departments/${dept}/datasets`, fd);
      if (r.stats.error) toast(`Dataset created, but the pull failed: ${r.stats.error}`, 'error');
      else if (r.stats.added != null) toast(fmtImport(r.stats), 'success');
      nav(`/d/${dept}/datasets/${r.dataset.id}`);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHead title="New dataset"
        actions={<Link to={`/d/${dept}/datasets`} className="btn-ghost">← Datasets</Link>} />
      <PageState loading={ucs.loading} error={ucs.error} onRetry={ucs.reload} />
      {ucs.data && (
        <form onSubmit={submit} className="grid-2">
          <Card title="Basics">
            <div className="form-grid">
              <div className="form-wide-2"><label>Name</label><input required value={v.name} onChange={(e) => set('name', e.target.value)} placeholder="e.g. Noida L2 owners · Nov 2026" /></div>
              <div>
                <label>Use case (optional)</label>
                <select value={v.use_case_id} onChange={(e) => set('use_case_id', e.target.value)}>
                  <option value="">— shared across use cases —</option>
                  {ucs.data.items.map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
                </select>
              </div>
              <div>
                <label>Source</label>
                <select value={v.source} onChange={(e) => set('source', e.target.value)}>
                  <option value="UPLOAD">Upload (CSV / Excel)</option>
                  <option value="CRM">CRM pull (Direct Inventory / Direct Demand)</option>
                  <option value="API">Other JSON API</option>
                </select>
              </div>
              <div className="form-wide-2"><label>Description</label><input value={v.description} onChange={(e) => set('description', e.target.value)} placeholder="Where it came from, what segment columns mean" /></div>
              <div className="form-wide-2">
                <label>File (first row = headers; needs a phone column; max 20 MB)</label>
                <input type="file" accept=".csv,.xlsx,.xls" onChange={(e) => setFile(e.target.files[0] ?? null)} />
              </div>
            </div>
            <div className="page-hint">Columns named city / society / segment are indexed for segment filters. Every other column is kept and can be mapped to agent variables in a workflow.</div>
          </Card>
          {pull && (
            <Card title="CRM / API pull">
              <div className="form-grid">
                <div className="form-wide-2"><label>Endpoint URL</label><input required value={v.url} onChange={(e) => set('url', e.target.value)} placeholder="https://…/api/leads?segment=…" /></div>
                <div><label>Method</label><select value={v.method} onChange={(e) => set('method', e.target.value)}><option>GET</option><option>POST</option></select></div>
                <div><label>Bearer token</label><input type="password" value={v.token} onChange={(e) => set('token', e.target.value)} placeholder="optional" /></div>
                <div className="form-wide-2"><label>Path to the array in the response</label><input value={v.path} onChange={(e) => set('path', e.target.value)} placeholder="e.g. data.leads (blank if the response is the array)" /></div>
                <div><label>Phone field</label><input value={v.phone_field} onChange={(e) => set('phone_field', e.target.value)} placeholder="auto-detect" /></div>
                <div><label>Name field</label><input value={v.name_field} onChange={(e) => set('name_field', e.target.value)} placeholder="auto-detect" /></div>
              </div>
              <div className="page-hint">Pulled on create and whenever you press “Pull again”. Rows are upserted by phone, so a lead is never duplicated.</div>
            </Card>
          )}
          <div className="form-wide-2" style={{ gridColumn: '1 / -1' }}>
            <button type="submit" className="btn-primary" disabled={busy}>{busy ? 'Creating…' : 'Create dataset'}</button>
          </div>
        </form>
      )}
    </>
  );
}

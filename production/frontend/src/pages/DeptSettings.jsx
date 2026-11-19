import { useState } from 'react';
import { Navigate, useParams } from 'react-router-dom';
import { api } from '../api/client.js';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { canAdminDept } from '../utils/format.js';
import { toast } from '../utils/toast.js';
import { PageHead, PageState, Card } from '../components/ui.jsx';

export default function DeptSettings() {
  const { dept } = useParams();
  const { user } = useAuth();
  if (!canAdminDept(user, dept)) return <Navigate to={`/d/${dept}`} replace />;
  return <SettingsFor key={dept} dept={dept} />;
}

function SettingsFor({ dept }) {
  const { data, error, loading, reload } = useFetch(`/api/departments/${dept}`);
  return (
    <>
      <PageHead title="Settings" />
      <PageState loading={loading} error={error} onRetry={reload} />
      {data && <Form key={data.updated_at} dept={dept} initial={data} reload={reload} />}
    </>
  );
}

function Form({ dept, initial, reload }) {
  const [v, setV] = useState({
    name: initial.name, window_start: initial.window_start, window_end: initial.window_end,
    line_share: initial.line_share, cooling_days: initial.cooling_days,
  });
  const [busy, setBusy] = useState(false);
  const set = (k, x) => setV((p) => ({ ...p, [k]: x }));

  async function save() {
    setBusy(true);
    try {
      await api.put(`/api/departments/${dept}`, { ...v, line_share: Number(v.line_share), cooling_days: Number(v.cooling_days) }, { silent: true });
      toast('Saved and re-planned', 'success');
      reload();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <div className="form-grid" style={{ maxWidth: 560 }}>
        <div className="form-wide-2"><label>Display name</label><input value={v.name} onChange={(e) => set('name', e.target.value)} /></div>
        <div><label>Window start (IST)</label><input type="time" value={v.window_start} onChange={(e) => set('window_start', e.target.value)} /></div>
        <div><label>Window end (IST)</label><input type="time" value={v.window_end} onChange={(e) => set('window_end', e.target.value)} /></div>
        <div><label>Line share (1 to 10)</label><input type="number" min={1} max={10} value={v.line_share} onChange={(e) => set('line_share', e.target.value)} /></div>
        <div><label>Cooling days (0 to 90)</label><input type="number" min={0} max={90} value={v.cooling_days} onChange={(e) => set('cooling_days', e.target.value)} /></div>
      </div>
      <div style={{ marginTop: 18 }}>
        <button type="button" className="btn-primary" disabled={busy || !v.name.trim()} onClick={save}>Save</button>
      </div>
    </Card>
  );
}

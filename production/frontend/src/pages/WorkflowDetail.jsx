import { useState } from 'react';
import { Link, Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { api } from '../api/client.js';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { toast } from '../utils/toast.js';
import { canAdminDept, canManageDept, describeTrigger, fmtIST, pct, userDepts } from '../utils/format.js';
import { PageHead, PageState, Card, Stat, Empty } from '../components/ui.jsx';
import WorkflowBuilder from '../components/WorkflowBuilder.jsx';

export default function WorkflowDetail() {
  const { dept, id } = useParams();
  const { user } = useAuth();
  if (!userDepts(user).includes(dept)) return <Navigate to="/" replace />;
  return <WorkflowDetailFor key={id} dept={dept} id={id} manage={canManageDept(user, dept)} admin={canAdminDept(user, dept)} />;
}

const STATUS_TONE = { RUNNING: 'badge-green', COMPLETED: 'badge-green', SCHEDULED: 'badge-amber', PAUSED: 'badge-amber', STOPPED: 'badge-red' };

function describeRule(r) {
  const when = [...(r.when.outcomes ?? []).map((o) => o.toLowerCase().replace('_', ' ')), ...(r.when.tags ?? [])].join(', ') || '—';
  const then = r.actions.map((a) => (a.type === 'whatsapp' ? `whatsapp(${a.template})` : a.type === 'redial' ? `redial in ${a.after_minutes}m` : a.type.replace('_', ' '))).join(', ') || '—';
  return `when ${when} → ${then}`;
}

function WorkflowDetailFor({ dept, id, manage, admin }) {
  const nav = useNavigate();
  const [sp, setSp] = useSearchParams();
  const editing = manage && sp.get('edit') === '1';
  const q = useFetch(`/api/workflows/${id}`);
  const builderQ = useFetch(editing ? `/api/departments/${dept}/builder-data` : null);
  const [busy, setBusy] = useState(false);
  const [when, setWhen] = useState('');
  const [ignoreCooling, setIgnoreCooling] = useState(false);

  const back = <Link to={`/d/${dept}/workflows`} className="btn-ghost">← Workflows</Link>;
  if (!q.data || (editing && !builderQ.data)) {
    return (
      <>
        <PageHead title="Workflow" actions={back} />
        <PageState loading={!q.error && !builderQ.error} error={q.error || builderQ.error}
          onRetry={() => { q.reload(); builderQ.reload(); }} />
      </>
    );
  }
  const { workflow: wf, use_case: uc, agent, dataset, children, runs, stats } = q.data;
  const isStep = Boolean(wf.parent_id);

  if (editing) {
    return (
      <>
        <PageHead title={`Edit · ${wf.name}`} actions={<button type="button" className="btn-ghost" onClick={() => setSp({})}>Cancel</button>} />
        <WorkflowBuilder dept={dept} data={builderQ.data} initial={wf}
          onSaved={() => { q.reload(); setSp({}); }} />
      </>
    );
  }

  async function act(fn, ok) {
    setBusy(true);
    try {
      const r = await fn();
      if (ok) toast(ok(r), 'success');
      q.reload();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }
  const fmtRun = (r) => (r.campaign_id === null
    ? 'Nothing to dial: the segment is empty, cooling, or already queued.'
    : `${r.leads} lead(s) queued${r.cooled ? `, ${r.cooled} skipped by cooling` : ''} · planned ${fmtIST(r.planned_start)}`);
  const run = (requested_start) => act(
    () => api.post(`/api/workflows/${id}/run`, { ignore_cooling: ignoreCooling, ...(requested_start ? { requested_start } : {}) }, { silent: true }), fmtRun);
  const toggle = () => act(() => api.post(`/api/workflows/${id}/toggle`, { active: !wf.active }, { silent: true }), () => (wf.active ? 'Deactivated' : 'Activated'));
  async function remove() {
    if (!window.confirm('Delete this workflow? Past campaigns and calls are kept.')) return;
    setBusy(true);
    try {
      await api.del(`/api/workflows/${id}`, { toast: 'Workflow deleted' });
      nav(`/d/${dept}/workflows`);
    } catch {
      setBusy(false); // api.del already toasted the failure
    }
  }

  const dataLabel = dataset ? <Link to={`/d/${dept}/datasets/${dataset.id}`}>{dataset.name}</Link> : isStep ? 'follow-up step' : 'no dataset';
  return (
    <>
      <PageHead
        title={<>{wf.name} {wf.active ? <span className="badge badge-green">active</span> : <span className="badge">off</span>}</>}
        sub={<>{uc.name} · {agent.name} · {dataLabel} · trigger: {describeTrigger(wf.trigger)}{wf.purpose ? ` · ${wf.purpose}` : ''}</>}
        actions={<>
          {back}
          {manage && <button type="button" className="btn-ghost" onClick={() => setSp({ edit: '1' })}>Edit</button>}
          {manage && !isStep && <Link to={`/d/${dept}/workflows/new?parent=${wf.id}`} className="btn-ghost">+ Follow-up step</Link>}
        </>} />
      <div className="grid-6" style={{ gridTemplateColumns: 'repeat(5, 1fr)' }}>
        <Stat label="Runs" value={stats.runs} />
        <Stat label="Leads" value={stats.leads} />
        <Stat label="Dialed" value={stats.dialed} />
        <Stat label="Connected" value={stats.connected} hint={pct(stats.connected, stats.dialed)} tone="green" />
        <Stat label="Priority" value={`P${wf.priority}`} hint={`${wf.redial_policy.max_attempts} attempts · ${wf.redial_policy.window_start}–${wf.redial_policy.window_end}`} />
      </div>
      <div className="mt grid-2">
        <Card title="Run">
          {manage ? (
            <div style={{ display: 'grid', gap: 10 }}>
              {isStep
                ? <div className="muted">Follow-up steps run by themselves when the parent's calls match the trigger.</div>
                : <>
                  <button type="button" className="btn-primary" disabled={busy} onClick={() => run()}>Run now (next free slot)</button>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} aria-label="Plan for (your local time)" />
                    <button type="button" className="btn-ghost" disabled={busy || !when} onClick={() => run(new Date(when).toISOString())}>Plan</button>
                  </div>
                  <label style={{ fontWeight: 400 }}><input type="checkbox" checked={ignoreCooling} onChange={(e) => setIgnoreCooling(e.target.checked)} /> Ignore the cooling guard this time</label>
                </>}
              <button type="button" className="btn-ghost" disabled={busy} onClick={toggle}>{wf.active ? 'Deactivate' : 'Activate'}</button>
              {admin && <button type="button" className="btn-danger" disabled={busy} onClick={remove}>Delete workflow</button>}
            </div>
          ) : <div className="muted">View only.</div>}
        </Card>
        <Card title="Rules">
          {wf.rules.length
            ? <ul style={{ display: 'grid', gap: 6, listStyle: 'none' }}>{wf.rules.map((r, i) => <li key={i} className="chip" style={{ borderRadius: 'var(--r-sm)' }}>{describeRule(r)}</li>)}</ul>
            : <div className="muted">Only the use-case defaults (hand-off on good/soft tags, DNC on dnc tags).</div>}
          {children.length > 0 && (
            <div style={{ marginTop: 14 }}>
              <label>Follow-up steps</label>
              {children.map((c) => (
                <div key={c.id}><Link to={`/d/${dept}/workflows/${c.id}`}>↳ {c.name}</Link> <span className="muted">· {describeTrigger(c.trigger)}</span></div>
              ))}
            </div>
          )}
        </Card>
      </div>
      <Card title="Run history">
        {runs.length === 0 ? <Empty>No runs yet.</Empty> : (
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Campaign</th><th>Status</th><th className="num">Leads</th><th className="num">Dialed</th><th className="num">Connected</th><th>Created</th></tr></thead>
              <tbody>
                {runs.map((c) => (
                  <tr key={c.id}>
                    <td><Link to={`/campaigns/${c.id}`}><strong>{c.name}</strong></Link>{c.notes && <div className="muted">{c.notes}</div>}</td>
                    <td><span className={`badge ${STATUS_TONE[c.status] ?? ''}`}>{c.status.toLowerCase()}</span></td>
                    <td className="num">{c.leads}</td><td className="num">{c.dialed}</td><td className="num">{c.connected}</td>
                    <td className="muted">{fmtIST(c.created_at)}</td>
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

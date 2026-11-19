import { useState } from 'react';
import { api } from '../api/client.js';
import { useFetch } from '../api/useFetch.js';
import { toast } from '../utils/toast.js';
import { dialTime, dialTimeText, fmtIST } from '../utils/format.js';
import RedialPolicyFields from './RedialPolicyFields.jsx';

// campaign = the campaign row. head = head of the department or owner (may start now); owner = may delete.
// onChanged reloads the page data; onDeleted leaves the page.
export default function CampaignControls({ campaign, head, owner, onChanged, onDeleted }) {
  const id = campaign.id;
  const s = campaign.status;
  const [busy, setBusy] = useState(false);
  const [halted, setHalted] = useState(null); // {verb, errors}: pause/stop problems stay until dismissed or the next action
  const [when, setWhen] = useState('');
  const [editPolicy, setEditPolicy] = useState(false);
  const [policy, setPolicy] = useState(campaign.redial_policy);
  const numbers = useFetch(editPolicy ? '/api/numbers' : null);
  // Start now and resume dial in the department's window, redial in the campaign's policy window: outside it the
  // backend schedules the batch for the next window start, and the confirm says so.
  const deptQ = useFetch(['DRAFT', 'PAUSED'].includes(s) ? `/api/departments/${campaign.vertical.toLowerCase()}` : null);
  const deptWhen = () => (deptQ.data ? dialTimeText(dialTime(deptQ.data.window_start, deptQ.data.window_end)) : '');
  const policyWhen = () => dialTimeText(dialTime(campaign.redial_policy.window_start, campaign.redial_policy.window_end));

  async function act(fn, done) {
    setBusy(true);
    setHalted(null);
    try {
      const r = await fn();
      const msg = done(r); // a string (success), {msg, type}, or null (already reported)
      if (msg) toast(msg.msg ?? msg, msg.type ?? 'success');
      onChanged();
    } catch (err) {
      toast(err.message, 'error');
      onChanged();
    } finally {
      setBusy(false);
    }
  }
  const post = (path, body) => api.post(`/api/campaigns/${id}/${path}`, body ?? {}, { silent: true });
  const sure = (msg) => window.confirm(msg);

  const plan = (scheduled_at) => act(() => post('plan', scheduled_at ? { scheduled_at } : {}),
    (r) => `Planned for ${fmtIST(r.planned_start)}`);
  // pause and stop answer {stopped, errors}: errors mean a batch may still be dialling, so never report plain success.
  const halt = (verb, done) => (r) => {
    if (r.errors.length) {
      setHalted({ verb, errors: r.errors });
      toast(`${done}, but a batch may still be dialling: ${r.errors.join('; ')}`, 'error');
      return null;
    }
    return `${done} · ${r.stopped} batch(es) stopped`;
  };
  // A round's answer -> one line: how many were queued and when they dial, or why nobody was.
  const round = (label) => (r) => {
    if (r.skipped) return { msg: `${label}: nobody queued — ${r.skipped}`, type: 'error' };
    const at = r.scheduled_at && new Date(r.scheduled_at).getTime() > Date.now() + 2 * 60000 ? `dials at ${fmtIST(r.scheduled_at)} IST` : 'dialling now';
    return `${label}: ${r.leads} lead(s) queued · ${at}`;
  };
  const pause = () => act(() => post('pause'), halt('Pause', 'Paused'));
  // Leads a paused batch never reached are handed back by the next sync and redialled then, so "nobody new" is normal.
  const resume = () => sure(`Resume this campaign? ${deptWhen()}`) && act(() => post('resume'),
    (r) => (r.leads ? round('Resumed')(r) : 'Resumed · leads the paused batch never reached are redialled after the next sync'));
  const stop = () => sure('Stop this campaign? Running batches are stopped at Bolna and no further rounds will run.')
    && act(() => post('stop'), halt('Stop', 'Stopped'));
  const start = () => sure(`Dial outside the calendar? This competes with whatever is running for the account's lines. ${deptWhen()}`)
    && act(() => post('start-now'), round('Started'));
  const redial = (ignore_gap) => sure(`Redial every eligible lead${ignore_gap ? ', ignoring the gap' : ''}? ${policyWhen()}`)
    && act(() => post('redial', { ignore_gap }), round('Redial'));
  const savePolicy = () => act(() => api.put(`/api/campaigns/${id}/policy`, { redial_policy: policy }, { silent: true }),
    () => { setEditPolicy(false); return 'Policy saved'; });
  async function remove() {
    if (!sure('Delete this campaign and all its leads? Calls already made are kept in Bolna only.')) return;
    setBusy(true);
    try {
      await api.del(`/api/campaigns/${id}`, { toast: 'Campaign deleted' });
      onDeleted();
    } catch {
      setBusy(false); // api.del already toasted
    }
  }

  const canDelete = owner && ['DRAFT', 'COMPLETED', 'STOPPED'].includes(s);
  return (
    <div style={{ display: 'grid', gap: 10 }}>
      {halted && (
        <div role="alert" className="page-hint" style={{ borderLeft: '3px solid var(--red, #c0392b)' }}>
          <strong>{halted.verb} finished with problems. A batch may still be dialling:</strong>
          <ul style={{ margin: '6px 0 6px 18px' }}>{halted.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
          <button type="button" className="btn-link" onClick={() => setHalted(null)}>Dismiss</button>
        </div>
      )}
      {deptQ.error && <div className="muted">Couldn’t load the department’s calling window, so starting is disabled.</div>}
      {s === 'DRAFT' && (
        <>
          <button type="button" className="btn-primary" disabled={busy} onClick={() => plan()}>Queue on the calendar (next free slot)</button>
          <div style={{ display: 'flex', gap: 8 }}>
            <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} aria-label="Plan from (your local time)" />
            <button type="button" className="btn-ghost" disabled={busy || !when} onClick={() => plan(new Date(when).toISOString())}>Plan</button>
          </div>
          {head && <button type="button" className="btn-ghost" disabled={busy || !deptQ.data} onClick={start}>Dial immediately (bypass calendar)</button>}
        </>
      )}
      {(s === 'RUNNING' || s === 'SCHEDULED') && (
        <div style={{ display: 'flex', gap: 8 }}>
          <button type="button" className="btn-ghost" disabled={busy} onClick={pause}>Pause</button>
          <button type="button" className="btn-danger" disabled={busy} onClick={stop}>Stop</button>
        </div>
      )}
      {s === 'PAUSED' && (
        <div style={{ display: 'flex', gap: 8 }}>
          <button type="button" className="btn-primary" disabled={busy || !deptQ.data} onClick={resume}>Resume</button>
          <button type="button" className="btn-danger" disabled={busy} onClick={stop}>Stop</button>
        </div>
      )}
      {s === 'RUNNING' && (
        <div style={{ display: 'flex', gap: 8 }}>
          <button type="button" className="btn-ghost" disabled={busy} onClick={() => redial(false)}>Redial eligible leads</button>
          <button type="button" className="btn-ghost" disabled={busy} onClick={() => redial(true)}>Ignore gap</button>
        </div>
      )}
      <button type="button" className="btn-link" onClick={() => setEditPolicy((v) => !v)}>{editPolicy ? 'Hide' : 'Edit'} redial policy</button>
      {editPolicy && (
        <div>
          {numbers.data
            ? <RedialPolicyFields value={policy} onChange={setPolicy}
              numbers={numbers.data.items.filter((n) => n.active && n.number !== campaign.from_number).map((n) => n.number)} />
            : <div className="muted">Loading…</div>}
          <div className="mt"><button type="button" className="btn-primary" disabled={busy || !numbers.data} onClick={savePolicy}>Save policy</button></div>
        </div>
      )}
      {canDelete && <button type="button" className="btn-danger" disabled={busy} onClick={remove}>Delete campaign</button>}
    </div>
  );
}

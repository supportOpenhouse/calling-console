import { useState } from 'react';
import { Link, Navigate, useNavigate, useParams } from 'react-router-dom';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { canAdminDept, canManageDept, fmtIST, pct, userDepts } from '../utils/format.js';
import { PageHead, PageState, Card, Stat, Empty, OutcomeBadge, TagBadge } from '../components/ui.jsx';
import CampaignControls from '../components/CampaignControls.jsx';
import { STATUS_TONE } from './Campaigns.jsx';

const PER = 50;
const LEAD_TABS = [['', 'All'], ['PENDING', 'Pending'], ['QUEUED', 'Queued'], ['CONNECTED', 'Connected'], ['NOT_CONNECTED', 'Not connected'], ['VOICEMAIL', 'Voicemail'], ['BLOCKED', 'Blocked'], ['FAILED', 'Failed'], ['DONE', 'Done'], ['DNC', 'DNC']];
const BATCH_TONE = { running: 'badge-green', completed: '', scheduled: 'badge-amber', stopped: 'badge-red' };

export default function CampaignDetail() {
  const { id } = useParams();
  return <CampaignDetailFor key={id} id={id} />;
}

function CampaignDetailFor({ id }) {
  const nav = useNavigate();
  const { user } = useAuth();
  const q = useFetch(`/api/campaigns/${id}`);
  const [tab, setTab] = useState('');
  const [page, setPage] = useState(1);
  const leads = useFetch(`/api/campaigns/${id}/leads?page=${page}${tab ? `&status=${tab}` : ''}`);

  if (!q.data) return <><PageHead title="Campaign" /><PageState loading={q.loading} error={q.error} onRetry={q.reload} /></>;
  const { campaign: c, agent, use_case: uc, counters, batches, run } = q.data;
  const dept = c.vertical.toLowerCase();
  if (!userDepts(user).includes(dept)) return <Navigate to="/" replace />;
  const manage = canManageDept(user, dept);
  const by = counters.by_status;
  const total = Object.values(by).reduce((a, b) => a + b, 0);
  const p = c.redial_policy;
  const reloadAll = () => { q.reload(); leads.reload(); };

  return (
    <>
      <PageHead
        title={<>{c.name} <span className={`badge ${STATUS_TONE[c.status] ?? ''}`}>{c.status.toLowerCase()}</span></>}
        sub={<>{agent.name} · <Link to={`/d/${dept}`}>{dept}</Link> · {uc ? uc.name : 'ad-hoc'}{c.purpose ? ` · ${c.purpose}` : ''} · from {c.from_number}
          {run && run.status === 'PLANNED' ? ` · planned ${fmtIST(run.planned_start)} (P${run.priority})` : ''}
          {c.workflow_id && <> · <Link to={`/d/${dept}/workflows/${c.workflow_id}`}>workflow</Link></>}</>}
        actions={<><Link to={`/d/${dept}/calls?campaign_id=${c.id}`} className="btn-ghost">Calls</Link><Link to={`/d/${dept}/campaigns`} className="btn-ghost">← Campaigns</Link></>} />
      <div className="grid-6">
        <Stat label="Leads" value={total} hint={`${(by.PENDING ?? 0) + (by.QUEUED ?? 0)} still to dial`} />
        <Stat label="Dialed" value={counters.dialed} />
        <Stat label="Connected" value={counters.connected} hint={`${pct(counters.connected, counters.dialed)} of dialed`} tone="green" />
        <Stat label="Good" value={counters.good} hint={uc ? pct(counters.good, counters.connected) + ' of connected' : 'no use case'} />
        <Stat label="Carrier blocked" value={by.BLOCKED ?? 0} tone="red" />
        <Stat label="DNC" value={by.DNC ?? 0} />
      </div>
      <div className="mt grid-2">
        <Card title="Controls">
          {manage
            ? <CampaignControls campaign={c} head={canAdminDept(user, dept)} owner={user.role === 'ADMIN'}
              onChanged={reloadAll} onDeleted={() => nav(`/d/${dept}/campaigns`)} />
            : <div className="muted">View only.</div>}
        </Card>
        <Card title={`Rounds (${batches.length})`}>
          {batches.length === 0 ? <Empty>Not started yet.</Empty> : (
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>#</th><th>Kind</th><th>When</th><th>Status</th><th className="num">Dialed</th><th className="num">Conn.</th></tr></thead>
                <tbody>
                  {batches.map((b) => (
                    <tr key={b.id}>
                      <td>{b.round}</td><td>{b.kind === 'FIRST' ? 'first' : 'redial'}<div className="muted">{b.from_number}</div></td>
                      <td>{fmtIST(b.scheduled_at)}</td>
                      <td><span className={`badge ${BATCH_TONE[b.status] ?? ''}`}>{b.status}</span></td>
                      <td className="num">{b.dialed}/{b.total}</td><td className="num">{b.connected}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
      <Card title={`Leads · ${leads.data?.total ?? '…'}`}
        action={<select value={tab} onChange={(e) => { setTab(e.target.value); setPage(1); }} aria-label="Lead status">
          {LEAD_TABS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>}>
        {!leads.data ? <PageState loading={leads.loading} error={leads.error} onRetry={leads.reload} />
          : leads.data.items.length === 0 ? <Empty>No leads{tab ? ' with this status' : ''}.</Empty> : (
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>Phone</th><th>Name</th><th>Variables</th><th>Status</th><th className="num">Tries</th><th>Last call</th><th>Outcome</th><th>Tag</th></tr></thead>
                <tbody>
                  {leads.data.items.map((l) => (
                    <tr key={l.id}>
                      <td>{l.phone}</td><td>{l.name ?? '—'}</td>
                      <td className="muted">{Object.entries(l.variables).filter(([k, v]) => v && !['owner_name', 'name'].includes(k)).map(([k, v]) => `${k}: ${v}`).join(' · ')}</td>
                      <td><span className="badge">{l.status.toLowerCase().replace('_', ' ')}</span></td>
                      <td className="num">{l.attempts}</td>
                      <td className="muted">{fmtIST(l.last_call_at)}</td>
                      <td>{l.last_outcome ? <OutcomeBadge o={l.last_outcome} /> : '—'}</td>
                      <td><TagBadge tag={l.final_tag} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        {leads.data && leads.data.total > PER && (
          <div className="pager">
            <span>Page {page} of {Math.ceil(leads.data.total / PER)}</span>
            <span>
              <button type="button" className="btn-ghost" disabled={page <= 1} onClick={() => setPage(page - 1)}>← Prev</button>{' '}
              <button type="button" className="btn-ghost" disabled={page * PER >= leads.data.total} onClick={() => setPage(page + 1)}>Next →</button>
            </span>
          </div>
        )}
      </Card>
      {c.notes && <div className="muted mt">Notes: {c.notes}</div>}
      <div className="muted mt">
        Redial policy: {p.max_attempts} attempts, {p.gap_minutes} min gap, {p.window_start}–{p.window_end} IST
        {p.rotate_numbers.length ? `, blocked → ${p.rotate_numbers.join(', ')}` : ''}{p.auto_redial ? '' : ' · auto-redial OFF'}
      </div>
    </>
  );
}

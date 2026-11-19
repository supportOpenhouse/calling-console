import { useState } from 'react';
import { Link, Navigate, useParams } from 'react-router-dom';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { canManageDept, fmtIST, pct, userDepts } from '../utils/format.js';
import { PageHead, PageState, Card, Empty } from '../components/ui.jsx';

export const STATUS_TONE = { RUNNING: 'badge-green', COMPLETED: 'badge-green', SCHEDULED: 'badge-amber', PAUSED: 'badge-amber', STOPPED: 'badge-red' };
const STATUSES = ['DRAFT', 'SCHEDULED', 'RUNNING', 'PAUSED', 'COMPLETED', 'STOPPED'];

export default function Campaigns() {
  const { dept } = useParams();
  const { user } = useAuth();
  if (!userDepts(user).includes(dept)) return <Navigate to="/" replace />;
  return <CampaignsFor key={dept} dept={dept} manage={canManageDept(user, dept)} />;
}

function CampaignsFor({ dept, manage }) {
  const [status, setStatus] = useState('');
  const q = useFetch(`/api/departments/${dept}/campaigns${status ? `?status=${status}` : ''}`);
  const head = (
    <PageHead title="Campaigns"
      actions={<>
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
          <option value="">All statuses</option>
          {STATUSES.map((s) => <option key={s} value={s}>{s.toLowerCase()}</option>)}
        </select>
        {manage && <Link to={`/d/${dept}/campaigns/new`} className="btn-primary">New campaign</Link>}
      </>} />
  );
  if (!q.data) return <>{head}<PageState loading={q.loading} error={q.error} onRetry={q.reload} /></>;
  const items = q.data.items;
  return (
    <>
      {head}
      <Card>
        {items.length === 0 ? <Empty>No campaigns{status ? ` with status ${status.toLowerCase()}` : ' yet'}.</Empty> : (
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Campaign</th><th>Agent</th><th>Use case</th><th>Status</th><th className="num">Leads</th><th className="num">Pending</th><th className="num">Dialed</th><th className="num">Connected</th><th className="num">Good</th><th>Created</th></tr></thead>
              <tbody>
                {items.map((c) => (
                  <tr key={c.id}>
                    <td><Link to={`/campaigns/${c.id}`}><strong>{c.name}</strong></Link>{c.purpose && <div className="muted">{c.purpose}</div>}</td>
                    <td>{c.agent_name}</td>
                    <td>{c.use_case_name ?? <span className="muted">ad-hoc</span>}</td>
                    <td><span className={`badge ${STATUS_TONE[c.status] ?? ''}`}>{c.status.toLowerCase()}</span></td>
                    <td className="num">{c.leads}</td><td className="num">{c.pending}</td><td className="num">{c.dialed}</td>
                    <td className="num">{c.connected} <span className="muted">{pct(c.connected, c.dialed)}</span></td>
                    <td className="num">{c.good}</td>
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

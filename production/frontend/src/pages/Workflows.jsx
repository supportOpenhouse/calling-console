import { Link, Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { canManageDept, describeTrigger, fmtIST, userDepts } from '../utils/format.js';
import { PageHead, PageState, Card, Empty } from '../components/ui.jsx';
import WorkflowBuilder from '../components/WorkflowBuilder.jsx';

export default function Workflows() {
  const { dept } = useParams();
  const { user } = useAuth();
  if (!userDepts(user).includes(dept)) return <Navigate to="/" replace />;
  return <WorkflowsFor dept={dept} manage={canManageDept(user, dept)} />;
}

// Parents first (by step order), each followed by its child steps.
function ordered(list) {
  const ids = new Set(list.map((w) => w.id));
  const roots = list.filter((w) => !w.parent_id || !ids.has(w.parent_id));
  return roots.flatMap((w) => [w, ...list.filter((c) => c.parent_id === w.id)]);
}

function WorkflowsFor({ dept, manage }) {
  const q = useFetch(`/api/departments/${dept}/workflows`);
  const newBtn = manage && <Link to={`/d/${dept}/workflows/new`} className="btn-primary">New workflow</Link>;
  const head = (
    <PageHead title="Workflows" actions={newBtn} />
  );
  if (!q.data) return <>{head}<PageState loading={q.loading} error={q.error} onRetry={q.reload} /></>;

  const items = q.data.items;
  const groups = new Map();
  for (const w of items) groups.set(w.use_case_name, [...(groups.get(w.use_case_name) ?? []), w]);
  return (
    <>
      {head}
      {items.length === 0 && <Card><Empty>No workflows yet. A workflow turns a dataset segment into scheduled calls with rules for every result.</Empty></Card>}
      {[...groups.entries()].map(([uc, list]) => (
        <Card key={uc} title={uc}>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr><th>Workflow</th><th>Agent</th><th>Data</th><th>Trigger</th><th className="num">Runs</th><th className="num">Live</th><th>Last run</th><th>State</th></tr>
              </thead>
              <tbody>
                {ordered(list).map((w) => (
                  <tr key={w.id}>
                    <td>
                      <Link to={`/d/${dept}/workflows/${w.id}`}>{w.parent_id && <span className="muted">↳ step </span>}<strong>{w.name}</strong></Link>
                      {w.purpose && <div className="muted">{w.purpose}</div>}
                    </td>
                    <td>{w.agent_name}</td>
                    <td>{w.dataset_name ?? (w.parent_id ? "parent's leads" : '—')}</td>
                    <td>{describeTrigger(w.trigger)}</td>
                    <td className="num">{w.runs}</td><td className="num">{w.live}</td>
                    <td className="muted">{fmtIST(w.last_run_at)}</td>
                    <td>{w.active ? <span className="badge badge-green">active</span> : <span className="badge">off</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ))}
    </>
  );
}

export function WorkflowNew() {
  const { dept } = useParams();
  const { user } = useAuth();
  if (!userDepts(user).includes(dept)) return <Navigate to="/" replace />;
  if (!canManageDept(user, dept)) return <Navigate to={`/d/${dept}/workflows`} replace />;
  return <WorkflowNewFor dept={dept} />;
}

function WorkflowNewFor({ dept }) {
  const nav = useNavigate();
  const [sp] = useSearchParams();
  const q = useFetch(`/api/departments/${dept}/builder-data`);
  const wfQ = useFetch(`/api/departments/${dept}/workflows`);
  const head = <PageHead title="New workflow"
    actions={<Link to={`/d/${dept}/workflows`} className="btn-ghost">Cancel</Link>} />;
  if (!q.data || !wfQ.data) {
    return <>{head}<PageState loading={!q.error && !wfQ.error} error={q.error || wfQ.error} onRetry={() => { q.reload(); wfQ.reload(); }} /></>;
  }
  const data = q.data;
  if (!data.use_cases.length) return <>{head}<Card>Define a use case first: <Link to={`/d/${dept}/use-cases`}>Use cases</Link>.</Card></>;
  if (!data.agents.length) return <>{head}<Card>No agents are assigned to this department yet. Set the vertical on the <Link to="/agents">Agents</Link> page.</Card></>;

  const parent = sp.get('parent');
  const initial = {
    redial_policy: data.default_policy,
    ...(sp.get('dataset_id') ? { dataset_id: sp.get('dataset_id') } : {}),
    ...(parent ? { parent_id: parent, trigger: { type: 'AFTER_STEP', delay_hours: 48, when_tags: data.use_cases[0].tag_taxonomy.good } } : {}),
  };
  return (
    <>
      {head}
      <WorkflowBuilder dept={dept} data={data} initial={initial} workflows={wfQ.data.items} onSaved={(id) => nav(`/d/${dept}/workflows/${id}`)} />
    </>
  );
}

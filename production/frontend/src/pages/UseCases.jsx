import { useState } from 'react';
import { Navigate, useParams } from 'react-router-dom';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { canAdminDept, userDepts } from '../utils/format.js';
import { PageHead, PageState, Card, Empty } from '../components/ui.jsx';
import UseCaseEditor from '../components/UseCaseEditor.jsx';

const ROWS = [['good', 'good', 'badge-good'], ['soft', 'soft', 'badge-amber'], ['retry', 'retry', ''], ['terminal', 'final', 'badge-faint'], ['dnc', 'DNC', 'badge-red']];

export default function UseCases() {
  const { dept } = useParams();
  const { user } = useAuth();
  if (!userDepts(user).includes(dept)) return <Navigate to="/" replace />;
  return <UseCasesFor key={dept} dept={dept} admin={canAdminDept(user, dept)} />;
}

function UseCasesFor({ dept, admin }) {
  const ucsQ = useFetch(`/api/departments/${dept}/use-cases`);
  const agentsQ = useFetch('/api/agents');
  const [editing, setEditing] = useState(null); // use case, or {} for a new one

  const ready = ucsQ.data && agentsQ.data;
  const agentName = (id) => agentsQ.data.items.find((a) => a.id === id)?.name ?? '—';
  const done = () => { setEditing(null); ucsQ.reload(); };

  return (
    <>
      <PageHead title="Use cases"
        actions={admin && <button type="button" className="btn-primary" onClick={() => setEditing({})}>Add use case</button>} />
      <PageState loading={!ready && !ucsQ.error && !agentsQ.error} error={ucsQ.error || agentsQ.error}
        onRetry={() => { ucsQ.reload(); agentsQ.reload(); }} />
      {ready && !ucsQ.data.items.length && <Empty>No use cases defined yet.{admin ? '' : ' Ask the department head.'}</Empty>}
      {ready && ucsQ.data.items.map((u) => (
        <Card key={u.id}
          title={<span><span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: '50%', background: u.color, marginRight: 8 }} />{u.name}{!u.active && <> <span className="badge badge-faint">inactive</span></>}</span>}
          action={<span className="muted">P{u.priority}{admin && <> · <button type="button" className="btn-edit" onClick={() => setEditing(u)}>Edit</button></>}</span>}>
          <div className="muted" style={{ marginBottom: 10 }}>
            {u.description || '—'}
            <div>Default agent: <b>{u.default_agent_id ? agentName(u.default_agent_id) : '—'}</b> · key <code>{u.key}</code></div>
          </div>
          {ROWS.map(([k, label, tone]) => (
            <div key={k} style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6, marginBottom: 5 }}>
              <span className="muted" style={{ width: 56 }}>{label}</span>
              {u.tag_taxonomy[k].length
                ? u.tag_taxonomy[k].map((t) => <span key={t} className={`badge ${tone}`}>{t}</span>)
                : <span className="muted">—</span>}
            </div>
          ))}
        </Card>
      ))}
      {editing && <UseCaseEditor dept={dept} uc={editing.id ? editing : null} agents={agentsQ.data.items}
        onClose={() => setEditing(null)} onSaved={done} />}
    </>
  );
}

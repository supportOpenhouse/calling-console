import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api/client.js';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { canManageDept, fmtIST } from '../utils/format.js';
import { toast } from '../utils/toast.js';
import { PageHead, PageState, Card } from '../components/ui.jsx';

export default function AgentDetail() {
  const { id } = useParams();
  return <AgentDetailFor key={id} id={id} />;
}

function AgentDetailFor({ id }) {
  const { user } = useAuth();
  const q = useFetch(`/api/agents/${id}`);
  const nums = useFetch('/api/numbers');
  if (!q.data || !nums.data) {
    return <><PageHead title="Agent" /><PageState loading={q.loading || nums.loading} error={q.error || nums.error} onRetry={() => { q.reload(); nums.reload(); }} /></>;
  }
  const a = q.data;
  const isOwner = user.role === 'ADMIN';
  const canTest = a.vertical === 'BOTH' ? isOwner : canManageDept(user, a.vertical.toLowerCase());
  const numbers = nums.data.items.filter((n) => n.active).map((n) => n.number);
  const cfg = a.config; // null until the first sync from Bolna
  const notSynced = 'Not synced from Bolna yet — use Refresh agents';
  const llm = cfg?.llm ?? {};
  const tr = cfg?.transcriber ?? {};
  const sy = cfg?.synthesizer ?? {};
  const pc = sy.provider_config ?? {};
  const task = cfg?.task ?? {};
  const dash = (x) => x ?? '—';

  return (
    <>
      <PageHead title={a.name}
        sub={<>{a.vertical === 'BOTH' ? 'both' : a.vertical.toLowerCase()} · <span className="mono">{a.bolna_agent_id}</span> · synced {fmtIST(a.last_synced_at)}</>}
        actions={<Link to="/agents" className="btn-ghost">← Agents</Link>} />
      <div className="two-col">
        <div>
          <Card title="System prompt" action={isOwner ? <span className="muted">Changes go live on the next call</span> : null}>
            {!cfg ? <div className="muted">{notSynced}</div> : isOwner
              ? <PromptEditor agent={a} onSaved={q.reload} />
              : <>
                <div className="muted">Welcome: {a.welcome_message || '—'}</div>
                <pre className="prompt-box">{cfg.prompt || '—'}</pre>
              </>}
          </Card>
        </div>
        <div>
          {isOwner && <Card title="Console settings"><MetaForm agent={a} numbers={numbers} onSaved={q.reload} /></Card>}
          <Card title="Bolna configuration">
            <dl className="kv"><dt>Variables</dt><dd>{a.variables.join(', ') || '—'}</dd></dl>
            {!cfg ? <div className="muted">{notSynced}</div> : <dl className="kv">
              <dt>Welcome</dt><dd>{a.welcome_message || '—'}</dd>
              <dt>LLM</dt><dd>{`${dash(llm.model)} · temp ${dash(llm.temperature)} · max ${dash(llm.max_tokens)} tok`}</dd>
              <dt>Transcriber</dt><dd>{`${tr.model ?? tr.provider ?? '—'} · ${tr.language ?? ''} · endpoint ${dash(tr.endpointing)}`}</dd>
              <dt>Voice</dt><dd>{`${dash(sy.provider)} · ${pc.voice ?? pc.voice_id ?? ''} ${pc.model ?? ''}`}</dd>
              <dt>Turn-taking</dt><dd>{`interrupt ${dash(task.number_of_words_for_interruption)} words · silence hangup ${dash(task.hangup_after_silence)}s · max ${dash(task.call_terminate)}s`}</dd>
            </dl>}
          </Card>
          {canTest && <Card title="Test call"><TestCall agent={a} numbers={numbers} /></Card>}
        </div>
      </div>
    </>
  );
}

function MetaForm({ agent, numbers, onSaved }) {
  const [v, setV] = useState({
    vertical: agent.vertical, description: agent.description ?? '',
    caller_name: agent.caller_name ?? '', default_from_number: agent.default_from_number ?? '',
  });
  const [busy, setBusy] = useState(false);
  const set = (k, x) => setV((p) => ({ ...p, [k]: x }));

  async function save() {
    setBusy(true);
    try {
      await api.put(`/api/agents/${agent.id}`, v, { silent: true });
      toast('Saved', 'success');
      onSaved();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="form-grid">
      <div className="form-wide-2"><label>Department</label>
        <select value={v.vertical} onChange={(e) => set('vertical', e.target.value)}>
          {['SUPPLY', 'DEMAND', 'BOTH'].map((d) => <option key={d} value={d}>{d.toLowerCase()}</option>)}
        </select></div>
      <div className="form-wide-2"><label>Caller name (shown on transcripts)</label>
        <input value={v.caller_name} onChange={(e) => set('caller_name', e.target.value)} /></div>
      <div className="form-wide-2"><label>Default calling number</label>
        <select value={v.default_from_number} onChange={(e) => set('default_from_number', e.target.value)}>
          <option value="">—</option>
          {agent.default_from_number && !numbers.includes(agent.default_from_number)
            && <option value={agent.default_from_number}>{agent.default_from_number} (inactive)</option>}
          {numbers.map((n) => <option key={n} value={n}>{n}</option>)}
        </select></div>
      <div className="form-wide-2"><label>Description</label>
        <input value={v.description} onChange={(e) => set('description', e.target.value)} /></div>
      <div className="form-wide-2"><button type="button" className="btn-primary" disabled={busy} onClick={save}>Save</button></div>
    </div>
  );
}

function PromptEditor({ agent, onSaved }) {
  const prompt = agent.config.prompt ?? '';
  const welcome = agent.welcome_message ?? '';
  const [edit, setEdit] = useState(false);
  const [p, setP] = useState(prompt);
  const [w, setW] = useState(welcome);
  const [busy, setBusy] = useState(false);

  async function push() {
    if (!window.confirm('Push this prompt to Bolna? It applies to every call from now on.')) return;
    setBusy(true);
    try {
      await api.put(`/api/agents/${agent.id}/prompt`, { system_prompt: p, welcome_message: w }, { silent: true });
      toast('Prompt updated on Bolna', 'success');
      setEdit(false);
      onSaved();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  if (!edit) {
    return (
      <>
        <div className="muted">Welcome: {welcome || '—'}</div>
        <pre className="prompt-box">{prompt || '—'}</pre>
        <div className="prompt-foot">
          <span className="muted">{prompt.length.toLocaleString()} chars</span>
          <button type="button" className="btn-ghost" onClick={() => { setP(prompt); setW(welcome); setEdit(true); }}>Edit prompt</button>
        </div>
      </>
    );
  }
  return (
    <>
      <label>Welcome message</label>
      <input value={w} onChange={(e) => setW(e.target.value)} />
      <label>System prompt</label>
      <textarea className="prompt-edit" rows={28} value={p} onChange={(e) => setP(e.target.value)} />
      <div className="prompt-foot">
        <span className="muted">{p.length.toLocaleString()} chars</span>
        <span>
          <button type="button" className="btn-primary" disabled={busy || !p.trim()} onClick={push}>Push to Bolna</button>{' '}
          <button type="button" className="btn-ghost" onClick={() => setEdit(false)}>Cancel</button>
        </span>
      </div>
    </>
  );
}

function TestCall({ agent, numbers }) {
  const [to, setTo] = useState('');
  const [from, setFrom] = useState(agent.default_from_number ?? numbers[0] ?? '');
  const [vars, setVars] = useState(() => Object.fromEntries(agent.variables.map((x) => [x, ''])));
  const [busy, setBusy] = useState(false);

  async function place() {
    setBusy(true);
    try {
      await api.post(`/api/agents/${agent.id}/test-call`, { to, from_number: from, variables: vars }, { silent: true });
      toast('Test call placed', 'success');
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="form-grid">
      <div><label>To</label><input value={to} placeholder="+91 98…" onChange={(e) => setTo(e.target.value)} /></div>
      <div><label>From</label>
        <select value={from} onChange={(e) => setFrom(e.target.value)}>
          {!from && <option value="">—</option>}
          {agent.default_from_number && !numbers.includes(agent.default_from_number)
            && <option value={agent.default_from_number}>{agent.default_from_number} (inactive)</option>}
          {numbers.map((n) => <option key={n} value={n}>{n}</option>)}
        </select></div>
      {agent.variables.map((x) => (
        <div key={x} className="form-wide-2"><label>{x}</label>
          <input value={vars[x]} onChange={(e) => setVars((p) => ({ ...p, [x]: e.target.value }))} /></div>
      ))}
      <div className="form-wide-2"><button type="button" className="btn-primary" disabled={busy || !to || !from} onClick={place}>Place test call</button></div>
    </div>
  );
}

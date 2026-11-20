import { useState } from 'react';
import { Link, Navigate, useParams, useSearchParams } from 'react-router-dom';
import { download } from '../api/client.js';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { toast } from '../utils/toast.js';
import { OUTCOME_LABEL, fmtDur, fmtHangup, fmtIST, userDepts } from '../utils/format.js';
import { PageHead, PageState, Card, Empty, OutcomeBadge, SourceBadge, TagBadge } from '../components/ui.jsx';

const PER = 50;
const FILTER_KEYS = ['agent_id', 'campaign_id', 'outcome', 'tag', 'from', 'to', 'q'];

export default function Calls() {
  const { dept } = useParams();
  const { user } = useAuth();
  if (!userDepts(user).includes(dept)) return <Navigate to="/" replace />;
  return <CallsFor key={dept} dept={dept} />;
}

function CallsFor({ dept }) {
  const [sp, setSp] = useSearchParams();
  const f = Object.fromEntries(FILTER_KEYS.map((k) => [k, sp.get(k) ?? '']));
  const page = Math.max(1, Number(sp.get('page')) || 1);
  const [qInput, setQInput] = useState(f.q);
  const [busy, setBusy] = useState(false);

  const filterQs = () => new URLSearchParams(FILTER_KEYS.filter((k) => f[k]).map((k) => [k, f[k]]));
  const q = useFetch(`/api/departments/${dept}/calls?${new URLSearchParams([...filterQs(), ['page', page]])}`);
  const agentsQ = useFetch('/api/agents');
  const campaignsQ = useFetch(`/api/departments/${dept}/campaigns`);
  const ucsQ = useFetch(`/api/departments/${dept}/use-cases`);

  function setFilter(k, v) {
    const next = new URLSearchParams(sp);
    if (v) next.set(k, v); else next.delete(k);
    next.delete('page');
    setSp(next);
  }
  function setPage(p) {
    const next = new URLSearchParams(sp);
    next.set('page', p);
    setSp(next);
  }

  async function exportCsv() {
    setBusy(true);
    try {
      const blob = await download(`/api/departments/${dept}/calls/export.csv?${filterQs()}`);
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `calls_${Date.now()}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(a.href);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  const agents = (agentsQ.data?.items ?? []).filter((a) => a.vertical === 'BOTH' || a.vertical.toLowerCase() === dept);
  const tags = new Set((ucsQ.data?.items ?? []).flatMap((u) => Object.values(u.tag_taxonomy).flat()));
  if (f.tag) tags.add(f.tag); // a tag from the URL (dashboard link) must stay selectable
  const items = q.data?.items ?? [];
  const total = q.data?.total ?? 0;

  return (
    <>
      <PageHead title="Calls" sub={q.data ? `${total.toLocaleString('en-IN')} calls match` : ' '}
        actions={<button type="button" className="btn-ghost" disabled={busy} onClick={exportCsv}>Export CSV</button>} />
      <div className="toolbar">
        <select value={f.agent_id} onChange={(e) => setFilter('agent_id', e.target.value)} aria-label="Agent">
          <option value="">All agents</option>
          {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <select value={f.campaign_id} onChange={(e) => setFilter('campaign_id', e.target.value)} aria-label="Campaign">
          <option value="">All campaigns</option>
          {(campaignsQ.data?.items ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <select value={f.outcome} onChange={(e) => setFilter('outcome', e.target.value)} aria-label="Outcome">
          <option value="">All outcomes</option>
          {Object.entries(OUTCOME_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
        <select value={f.tag} onChange={(e) => setFilter('tag', e.target.value)} aria-label="Tag">
          <option value="">All tags</option>
          {[...tags].sort().map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <input type="date" value={f.from} onChange={(e) => setFilter('from', e.target.value)} aria-label="From date" />
        <input type="date" value={f.to} onChange={(e) => setFilter('to', e.target.value)} aria-label="To date" />
        <form onSubmit={(e) => { e.preventDefault(); setFilter('q', qInput.trim()); }}>
          <input type="search" value={qInput} onChange={(e) => setQInput(e.target.value)} placeholder="phone / name / words" />
        </form>
      </div>
      <Card>
        <PageState loading={q.loading} error={q.error} onRetry={q.reload} />
        {q.data && !items.length && <Empty>No calls match.</Empty>}
        {items.length > 0 && (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr><th>When</th><th>Source</th><th>Phone</th><th>Name</th><th>Agent / campaign</th><th>Outcome</th><th>Hangup</th><th className="num">Dur.</th><th>Tag</th><th>Rec.</th></tr>
              </thead>
              <tbody>
                {items.map((c) => (
                  <tr key={c.id}>
                    <td style={{ whiteSpace: 'nowrap' }}><Link to={`/calls/${c.id}`}>{fmtIST(c.started_at)}</Link></td>
                    <td><SourceBadge previous={c.source === 'previous'} /></td>
                    <td><code>{c.phone}</code></td>
                    <td>{c.lead_name || '—'}</td>
                    <td>{c.agent_name ?? '—'}<div className="muted">{c.campaign_name}</div></td>
                    <td><OutcomeBadge o={c.outcome} /></td>
                    <td className="muted">{fmtHangup(c.hangup_code, c.status)}</td>
                    <td className="num">{fmtDur(c.duration_sec)}{c.human_turns ? <div className="muted">{c.human_turns} turns</div> : null}</td>
                    <td><TagBadge tag={c.tag} /></td>
                    <td>{c.has_recording ? <Link to={`/calls/${c.id}`}>▶ play</Link> : '—'}</td>
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
    </>
  );
}

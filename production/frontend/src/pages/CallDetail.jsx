import { useState } from 'react';
import { Link, Navigate, useParams } from 'react-router-dom';
import { api } from '../api/client.js';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { canManageDept, fmtDur, fmtHangup, fmtIST, queuedWarning, userDepts } from '../utils/format.js';
import { toast } from '../utils/toast.js';
import { PageHead, PageState, Card, OutcomeBadge, TagBadge } from '../components/ui.jsx';
import RecordingPlayer from '../components/RecordingPlayer.jsx';

// Port of parseTranscript in the old console: "assistant: …" / "user: …" lines, continuation lines join the last turn.
function parseTranscript(t) {
  if (!t) return [];
  const out = [];
  for (const line of t.split(/\r?\n/)) {
    const m = line.match(/^\s*(assistant|agent|user)\s*:\s*(.*)$/i);
    if (!m) {
      if (out.length && line.trim()) out[out.length - 1].text += ` ${line.trim()}`;
      continue;
    }
    out.push({ who: m[1].toLowerCase() === 'user' ? 'user' : 'agent', text: m[2].trim() });
  }
  return out;
}

function KV({ rows }) {
  return <dl className="kv">{rows.map(([k, v]) => <div key={k} style={{ display: 'contents' }}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>;
}

export default function CallDetail() {
  const { id } = useParams();
  return <CallDetailFor key={id} id={id} />;
}

function CallDetailFor({ id }) {
  const { user } = useAuth();
  const q = useFetch(`/api/calls/${id}`);
  if (!q.data) return <><PageHead title="Call" /><PageState loading={q.loading} error={q.error} onRetry={q.reload} /></>;
  const dept = q.data.campaign?.vertical.toLowerCase() ?? null;
  if (dept && !userDepts(user).includes(dept)) return <Navigate to="/" replace />;
  return <CallView data={q.data} dept={dept} manage={dept ? canManageDept(user, dept) : false} reload={q.reload} />;
}

function CallView({ data, dept, manage, reload }) {
  const { call: c, campaign, agent, lead, fields, history } = data;
  const ucsQ = useFetch(manage && dept ? `/api/departments/${dept}/use-cases` : null);
  const [newTag, setNewTag] = useState(c.tag ?? '');
  const [busy, setBusy] = useState(false);
  const turns = parseTranscript(c.transcript);
  const hasRecording = !!(c.recording_url || c.recording_key);

  // Tags the call's use case defines (all of the department's when the campaign is ad hoc), plus the current one.
  const ucs = ucsQ.data?.items ?? [];
  const scoped = campaign?.use_case_id ? ucs.filter((u) => u.id === campaign.use_case_id) : ucs;
  const tagOptions = [...new Set([...scoped.flatMap((u) => Object.values(u.tag_taxonomy).flat()), ...(c.tag ? [c.tag] : [])])];

  async function act(fn) {
    setBusy(true);
    try { await fn(); reload(); } catch { /* api client already toasted */ } finally { setBusy(false); }
  }
  const saveTag = () => act(() => api.post(`/api/calls/${c.id}/tag`, { tag: newTag }, { toast: 'Tag updated' }));
  async function markDnc() {
    if (!window.confirm('Mark this phone Do-Not-Call? It will be excluded from every redial.')) return;
    setBusy(true);
    try {
      const warning = queuedWarning(await api.post(`/api/calls/${c.id}/dnc`, {}, { silent: true }));
      toast(warning ? `Marked DNC. ${warning}` : 'Marked DNC', warning ? 'error' : 'success');
      reload();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHead
        title={<><code>{c.phone}</code> <OutcomeBadge o={c.outcome} /> <TagBadge tag={c.tag} /></>}
        sub={<>{lead?.name ? `${lead.name} · ` : ''}{agent?.name}{campaign && <> · <Link to={`/campaigns/${campaign.id}`}>{campaign.name}</Link></>} · {fmtIST(c.started_at, true)} · {fmtDur(c.duration_sec)}</>}
        actions={<Link to={dept ? `/d/${dept}/calls` : '/'} className="btn-ghost">← Calls</Link>} />
      <div className="split-2">
        <div className="stack">
          <Card title="Recording">
            {hasRecording ? (
              <>
                <RecordingPlayer callId={c.id} />
                <div className="muted mt">{c.recording_key ? 'Archived copy' : 'Streaming from Bolna (not yet archived)'}</div>
              </>
            ) : <div className="muted">No recording for this call{c.outcome !== 'CONNECTED' ? ' — the call never connected.' : '.'}</div>}
          </Card>
          <Card title="Transcript">
            {turns.length ? (
              <ol className="transcript">
                {turns.map((t, i) => (
                  <li key={i} className={t.who}>
                    <div className="who">{t.who === 'user' ? (lead?.name || 'Owner') : (agent?.caller_name || 'Agent')}</div>{t.text}
                  </li>
                ))}
              </ol>
            ) : <div className="muted">No transcript.</div>}
          </Card>
          {c.summary && <Card title="Bolna summary"><p style={{ margin: 0 }}>{c.summary}</p></Card>}
        </div>
        <div className="stack">
          <Card title="Extracted">
            {Object.keys(fields).length
              ? <KV rows={Object.entries(fields).map(([k, v]) => [k.replace(/_/g, ' '), v || '—'])} />
              : <div className="muted">No extraction yet (Bolna fills this a minute after the call).</div>}
          </Card>
          <Card title="Call details">
            <KV rows={[
              ['Bolna status', c.status],
              ['Hangup', c.hangup_code ? fmtHangup(c.hangup_code) : (c.hangup_reason || '—')],
              ['Human turns', String(c.human_turns)],
              ['From', c.from_number || '—'],
              ['Cost', c.cost != null ? `₹${c.cost.toFixed(2)}` : '—'],
              ['Execution', <code key="x">{c.bolna_execution_id}</code>],
            ]} />
          </Card>
          {lead && (
            <Card title="Lead">
              <KV rows={[
                ['Status', lead.status.toLowerCase()], ['Attempts', String(lead.attempts)],
                ...Object.entries(lead.variables ?? {}).filter(([, v]) => v).map(([k, v]) => [k.replace(/_/g, ' '), v]),
              ]} />
              {history.length > 1 && (
                <div className="mt">
                  <div className="muted">All attempts</div>
                  {history.map((h) => (
                    <div key={h.id} style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0' }}>
                      <Link to={`/calls/${h.id}`} style={h.id === c.id ? { fontWeight: 700 } : undefined}>{fmtIST(h.started_at)}</Link>
                      <OutcomeBadge o={h.outcome} />
                    </div>
                  ))}
                </div>
              )}
            </Card>
          )}
          {manage && (
            <Card title="Actions">
              <div style={{ display: 'grid', gap: 10 }}>
                <label className="muted" htmlFor="retag">Correct the tag (QA)</label>
                <div className="ho-ctl">
                  <select id="retag" value={newTag} onChange={(e) => setNewTag(e.target.value)} disabled={busy}>
                    {!newTag && <option value="">select…</option>}
                    {tagOptions.map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                  <button type="button" className="btn-primary" disabled={busy || !newTag || newTag === c.tag} onClick={saveTag}>Save</button>
                </div>
                <button type="button" className="btn-danger" disabled={busy} onClick={markDnc}>Mark DNC</button>
              </div>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}

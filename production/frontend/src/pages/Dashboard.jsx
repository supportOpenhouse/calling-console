import { Link, Navigate, useParams, useSearchParams } from 'react-router-dom';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { OUTCOME_LABEL, fmtIST, pct, userDepts } from '../utils/format.js';
import { PageHead, PageState, Card, Stat, Bars, Table, TagBadge, Empty } from '../components/ui.jsx';

const RANGES = [[1, 'Today'], [7, '7d'], [30, '30d']];
const inr = (n) => Number(n).toLocaleString('en-IN');
const cap = (s) => s[0].toUpperCase() + s.slice(1);
// IST calendar date `days` ago, for the calls page's `from` filter.
const istDate = (days) => new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata' }).format(new Date(Date.now() - days * 86400000));

export default function Dashboard() {
  const { dept } = useParams();
  const { user } = useAuth();
  if (!userDepts(user).includes(dept)) return <Navigate to="/" replace />;
  return <DashboardFor key={dept} dept={dept} />;
}

function DashboardFor({ dept }) {
  const [sp, setSp] = useSearchParams();
  const days = Number(sp.get('days') || 7);
  const ucId = sp.get('uc') || '';
  const rangeOk = RANGES.some(([d]) => d === days);
  const ucsQ = useFetch(`/api/departments/${dept}/use-cases`);
  const q = new URLSearchParams({ days });
  if (ucId) q.set('use_case_id', ucId);
  const { data, error, loading, reload } = useFetch(rangeOk ? `/api/departments/${dept}/dashboard?${q}` : null);
  const ucs = ucsQ.data?.items;

  const base = `/d/${dept}`;
  const setParam = (k, v) => setSp((p) => { const n = new URLSearchParams(p); if (v) n.set(k, v); else n.delete(k); return n; });
  const ucName = ucs?.find((u) => u.id === ucId)?.name;
  const unknownUc = ucId && ucs && !ucName;

  const head = (
    <PageHead title={`${cap(dept)} dashboard`}
      actions={
        <div className="range-btns">
          <select value={ucId} onChange={(e) => setParam('uc', e.target.value)} aria-label="Use case">
            <option value="">All use cases</option>
            {unknownUc && <option value={ucId}>Unknown use case</option>}
            {(ucs || []).map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
          </select>
          {ucsQ.error && <span className="muted" style={{ color: 'var(--red-fg)' }}>Couldn’t load use cases</span>}
          {RANGES.map(([d, label]) => (
            <button key={d} type="button" className={d === days ? 'btn-primary' : 'btn-ghost'} onClick={() => setParam('days', String(d))}>{label}</button>
          ))}
        </div>
      } />
  );
  if (!rangeOk) return <>{head}<PageState error="Unknown range" /></>;
  if (!data) return <>{head}<PageState loading={loading} error={error} onRetry={reload} /></>;

  const { stats: s, funnel, outcomes, tags, agents, up_next: upNext, open_handoffs: handoffs } = data;
  const dialed = Number(s.dialed), connected = Number(s.connected);
  const good = funnel.reduce((a, r) => a + r.good, 0);
  const openH = funnel.reduce((a, r) => a + r.open, 0);
  const cost = Number(s.cost);
  const from = istDate(days);
  const callsLink = (k, v) => `${base}/calls?${new URLSearchParams({ [k]: v, from })}`;

  return (
    <>
      {head}
      <div className="grid-6">
        <Stat label="Dialed" value={inr(dialed)} hint={`${s.unique_phones} unique numbers`} />
        <Stat label="Connected" value={inr(connected)} hint={`${pct(connected, dialed)} dial → connect`}
          tone={dialed && connected / dialed >= 0.5 ? 'green' : 'amber'} />
        <Stat label="Good tags" value={good} hint={`${pct(good, connected)} of connected`} tone="green" />
        <Stat label="Carrier blocked" value={s.blocked} hint={pct(Number(s.blocked), dialed)} tone="red" />
        <Stat label="Open hand-offs" value={openH} hint="waiting for an RM" tone="brand" />
        <Stat label="Spend" value={`₹${inr(Math.round(cost))}`}
          hint={`${Math.round(Number(s.minutes))} talk-min · ₹${(cost / Math.max(1, good)).toFixed(0)} per good lead`} />
      </div>

      <div className="mt">
        <Card title="Funnel by use case" action={<Link className="link-sm" to={`${base}/use-cases`}>manage →</Link>}>
          {funnel.length ? (
            <Table rows={funnel} cols={[
              { key: 'name', label: 'Use case', render: (r) => (
                <button type="button" className="btn-link" style={{ padding: 0, color: 'inherit' }} onClick={() => setParam('uc', r.use_case_id)}>
                  <span className="uc-dot" style={{ background: r.color }} />{r.name}
                </button>) },
              { key: 'leads', label: 'Leads loaded', align: 'right' },
              { key: 'dialed', label: 'Dialed', align: 'right' },
              { key: 'connected', label: 'Connected', align: 'right', render: (r) => <>{r.connected} <span className="muted">{pct(r.connected, r.dialed)}</span></> },
              { key: 'good', label: 'Good', align: 'right', render: (r) => <span className="val-green">{r.good}</span> },
              { key: 'soft', label: 'Soft', align: 'right', render: (r) => <span className="val-amber">{r.soft}</span> },
              { key: 'conv', label: 'Conv.', align: 'right', render: (r) => pct(r.good, r.connected) },
              { key: 'handoffs', label: 'Handed off', align: 'right' },
              { key: 'open', label: 'Open', align: 'right', render: (r) => <span className="val-orange">{r.open}</span> },
            ]} />
          ) : <div className="muted">No use cases yet — define them under Use cases.</div>}
        </Card>
      </div>

      <div className="mt grid-3">
        <Card title="Call outcomes">
          <Bars items={outcomes.map((o) => ({ label: OUTCOME_LABEL[o.key], n: Number(o.n), href: callsLink('outcome', o.key) }))} />
        </Card>
        <Card title="Tags on connected calls">
          {/* 'untagged' is NULL in the DB; the calls filter is an exact match, so it gets no link. */}
          <Bars items={tags.map((t) => ({ label: t.tag.replace(/_/g, ' '), n: Number(t.n), href: t.tag === 'untagged' ? undefined : callsLink('tag', t.tag) }))} />
        </Card>
        <Card title="Agents">
          {agents.length ? (
            <Table rows={agents} cols={[
              { key: 'name', label: 'Agent', render: (a) => <Link to={`/agents/${a.id}`}>{a.name}</Link> },
              { key: 'dialed', label: 'Dialed', align: 'right' },
              { key: 'rate', label: 'Connect', align: 'right', render: (a) => pct(Number(a.connected), Number(a.dialed)) },
            ]} />
          ) : <div className="muted">No agents assigned to this department. Set the vertical on the Agents page.</div>}
        </Card>
      </div>

      <div className="mt grid-2">
        <Card title="Up next on the calendar" action={<Link className="link-sm" to={`${base}/schedule`}>schedule →</Link>}>
          {upNext.length ? (
            <Table rows={upNext} cols={[
              { key: 'name', label: 'Run', render: (r) => (
                <>
                  {r.campaign_id ? <Link to={`/campaigns/${r.campaign_id}`}>{r.campaign_name || r.purpose || 'run'}</Link> : (r.purpose || 'run')}
                  <div className="muted">{r.use_case_name || '—'} · {r.est_leads} leads · P{r.priority}</div>
                </>) },
              { key: 'when', label: 'When', align: 'right', render: (r) => (r.status === 'RUNNING' ? <span className="badge badge-green">running</span> : fmtIST(r.planned_start)) },
            ]} />
          ) : <div className="muted">Nothing planned.</div>}
        </Card>
        <Card title="Open hand-offs" action={<Link className="link-sm" to={`${base}/handoffs`}>all →</Link>}>
          {handoffs.length ? (
            <Table rows={handoffs} cols={[
              { key: 'name', label: 'Lead', render: (h) => (
                <>
                  {h.name || h.phone}
                  <div className="muted">{h.details?.society_name || h.details?.society_and_location || h.use_case_name || ''} · {fmtIST(h.created_at)}</div>
                </>) },
              { key: 'tag', label: 'Tag', align: 'right', render: (h) => <TagBadge tag={h.tag} /> },
            ]} />
          ) : <Empty>No open hand-offs.</Empty>}
        </Card>
      </div>

      <div className="mt"><SyncStatus /></div>
    </>
  );
}

// A sync that finished longer ago than this is shown as stale: the background job may be down.
const SYNC_STALE_MS = 15 * 60000;

// The last sync, for everyone signed in: when it finished, its errors (collapsible) and a stale flag.
function SyncStatus() {
  const { data, error, loading, reload } = useFetch('/api/sync/status');
  if (!data) return <Card title="Sync"><PageState loading={loading} error={error} onRetry={reload} /></Card>;
  const r = data.last_report;
  const stale = !r || Date.now() - new Date(r.finished_at).getTime() > SYNC_STALE_MS;
  const errors = r?.errors ?? [];
  return (
    <Card title="Sync" action={<span className={`badge ${stale ? 'badge-red' : 'badge-green'}`}>{stale ? 'stale' : 'up to date'}</span>}>
      {!r ? <div className="tone-red">Never synced — call results are not coming in.</div> : (
        <>
          <div className={stale ? 'tone-red' : 'muted'}>
            Last sync finished {fmtIST(r.finished_at, true)} IST · {r.calls_upserted} calls · {r.runs_started} runs started · {r.triggers_fired} triggers
            {stale && ' — more than 15 minutes ago: the background sync may be down.'}
          </div>
          {errors.length === 0 ? <div className="muted mt">No errors.</div> : (
            <details className="mt">
              <summary className="tone-red">{errors.length} error(s)</summary>
              <ul>{errors.map((e, i) => <li key={i} className="tone-red">{e}</li>)}</ul>
            </details>
          )}
        </>
      )}
    </Card>
  );
}

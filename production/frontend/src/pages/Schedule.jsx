import { useState } from 'react';
import { Link, Navigate, useParams, useSearchParams } from 'react-router-dom';
import { api } from '../api/client.js';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { toast } from '../utils/toast.js';
import { canAdminDept, canManageDept, fmtIST, userDepts } from '../utils/format.js';
import { PageHead, PageState, Card, Empty } from '../components/ui.jsx';

// IST is a fixed UTC+5:30 (no DST), so day math is done on a +330 min shifted UTC clock.
const IST_MIN = 330;
const DAY_MS = 86400000;
const HOUR_PX = 56;
const ymd = (ms) => new Date(ms).toISOString().slice(0, 10); // ms = shifted "IST wall clock" instant
const hm = (s) => { const [h, m] = s.split(':').map(Number); return h * 60 + m; };
const istWall = (iso) => new Date(iso).getTime() + IST_MIN * 60000;

// Midnight IST of `wallDayMs` (a shifted-clock day start) as a real instant.
const istMidnightToUtc = (wallDayMs) => new Date(wallDayMs - IST_MIN * 60000);

function mondayOf(weekParam) {
  let day;
  if (weekParam) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(weekParam) || Number.isNaN(Date.parse(weekParam))) throw new Error(`bad week: ${weekParam}`);
    day = Date.parse(weekParam);
  } else {
    const now = Date.now() + IST_MIN * 60000;
    day = now - (now % DAY_MS);
  }
  const dow = (new Date(day).getUTCDay() + 6) % 7; // Monday = 0
  return day - dow * DAY_MS;
}

export default function Schedule() {
  const { dept } = useParams();
  const { user } = useAuth();
  const [sp] = useSearchParams();
  if (!userDepts(user).includes(dept)) return <Navigate to="/" replace />;
  let monday;
  try { monday = mondayOf(sp.get('week')); } catch { return <Navigate to={`/d/${dept}/schedule`} replace />; }
  return <ScheduleFor dept={dept} monday={monday} manage={canManageDept(user, dept)} admin={canAdminDept(user, dept)} />;
}

function ScheduleFor({ dept, monday, manage, admin }) {
  const from = istMidnightToUtc(monday).toISOString();
  const to = new Date(istMidnightToUtc(monday + 7 * DAY_MS).getTime() - 1000).toISOString();
  const cfgQ = useFetch(`/api/departments/${dept}`);
  const q = useFetch(`/api/departments/${dept}/schedule?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`);
  const [busy, setBusy] = useState(false);

  const shift = (n) => `/d/${dept}/schedule?week=${ymd(monday + n * 7 * DAY_MS)}`;
  const nav = (
    <>
      <Link to={shift(-1)} className="btn-ghost">← Prev</Link>
      <Link to={`/d/${dept}/schedule`} className="btn-ghost">This week</Link>
      <Link to={shift(1)} className="btn-ghost">Next →</Link>
    </>
  );
  if (!q.data || !cfgQ.data) {
    return (
      <>
        <PageHead title="Schedule" actions={nav} />
        <PageState loading={!q.error && !cfgQ.error} error={q.error || cfgQ.error} onRetry={() => { q.reload(); cfgQ.reload(); }} />
      </>
    );
  }
  const cfg = cfgQ.data;
  const { queue } = q.data;
  const runs = q.data.runs.filter((r) => r.status !== 'CANCELLED'); // the endpoint returns cancelled runs too; they no longer occupy the calendar
  const ws = hm(cfg.window_start), we = hm(cfg.window_end);
  if (!(we > ws)) throw new Error(`department window ${cfg.window_start}-${cfg.window_end} does not fit one day`);
  const first = Math.floor(ws / 60), last = Math.ceil(we / 60);
  const todayKey = ymd(Date.now() + IST_MIN * 60000);
  const days = Array.from({ length: 7 }, (_, i) => monday + i * DAY_MS);
  const deptKey = dept.toUpperCase();

  async function act(fn, ok) {
    setBusy(true);
    try { await fn(); if (ok) toast(ok, 'success'); } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); q.reload(); }
  }
  const replan = () => act(() => api.post(`/api/departments/${dept}/replan`, {}, { silent: true }), 'Re-planned');
  const cancel = (r) => window.confirm('Remove this run from the calendar? The campaign goes back to draft.')
    && act(() => api.post(`/api/runs/${r.id}/cancel`, {}, { silent: true }), 'Run cancelled');
  const setPriority = (r, priority) => act(() => api.post(`/api/runs/${r.id}/priority`, { priority }, { silent: true }), 'Priority updated');

  // Clip a run to one day's calling window; null when nothing of it falls on that day.
  function blockFor(r, dayMs) {
    if (!r.planned_start || !r.planned_end) return null;
    const s = Math.max(istWall(r.planned_start) - dayMs, 0) / 60000;
    const e = Math.min(istWall(r.planned_end) - dayMs, DAY_MS) / 60000;
    const cs = Math.max(s, first * 60), ce = Math.min(e, last * 60);
    if (ce <= cs) return null;
    return { top: ((cs - first * 60) / 60) * HOUR_PX, height: Math.max(((ce - cs) / 60) * HOUR_PX, 14) };
  }

  return (
    <>
      <PageHead title="Schedule"
        sub={`${cfg.name} window ${cfg.window_start}–${cfg.window_end} IST · ${cfg.line_share} lines`}
        actions={<>{nav}{manage && <button type="button" className="btn-primary" disabled={busy} onClick={replan}>{busy ? 'Planning…' : 'Re-plan'}</button>}</>} />
      <Card title={`${fmtIST(from).slice(0, 6)} – ${fmtIST(to).slice(0, 6)}`}>
        <div className="sched-scroll">
          <div className="sched-grid">
            <div />
            {days.map((d) => <div key={d} className={`sched-dayhead${ymd(d) === todayKey ? ' today' : ''}`}>
              {new Intl.DateTimeFormat('en-IN', { timeZone: 'UTC', weekday: 'short', day: '2-digit', month: 'short' }).format(d)}
            </div>)}
            <div className="sched-hours" style={{ height: (last - first) * HOUR_PX }}>
              {Array.from({ length: last - first }, (_, i) => (
                <span key={i} className="sched-hour" style={{ top: i * HOUR_PX }}>{String(first + i).padStart(2, '0')}:00</span>
              ))}
            </div>
            {days.map((d) => (
              <div key={d} className="sched-col" style={{ height: (last - first) * HOUR_PX }} data-day={ymd(d)}>
                {Array.from({ length: last - first }, (_, i) => <div key={i} className="sched-line" style={{ top: i * HOUR_PX }} />)}
                {runs.map((r) => {
                  const b = blockFor(r, d);
                  if (!b) return null;
                  const mine = r.department === deptKey;
                  const named = 'campaign_name' in r;
                  const label = named ? `${fmtIST(r.planned_start).split(', ').pop()} ${r.campaign_name || r.purpose || 'run'} · ${r.est_leads}` : 'Booked';
                  const title = named ? `${r.campaign_name || r.purpose || 'run'} · ${r.est_leads} leads · ~${r.est_minutes} min` : 'Booked';
                  const cls = `sched-run${r.status === 'RUNNING' ? ' running' : ''}${mine ? '' : ' other'}`;
                  const style = { top: b.top, height: b.height };
                  return mine && named && r.campaign_id
                    ? <Link key={r.id} to={`/campaigns/${r.campaign_id}`} className={cls} style={style} title={title} data-run={r.id}>{label}</Link>
                    : <div key={r.id} className={cls} style={style} title={title} data-run={r.id}>{label}</div>;
                })}
              </div>
            ))}
          </div>
        </div>
      </Card>
      <div className="mt" />
      <Card title={`Queue · ${queue.length}`}>
        {queue.length === 0 ? <Empty>Nothing queued. Start a workflow run or create a campaign.</Empty> : (
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>#</th><th>Campaign</th><th>Purpose</th><th className="num">Leads</th><th>Planned</th><th>Ends</th><th>Status</th>{manage && <th />}</tr></thead>
              <tbody>
                {queue.map((r, i) => (
                  <tr key={r.id} data-run={r.id}>
                    <td className="muted">{i + 1}</td>
                    <td>{r.campaign_id ? <Link to={`/campaigns/${r.campaign_id}`}><strong>{r.campaign_name}</strong></Link> : '—'}</td>
                    <td className="muted">{r.purpose || '—'}</td>
                    <td className="num">{r.est_leads}<div className="muted">~{r.est_minutes} min</div></td>
                    <td className="muted">{fmtIST(r.planned_start)}</td>
                    <td className="muted">{fmtIST(r.planned_end)}</td>
                    <td><span className="badge badge-amber">P{r.priority} planned</span>{r.note && <div className="muted">{r.note}</div>}</td>
                    {manage && <td><RunControls r={r} admin={admin} busy={busy} onPriority={setPriority} onCancel={cancel} /></td>}
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

function RunControls({ r, admin, busy, onPriority, onCancel }) {
  const [raw, setRaw] = useState(String(r.priority)); // the box as typed: an empty box is not 0
  const p = /^[1-9]$/.test(raw.trim()) ? Number(raw.trim()) : null; // a whole number 1–9, else nothing to send
  return (
    <span className="sched-ctl">
      {admin && <>
        <input type="number" min={1} max={9} value={raw} aria-label="Priority" onChange={(e) => setRaw(e.target.value)} />
        <button type="button" className="btn-ghost" disabled={busy || p === null || p === r.priority} onClick={() => onPriority(r, p)}
          title={p === null ? 'Priority is a whole number from 1 to 9' : undefined}>Set</button>
      </>}
      <button type="button" className="btn-danger" disabled={busy} onClick={() => onCancel(r)}>Cancel</button>
    </span>
  );
}

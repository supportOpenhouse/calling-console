// Backend sends ISO-8601 UTC; everything is displayed in IST.
const IST = 'Asia/Kolkata';

export function fmtIST(iso, seconds = false) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return new Intl.DateTimeFormat('en-IN', {
    timeZone: IST, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
    ...(seconds ? { second: '2-digit' } : {}), hour12: false,
  }).format(d);
}

// When a dial asked for at `now` goes out, as the backend decides it: inside the IST window [start, end) "now",
// else the next window start (Bolna holds the batch until then). IST is a fixed UTC+5:30.
// Returns {now: true} or {now: false, at: ISO string}.
export function dialTime(start, end, now = new Date()) {
  const mins = (s) => { const [h, m] = s.split(':').map(Number); return h * 60 + m; };
  const wall = new Date(now.getTime() + 330 * 60000); // IST wall clock in the UTC fields
  const cur = wall.getUTCHours() * 60 + wall.getUTCMinutes();
  if (mins(start) <= cur && cur < mins(end)) return { now: true };
  const day = Date.UTC(wall.getUTCFullYear(), wall.getUTCMonth(), wall.getUTCDate() + (cur < mins(start) ? 0 : 1));
  return { now: false, at: new Date(day + (mins(start) - 330) * 60000).toISOString() };
}

export const dialTimeText = (t) => (t.now ? 'Dials now.' : `Dials at ${fmtIST(t.at)} IST, when the calling window opens.`);

// A DNC response's {queued_in} -> the warning to show, or null. Marking DNC never takes a phone out of a batch
// Bolna already has; pausing that campaign does.
export function queuedWarning(res) {
  const q = res?.queued_in ?? [];
  if (!q.length) return null;
  return `Still queued in ${q.map((x) => x.campaign_name).join(', ')} — pause that campaign to remove it`;
}

// 75 -> "1m 15s"
export function fmtDur(sec) {
  if (sec == null) return '—';
  const s = Math.round(sec);
  const m = Math.floor(s / 60);
  return m ? `${m}m ${String(s % 60).padStart(2, '0')}s` : `${s}s`;
}

// "NN%" of a over b; "—" when b is 0 (undefined ratio).
export function pct(a, b) {
  return b ? `${Math.round((a / b) * 100)}%` : '—';
}

export const OUTCOME_LABEL = {
  CONNECTED: 'Connected',
  NOT_PICKED: 'Not picked',
  CARRIER_BLOCKED: 'Carrier blocked',
  VOICEMAIL: 'Voicemail',
  FAILED: 'Failed',
  BALANCE_LOW: 'Balance low',
  UNKNOWN: 'Unknown',
};

// Port of initial zip/lib/describe.ts (trigger keys are snake_case here).
export function describeTrigger(t) {
  switch (t.type) {
    case 'MANUAL': return 'manual';
    case 'SCHEDULE':
      return t.every_minutes
        ? `every ${t.every_minutes} min`
        : `${(t.days || []).map((d) => 'SMTWTFS'[d]).join('')} at ${t.time}`;
    case 'ON_NEW_LEAD': return 'when new leads arrive';
    case 'AFTER_STEP': return `follow-up · ${t.delay_hours} h after ${(t.when_tags || []).join('/') || 'any'}`;
    default: throw new Error(`unknown trigger type: ${t.type}`);
  }
}

export const DEPTS = ['supply', 'demand'];

// Departments a user can see: ADMIN or BOTH -> both, else their own vertical.
export function userDepts(user) {
  if (user.role === 'ADMIN' || user.vertical === 'BOTH') return DEPTS;
  return [user.vertical.toLowerCase()];
}

// The department the user last worked in (remembered in this browser); their first one when the remembered one is
// not theirs (or nothing is remembered yet).
const DEPT_KEY = 'cc_dept';
export function homeDept(user) {
  const depts = userDepts(user);
  const saved = localStorage.getItem(DEPT_KEY);
  return depts.includes(saved) ? saved : depts[0];
}
export function rememberDept(dept) {
  localStorage.setItem(DEPT_KEY, dept);
}

// Owner, or a head whose departments include `dept` (mirrors the backend's "admin" action).
export function canAdminDept(user, dept) {
  return user.role === 'ADMIN' || (user.role === 'HEAD' && userDepts(user).includes(dept));
}

// Owner, or a head/manager whose departments include `dept` (mirrors the backend's "manage" action).
export function canManageDept(user, dept) {
  return user.role === 'ADMIN' || ((user.role === 'HEAD' || user.role === 'MANAGER') && userDepts(user).includes(dept));
}

// Backend rejects uploads over 20 MB with a 413; check first so the user gets the message without the round trip.
export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
export function uploadSizeError(file) {
  return file && file.size > MAX_UPLOAD_BYTES ? 'file too large (max 20 MB)' : null;
}

// Import stats from create/append/pull -> one line.
export function fmtImport(s) {
  return `+${s.added} new · ${s.updated} refreshed · ${s.invalid} invalid · ${s.duplicates} dupes`;
}

// Bolna hangup codes (port of HANGUP_CODE_LABEL in the old console's lib/classify.ts).
export const HANGUP_CODE_LABEL = {
  3010: 'Carrier blocked', 3080: 'Carrier error', 3020: 'No answer', 3040: 'Declined / busy', 4010: 'Voicemail', 4000: 'Normal',
};
export const fmtHangup = (code, status) => (code ? `${code} ${HANGUP_CODE_LABEL[code] ?? ''}`.trim() : status);

export const HANDOFF_STATUSES = ['OPEN', 'CONTACTED', 'VISIT_FIXED', 'CLOSED_WON', 'CLOSED_LOST', 'INVALID'];

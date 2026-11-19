// Thin fetch wrapper around the Flask API. No mock layer: every call hits the backend.

import { toast } from '../utils/toast.js';

const BASE = import.meta.env.VITE_API_BASE || '';

let token = null;
// Guards the expiry notice: parallel 401s must not stack toasts/redirects.
let expiredFired = false;

export function setAuthToken(t) {
  token = t;
  if (t) expiredFired = false;   // fresh session — re-arm the expiry notice
  // User changed (login/logout) — drop everything cached for the old identity.
  wipeCache();
}

// A 401 while we still hold a token = the server killed the session. Say so once,
// then let AuthContext drop auth so RequireAuth lands the user on /login.
function sessionExpired() {
  if (expiredFired) return;
  expiredFired = true;
  toast('Session expired. Please login again.', 'error');
  window.dispatchEvent(new CustomEvent('auth:expired'));
}

async function send(method, path, { body, headers = {} }) {
  // The bearer token must only ever go to our API.
  if (typeof path !== 'string' || !path.startsWith('/api/')) throw new Error(`api client: path must start with /api/, got ${path}`);
  const h = { ...headers };
  if (token) h.Authorization = `Bearer ${token}`;
  let res;
  try {
    res = await fetch(BASE + path, { method, headers: h, body });
  } catch {
    // Unreachable backend. Surface it; never fake success.
    const err = new Error('Network error — could not reach the server. Please retry.');
    err.status = 0;
    throw err;
  }
  return res;
}

async function failFrom(res) {
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* non-JSON error body */ }
  // `token` gate: a 401 with no token is just a failed sign-in, not an expiry.
  if (res.status === 401 && token) sessionExpired();
  const err = new Error(data?.error || `HTTP ${res.status}`);
  err.status = res.status;
  err.data = data;
  return err;
}

async function request(method, path, body) {
  const res = await send(method, path, {
    body: body == null ? undefined : JSON.stringify(body),
    headers: { 'Content-Type': 'application/json' },
  });
  if (!res.ok) throw await failFrom(res);
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

// Authenticated GET returning the raw response Blob (CSV export and the like).
export async function download(path) {
  const res = await send('GET', path, {});
  if (!res.ok) throw await failFrom(res);
  return res.blob();
}

// Multipart POST. No Content-Type header: the browser sets the boundary.
export async function upload(path, formData) {
  let data;
  try {
    const res = await send('POST', path, { body: formData });
    if (!res.ok) throw await failFrom(res);
    const text = await res.text();
    data = text ? JSON.parse(text) : null;
  } finally {
    wipeCache();
  }
  return data;
}

// Tiny in-memory GET layer: identical concurrent GETs share one promise, and a
// few read endpoints get a short TTL cache (keyed on full path+query). Any write
// wipes the whole cache. Cache hits hand out structuredClone copies.

// Path-prefix → TTL in seconds. First match wins.
const TTL_RULES = [
  ['/api/departments/', 60],   // dashboards
  ['/api/agents', 300],
];
function ttlFor(path) {
  const rule = TTL_RULES.find(([prefix]) => path.startsWith(prefix));
  return rule ? rule[1] * 1000 : 0;
}

const cache = new Map();    // path -> { data, expires }
const inflight = new Map(); // path -> promise
let epoch = 0;              // bumped on writes so stale in-flight GETs never cache

function wipeCache() {
  epoch += 1;
  cache.clear();
  inflight.clear();
}

function cachedGet(path, fresh = false) {
  // fresh=true skips the cache READ but still joins an in-flight fetch and refreshes the cache.
  if (!fresh) {
    const hit = cache.get(path);
    if (hit && hit.expires > Date.now()) return Promise.resolve(structuredClone(hit.data));
  }
  let p = inflight.get(path);
  if (!p) {
    const started = epoch;
    p = request('GET', path)
      .then((data) => {
        const ttl = ttlFor(path);
        if (ttl && epoch === started) cache.set(path, { data, expires: Date.now() + ttl });
        return data;
      })
      .finally(() => inflight.delete(path));
    inflight.set(path, p);
  }
  return p.then((data) => structuredClone(data));
}

// opts.silent → no toast at all (sign-in, background writes).
// opts.toast → custom success message (string), or false to skip the success toast.
// Failures ALWAYS toast (unless silent) so a change that never reached the server can't fail invisibly.
function mutate(method, path, body, opts = {}) {
  return request(method, path, body).then(
    (data) => {
      if (!opts.silent && opts.toast !== false) {
        toast(typeof opts.toast === 'string' ? opts.toast : 'Saved', 'success');
      }
      return data;
    },
    (err) => {
      // 401 → the session-expired toast already explains it; don't stack a second.
      if (!opts.silent && err?.status !== 401) {
        const msg = err?.status === 0
          ? 'Not saved — network issue. Your change wasn’t stored. Please retry.'
          : `Not saved — ${err?.message || 'the server rejected the change'}. Please retry.`;
        toast(msg, 'error');
      }
      throw err;
    },
  ).finally(wipeCache);
}

// Targeted invalidation for cross-user changes detected by polling.
function invalidate(prefix) {
  for (const k of [...cache.keys()]) if (k.startsWith(prefix)) cache.delete(k);
  for (const k of [...inflight.keys()]) if (k.startsWith(prefix)) inflight.delete(k);
}

export const api = {
  get: (p, opts) => cachedGet(p, opts?.fresh),
  post: (p, b, opts) => mutate('POST', p, b, opts),
  put: (p, b, opts) => mutate('PUT', p, b, opts),
  patch: (p, b, opts) => mutate('PATCH', p, b, opts),
  del: (p, opts) => mutate('DELETE', p, undefined, opts),
  download,
  upload,
  invalidate,
};

// Fire-and-forget toast bus. Anything can call toast(); <Toaster/> renders them.
// Kept React-free so the API layer (client.js) can emit without importing UI.
export const TOAST_EVENT = 'app:toast';

export function toast(message, type = 'info', opts = {}) {
  window.dispatchEvent(new CustomEvent(TOAST_EVENT, { detail: { message, type, ...opts } }));
}

// GETs don't toast on their own (only mutations do); pages call this from .catch.
// 401 is skipped: the session-expired notice already covers it.
export function reportError(err) {
  if (err?.status !== 401) toast(err?.message || 'Request failed', 'error');
}

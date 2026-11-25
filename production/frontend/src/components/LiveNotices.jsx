import { useEffect, useState } from 'react';
import { api } from '../api/client.js';
import { IconInfo, IconWarning } from './icons.jsx';

const POLL_MS = 10000; // the backend pulls Bolna every 10 s

// Polls /api/sync/pulse for the department. A call saved after the first poll -> "New rows added" until the page is
// reloaded (clicking reloads); a failed poll, or no Bolna pull for 2+ minutes -> "Connection problem" until a poll
// is fine again. Hidden tabs skip the poll.
export default function LiveNotices({ dept }) {
  const [fresh, setFresh] = useState(false);
  const [problem, setProblem] = useState(null);

  useEffect(() => {
    let base; // the newest call (epoch s, or null) at this department's first poll
    let live = true;
    async function poll() {
      if (document.hidden) return;
      try {
        const r = await api.get(`/api/sync/pulse?dept=${dept}`);
        if (!live) return;
        if (base === undefined) base = r.latest;
        else if (r.latest > base) setFresh(true);
        setProblem(r.stale ? 'Connection problem: no update from Bolna for 2+ minutes' : null);
      } catch (err) {
        if (live && err.status !== 401) setProblem('Connection problem'); // 401: the session-expired notice covers it
      }
    }
    poll();
    const t = setInterval(poll, POLL_MS);
    return () => { live = false; clearInterval(t); };
  }, [dept]);

  if (!fresh && !problem) return null;
  return (
    <div className="toaster live-notices">
      {problem && (
        <div className="toast toast-error" role="alert">
          <span className="toast-ic"><IconWarning /></span>
          <span className="toast-msg">{problem}</span>
        </div>
      )}
      {fresh && (
        <div className="toast toast-info" role="status" onClick={() => window.location.reload()}>
          <span className="toast-ic"><IconInfo /></span>
          <span className="toast-msg">New rows added, pls reload</span>
          <button type="button" className="btn-link">Reload</button>
        </div>
      )}
    </div>
  );
}

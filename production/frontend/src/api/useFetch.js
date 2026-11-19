import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './client.js';
import { reportError } from '../utils/toast.js';

// GET `path` (null/undefined = don't fetch). Returns {data, error, loading, reload}.
// data/error are only ever those of the CURRENT path (never a previous path's); loading is
// true until the current path has resolved. Only reload() bypasses the cache,
// and it keeps the current data on screen until the fresh response lands.
export function useFetch(path) {
  const [res, setRes] = useState({ path: null, data: null, error: null });
  const [tick, setTick] = useState(0);
  const fresh = useRef(false);

  useEffect(() => {
    if (!path) return undefined;
    let stale = false;
    const bypass = fresh.current;
    fresh.current = false;
    api.get(path, { fresh: bypass }).then(
      (data) => { if (!stale) setRes({ path, data, error: null }); },
      (error) => { if (!stale) { reportError(error); setRes({ path, data: null, error }); } },
    );
    return () => { stale = true; };
  }, [path, tick]);

  const reload = useCallback(() => {
    fresh.current = true;
    setRes((r) => (r.error ? { path: null, data: null, error: null } : r)); // keep shown data while refetching; a failed load shows loading again
    setTick((t) => t + 1);
  }, []);

  const cur = path && res.path === path ? res : null;
  return { data: cur?.data ?? null, error: cur?.error ?? null, loading: !!path && !cur, reload };
}

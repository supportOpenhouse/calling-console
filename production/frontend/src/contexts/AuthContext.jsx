import { createContext, useContext, useEffect, useState } from 'react';
import { api, setAuthToken } from '../api/client.js';
import { toast } from '../utils/toast.js';

const AuthContext = createContext(null);
const KEY = 'cc_token';

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  const [meError, setMeError] = useState(false);

  // Validate the stored token. Only a 401 drops it; network / 5xx keep it and set meError.
  function loadMe() {
    setMeError(false);
    const token = localStorage.getItem(KEY);
    if (!token) { setLoading(false); return; }
    setAuthToken(token);
    setLoading(true);
    api.get('/api/auth/me')
      .then((r) => setUser(r.user))
      .catch((err) => {
        if (err.status === 401) {
          localStorage.removeItem(KEY);
          setAuthToken(null);
        } else {
          setMeError(true);
          toast('Couldn\u2019t reach the server', 'error');
        }
      })
      .finally(() => setLoading(false));
  }

  useEffect(loadMe, []);

  // Any 401 while signed in (see client.js): drop auth so RequireAuth lands on /login.
  useEffect(() => {
    function onExpired() {
      localStorage.removeItem(KEY);
      setAuthToken(null);
      setUser(null);
    }
    window.addEventListener('auth:expired', onExpired);
    return () => window.removeEventListener('auth:expired', onExpired);
  }, []);

  async function signIn(path, body) {
    setMeError(false);
    const r = await api.post(path, body, { silent: true });
    localStorage.setItem(KEY, r.token);
    setAuthToken(r.token);
    setUser(r.user);
  }

  const loginWithGoogle = (idToken) => signIn('/api/auth/google', { id_token: idToken });
  const devLogin = (email) => signIn('/api/auth/dev', { email });

  function logout() {
    setMeError(false);
    localStorage.removeItem(KEY);
    setAuthToken(null);
    setUser(null);
  }

  return (
    <AuthContext.Provider value={{ user, loading, meError, retryMe: loadMe, loginWithGoogle, devLogin, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider');
  return ctx;
}

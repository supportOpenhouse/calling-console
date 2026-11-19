import { useState } from 'react';
import { GoogleLogin } from '@react-oauth/google';
import { Navigate, useNavigate, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext.jsx';
import Waves from '../components/Waves.jsx';

const GOOGLE_ENABLED = !!import.meta.env.VITE_GOOGLE_OAUTH_CLIENT_ID;

export default function Login() {
  const { user, loginWithGoogle, devLogin } = useAuth();
  const [error, setError] = useState(null);
  const [email, setEmail] = useState('');
  const nav = useNavigate();
  // RequireAuth stashes where the user was headed; else "/", which opens the last-chosen department's dashboard.
  const dest = useLocation().state?.from || '/';

  if (user) return <Navigate to={dest} replace />;

  async function run(fn) {
    setError(null);
    try { await fn(); nav(dest, { replace: true }); }
    catch (e) { setError(e.data?.error || e.message); }
  }

  return (
    <div className="login-page">
      <div className="login-hero">
        <Waves />
        <div className="lh-brand">
          <img src="/oh_full_logo.png" alt="Openhouse" />
        </div>
        <div>
          <h2>Calling Console</h2>
          <p className="lh-sub">by Openhouse</p>
        </div>
        <div className="lh-sub">Supply and Demand · Internal console</div>
      </div>

      <div className="login-panel">
        <div className="login-card">
          <h1>Welcome back</h1>
          <p className="login-hint">
            Sign in with your <strong>@openhouse.in</strong> Google account.
          </p>

          {GOOGLE_ENABLED ? (
            <div className="login-google">
              <GoogleLogin
                onSuccess={(cred) => run(() => loginWithGoogle(cred.credential))}
                onError={() => setError('Google sign-in failed')}
                theme="filled_black"
                shape="pill"
                text="signin_with"
                useOneTap={false}
              />
            </div>
          ) : (
            <form onSubmit={(e) => { e.preventDefault(); run(() => devLogin(email)); }}>
              <label htmlFor="dev-email">Dev sign-in email</label>
              <input id="dev-email" type="text" value={email} onChange={(e) => setEmail(e.target.value)}
                placeholder="owner@openhouse.in" autoFocus />
              <button type="submit" className="btn-primary" style={{ marginTop: 12 }} disabled={!email}>Sign in</button>
            </form>
          )}

          {error && <div className="login-error">{error}</div>}
        </div>
      </div>
    </div>
  );
}

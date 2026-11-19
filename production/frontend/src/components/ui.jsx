import { createContext, useContext } from 'react';
import { createPortal } from 'react-dom';
import { Link } from 'react-router-dom';
import { OUTCOME_LABEL } from '../utils/format.js';

// The top-bar title element (Layout provides it). PageHead writes its title there, so a page name shows once.
export const TitleSlot = createContext(null);

export function PageHead({ title, sub, actions }) {
  const slot = useContext(TitleSlot);
  return (
    <>
      {slot && createPortal(title, slot)}
      {(sub || actions) && (
        <div className="page-head">
          {sub && <div className="ph-sub">{sub}</div>}
          <div className="page-head-spacer" />
          {actions}
        </div>
      )}
    </>
  );
}

// `action` is an optional node shown at the right of the title row.
export function Card({ title, action, children }) {
  return (
    <section className="card-block">
      {(title || action) && (
        <div className="card-head">
          <h3>{title}</h3>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}

// tone: green | amber | red | brand (else neutral)
export function Stat({ label, value, hint, tone }) {
  return (
    <div className="stat-tile">
      <div className="st-lbl">{label}</div>
      <div className={`st-num ${tone ? `tone-${tone}` : ''}`}>{value}</div>
      {hint && <div className="st-hint">{hint}</div>}
    </div>
  );
}

// items: [{label, n, href?}]. Bar width is relative to the largest, % is of the total.
export function Bars({ items }) {
  if (!items.length) return <div className="muted">No data yet.</div>;
  const max = Math.max(1, ...items.map((i) => i.n));
  const sum = items.reduce((a, i) => a + i.n, 0);
  return (
    <ul className="bars">
      {items.map((i) => {
        const row = (
          <>
            <div className="bar-top">
              <span>{i.label}</span>
              <span className="bar-n">{i.n}{sum ? ` · ${Math.round((i.n / sum) * 100)}%` : ''}</span>
            </div>
            <div className="bar-track"><div className="bar-fill" style={{ width: `${(i.n / max) * 100}%` }} /></div>
          </>
        );
        return (
          <li key={i.label}>
            {i.href ? <Link to={i.href} className="bar-row">{row}</Link> : <div className="bar-row">{row}</div>}
          </li>
        );
      })}
    </ul>
  );
}

const OUTCOME_TONE = {
  CONNECTED: 'badge-green', VOICEMAIL: 'badge-amber', CARRIER_BLOCKED: 'badge-red', FAILED: 'badge-red',
  BALANCE_LOW: 'badge-amber', NOT_PICKED: '', UNKNOWN: 'badge-faint',
};
export function OutcomeBadge({ o }) {
  if (!(o in OUTCOME_LABEL)) throw new Error(`unknown outcome: ${o}`);
  return <span className={`badge ${OUTCOME_TONE[o]}`}>{OUTCOME_LABEL[o]}</span>;
}

// Colour families from the old console's ui.tsx: good = green, soft = amber, dnc = red, rest neutral.
const GOOD = ['qualified', 'want_to_sell', 'hot_buyer', 'booked', 'interested', 'yes'];
const SOFT = ['qualified_no_price', 'want_to_sell_later', 'sell_later', 'callback_requested', 'yes_unverified'];
const DNC = ['dnc', 'do_not_call'];
export function TagBadge({ tag }) {
  if (!tag) return <span className="badge badge-faint">untagged</span>;
  const t = tag.toLowerCase();
  const tone = GOOD.includes(t) ? 'badge-good' : SOFT.includes(t) ? 'badge-amber' : DNC.includes(t) ? 'badge-red' : '';
  return <span className={`badge ${tone}`}>{t.replace(/_/g, ' ')}</span>;
}

export function Empty({ children }) {
  return <div className="empty-state">{children}</div>;
}

export function Modal({ open, onClose, title, children }) {
  if (!open) return null;
  return (
    <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true">
        <div className="modal-head-row">
          <h3>{title}</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        {children}
      </div>
    </div>
  );
}

// cols: [{key, label, align?: 'right', render?: (row) => node}]; rows: objects (each needs `id` or falls back to index).
export function Table({ cols, rows }) {
  return (
    <table className="data-table">
      <thead>
        <tr>{cols.map((c) => <th key={c.key} className={c.align === 'right' ? 'num' : ''}>{c.label}</th>)}</tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={r.id ?? i}>
            {cols.map((c) => (
              <td key={c.key} className={c.align === 'right' ? 'num' : ''}>{c.render ? c.render(r) : r[c.key]}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// Loading line, or in-page error block with Retry; renders nothing once data is ready.
// error: Error or message string.
export function PageState({ loading, error, onRetry }) {
  if (error) {
    return (
      <div className="empty-state">
        <div>Couldn’t load: {error.message ?? error}</div>
        {onRetry && <button type="button" className="btn-ghost" onClick={onRetry}>Retry</button>}
      </div>
    );
  }
  if (loading) return <div className="muted">Loading…</div>;
  return null;
}

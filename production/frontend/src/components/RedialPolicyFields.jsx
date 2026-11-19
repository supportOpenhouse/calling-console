const OUTCOMES = [
  ['NOT_PICKED', 'Not picked / busy', 'rang out, declined, busy'],
  ['CARRIER_BLOCKED', 'Carrier blocked', 'code 3010, rotate the number'],
  ['VOICEMAIL', 'Voicemail', 'code 4010 or silent connect'],
  ['FAILED', 'Provider error', 'code 3080 and failures'],
  ['BALANCE_LOW', 'Balance low', 'wallet ran out mid-batch'],
];
const TAGS = ['not_picked', 'callback_requested', 'unclear', 'yes_unverified'];
const toggle = (arr, x) => (arr.includes(x) ? arr.filter((y) => y !== x) : [...arr, x]);

// Redial policy form (snake_case keys, as stored). value/onChange are the whole policy object;
// numbers = the other calling numbers a carrier-blocked lead may rotate to.
export default function RedialPolicyFields({ value, onChange, numbers }) {
  const set = (k, v) => onChange({ ...value, [k]: v });
  return (
    <div className="form-grid">
      <div className="form-wide-2">
        <label><input type="checkbox" checked={value.auto_redial} onChange={(e) => set('auto_redial', e.target.checked)} /> Auto-redial (the sync job opens the next round by itself)</label>
      </div>
      <div><label>Max attempts per lead</label><input type="number" min={1} max={8} value={value.max_attempts} onChange={(e) => set('max_attempts', Number(e.target.value))} /></div>
      <div><label>Minimum gap between attempts (minutes)</label><input type="number" min={15} step={15} value={value.gap_minutes} onChange={(e) => set('gap_minutes', Number(e.target.value))} /></div>
      <div><label>Calling window start (IST)</label><input type="time" value={value.window_start} onChange={(e) => set('window_start', e.target.value)} /></div>
      <div><label>Calling window end (IST)</label><input type="time" value={value.window_end} onChange={(e) => set('window_end', e.target.value)} /></div>
      <div>
        <label>Redial these outcomes</label>
        {OUTCOMES.map(([k, l, hint]) => (
          <label key={k} style={{ fontWeight: 400 }}>
            <input type="checkbox" checked={value.retry_outcomes.includes(k)} onChange={() => set('retry_outcomes', toggle(value.retry_outcomes, k))} /> {l} <span className="muted">· {hint}</span>
          </label>
        ))}
      </div>
      <div>
        <label>Redial connected calls tagged</label>
        {TAGS.map((t) => (
          <label key={t} style={{ fontWeight: 400 }}>
            <input type="checkbox" checked={value.retry_tags.includes(t)} onChange={() => set('retry_tags', toggle(value.retry_tags, t))} /> {t.replace(/_/g, ' ')}
          </label>
        ))}
        <label style={{ marginTop: 12 }}>Rotate carrier-blocked leads to</label>
        {numbers.length ? numbers.map((n) => (
          <label key={n} style={{ fontWeight: 400 }}>
            <input type="checkbox" checked={value.rotate_numbers.includes(n)} onChange={() => set('rotate_numbers', toggle(value.rotate_numbers, n))} /> {n}
          </label>
        )) : <div className="muted">Add more numbers in Integrations to enable rotation.</div>}
      </div>
    </div>
  );
}

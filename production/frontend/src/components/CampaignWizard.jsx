import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { upload } from '../api/client.js';
import { toast } from '../utils/toast.js';
import { uploadSizeError } from '../utils/format.js';
import { Card } from './ui.jsx';
import RedialPolicyFields from './RedialPolicyFields.jsx';

const START_MODES = [
  ['now', 'Queue now', 'Takes the next free slot on the department calendar.'],
  ['schedule', 'Schedule', 'Earliest start; the planner may push it later if the lines are busy.'],
  ['draft', 'Save as draft', 'Nothing is dialed until you plan or start it.'],
];

function MapRow({ label, required, value, columns, sample, onChange }) {
  const ex = value ? sample.map((r) => r[value]).filter(Boolean).slice(0, 2).join(' · ') : '';
  return (
    <div>
      <label>{label}{required && <span className="req"> *</span>}</label>
      <select value={value || ''} onChange={(e) => onChange(e.target.value)}>
        <option value="">— not in file —</option>
        {columns.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>
      {ex && <div className="muted">e.g. {ex}</div>}
    </div>
  );
}

// data = GET /api/departments/<dept>/builder-data (agents of the department or BOTH, active numbers, default_policy, use cases).
export default function CampaignWizard({ dept, data }) {
  const nav = useNavigate();
  const { agents, numbers, use_cases: useCases } = data;
  const [useCaseId, setUseCaseId] = useState('');
  const [purpose, setPurpose] = useState('');
  const [agentId, setAgentId] = useState(agents[0]?.id ?? '');
  const agent = agents.find((a) => a.id === agentId);
  const [name, setName] = useState('');
  const [fromNumber, setFromNumber] = useState(agent?.default_from_number ?? numbers[0]?.number ?? '');
  const [file, setFile] = useState(null);
  const [preview, setPreview] = useState(null);
  const [columnMap, setColumnMap] = useState({});
  const [step, setStep] = useState(1);
  const [policy, setPolicy] = useState(data.default_policy);
  const [notes, setNotes] = useState('');
  const [startMode, setStartMode] = useState('now');
  const [scheduledAt, setScheduledAt] = useState('');
  const [speakBhk, setSpeakBhk] = useState(true);
  const [busy, setBusy] = useState(false);

  function pickAgent(id) {
    setAgentId(id);
    const a = agents.find((x) => x.id === id);
    if (a?.default_from_number) setFromNumber(a.default_from_number);
    setPreview(null); setStep(1);
  }
  function pickUseCase(id) {
    setUseCaseId(id);
    const u = useCases.find((x) => x.id === id);
    if (u?.default_agent_id && agents.some((a) => a.id === u.default_agent_id)) pickAgent(u.default_agent_id);
  }

  async function readFile() {
    if (!file || !agentId) { toast('Choose an agent and a file first.', 'error'); return; }
    const tooBig = uploadSizeError(file);
    if (tooBig) { toast(tooBig, 'error'); return; }
    const fd = new FormData();
    fd.append('file', file);
    fd.append('agent_id', agentId);
    setBusy(true);
    try {
      const p = await upload(`/api/departments/${dept}/campaigns/preview`, fd);
      setPreview(p); setColumnMap(p.column_map); setStep(2);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (!columnMap.phone) { toast('Map the phone column.', 'error'); return; }
    if (name.trim().length < 2) { toast('Give the campaign a name.', 'error'); return; }
    if (!fromNumber.trim()) { toast('Choose a calling number.', 'error'); return; }
    if (startMode === 'schedule' && !scheduledAt) { toast('Pick a start time.', 'error'); return; }
    const payload = {
      name, agent_id: agentId, from_number: fromNumber, notes: notes || null, column_map: columnMap,
      redial_policy: policy, start_mode: startMode, speak_bhk: preview.variables.includes('bhk') && speakBhk,
      use_case_id: useCaseId || null, purpose: purpose || null,
      ...(startMode === 'schedule' ? { scheduled_at: new Date(scheduledAt).toISOString() } : {}),
    };
    const fd = new FormData();
    fd.append('file', file);
    fd.append('payload', JSON.stringify(payload));
    setBusy(true);
    try {
      const r = await upload(`/api/departments/${dept}/campaigns`, fd);
      toast(`Imported ${r.created} lead(s) · ${r.invalid} invalid · ${r.duplicates} duplicate(s)`, 'success');
      nav(`/campaigns/${r.id}`);
    } catch (err) {
      toast(err.message, 'error');
      setBusy(false);
    }
  }

  if (agents.length === 0) return <Card><div className="muted">No agents available for this department. Refresh agents in Integrations first.</div></Card>;
  const unmapped = preview ? preview.variables.filter((v) => !columnMap[v]) : [];
  const label = { now: 'Create & queue', schedule: 'Create & schedule', draft: 'Save draft' }[startMode];

  return (
    <div className="grid-2">
      <div>
        <Card title="1 · Agent & list">
          <div className="form-grid">
            <div>
              <label>Agent</label>
              <select value={agentId} onChange={(e) => pickAgent(e.target.value)}>
                {agents.map((a) => <option key={a.id} value={a.id}>{a.name} · {a.vertical.toLowerCase()}</option>)}
              </select>
              {agent && <div className="muted">Variables: {agent.variables.length ? agent.variables.join(', ') : 'none'}</div>}
            </div>
            <div><label>Campaign name</label><input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. L2 Noida societies · Nov 18" /></div>
            <div>
              <label>Use case (optional)</label>
              <select value={useCaseId} onChange={(e) => pickUseCase(e.target.value)}>
                <option value="">— ad-hoc —</option>
                {useCases.map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
              </select>
            </div>
            <div>
              <label>Calling number</label>
              <select value={fromNumber} onChange={(e) => setFromNumber(e.target.value)}>
                {fromNumber && !numbers.some((n) => n.number === fromNumber) && <option value={fromNumber}>{fromNumber}</option>}
                {!fromNumber && <option value="">— choose —</option>}
                {numbers.map((n) => <option key={n.number} value={n.number}>{n.number}{n.label ? ` · ${n.label}` : ''}</option>)}
              </select>
            </div>
            <div className="form-wide-2"><label>Purpose (shown on the calendar)</label><input value={purpose} onChange={(e) => setPurpose(e.target.value)} placeholder="Why this list, this week" /></div>
            <div className="form-wide-2">
              <label>Lead file (CSV or Excel, first row = headers; max 20 MB)</label>
              <input type="file" accept=".csv,.xlsx,.xls" onChange={(e) => { setFile(e.target.files[0] ?? null); setPreview(null); setStep(1); }} />
            </div>
          </div>
          {step === 1 && <div className="mt"><button type="button" className="btn-primary" disabled={busy || !file} onClick={readFile}>{busy ? 'Reading…' : 'Read file →'}</button></div>}
        </Card>
        {preview && (
          <Card title={`2 · Map columns · ${preview.total.toLocaleString('en-IN')} rows`}>
            <div className="page-hint">Only phone is required; any agent variable left unmapped is sent empty. Rows without a valid Indian mobile number are dropped; duplicates within the file are dialed once.</div>
            <div className="form-grid mt">
              <MapRow label="phone" required value={columnMap.phone} columns={preview.columns} sample={preview.sample} onChange={(c) => setColumnMap({ ...columnMap, phone: c })} />
              {preview.variables.map((v) => (
                <MapRow key={v} label={v} value={columnMap[v]} columns={preview.columns} sample={preview.sample} onChange={(c) => setColumnMap({ ...columnMap, [v]: c })} />
              ))}
            </div>
            {preview.variables.includes('bhk') && (
              <label style={{ fontWeight: 400 }}><input type="checkbox" checked={speakBhk} onChange={(e) => setSpeakBhk(e.target.checked)} /> Convert BHK to spoken form (3 BHK → थ्री बी एच के) so the voice says it right</label>
            )}
            {unmapped.length > 0 && <div className="page-hint">Unmapped: {unmapped.join(', ')}. These are sent empty.</div>}
            {step === 2 && <div className="mt"><button type="button" className="btn-primary" disabled={!columnMap.phone} onClick={() => setStep(3)}>Redial rules →</button></div>}
          </Card>
        )}
        {step >= 3 && (
          <Card title="3 · Redial policy">
            <RedialPolicyFields value={policy} onChange={setPolicy} numbers={numbers.map((n) => n.number).filter((n) => n !== fromNumber)} />
            <div className="mt"><label>Notes (optional)</label><input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Where the list came from, what we are testing" /></div>
          </Card>
        )}
      </div>
      <div>
        <Card title="Launch">
          <div style={{ display: 'grid', gap: 10 }}>
            {START_MODES.map(([m, l, hint]) => (
              <label key={m} style={{ fontWeight: 400 }}>
                <input type="radio" name="start" checked={startMode === m} onChange={() => setStartMode(m)} /> <strong>{l}</strong>
                <div className="muted">{hint}</div>
              </label>
            ))}
            {startMode === 'schedule' && <input type="datetime-local" value={scheduledAt} onChange={(e) => setScheduledAt(e.target.value)} aria-label="Start (your local time)" />}
            <div className="muted">
              {agent?.name ?? '—'} · from {fromNumber || '—'} · {preview ? `${preview.total} rows` : 'no file read yet'} · up to {policy.max_attempts} attempts, {policy.window_start}–{policy.window_end} IST
            </div>
            <button type="button" className="btn-primary" disabled={busy || step < 3} onClick={submit}>{busy ? 'Working…' : label}</button>
            {step < 3 && <div className="muted">Finish the steps on the left first.</div>}
          </div>
        </Card>
      </div>
    </div>
  );
}

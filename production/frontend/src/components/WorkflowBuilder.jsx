import { useMemo, useState } from 'react';
import { api } from '../api/client.js';
import { Card } from './ui.jsx';
import RedialPolicyFields from './RedialPolicyFields.jsx';

const OUTCOMES = ['CONNECTED', 'NOT_PICKED', 'CARRIER_BLOCKED', 'VOICEMAIL', 'FAILED'];
const ACTIONS = [
  ['handoff', 'Hand off to RM'],
  ['whatsapp', 'Send WhatsApp template'],
  ['redial', 'Redial later'],
  ['dnc', 'Do-not-call everywhere'],
  ['stop', 'Stop trying'],
  ['next_step', 'Continue to follow-up step'],
];
const TRIGGERS = [
  ['MANUAL', 'Manual / scheduled by hand'],
  ['SCHEDULE', 'On a schedule'],
  ['ON_NEW_LEAD', 'When new leads arrive'],
  ['AFTER_STEP', 'Follow-up step after another workflow'],
];
const toggleIn = (arr, x) => ((arr || []).includes(x) ? arr.filter((y) => y !== x) : [...(arr || []), x]);
const csv = (s) => s.split(',').map((x) => x.trim()).filter(Boolean);
const num = (s) => (s === '' ? undefined : Number(s));

// Which dataset column feeds an agent variable (shortened names, same hints as the old console).
function guessCol(v, cols) {
  const n = (s) => s.toLowerCase().replace(/[^a-z0-9]/g, '');
  const hints = {
    owner_name: ['ownername', 'name', 'owner'], society_name: ['societyname', 'society', 'project'], city: ['city'],
    bhk: ['bhk', 'configuration', 'config'], size_sqft: ['size', 'sqft', 'area'], cp_name: ['cpname', 'name'],
    firm_name: ['firm', 'company'], area: ['area', 'locality', 'sector'], cp_status: ['status', 'category', 'segment'],
  };
  const want = [n(v), ...(hints[v] || [])];
  return cols.find((c) => want.includes(n(c))) ?? cols.find((c) => want.some((w) => n(c).includes(w))) ?? '';
}

function Chips({ label, items, selected, onToggle }) {
  return (
    <div>
      <label>{label} {selected?.length ? `(${selected.length} selected)` : '(all)'}</label>
      {items.length ? (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, maxHeight: 120, overflow: 'auto' }}>
          {items.map((it) => (
            <button key={it.v} type="button" className={`pill ${selected?.includes(it.v) ? 'pill-on' : ''}`} onClick={() => onToggle(it.v)}>
              {it.v} <span style={{ opacity: 0.6 }}>{it.n}</span>
            </button>
          ))}
        </div>
      ) : <div className="muted">—</div>}
    </div>
  );
}

// data = GET builder-data; initial = existing workflow row (edit) or a partial prefill (create).
// workflows = the department's workflow list (new follow-up steps take the next step_order under their parent).
export default function WorkflowBuilder({ dept, data, initial, workflows = [], onSaved }) {
  const editId = initial.id ?? null;
  const [useCaseId, setUseCaseId] = useState(initial.use_case_id ?? data.use_cases[0]?.id ?? '');
  const uc = data.use_cases.find((u) => u.id === useCaseId);
  const [agentId, setAgentId] = useState(initial.agent_id ?? uc?.default_agent_id ?? data.agents[0]?.id ?? '');
  const agent = data.agents.find((a) => a.id === agentId);
  const [name, setName] = useState(initial.name ?? '');
  const [purpose, setPurpose] = useState(initial.purpose ?? '');
  const [datasetId, setDatasetId] = useState(initial.dataset_id ?? (initial.parent_id ? '' : data.datasets[0]?.id ?? ''));
  const ds = data.datasets.find((x) => x.id === datasetId);
  const facets = data.facets[datasetId];
  const [segment, setSegment] = useState(initial.segment ?? { never_called: true });
  const [fromNumber, setFromNumber] = useState(initial.from_number ?? agent?.default_from_number ?? data.numbers[0]?.number ?? '');
  const [variableMap, setVariableMap] = useState(initial.variable_map ?? {});
  const [trigger, setTrigger] = useState(initial.trigger ?? { type: 'MANUAL' });
  const [policy, setPolicy] = useState(initial.redial_policy ?? data.default_policy);
  const [rules, setRules] = useState(initial.rules ?? [{ when: { tags: ['callback_requested'] }, actions: [{ type: 'redial', after_minutes: 1440 }] }]);
  const [parentId, setParentId] = useState(initial.parent_id ?? '');
  const [priority, setPriority] = useState(initial.priority ?? uc?.priority ?? 5);
  const [active, setActive] = useState(initial.active ?? true);
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const isStep = trigger.type === 'AFTER_STEP';
  const noDays = trigger.type === 'SCHEDULE' && !trigger.every_minutes && !trigger.days?.length;
  // A follow-up step with no tag and no outcome would follow up every call of its parent: the backend refuses it.
  const noMatch = isStep && !trigger.when_tags?.length && !trigger.when_outcomes?.length;

  const autoMap = useMemo(
    () => Object.fromEntries((agent?.variables ?? []).map((v) => [v, variableMap[v] ?? guessCol(v, ds?.columns ?? [])])),
    [agent, ds, variableMap],
  );

  function pickUseCase(id) {
    setUseCaseId(id);
    const u = data.use_cases.find((x) => x.id === id);
    if (!editId && u) { // a new workflow follows its use case's defaults
      if (u.default_agent_id) setAgentId(u.default_agent_id);
      setPriority(u.priority);
    }
  }
  function pickAgent(id) {
    setAgentId(id);
    const a = data.agents.find((x) => x.id === id);
    if (a?.default_from_number) setFromNumber(a.default_from_number);
    setVariableMap({});
  }
  function pickTrigger(t) {
    setTrigger(
      t === 'MANUAL' ? { type: 'MANUAL' }
        : t === 'SCHEDULE' ? { type: 'SCHEDULE', days: [1, 2, 3, 4, 5, 6], time: '10:30' }
          : t === 'ON_NEW_LEAD' ? { type: 'ON_NEW_LEAD' }
            : { type: 'AFTER_STEP', delay_hours: 48, when_tags: uc?.tag_taxonomy?.good ?? [] },
    );
    if (t !== 'AFTER_STEP') setParentId('');
    else setDatasetId('');
  }
  const setSeg = (patch) => { setSegment({ ...segment, ...patch }); setPreview(null); };

  async function doPreview() {
    setBusy(true);
    try {
      setPreview(await api.post(`/api/datasets/${datasetId}/preview`, { segment }, { toast: false }));
    } catch { /* api client already toasted */ } finally { setBusy(false); }
  }

  function nextStepOrder() {
    if (editId) return initial.step_order; // an edit keeps the stored order
    if (!isStep) return 0;
    return Math.max(0, ...workflows.filter((w) => w.parent_id === parentId).map((w) => w.step_order)) + 1;
  }

  async function submit() {
    setBusy(true);
    try {
      // the editor holds both a time and an interval; only one may go out
      const trig = trigger.type === 'SCHEDULE'
        ? { type: 'SCHEDULE', days: trigger.days, ...(trigger.every_minutes ? { every_minutes: trigger.every_minutes } : { time: trigger.time }) }
        : trigger;
      const body = {
        use_case_id: useCaseId, name, purpose: purpose.trim() || null, agent_id: agentId,
        dataset_id: isStep ? null : datasetId || null, segment: isStep ? {} : segment,
        from_number: fromNumber, variable_map: autoMap, trigger: trig, redial_policy: policy, rules,
        parent_id: isStep ? parentId || null : null, step_order: nextStepOrder(), priority: Number(priority), active,
      };
      const row = editId
        ? await api.put(`/api/workflows/${editId}`, body, { toast: 'Workflow saved' })
        : await api.post(`/api/departments/${dept}/workflows`, body, { toast: 'Workflow created' });
      onSaved(row.id);
    } catch { /* api client already toasted the backend error */ } finally { setBusy(false); }
  }

  const patchRule = (i, fn) => setRules(rules.map((r, j) => (j === i ? fn(r) : r)));

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 300px', gap: 20, alignItems: 'start' }}>
      <div>
        <Card title="1 · Purpose">
          <div className="form-grid">
            <div><label>Use case</label>
              <select value={useCaseId} onChange={(e) => pickUseCase(e.target.value)}>
                {data.use_cases.map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
              </select>
            </div>
            <div><label>Workflow name</label><input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. L2 owners · first touch" /></div>
            <div className="form-wide-2"><label>Purpose (shown on the calendar and to RMs)</label><input value={purpose} onChange={(e) => setPurpose(e.target.value)} placeholder="Why we are calling these people this week" /></div>
          </div>
        </Card>

        <Card title="2 · Agent and number">
          <div className="form-grid">
            <div><label>Agent</label>
              <select value={agentId} onChange={(e) => pickAgent(e.target.value)}>
                {data.agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
              {agent && <div className="page-hint" style={{ marginTop: 4 }}>needs: {agent.variables.join(', ') || 'no variables'}</div>}
            </div>
            <div><label>Calling number</label>
              {data.numbers.length ? (
                <select value={fromNumber} onChange={(e) => setFromNumber(e.target.value)}>
                  {!data.numbers.some((n) => n.number === fromNumber) && <option value={fromNumber}>{fromNumber || '— pick —'}</option>}
                  {data.numbers.map((n) => <option key={n.id} value={n.number}>{n.number}{n.label ? ` · ${n.label}` : ''}</option>)}
                </select>
              ) : <input value={fromNumber} onChange={(e) => setFromNumber(e.target.value)} />}
            </div>
          </div>
        </Card>

        <Card title="3 · Trigger: when does it run?">
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 14 }}>
            {TRIGGERS.map(([t, l]) => (
              <button key={t} type="button" className={`pill ${trigger.type === t ? 'pill-on' : ''}`} onClick={() => pickTrigger(t)}>{l}</button>
            ))}
          </div>
          {trigger.type === 'SCHEDULE' && (
            <div className="form-grid" style={{ gridTemplateColumns: '1fr 1fr 1fr' }}>
              <div><label>Days</label>
                <div style={{ display: 'flex', gap: 4 }}>
                  {['S', 'M', 'T', 'W', 'T', 'F', 'S'].map((l, i) => (
                    <button key={i} type="button" className={`pill ${trigger.days?.includes(i) ? 'pill-on' : ''}`} style={{ padding: '6px 9px' }}
                      onClick={() => setTrigger({ ...trigger, days: toggleIn(trigger.days, i) })}>{l}</button>
                  ))}
                </div>
              </div>
              <div><label>Time (IST)</label><input type="time" value={trigger.time ?? ''} disabled={!!trigger.every_minutes} onChange={(e) => setTrigger({ ...trigger, time: e.target.value })} /></div>
              <div><label>Or every N minutes (empty = use the time)</label>
                <input type="number" min={1} value={trigger.every_minutes ?? ''} onChange={(e) => setTrigger({ ...trigger, every_minutes: num(e.target.value) })} />
              </div>
              {noDays && <div className="form-wide-2 page-hint" style={{ marginTop: 0 }}>No days selected = every day.</div>}
              <div className="form-wide-2 page-hint" style={{ marginTop: 0 }}>Each run takes whatever the segment selects at that moment (e.g. never called keeps draining the list day by day) and lands on the calendar at the next free slot.</div>
            </div>
          )}
          {trigger.type === 'ON_NEW_LEAD' && (
            <div className="page-hint" style={{ marginTop: 0 }}>Fires for rows added to the dataset (upload, append, or CRM pull) that have never been called. Only one active workflow per dataset may use this trigger. Outside the calling window the run waits for the window to open.</div>
          )}
          {trigger.type === 'AFTER_STEP' && (
            <div className="form-grid" style={{ gridTemplateColumns: '1fr 1fr 1fr' }}>
              <div className="form-wide-2"><label>Follows workflow</label>
                <select value={parentId} onChange={(e) => setParentId(e.target.value)}>
                  <option value="">— pick —</option>
                  {data.parents.filter((p) => p.id !== editId).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </div>
              <div><label>Delay (hours)</label><input type="number" min={1} value={trigger.delay_hours ?? ''} onChange={(e) => setTrigger({ ...trigger, delay_hours: num(e.target.value) })} /></div>
              <div className="form-wide-2"><label>Only leads whose previous call was tagged</label>
                <input value={(trigger.when_tags ?? []).join(', ')} onChange={(e) => setTrigger({ ...trigger, when_tags: csv(e.target.value) })} placeholder="qualified, qualified_no_price" />
              </div>
              {noMatch && <div className="form-wide-2 modal-error" style={{ marginTop: 0 }}>Name at least one tag: a step with none would follow up every call of the workflow it follows.</div>}
              <div className="form-wide-2 page-hint" style={{ marginTop: 0 }}>The step reuses the parent lead's details, so no dataset is needed.</div>
            </div>
          )}
        </Card>

        {!isStep && (
          <Card title="4 · Data: who gets called">
            <div style={{ marginBottom: 14 }}><label>Dataset</label>
              <select value={datasetId} onChange={(e) => { setDatasetId(e.target.value); setSegment({ never_called: true }); setVariableMap({}); setPreview(null); }}>
                {!datasetId && <option value="">— pick —</option>}
                {data.datasets.map((x) => <option key={x.id} value={x.id}>{x.name} · {x.row_count}</option>)}
              </select>
            </div>
            {facets && (
              <div className="form-grid" style={{ gridTemplateColumns: '1fr 1fr 1fr' }}>
                <Chips label="Segments" items={facets.segments} selected={segment.segments} onToggle={(v) => setSeg({ segments: toggleIn(segment.segments, v) })} />
                <Chips label="Cities" items={facets.cities} selected={segment.cities} onToggle={(v) => setSeg({ cities: toggleIn(segment.cities, v) })} />
                <Chips label="Societies" items={facets.societies.slice(0, 30)} selected={segment.societies} onToggle={(v) => setSeg({ societies: toggleIn(segment.societies, v) })} />
                <div><label><input type="checkbox" checked={Boolean(segment.never_called)} onChange={(e) => setSeg({ never_called: e.target.checked })} /> Never called only</label></div>
                <div><label>Max attempts so far</label><input type="number" min={0} value={segment.max_attempts ?? ''} placeholder="any" onChange={(e) => setSeg({ max_attempts: num(e.target.value) })} /></div>
                <div><label>Cap per run</label><input type="number" min={1} value={segment.limit ?? ''} placeholder="all" onChange={(e) => setSeg({ limit: num(e.target.value) })} /></div>
                {facets.tags.length > 0 && <Chips label="Last tag" items={facets.tags} selected={segment.last_tag_in} onToggle={(v) => setSeg({ last_tag_in: toggleIn(segment.last_tag_in, v) })} />}
              </div>
            )}
            <div className="toolbar" style={{ marginTop: 14, marginBottom: 0 }}>
              <button type="button" className="btn-ghost" onClick={doPreview} disabled={busy || !datasetId}>Preview selection</button>
              {preview && <span>{preview.selected.toLocaleString('en-IN')} leads would be dialed · {preview.cooled} skipped by the cooling guard</span>}
            </div>
            {agent && agent.variables.length > 0 && ds && (
              <div style={{ marginTop: 14 }}>
                <label>Map agent variables to dataset columns</label>
                <div className="form-grid" style={{ gridTemplateColumns: '1fr 1fr 1fr' }}>
                  {agent.variables.map((v) => (
                    <div key={v}>
                      <div className="muted">{`{{${v}}}`}</div>
                      <select value={autoMap[v]} onChange={(e) => setVariableMap({ ...variableMap, [v]: e.target.value })}>
                        <option value="">— empty —</option>
                        {ds.columns.map((c) => <option key={c} value={c}>{c}</option>)}
                      </select>
                    </div>
                  ))}
                </div>
                <div className="page-hint">Names are shortened to the first word; bhk is spoken as थ्री बी एच के automatically.</div>
              </div>
            )}
          </Card>
        )}

        <Card title="5 · Redial policy">
          <RedialPolicyFields value={policy} onChange={setPolicy} numbers={data.numbers.map((n) => n.number).filter((n) => n !== fromNumber)} />
        </Card>

        <Card title="6 · What happens after each call">
          <div className="page-hint" style={{ marginTop: 0, marginBottom: 12 }}>Good and soft tags from the use case already create a hand-off, and DNC tags block the number. These rules add to that.</div>
          {rules.map((r, i) => (
            <div key={i} style={{ border: '1px solid var(--border)', borderRadius: 'var(--r)', padding: 14, marginBottom: 10 }}>
              <div className="form-grid">
                <div><label>When outcome is</label>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                    {OUTCOMES.map((o) => (
                      <button key={o} type="button" className={`pill ${r.when.outcomes?.includes(o) ? 'pill-on' : ''}`}
                        onClick={() => patchRule(i, (x) => ({ ...x, when: { ...x.when, outcomes: toggleIn(x.when.outcomes, o) } }))}>{o.toLowerCase().replace('_', ' ')}</button>
                    ))}
                  </div>
                </div>
                <div><label>or tag is one of</label>
                  <input value={(r.when.tags ?? []).join(', ')} placeholder="qualified, callback_requested"
                    onChange={(e) => patchRule(i, (x) => ({ ...x, when: { ...x.when, tags: csv(e.target.value) } }))} />
                </div>
              </div>
              <div style={{ marginTop: 10 }}>
                <label>Then</label>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
                  {r.actions.map((a, k) => (
                    <span key={k} className="chip" style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
                      {ACTIONS.find(([t]) => t === a.type)[1]}
                      {a.type === 'whatsapp' && <input style={{ width: 130, padding: '2px 8px' }} placeholder="template" value={a.template}
                        onChange={(e) => patchRule(i, (x) => ({ ...x, actions: x.actions.map((y, m) => (m === k ? { ...y, template: e.target.value } : y)) }))} />}
                      {a.type === 'redial' && <><input type="number" min={1} style={{ width: 80, padding: '2px 8px' }} value={a.after_minutes}
                        onChange={(e) => patchRule(i, (x) => ({ ...x, actions: x.actions.map((y, m) => (m === k ? { ...y, after_minutes: Number(e.target.value) } : y)) }))} /> min</>}
                      <button type="button" className="modal-close" style={{ fontSize: 16 }} aria-label="Remove action"
                        onClick={() => patchRule(i, (x) => ({ ...x, actions: x.actions.filter((_, m) => m !== k) }))}>×</button>
                    </span>
                  ))}
                  <select style={{ width: 'auto' }} value="" onChange={(e) => {
                    const t = e.target.value;
                    if (!t) return;
                    const a = t === 'whatsapp' ? { type: t, template: '' } : t === 'redial' ? { type: t, after_minutes: 180 } : { type: t };
                    patchRule(i, (x) => ({ ...x, actions: [...x.actions, a] }));
                  }}>
                    <option value="">+ action</option>
                    {ACTIONS.map(([t, l]) => <option key={t} value={t}>{l}</option>)}
                  </select>
                  <button type="button" className="btn-danger" style={{ marginLeft: 'auto' }} onClick={() => setRules(rules.filter((_, j) => j !== i))}>Remove rule</button>
                </div>
              </div>
            </div>
          ))}
          <button type="button" className="btn-ghost" onClick={() => setRules([...rules, { when: { tags: [] }, actions: [] }])}>+ Add rule</button>
        </Card>
      </div>

      <aside className="card-block" style={{ position: 'sticky', top: 16 }}>
        <h3>Save</h3>
        <div style={{ marginBottom: 12 }}><label>Priority (1 = first on the calendar)</label><input type="number" min={1} max={9} value={priority} onChange={(e) => setPriority(e.target.value)} /></div>
        <div style={{ marginBottom: 12 }}><label><input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} /> Active (triggers may fire)</label></div>
        <div className="muted" style={{ marginBottom: 12, display: 'grid', gap: 3 }}>
          <div>Use case: {uc?.name}</div>
          <div>Agent: {agent?.name}</div>
          <div>Trigger: {trigger.type.toLowerCase().replace('_', ' ')}</div>
          {!isStep && <div>Data: {ds?.name ?? '—'}</div>}
          <div>Rules: {rules.length}</div>
        </div>
        <button type="button" className="btn-primary" style={{ width: '100%' }} disabled={busy || !name.trim() || noMatch} onClick={submit}>{editId ? 'Save changes' : 'Create workflow'}</button>
        <div className="page-hint">Saving does not dial anyone. Start a run from the workflow page, or let the trigger do it.</div>
      </aside>
    </div>
  );
}

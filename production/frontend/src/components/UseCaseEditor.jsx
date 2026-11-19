import { useState } from 'react';
import { api } from '../api/client.js';
import { toast } from '../utils/toast.js';
import { Modal } from './ui.jsx';

const TAX = [['good', 'Good'], ['soft', 'Soft'], ['retry', 'Retry'], ['terminal', 'Final'], ['dnc', 'DNC']];
const list = (s) => s.split(',').map((x) => x.trim().toLowerCase()).filter(Boolean);

// uc = existing use case (edit) or null (create). Every taxonomy list is sent, even when empty.
export default function UseCaseEditor({ dept, uc, agents, onClose, onSaved }) {
  const [v, setV] = useState(() => ({
    name: uc?.name ?? '',
    description: uc?.description ?? '',
    default_agent_id: uc?.default_agent_id ?? '',
    priority: uc?.priority ?? 5,
    color: uc?.color ?? (dept === 'supply' ? '#12315C' : '#FA531B'),
    active: uc?.active ?? true,
    tax: Object.fromEntries(TAX.map(([k]) => [k, (uc?.tag_taxonomy?.[k] ?? []).join(', ')])),
  }));
  const [busy, setBusy] = useState(false);
  const set = (k, x) => setV((p) => ({ ...p, [k]: x }));

  async function save() {
    setBusy(true);
    try {
      const body = {
        name: v.name,
        description: v.description || null,
        default_agent_id: v.default_agent_id || null,
        priority: Number(v.priority),
        color: v.color,
        active: v.active,
        tag_taxonomy: Object.fromEntries(TAX.map(([k]) => [k, list(v.tax[k])])),
      };
      if (uc) await api.put(`/api/use-cases/${uc.id}`, { ...body, key: uc.key }, { silent: true });
      else await api.post(`/api/departments/${dept}/use-cases`, { ...body, key: v.name }, { silent: true });
      toast(uc ? 'Use case saved' : 'Use case added', 'success');
      onSaved();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open onClose={onClose} title={uc ? `Edit ${uc.name}` : 'Add a use case'}>
      <div className="form-grid">
        <div><label>Name</label><input value={v.name} onChange={(e) => set('name', e.target.value)} placeholder="Seller qualification" /></div>
        <div>
          <label>Default agent</label>
          <select value={v.default_agent_id} onChange={(e) => set('default_agent_id', e.target.value)}>
            <option value="">—</option>
            {agents.filter((a) => a.vertical === dept.toUpperCase() || a.vertical === 'BOTH').map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </div>
        <div className="form-wide-2"><label>Description</label><input value={v.description} onChange={(e) => set('description', e.target.value)} placeholder="Who we call and what a win looks like" /></div>
        <div><label>Priority (1 = first)</label><input type="number" min={1} max={9} value={v.priority} onChange={(e) => set('priority', e.target.value)} /></div>
        <div><label>Colour</label><input type="color" value={v.color} onChange={(e) => set('color', e.target.value)} /></div>
        <div className="form-wide-2"><label><input type="checkbox" checked={v.active} onChange={(e) => set('active', e.target.checked)} /> Active</label></div>
        <div className="form-wide-2 page-hint" style={{ marginTop: 0 }}>
          Tag taxonomy: comma-separated, must match the agent's extraction tags exactly. good + soft go to an RM as a hand-off; retry is redialled; final is never called again for this use case; DNC is blocked everywhere.
        </div>
        {TAX.map(([k, label]) => (
          <div key={k}>
            <label>{label}</label>
            <input value={v.tax[k]} onChange={(e) => set('tax', { ...v.tax, [k]: e.target.value })} />
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 8, marginTop: 18 }}>
        <button type="button" className="btn-primary" disabled={busy || !v.name.trim()} onClick={save}>{uc ? 'Save' : 'Add use case'}</button>
        <button type="button" className="btn-ghost" onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  );
}

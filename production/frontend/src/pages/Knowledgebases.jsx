import { Fragment, useEffect, useState } from 'react';
import { api, download, upload } from '../api/client.js';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { uploadSizeError } from '../utils/format.js';
import { toast } from '../utils/toast.js';
import { PageHead, PageState, Card, Table, Empty, Modal } from '../components/ui.jsx';

// Bolna knowledge bases: every endpoint of their knowledgebase API (list, get, create, delete). Bolna has no update.
const DEFAULTS = { chunk_size: '512', similarity_top_k: '15', overlapping: '128' }; // Bolna's documented defaults
const POLL_MS = 10000; // while one is processing

const num = (v) => (v == null ? '—' : Number(v).toLocaleString('en-IN'));
const lang = (v) => (v === 'multilingual' ? 'Multilingual' : 'English');

function StatusBadge({ s }) {
  const cls = s === 'processed' ? 'badge-green' : s === 'processing' ? 'badge-amber' : 'badge-red';
  return <span className={`badge ${cls}`}>{s}</span>;
}

export default function Knowledgebases() {
  const { user } = useAuth();
  const owner = user.role === 'ADMIN';
  const q = useFetch('/api/knowledgebases');
  const [creating, setCreating] = useState(false);
  const [viewing, setViewing] = useState(null);
  const items = q.data?.items ?? [];
  const processing = items.some((k) => k.status === 'processing');

  useEffect(() => {
    if (!processing) return undefined;
    const t = setInterval(q.reload, POLL_MS);
    return () => clearInterval(t);
  }, [processing, q.reload]);

  // A web page opens directly; a PDF opens our copy (kept when it was uploaded here; Bolna never gives files back).
  async function open(k) {
    if (k.source) { window.open(k.source, '_blank', 'noopener'); return; }
    const w = window.open('', '_blank'); // opened before the await, so popup blockers allow it
    try {
      const blob = await download(`/api/knowledgebases/${k.rag_id}/file`);
      w.location = URL.createObjectURL(blob);
    } catch (err) {
      w.close();
      toast(err.message, 'error');
    }
  }

  async function remove(k) {
    if (!window.confirm(`Delete ${k.file_name}? Bolna deletes it for good.`)) return;
    try {
      await api.del(`/api/knowledgebases/${k.rag_id}`, { toast: 'Knowledge base deleted' });
      q.reload();
    } catch { /* api.del already toasted */ }
  }

  const cols = [
    { key: 'file_name', label: 'Name' },
    { key: 'source_type', label: 'Type', render: (k) => (k.source_type || '—').toUpperCase() },
    { key: 'status', label: 'Status', render: (k) => <StatusBadge s={k.status} /> },
    { key: 'chunk_size', label: 'Chunk size', align: 'right', render: (k) => num(k.chunk_size) },
    { key: 'similarity_top_k', label: 'Top K', align: 'right', render: (k) => num(k.similarity_top_k) },
    { key: 'overlapping', label: 'Overlap', align: 'right', render: (k) => num(k.overlapping) },
    { key: 'language_support', label: 'Language', render: (k) => lang(k.language_support) },
    { key: 'agent_count', label: 'Agents', align: 'right', render: (k) => num(k.agent_count) },
    { key: 'humanized_created_at', label: 'Created', render: (k) => <span title={k.created_at}>{k.humanized_created_at || '—'}</span> },
    { key: 'actions', label: '', render: (k) => (
      <span style={{ display: 'flex', gap: 8 }}>
        <button type="button" className="btn-link" onClick={() => open(k)}>Open</button>
        <button type="button" className="btn-link" onClick={() => setViewing(k.rag_id)}>Details</button>
        {owner && <button type="button" className="btn-link" onClick={() => remove(k)}>Delete</button>}
      </span>
    ) },
  ];

  return (
    <>
      <PageHead title="Knowledge base" sub={q.data ? `${items.length} in Bolna${processing ? ' · processing, refreshing every 10 s' : ''}` : ' '}
        actions={(
          <span style={{ display: 'flex', gap: 8 }}>
            <button type="button" className="btn-ghost" onClick={q.reload}>Refresh</button>
            {owner && <button type="button" className="btn-primary" onClick={() => setCreating(true)}>New knowledge base</button>}
          </span>
        )} />
      <PageState loading={q.loading} error={q.error} onRetry={q.reload} />
      {q.data && (items.length
        ? <Card><Table cols={cols} rows={items.map((k) => ({ ...k, id: k.rag_id }))} /></Card>
        : <Empty>No knowledge bases in Bolna yet.</Empty>)}
      {creating && <CreateModal onClose={() => setCreating(false)} onCreated={() => { setCreating(false); q.reload(); }} />}
      {viewing && <DetailsModal ragId={viewing} onClose={() => setViewing(null)} />}
    </>
  );
}

function CreateModal({ onClose, onCreated }) {
  const [source, setSource] = useState('pdf');
  const [file, setFile] = useState(null);
  const [url, setUrl] = useState('');
  const [opts, setOpts] = useState(DEFAULTS);
  const [multilingual, setMultilingual] = useState(false);
  const [busy, setBusy] = useState(false);
  const set = (k, v) => setOpts((o) => ({ ...o, [k]: v }));

  async function submit(e) {
    e.preventDefault();
    if (source === 'pdf') {
      if (!file) { toast('Choose a PDF', 'error'); return; }
      const tooBig = uploadSizeError(file);
      if (tooBig) { toast(tooBig, 'error'); return; }
    }
    const fd = new FormData();
    if (source === 'pdf') fd.append('file', file); else fd.append('url', url.trim());
    for (const [k, v] of Object.entries(opts)) fd.append(k, v);
    if (multilingual) fd.append('language_support', 'multilingual');
    setBusy(true);
    try {
      const r = await upload('/api/knowledgebases', fd);
      toast(`${r.file_name} created · ${r.status}`, 'success');
      onCreated();
    } catch (err) {
      toast(err.message, 'error');
      setBusy(false);
    }
  }

  return (
    <Modal open onClose={onClose} title="New knowledge base">
      <form onSubmit={submit}>
        <div className="form-grid">
          <div className="form-wide-2" style={{ display: 'flex', gap: 6 }}>
            <button type="button" className={`pill ${source === 'pdf' ? 'pill-on' : ''}`} onClick={() => setSource('pdf')}>PDF file</button>
            <button type="button" className={`pill ${source === 'url' ? 'pill-on' : ''}`} onClick={() => setSource('url')}>Web page (URL)</button>
          </div>
          {source === 'pdf' ? (
            <div className="form-wide-2"><label>PDF (max 20 MB)</label>
              <input key="pdf" type="file" accept="application/pdf,.pdf" required onChange={(e) => setFile(e.target.files[0] ?? null)} /></div>
          ) : (
            <div className="form-wide-2"><label>URL to scrape</label>
              <input key="url" type="url" required value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" /></div>
          )}
          <div><label>Chunk size</label>
            <input type="number" min="1" step="1" required value={opts.chunk_size} onChange={(e) => set('chunk_size', e.target.value)} /></div>
          <div><label>Overlap (characters)</label>
            <input type="number" min="0" step="1" required value={opts.overlapping} onChange={(e) => set('overlapping', e.target.value)} /></div>
          <div><label>Similar chunks per answer (top K)</label>
            <input type="number" min="1" step="1" required value={opts.similarity_top_k} onChange={(e) => set('similarity_top_k', e.target.value)} /></div>
          <div><label>Language</label>
            <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontWeight: 400 }}>
              <input type="checkbox" checked={multilingual} onChange={(e) => setMultilingual(e.target.checked)} style={{ width: 'auto' }} />
              Multilingual (100+ languages)
            </label></div>
        </div>
        <div className="mt" style={{ display: 'flex', gap: 8 }}>
          <button type="submit" className="btn-primary" disabled={busy}>{busy ? 'Uploading…' : 'Create'}</button>
          <button type="button" className="btn-ghost" onClick={onClose}>Cancel</button>
        </div>
      </form>
    </Modal>
  );
}

function DetailsModal({ ragId, onClose }) {
  const q = useFetch(`/api/knowledgebases/${ragId}`);
  return (
    <Modal open onClose={onClose} title={q.data?.file_name ?? 'Knowledge base'}>
      <PageState loading={q.loading} error={q.error} onRetry={q.reload} />
      {q.data && (
        <dl className="kv">
          {Object.entries(q.data).map(([k, v]) => (
            <Fragment key={k}><dt>{k}</dt><dd>{v == null || v === '' ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v)}</dd></Fragment>
          ))}
        </dl>
      )}
    </Modal>
  );
}

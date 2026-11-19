import { useFetch } from '../api/useFetch.js';
import { PageState } from './ui.jsx';

// The <audio> element can't send the Bearer header, so fetch the (signed) URL first.
// Only mount this for a call that has a recording: the endpoint 404s otherwise.
export default function RecordingPlayer({ callId }) {
  const q = useFetch(`/api/calls/${callId}/recording`);
  if (!q.data) return <PageState loading={q.loading} error={q.error} onRetry={q.reload} />;
  return <audio controls preload="none" style={{ width: '100%' }} src={q.data.url} />;
}

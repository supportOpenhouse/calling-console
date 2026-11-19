import { Link, Navigate, useParams } from 'react-router-dom';
import { useFetch } from '../api/useFetch.js';
import { useAuth } from '../contexts/AuthContext.jsx';
import { canManageDept } from '../utils/format.js';
import { PageHead, PageState } from '../components/ui.jsx';
import CampaignWizard from '../components/CampaignWizard.jsx';

export default function CampaignNew() {
  const { dept } = useParams();
  const { user } = useAuth();
  if (!canManageDept(user, dept)) return <Navigate to={`/d/${dept}/campaigns`} replace />;
  return <CampaignNewFor key={dept} dept={dept} />;
}

function CampaignNewFor({ dept }) {
  const q = useFetch(`/api/departments/${dept}/builder-data`);
  return (
    <>
      <PageHead title="New campaign"
        actions={<Link to={`/d/${dept}/campaigns`} className="btn-ghost">← Campaigns</Link>} />
      <PageState loading={q.loading} error={q.error} onRetry={q.reload} />
      {q.data && <CampaignWizard dept={dept} data={q.data} />}
    </>
  );
}

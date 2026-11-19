import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { useAuth } from './contexts/AuthContext.jsx';
import { homeDept } from './utils/format.js';
import Layout from './components/Layout.jsx';
import Toaster from './components/Toaster.jsx';
import Login from './pages/Login.jsx';
import Dashboard from './pages/Dashboard.jsx';
import UseCases from './pages/UseCases.jsx';
import DeptSettings from './pages/DeptSettings.jsx';
import Team from './pages/Team.jsx';
import Datasets from './pages/Datasets.jsx';
import DatasetNew from './pages/DatasetNew.jsx';
import DatasetDetail from './pages/DatasetDetail.jsx';
import Workflows, { WorkflowNew } from './pages/Workflows.jsx';
import WorkflowDetail from './pages/WorkflowDetail.jsx';
import Schedule from './pages/Schedule.jsx';
import Campaigns from './pages/Campaigns.jsx';
import CampaignNew from './pages/CampaignNew.jsx';
import CampaignDetail from './pages/CampaignDetail.jsx';
import Calls from './pages/Calls.jsx';
import CallDetail from './pages/CallDetail.jsx';
import Handoffs from './pages/Handoffs.jsx';
import Agents from './pages/Agents.jsx';
import AgentDetail from './pages/AgentDetail.jsx';
import Integrations from './pages/Integrations.jsx';

function RequireAuth({ children }) {
  const { user, loading, meError, retryMe } = useAuth();
  const loc = useLocation();
  if (loading) return null;
  if (!user && meError) {
    return (
      <div className="empty-state" style={{ height: '100vh', justifyContent: 'center' }}>
        <div>Can’t reach the server</div>
        <button type="button" className="btn-ghost" onClick={retryMe}>Retry</button>
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace state={{ from: loc.pathname + loc.search }} />;
  return children;
}

// No home page: straight to the dashboard of the department the user last chose.
function DeptHome() {
  const { user } = useAuth();
  return <Navigate to={`/d/${homeDept(user)}`} replace />;
}

export default function App() {
  return (
    <>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route element={<RequireAuth><Layout /></RequireAuth>}>
          <Route path="/" element={<DeptHome />} />
          <Route path="/d/:dept" element={<Dashboard />} />
          <Route path="/d/:dept/use-cases" element={<UseCases />} />
          <Route path="/d/:dept/datasets" element={<Datasets />} />
          <Route path="/d/:dept/datasets/new" element={<DatasetNew />} />
          <Route path="/d/:dept/datasets/:id" element={<DatasetDetail />} />
          <Route path="/d/:dept/workflows" element={<Workflows />} />
          <Route path="/d/:dept/workflows/new" element={<WorkflowNew />} />
          <Route path="/d/:dept/workflows/:id" element={<WorkflowDetail />} />
          <Route path="/d/:dept/schedule" element={<Schedule />} />
          <Route path="/d/:dept/campaigns" element={<Campaigns />} />
          <Route path="/d/:dept/campaigns/new" element={<CampaignNew />} />
          <Route path="/campaigns/:id" element={<CampaignDetail />} />
          <Route path="/d/:dept/calls" element={<Calls />} />
          <Route path="/calls/:id" element={<CallDetail />} />
          <Route path="/d/:dept/handoffs" element={<Handoffs />} />
          <Route path="/d/:dept/team" element={<Team />} />
          <Route path="/d/:dept/settings" element={<DeptSettings />} />
          <Route path="/agents" element={<Agents />} />
          <Route path="/agents/:id" element={<AgentDetail />} />
          <Route path="/team" element={<Team />} />
          <Route path="/settings" element={<Integrations />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <Toaster />
    </>
  );
}

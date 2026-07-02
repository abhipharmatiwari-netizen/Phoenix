import React from 'react';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import Shell from './components/layout/Shell';
import Overview from './pages/Overview';
import Tenants from './pages/Tenants';
import Alerts from './pages/Alerts';
import Mitigations from './pages/Mitigations';
import Positions from './pages/Positions';
import Orders from './pages/Orders';
import Trades from './pages/Trades';
import Pnl from './pages/Pnl';
import Accounts from './pages/Accounts';
import Audit from './pages/Audit';
import ControlTower from './pages/ControlTower';
import ReleaseEvidence from './pages/ReleaseEvidence';
import Safety from './pages/Safety';
import Settings from './pages/Settings';
import Strategies from './pages/Strategies';
import StrategyCandidates from './pages/StrategyCandidates';
import Login from './pages/Login';
import NotFound from './pages/NotFound';
import { AuthProvider } from './auth/AuthContext';
import ProtectedRoute from './auth/ProtectedRoute';
import './App.css';

function App() {
  return (
    <AuthProvider>
      <Router>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route element={<ProtectedRoute />}>
            <Route element={<Shell />}>
              <Route path="/" element={<Overview />} />
              <Route path="/tenants" element={<Tenants />} />
              <Route path="/alerts" element={<Alerts />} />
              <Route path="/mitigations" element={<Mitigations />} />
              <Route path="/positions" element={<Positions />} />
              <Route path="/orders" element={<Orders />} />
              <Route path="/strategies" element={<Strategies />} />
              <Route path="/accounts" element={<Accounts />} />
              <Route path="/trades" element={<Trades />} />
              <Route path="/pnl" element={<Pnl />} />
              <Route path="/control-tower" element={<ControlTower />} />
              <Route path="/admin/strategy-candidates" element={<StrategyCandidates />} />
              <Route path="/safety" element={<Safety />} />
              <Route path="/audit" element={<Audit />} />
              <Route path="/release-evidence" element={<ReleaseEvidence />} />
              <Route path="/settings" element={<Settings />} />
            </Route>
          </Route>
          <Route path="*" element={<NotFound />} />
        </Routes>
      </Router>
    </AuthProvider>
  );
}

export default App;

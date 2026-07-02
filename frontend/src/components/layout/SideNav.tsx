import React from 'react';
import { NavLink } from 'react-router-dom';
import {
  Home,
  Users,
  AlertCircle,
  Shield,
  Archive,
  ShoppingCart,
  BarChart2,
  DollarSign,
  Settings,
  GitPullRequest,
  Heart,
  Activity,
  Clipboard,
  FileText,
  Sliders,
} from 'react-feather';
import './SideNav.css';

const SideNav: React.FC = () => {
  return (
    <nav className="side-nav" aria-label="Phoenix operator console">
      <div className="side-nav__brand">
        <span className="side-nav__mark">P</span>
        <span>Phoenix</span>
      </div>
      <ul>
        <li>
          <NavLink to="/">
            <Home className="nav-icon" />
            Overview
          </NavLink>
        </li>
        <li>
          <NavLink to="/safety">
            <Heart className="nav-icon" />
            Safety
          </NavLink>
        </li>
        <li>
          <NavLink to="/positions">
            <Archive className="nav-icon" />
            Positions
          </NavLink>
        </li>
        <li>
          <NavLink to="/orders">
            <ShoppingCart className="nav-icon" />
            Orders
          </NavLink>
        </li>
        <li>
          <NavLink to="/strategies">
            <Activity className="nav-icon" />
            Strategies
          </NavLink>
        </li>
        <li>
          <NavLink to="/accounts">
            <Users className="nav-icon" />
            Accounts
          </NavLink>
        </li>
        <li>
          <NavLink to="/alerts">
            <AlertCircle className="nav-icon" />
            Alerts
          </NavLink>
        </li>
        <li>
          <NavLink to="/mitigations">
            <Shield className="nav-icon" />
            Mitigations
          </NavLink>
        </li>
        <li>
          <NavLink to="/trades">
            <BarChart2 className="nav-icon" />
            Trades
          </NavLink>
        </li>
        <li>
          <NavLink to="/pnl">
            <DollarSign className="nav-icon" />
            PnL
          </NavLink>
        </li>
        <li>
          <NavLink to="/audit">
            <Clipboard className="nav-icon" />
            Audit
          </NavLink>
        </li>
        <li>
          <NavLink to="/release-evidence">
            <FileText className="nav-icon" />
            Release Evidence
          </NavLink>
        </li>
        <li>
          <NavLink to="/control-tower">
            <Sliders className="nav-icon" />
            Control Tower
          </NavLink>
        </li>
        <li>
          <NavLink to="/tenants">
            <Users className="nav-icon" />
            Tenants
          </NavLink>
        </li>
        <li>
          <NavLink to="/admin/strategy-candidates">
            <GitPullRequest className="nav-icon" />
            Candidates
          </NavLink>
        </li>
        <li>
          <NavLink to="/settings">
            <Settings className="nav-icon" />
            Settings
          </NavLink>
        </li>
      </ul>
    </nav>
  );
};

export default SideNav;

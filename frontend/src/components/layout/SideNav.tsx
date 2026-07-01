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
} from 'react-feather';
import './SideNav.css';

interface NavItem {
  to: string;
  label: string;
  icon: React.ReactNode;
  end?: boolean;
}

interface SideNavProps {
  isOpen?: boolean;
  onNavigate?: () => void;
}

const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: <Home className="nav-icon" aria-hidden="true" />, end: true },
  { to: '/tenants', label: 'Tenants', icon: <Users className="nav-icon" aria-hidden="true" /> },
  { to: '/alerts', label: 'Alerts', icon: <AlertCircle className="nav-icon" aria-hidden="true" /> },
  { to: '/mitigations', label: 'Mitigations', icon: <Shield className="nav-icon" aria-hidden="true" /> },
  { to: '/positions', label: 'Positions', icon: <Archive className="nav-icon" aria-hidden="true" /> },
  { to: '/orders', label: 'Orders', icon: <ShoppingCart className="nav-icon" aria-hidden="true" /> },
  { to: '/trades', label: 'Trades', icon: <BarChart2 className="nav-icon" aria-hidden="true" /> },
  { to: '/pnl', label: 'PnL', icon: <DollarSign className="nav-icon" aria-hidden="true" /> },
  { to: '/control-tower', label: 'Control Tower', icon: <Settings className="nav-icon" aria-hidden="true" /> },
  { to: '/admin/strategy-candidates', label: 'Candidates', icon: <GitPullRequest className="nav-icon" aria-hidden="true" /> },
  { to: '/safety', label: 'Safety', icon: <Heart className="nav-icon" aria-hidden="true" /> },
];

const SideNav: React.FC<SideNavProps> = ({ isOpen = false, onNavigate }) => {
  return (
    <nav
      id="primary-navigation"
      className={`side-nav${isOpen ? ' is-open' : ''}`}
      aria-label="Primary navigation"
    >
      <ul>
        {NAV_ITEMS.map((item) => (
          <li key={item.to}>
            <NavLink to={item.to} end={item.end} onClick={onNavigate}>
              {item.icon}
              <span>{item.label}</span>
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
};

export default SideNav;

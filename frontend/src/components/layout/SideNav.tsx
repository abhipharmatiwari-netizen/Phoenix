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

interface NavItem {
  to: string;
  label: string;
  icon: React.ReactNode;
  end?: boolean;
}

interface NavSection {
  label: string;
  items: NavItem[];
}

interface SideNavProps {
  isOpen?: boolean;
  onNavigate?: () => void;
}

const NAV_SECTIONS: NavSection[] = [
  {
    label: 'Trading',
    items: [
      { to: '/', label: 'Overview', icon: <Home className="nav-icon" aria-hidden="true" />, end: true },
      { to: '/positions', label: 'Positions', icon: <Archive className="nav-icon" aria-hidden="true" /> },
      { to: '/orders', label: 'Orders', icon: <ShoppingCart className="nav-icon" aria-hidden="true" /> },
      { to: '/trades', label: 'Trades', icon: <BarChart2 className="nav-icon" aria-hidden="true" /> },
      { to: '/pnl', label: 'PnL', icon: <DollarSign className="nav-icon" aria-hidden="true" /> },
    ],
  },
  {
    label: 'Risk & Safety',
    items: [
      { to: '/safety', label: 'Safety', icon: <Heart className="nav-icon" aria-hidden="true" /> },
      { to: '/mitigations', label: 'Mitigations', icon: <Shield className="nav-icon" aria-hidden="true" /> },
      { to: '/alerts', label: 'Alerts', icon: <AlertCircle className="nav-icon" aria-hidden="true" /> },
    ],
  },
  {
    label: 'Strategy',
    items: [
      { to: '/strategies', label: 'Strategies', icon: <Activity className="nav-icon" aria-hidden="true" /> },
      { to: '/admin/strategy-candidates', label: 'Candidates', icon: <GitPullRequest className="nav-icon" aria-hidden="true" /> },
    ],
  },
  {
    label: 'Admin',
    items: [
      { to: '/accounts', label: 'Accounts', icon: <Users className="nav-icon" aria-hidden="true" /> },
      { to: '/tenants', label: 'Tenants', icon: <Users className="nav-icon" aria-hidden="true" /> },
      { to: '/audit', label: 'Audit', icon: <Clipboard className="nav-icon" aria-hidden="true" /> },
      { to: '/release-evidence', label: 'Release Evidence', icon: <FileText className="nav-icon" aria-hidden="true" /> },
      { to: '/control-tower', label: 'Control Tower', icon: <Sliders className="nav-icon" aria-hidden="true" /> },
      { to: '/settings', label: 'Settings', icon: <Settings className="nav-icon" aria-hidden="true" /> },
    ],
  },
];

const SideNav: React.FC<SideNavProps> = ({ isOpen = false, onNavigate }) => {
  return (
    <nav
      id="primary-navigation"
      className={`side-nav${isOpen ? ' is-open' : ''}`}
      aria-label="Primary navigation"
    >
      <div className="side-nav__brand">
        <span className="side-nav__mark">P</span>
        <span>Phoenix</span>
      </div>
      <div className="side-nav__sections">
        {NAV_SECTIONS.map((section) => (
          <section className="side-nav__section" key={section.label}>
            <div className="side-nav__section-label">{section.label}</div>
            <ul aria-label={section.label}>
              {section.items.map((item) => (
                <li key={item.to}>
                  <NavLink to={item.to} end={item.end} onClick={onNavigate}>
                    {item.icon}
                    <span>{item.label}</span>
                  </NavLink>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </nav>
  );
};

export default SideNav;

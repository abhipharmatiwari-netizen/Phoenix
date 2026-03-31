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
  Heart,
} from 'react-feather';
import './SideNav.css';

const SideNav: React.FC = () => {
  return (
    <div className="side-nav">
      <ul>
        <li>
          <NavLink to="/">
            <Home className="nav-icon" />
            Dashboard
          </NavLink>
        </li>
        <li>
          <NavLink to="/tenants">
            <Users className="nav-icon" />
            Tenants
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
          <NavLink to="/control-tower">
            <Settings className="nav-icon" />
            Control Tower
          </NavLink>
        </li>
        <li>
          <NavLink to="/safety">
            <Heart className="nav-icon" />
            Safety
          </NavLink>
        </li>
      </ul>
    </div>
  );
};

export default SideNav;

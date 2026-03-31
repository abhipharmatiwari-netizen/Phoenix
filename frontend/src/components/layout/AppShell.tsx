import React from 'react';
import TopNav from './TopNav';
import SideNav from './SideNav';

interface AppShellProps {
  children: React.ReactNode;
}

const AppShell: React.FC<AppShellProps> = ({ children }) => {
  return (
    <div className="app-shell">
      <TopNav />
      <div className="main-content">
        <SideNav />
        <div className="content">{children}</div>
      </div>
    </div>
  );
};

export default AppShell;

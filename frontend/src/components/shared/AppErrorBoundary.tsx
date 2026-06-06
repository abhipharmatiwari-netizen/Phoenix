import React from 'react';

interface AppErrorBoundaryState {
  message: string | null;
}

class AppErrorBoundary extends React.Component<React.PropsWithChildren, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = { message: null };

  componentDidMount() {
    window.addEventListener('error', this.handleWindowError);
    window.addEventListener('unhandledrejection', this.handleUnhandledRejection);
  }

  componentWillUnmount() {
    window.removeEventListener('error', this.handleWindowError);
    window.removeEventListener('unhandledrejection', this.handleUnhandledRejection);
  }

  componentDidCatch(error: Error) {
    this.setState({ message: error.message || 'The dashboard failed to render.' });
  }

  private handleWindowError = (event: ErrorEvent) => {
    this.setState({ message: event.error?.message || event.message || 'The dashboard failed to render.' });
  };

  private handleUnhandledRejection = (event: PromiseRejectionEvent) => {
    const reason = event.reason;
    this.setState({
      message: reason instanceof Error
        ? reason.message
        : String(reason || 'The dashboard session failed to initialize.'),
    });
  };

  private resetSession = () => {
    try {
      window.localStorage.removeItem('token');
      window.localStorage.removeItem('refresh_token');
    } catch {
      // Continue to /login even when browser storage is unavailable.
    }
    window.location.assign('/login');
  };

  render() {
    if (!this.state.message) {
      return this.props.children;
    }

    return (
      <main style={{ padding: '1rem', fontFamily: 'system-ui, sans-serif' }}>
        <h1>Phoenix could not render</h1>
        <p>{this.state.message}</p>
        <button type="button" onClick={this.resetSession}>
          Reset session
        </button>
      </main>
    );
  }
}

export default AppErrorBoundary;

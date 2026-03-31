/**
 * Sweep Manager UI - Manages profit sweep operations via REST API
 */

class SweepManager {
  constructor() {
    this.state = {
      tenantId: "",
      brokerAccountId: "",
      sweepStatus: null,
      sweepHistory: [],
      config: {},
      isLoading: false,
      error: null,
    };

    this.elements = {
      tenantInput: document.getElementById("sweep-tenant-id"),
      accountInput: document.getElementById("sweep-broker-account"),
      statusPanel: document.getElementById("sweep-status-panel"),
      historyPanel: document.getElementById("sweep-history-panel"),
      configPanel: document.getElementById("sweep-config-panel"),
      executeBtn: document.getElementById("sweep-execute-btn"),
      resetBtn: document.getElementById("sweep-reset-btn"),
      refreshBtn: document.getElementById("sweep-refresh-btn"),
      errorMsg: document.getElementById("sweep-error-msg"),
      successMsg: document.getElementById("sweep-success-msg"),
    };

    this.init();
  }

  init() {
    // Setup event listeners
    if (this.elements.executeBtn) {
      this.elements.executeBtn.addEventListener("click", () =>
        this.executeSweep()
      );
    }
    if (this.elements.resetBtn) {
      this.elements.resetBtn.addEventListener("click", () => this.resetSweep());
    }
    if (this.elements.refreshBtn) {
      this.elements.refreshBtn.addEventListener("click", () => this.refresh());
    }

    // Load initial data
    this.refresh();
  }

  showError(message) {
    this.state.error = message;
    if (this.elements.errorMsg) {
      this.elements.errorMsg.textContent = message;
      this.elements.errorMsg.style.display = "block";
      setTimeout(() => {
        this.elements.errorMsg.style.display = "none";
      }, 5000);
    }
  }

  showSuccess(message) {
    if (this.elements.successMsg) {
      this.elements.successMsg.textContent = message;
      this.elements.successMsg.style.display = "block";
      setTimeout(() => {
        this.elements.successMsg.style.display = "none";
      }, 3000);
    }
  }

  async refresh() {
    const tenantId = this.elements.tenantInput?.value || "default_tenant";
    const brokerAccountId =
      this.elements.accountInput?.value || "default_account";

    this.state.tenantId = tenantId;
    this.state.brokerAccountId = brokerAccountId;

    try {
      this.state.isLoading = true;
      await Promise.all([
        this.loadStatus(),
        this.loadHistory(),
        this.loadConfig(),
      ]);
      this.render();
    } catch (error) {
      this.showError(`Failed to load sweep data: ${error.message}`);
    } finally {
      this.state.isLoading = false;
    }
  }

  async loadStatus() {
    const params = new URLSearchParams({
      tenant_id: this.state.tenantId,
      broker_account_id: this.state.brokerAccountId,
    });

    const response = await fetch(`/api/sweep/status?${params}`);
    if (!response.ok) throw new Error("Failed to load status");
    this.state.sweepStatus = await response.json();
  }

  async loadHistory(limit = 10) {
    const params = new URLSearchParams({
      tenant_id: this.state.tenantId,
      broker_account_id: this.state.brokerAccountId,
      limit,
    });

    const response = await fetch(`/api/sweep/history?${params}`);
    if (!response.ok) throw new Error("Failed to load history");
    const data = await response.json();
    this.state.sweepHistory = data.records || [];
  }

  async loadConfig() {
    const params = new URLSearchParams({
      tenant_id: this.state.tenantId,
      broker_account_id: this.state.brokerAccountId,
    });

    const response = await fetch(`/api/sweep/status?${params}`);
    if (!response.ok) throw new Error("Failed to load config");
    const status = await response.json();
    this.state.config = {
      strategy: status.strategy || "SIMPLE",
      enabled: status.sweep_enabled !== false,
    };
  }

  async executeSweep() {
    try {
      this.state.isLoading = true;
      const forceFlag = document.getElementById("sweep-force-flag")?.checked;

      const response = await fetch("/api/sweep/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tenant_id: this.state.tenantId,
          broker_account_id: this.state.brokerAccountId,
          force: forceFlag,
        }),
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || "Failed to execute sweep");
      }

      this.showSuccess("Sweep executed successfully!");
      await this.refresh();
    } catch (error) {
      this.showError(error.message);
    } finally {
      this.state.isLoading = false;
    }
  }

  async resetSweep() {
    if (!confirm("Are you sure you want to reset the sweep state?"))
      return;

    try {
      this.state.isLoading = true;
      const params = new URLSearchParams({
        tenant_id: this.state.tenantId,
        broker_account_id: this.state.brokerAccountId,
        reason: "Manual reset via dashboard",
      });

      const response = await fetch(`/api/sweep/reset?${params}`, {
        method: "DELETE",
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || "Failed to reset sweep");
      }

      this.showSuccess("Sweep state reset successfully!");
      await this.refresh();
    } catch (error) {
      this.showError(error.message);
    } finally {
      this.state.isLoading = false;
    }
  }

  render() {
    this.renderStatus();
    this.renderHistory();
    this.renderConfig();
  }

  renderStatus() {
    if (!this.elements.statusPanel || !this.state.sweepStatus) return;

    const status = this.state.sweepStatus;
    const html = `
      <div class="sweep-status-content">
        <div class="status-row">
          <span class="status-label">Strategy:</span>
          <span class="status-value">${status.strategy || "N/A"}</span>
        </div>
        <div class="status-row">
          <span class="status-label">Sweeps Today:</span>
          <span class="status-value status-count">${
            status.sweep_count_today || 0
          }</span>
        </div>
        <div class="status-row">
          <span class="status-label">Last Sweep:</span>
          <span class="status-value">${
            status.last_sweep_time
              ? new Date(status.last_sweep_time).toLocaleTimeString()
              : "Never"
          }</span>
        </div>
        <div class="status-row">
          <span class="status-label">Minutes Since:</span>
          <span class="status-value">${(
            status.minutes_since_last_sweep || 0
          ).toFixed(1)}</span>
        </div>
        <div class="status-row">
          <span class="status-label">Status:</span>
          <span class="status-value ${
            status.is_active ? "status-active" : "status-inactive"
          }">
            ${status.is_active ? "Active" : "Inactive"}
          </span>
        </div>
      </div>
    `;
    this.elements.statusPanel.innerHTML = html;
  }

  renderHistory() {
    if (!this.elements.historyPanel) return;

    if (!this.state.sweepHistory || this.state.sweepHistory.length === 0) {
      this.elements.historyPanel.innerHTML =
        "<p style='color: var(--muted)'>No history yet</p>";
      return;
    }

    const rows = this.state.sweepHistory
      .map(
        (record) => `
      <tr>
        <td>${new Date(record.timestamp).toLocaleString()}</td>
        <td>${record.strategy}</td>
        <td>${(record.amount_swept || 0).toFixed(2)}</td>
        <td>${record.status}</td>
      </tr>
    `
      )
      .join("");

    const html = `
      <table class="sweep-history-table">
        <thead>
          <tr>
            <th>Timestamp</th>
            <th>Strategy</th>
            <th>Amount</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          ${rows}
        </tbody>
      </table>
    `;
    this.elements.historyPanel.innerHTML = html;
  }

  renderConfig() {
    if (!this.elements.configPanel) return;

    const config = this.state.config;
    const html = `
      <div class="config-content">
        <div class="config-item">
          <label>Strategy:</label>
          <select id="sweep-strategy-select">
            <option value="SIMPLE" ${
              config.strategy === "SIMPLE" ? "selected" : ""
            }>SIMPLE</option>
            <option value="MULTIPLE" ${
              config.strategy === "MULTIPLE" ? "selected" : ""
            }>MULTIPLE</option>
          </select>
        </div>
        <div class="config-item">
          <label>
            <input type="checkbox" id="sweep-enabled" ${
              config.enabled ? "checked" : ""
            } />
            Sweep Enabled
          </label>
        </div>
        <button id="sweep-config-save" class="btn btn-secondary">Save Config</button>
      </div>
    `;
    this.elements.configPanel.innerHTML = html;

    // Add save config listener
    document.getElementById("sweep-config-save")?.addEventListener("click", () =>
      this.saveConfig()
    );
  }

  async saveConfig() {
    try {
      const strategy = document.getElementById("sweep-strategy-select")?.value;
      const enabled = document.getElementById("sweep-enabled")?.checked;

      const response = await fetch("/api/sweep/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tenant_id: this.state.tenantId,
          broker_account_id: this.state.brokerAccountId,
          sweep_strategy: strategy,
          sweep_enabled: enabled,
        }),
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || "Failed to save config");
      }

      this.showSuccess("Configuration saved successfully!");
      await this.loadConfig();
    } catch (error) {
      this.showError(error.message);
    }
  }
}

// Initialize when DOM is ready
document.addEventListener("DOMContentLoaded", () => {
  window.sweepManager = new SweepManager();
});

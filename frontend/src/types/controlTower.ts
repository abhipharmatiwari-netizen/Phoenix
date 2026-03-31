export interface MatrixTenant {
  tenant_id: string;
  name: string;
}

export interface MatrixStrategy {
  strategy_id: string;
  display_name: string;
}

export interface MatrixResponse {
  tenants: MatrixTenant[];
  strategies: MatrixStrategy[];
  matrix: Record<string, Record<string, boolean>>;
}

import React, { useState, useMemo, useCallback, useRef } from 'react';

export interface Column<T> {
  key: string;
  header: string;
  render?: (row: T) => React.ReactNode;
  sortable?: boolean;
}

interface Props<T> {
  columns: Column<T>[];
  data: T[];
  sortable?: boolean;
  filterable?: boolean;
  onExport?: () => void;
  onRowClick?: (row: T) => void;
  rowKey?: (row: T) => string;
  emptyMessage?: string;
}

const ROW_HEIGHT = 36;
const VIRTUAL_THRESHOLD = 200;
const CONTAINER_HEIGHT = 600;
const OVERSCAN = 5;

const MemoRow = React.memo(function MemoRow<T extends Record<string, unknown>>({
  row, columns, onClick,
}: {
  row: T;
  columns: Column<T>[];
  onClick?: (row: T) => void;
}) {
  return (
    <tr
      role="row"
      onClick={onClick ? () => onClick(row) : undefined}
      style={{
        cursor: onClick ? 'pointer' : 'default',
        borderBottom: '1px solid #f3f4f6',
        height: ROW_HEIGHT,
      }}
      onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.backgroundColor = '#f9fafb'; }}
      onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.backgroundColor = ''; }}
    >
      {columns.map((col) => (
        <td key={col.key} role="cell" style={{ padding: '0.5rem', whiteSpace: 'nowrap' }}>
          {col.render ? col.render(row) : String(row[col.key] ?? '')}
        </td>
      ))}
    </tr>
  );
}) as <T extends Record<string, unknown>>(props: {
  row: T; columns: Column<T>[]; onClick?: (row: T) => void;
}) => React.ReactElement;

function DataTable<T extends Record<string, unknown>>({
  columns, data, sortable = true, filterable = true,
  onExport, onRowClick, rowKey, emptyMessage = 'No data',
}: Props<T>) {
  const [sortCol, setSortCol] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc');
  const [filter, setFilter] = useState('');
  const [scrollTop, setScrollTop] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);

  const filtered = useMemo(() => {
    if (!filter) return data;
    const lower = filter.toLowerCase();
    return data.filter((row) =>
      columns.some((col) => {
        const val = col.render ? '' : String(row[col.key] ?? '');
        return val.toLowerCase().includes(lower);
      })
    );
  }, [data, filter, columns]);

  const sorted = useMemo(() => {
    if (!sortCol) return filtered;
    return [...filtered].sort((a, b) => {
      const av = a[sortCol] ?? '';
      const bv = b[sortCol] ?? '';
      const cmp = String(av).localeCompare(String(bv), undefined, { numeric: true });
      return sortDir === 'asc' ? cmp : -cmp;
    });
  }, [filtered, sortCol, sortDir]);

  const handleSort = useCallback((key: string) => {
    if (sortCol === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortCol(key);
      setSortDir('asc');
    }
  }, [sortCol]);

  const useVirtual = sorted.length > VIRTUAL_THRESHOLD;
  const startIdx = useVirtual ? Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN) : 0;
  const visibleCount = useVirtual ? Math.ceil(CONTAINER_HEIGHT / ROW_HEIGHT) + 2 * OVERSCAN : sorted.length;
  const endIdx = Math.min(sorted.length, startIdx + visibleCount);
  const visibleRows = sorted.slice(startIdx, endIdx);

  const handleScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    setScrollTop(e.currentTarget.scrollTop);
  }, []);

  return (
    <div style={{ minWidth: 0, maxWidth: '100%' }}>
      <div style={{
        display: 'flex',
        gap: '0.5rem',
        marginBottom: '0.75rem',
        alignItems: 'center',
        flexWrap: 'wrap',
        minWidth: 0,
      }}>
        {filterable && (
          <input
            placeholder="Filter..." value={filter}
            onChange={(e) => setFilter(e.target.value)}
            aria-label="Filter table"
            style={{
              padding: '0.55rem 0.75rem',
              border: '1px solid #d1d5db',
              borderRadius: 4,
              flex: '1 1 220px',
              maxWidth: 300,
              minWidth: 0,
            }}
          />
        )}
        {onExport && (
          <button onClick={onExport} aria-label="Export to CSV" style={{ padding: '0.55rem 0.75rem', border: '1px solid #d1d5db', borderRadius: 4, cursor: 'pointer', backgroundColor: '#fff', fontSize: '0.8rem' }}>
            Export CSV
          </button>
        )}
        <span style={{ fontSize: '0.75rem', color: '#9ca3af' }}>{sorted.length} rows</span>
      </div>
      <div
        ref={containerRef}
        onScroll={useVirtual ? handleScroll : undefined}
        style={{
          overflowX: 'auto',
          maxWidth: '100%',
          WebkitOverflowScrolling: 'touch',
          ...(useVirtual ? { overflowY: 'auto', maxHeight: CONTAINER_HEIGHT } : {}),
        }}
      >
        <table role="table" style={{ width: '100%', minWidth: 'max-content', borderCollapse: 'collapse', fontSize: '0.875rem' }}>
          <thead>
            <tr role="row">
              {columns.map((col) => (
                <th
                  key={col.key} role="columnheader"
                  aria-sort={sortCol === col.key ? (sortDir === 'asc' ? 'ascending' : 'descending') : undefined}
                  onClick={sortable && col.sortable !== false ? () => handleSort(col.key) : undefined}
                  style={{
                    textAlign: 'left', padding: '0.5rem', borderBottom: '2px solid #e5e7eb',
                    cursor: sortable && col.sortable !== false ? 'pointer' : 'default',
                    userSelect: 'none', whiteSpace: 'nowrap', color: '#374151', fontWeight: 600,
                    position: useVirtual ? 'sticky' : undefined, top: useVirtual ? 0 : undefined,
                    backgroundColor: '#fff', zIndex: useVirtual ? 1 : undefined,
                  }}
                >
                  {col.header}
                  {sortCol === col.key && (sortDir === 'asc' ? ' \u25B2' : ' \u25BC')}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 ? (
              <tr><td colSpan={columns.length} style={{ padding: '2rem', textAlign: 'center', color: '#9ca3af' }}>{emptyMessage}</td></tr>
            ) : useVirtual ? (
              <>
                {startIdx > 0 && (
                  <tr style={{ height: startIdx * ROW_HEIGHT }}><td colSpan={columns.length} /></tr>
                )}
                {visibleRows.map((row, i) => (
                  <MemoRow
                    key={rowKey ? rowKey(row) : startIdx + i}
                    row={row}
                    columns={columns}
                    onClick={onRowClick}
                  />
                ))}
                {endIdx < sorted.length && (
                  <tr style={{ height: (sorted.length - endIdx) * ROW_HEIGHT }}><td colSpan={columns.length} /></tr>
                )}
              </>
            ) : (
              sorted.map((row, i) => (
                <MemoRow
                  key={rowKey ? rowKey(row) : i}
                  row={row}
                  columns={columns}
                  onClick={onRowClick}
                />
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default DataTable;

import { useState, useEffect, useRef } from 'react';

const MAX_RETRIES = 5;
const INITIAL_DELAY = 1000; // 1 second
const MAX_DELAY = 30000; // 30 seconds

type WebSocketUrlFactory = () => Promise<string>;

function mergeDashboardPayload(previous: unknown, incoming: unknown): unknown {
  if (
    incoming &&
    typeof incoming === 'object' &&
    !Array.isArray(incoming) &&
    (incoming as { _mode?: unknown })._mode === 'delta' &&
    previous &&
    typeof previous === 'object' &&
    !Array.isArray(previous)
  ) {
    return {
      ...(previous as Record<string, unknown>),
      ...(incoming as Record<string, unknown>),
    };
  }
  return incoming;
}

export const useWebSocket = (urlFactory: WebSocketUrlFactory) => {
  const [data, setData] = useState<unknown>(null);
  const [isStale, setIsStale] = useState(false);
  const ws = useRef<WebSocket | null>(null);
  const retryCount = useRef(0);
  const reconnectTimeout = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;

    const scheduleReconnect = () => {
      if (cancelled) {
        return;
      }
      if (retryCount.current < MAX_RETRIES) {
        const delay = Math.min(
          INITIAL_DELAY * Math.pow(2, retryCount.current) + Math.random() * 1000,
          MAX_DELAY
        );
        reconnectTimeout.current = window.setTimeout(() => {
          void connect();
        }, delay);
        retryCount.current++;
        return;
      }
      setIsStale(true);
    };

    const connect = async () => {
      let resolvedUrl = '';
      try {
        resolvedUrl = await urlFactory();
      } catch (error) {
        console.error('WebSocket ticket fetch failed:', error);
        setIsStale(true);
        scheduleReconnect();
        return;
      }

      if (cancelled) {
        return;
      }

      ws.current = new WebSocket(resolvedUrl);

      ws.current.onopen = () => {
        console.log('WebSocket connected');
        retryCount.current = 0;
      };

      ws.current.onmessage = (event) => {
        try {
          const parsed: unknown = JSON.parse(event.data);
          setData((previous: unknown): unknown => mergeDashboardPayload(previous, parsed));
          setIsStale(false);
        } catch {
          setIsStale(true);
        }
      };

      ws.current.onclose = () => {
        console.log('WebSocket disconnected');
        scheduleReconnect();
      };

      ws.current.onerror = (error) => {
        console.error('WebSocket error:', error);
        setIsStale(true);
      };
    };

    void connect();

    return () => {
      cancelled = true;
      if (reconnectTimeout.current !== null) {
        window.clearTimeout(reconnectTimeout.current);
      }
      if (ws.current) {
        ws.current.close();
      }
    };
  }, [urlFactory]);

  return { data, isStale };
};

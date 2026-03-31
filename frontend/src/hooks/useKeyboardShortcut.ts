import { useEffect, useRef, useCallback } from 'react';

type ShortcutHandler = () => void;

interface Shortcut {
  keys: string[];
  handler: ShortcutHandler;
}

/**
 * Hook for keyboard shortcuts supporting chord sequences (e.g., 'g' then 'o').
 * Ignores shortcuts when focus is in input/textarea/select elements.
 */
export function useKeyboardShortcut(shortcuts: Shortcut[]) {
  const bufferRef = useRef<string[]>([]);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    const target = e.target as HTMLElement;
    const tag = target.tagName.toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable) {
      return;
    }

    if (e.ctrlKey || e.metaKey || e.altKey) return;

    const key = e.key.toLowerCase();
    bufferRef.current.push(key);

    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => { bufferRef.current = []; }, 800);

    for (const shortcut of shortcuts) {
      const buf = bufferRef.current;
      if (buf.length >= shortcut.keys.length) {
        const tail = buf.slice(-shortcut.keys.length);
        if (tail.every((k, i) => k === shortcut.keys[i])) {
          e.preventDefault();
          bufferRef.current = [];
          shortcut.handler();
          return;
        }
      }
    }
  }, [shortcuts]);

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [handleKeyDown]);
}

export default useKeyboardShortcut;

/**
 * useApi — shared data-fetch hook (codifies the ExecutionsTab pattern).
 * TabErrorBoundary does NOT catch async errors, so every real-API view uses
 * this: {data, loading, error} + optional polling. No mock, ever.
 */
'use client';

import { useEffect, useState } from 'react';

export function useApi<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
  pollMs?: number,
): { data: T | null; loading: boolean; error: string | null } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    const load = () =>
      fetcher()
        .then(d => { if (alive) { setData(d); setError(null); } })
        .catch(e => { if (alive) setError(String(e?.message ?? e)); })
        .finally(() => { if (alive) setLoading(false); });
    load();
    if (pollMs) {
      const t = setInterval(load, pollMs);
      return () => { alive = false; clearInterval(t); };
    }
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, loading, error };
}

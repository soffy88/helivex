/**
 * useApi — shared data-fetch hook (codifies the ExecutionsTab pattern).
 * TabErrorBoundary does NOT catch async errors, so every real-API view uses
 * this: {data, loading, error, stale} + optional polling. No mock, ever.
 *
 * Resilience (vs the old version that blanked the whole view on any poll error):
 *  - data is RETAINED on error → callers render last-good data + a `stale` flag,
 *    instead of wiping a populated dashboard on a transient 15s-poll blip.
 *  - optional `cacheKey` gives stale-while-revalidate: a remounted view (tab/
 *    strategy switch) renders cached data instantly, then revalidates in the
 *    background — no skeleton flash, no duplicate cold loads.
 *  - the `alive` guard already prevents a slow earlier request from overwriting
 *    a newer one on dep change (last-write-wins by mount, not by arrival).
 */
'use client';

import { useEffect, useState } from 'react';

// module-level stale-while-revalidate cache (survives unmount / tab switch)
const _cache = new Map<string, unknown>();

export function useApi<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
  pollMs?: number,
  cacheKey?: string,
): { data: T | null; loading: boolean; error: string | null; stale: boolean } {
  const cached = cacheKey && _cache.has(cacheKey) ? (_cache.get(cacheKey) as T) : null;
  const [data, setData] = useState<T | null>(cached);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(cached === null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      fetcher()
        .then(d => {
          if (alive) {
            setData(d);
            setError(null);
            if (cacheKey) _cache.set(cacheKey, d);
          }
        })
        .catch(e => { if (alive) setError(String(e?.message ?? e)); })  // keep last-good data
        .finally(() => { if (alive) setLoading(false); });
    load();
    if (pollMs) {
      const t = setInterval(load, pollMs);
      return () => { alive = false; clearInterval(t); };
    }
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  // stale = showing retained data while the latest fetch failed (reconnecting)
  return { data, loading, error, stale: error !== null && data !== null };
}

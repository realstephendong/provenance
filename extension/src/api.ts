import { ContextResponse, CountResponse, Selection } from './types';

const CONTEXT_TIMEOUT_MS = 30_000;
// CodeLens must never visibly hang, so its budget is far tighter.
const COUNT_TIMEOUT_MS = 5_000;

async function post<T>(url: string, body: unknown, timeoutMs: number): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}`);
    }
    return (await response.json()) as T;
  } catch (err) {
    if (err instanceof Error && err.name === 'AbortError') {
      throw new Error(`request timed out after ${timeoutMs / 1000}s`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

export function postContext(base: string, selection: Selection): Promise<ContextResponse> {
  return post<ContextResponse>(`${base}/context`, selection, CONTEXT_TIMEOUT_MS);
}

export function postCount(base: string, selection: Selection): Promise<CountResponse> {
  return post<CountResponse>(`${base}/context/count`, selection, COUNT_TIMEOUT_MS);
}

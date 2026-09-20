import { ContextResponse, CountResponse, IngestResult, IngestStatus, Selection } from './types';

const CONTEXT_TIMEOUT_MS = 30_000;
// CodeLens must never visibly hang, so its budget is far tighter.
const COUNT_TIMEOUT_MS = 5_000;
// A sync reads Slack and then summarizes and embeds every affected thread. The first
// one on a cold index is a full backfill, so this is generous on purpose.
const SYNC_TIMEOUT_MS = 15 * 60_000;
const STATUS_TIMEOUT_MS = 5_000;

async function request<T>(
  url: string, timeoutMs: number, init: RequestInit = {},
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...init, signal: controller.signal });
    if (!response.ok) {
      // The ingest endpoints answer a refusal with a readable `detail` -- a missing
      // Slack scope, a corpus mismatch, a sync already running. Surfacing the status
      // line instead would throw that away and say "409 Conflict".
      const detail = await response.json().then(
        (body: { detail?: string }) => body?.detail, () => undefined,
      );
      throw new Error(detail || `${response.status} ${response.statusText}`);
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

function post<T>(url: string, body: unknown, timeoutMs: number): Promise<T> {
  return request<T>(url, timeoutMs, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function postContext(base: string, selection: Selection): Promise<ContextResponse> {
  return post<ContextResponse>(`${base}/context`, selection, CONTEXT_TIMEOUT_MS);
}

export function postCount(base: string, selection: Selection): Promise<CountResponse> {
  return post<CountResponse>(`${base}/context/count`, selection, COUNT_TIMEOUT_MS);
}

export function getIngestStatus(base: string): Promise<IngestStatus> {
  return request<IngestStatus>(`${base}/ingest/status`, STATUS_TIMEOUT_MS);
}

export function postIngestSync(base: string): Promise<IngestResult> {
  return request<IngestResult>(`${base}/ingest/sync`, SYNC_TIMEOUT_MS, { method: 'POST' });
}

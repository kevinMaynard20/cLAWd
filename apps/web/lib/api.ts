/**
 * Thin wrapper around `fetch` for the FastAPI backend.
 *
 * Goes DIRECT to `http://127.0.0.1:8000/*` rather than through the Next.js
 * `/api/*` rewrite. The rewrite was buffering long LLM responses and
 * disconnecting with `socket hang up` (ECONNRESET) for any feature call
 * that took 20+ seconds — case-brief, synthesis, attack-sheet, outline. We
 * already bypass the proxy for PDF uploads (Next caps proxied bodies at
 * 10 MiB); this extends that to ALL backend calls so long-running feature
 * generations don't time out at the proxy layer. Backend CORS in
 * `apps/api/src/main.py` whitelists localhost:3000 / 127.0.0.1:3000.
 *
 * Override the base via NEXT_PUBLIC_LAWSCHOOL_API_BASE for non-loopback
 * deployments (matches the upload-endpoint resolver in `lib/uploadEndpoint.ts`).
 *
 * Non-2xx responses surface an {@link ApiError} whose `message` is either
 * the JSON `detail` field or the raw body text.
 */

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

type Query = Record<string, string | number | boolean | undefined | null>;

function apiBase(): string {
  const override = process.env.NEXT_PUBLIC_LAWSCHOOL_API_BASE;
  return (override ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
}

function buildUrl(path: string, query?: Query): string {
  // Strip any legacy `/api/` prefix that older callers may pass — we go
  // direct to the backend root now.
  const stripped = path.startsWith("/api/") ? path.slice(4) : path;
  const suffix = stripped.startsWith("/") ? stripped : `/${stripped}`;
  const base = `${apiBase()}${suffix}`;
  if (!query) return base;
  const entries = Object.entries(query).filter(
    ([, v]) => v !== undefined && v !== null && v !== "",
  );
  if (entries.length === 0) return base;
  const params = new URLSearchParams();
  for (const [k, v] of entries) {
    params.set(k, String(v));
  }
  const sep = base.includes("?") ? "&" : "?";
  return `${base}${sep}${params.toString()}`;
}

async function parseBody(res: Response): Promise<unknown> {
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("application/json")) {
    try {
      return (await res.json()) as unknown;
    } catch {
      return null;
    }
  }
  try {
    return await res.text();
  } catch {
    return null;
  }
}

async function raise(res: Response): Promise<never> {
  const body = await parseBody(res);
  let message = res.statusText || `HTTP ${res.status}`;
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") message = detail;
    else if (detail) message = JSON.stringify(detail);
  } else if (typeof body === "string" && body.trim().length > 0) {
    message = body;
  }
  throw new ApiError(res.status, message, body);
}

async function request<T>(
  method: string,
  path: string,
  opts: { query?: Query; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
  };
  let payload: BodyInit | undefined;
  if (opts.body !== undefined && opts.body !== null) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(opts.body);
  }
  const res = await fetch(buildUrl(path, opts.query), {
    method,
    headers,
    body: payload,
    signal: opts.signal,
    // Local single-user app; no cookies, no credentials, no cache interference.
    credentials: "omit",
    cache: "no-store",
  });
  if (!res.ok) {
    await raise(res);
  }
  // Some endpoints (e.g., DELETE) may return 204 No Content.
  if (res.status === 204) return undefined as T;
  const parsed = await parseBody(res);
  return parsed as T;
}

export const api = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) =>
    request<T>("GET", path, { query, signal }),
  post: <T>(path: string, body?: unknown, query?: Query, signal?: AbortSignal) =>
    request<T>("POST", path, { body, query, signal }),
  delete: <T>(path: string, query?: Query, signal?: AbortSignal) =>
    request<T>("DELETE", path, { query, signal }),
};

export { buildUrl as _buildUrl };

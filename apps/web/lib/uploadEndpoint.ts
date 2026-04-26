/**
 * Resolve the absolute URL of the FastAPI backend for upload requests that
 * MUST bypass the Next.js dev-server rewrite proxy. Next 15's dev server
 * caps proxied request bodies at 10 MiB and truncates anything larger,
 * which corrupts casebook PDFs (typical 50–500 MB) before they ever reach
 * the backend's streaming uploader.
 *
 * Design:
 * - Default to `http://127.0.0.1:8000` (the spec-mandated bind, §7.6).
 * - Allow override via `NEXT_PUBLIC_LAWSCHOOL_API_BASE` so a future
 *   non-loopback deploy doesn't have to fork this file.
 *
 * Backend CORS in `apps/api/src/main.py` whitelists localhost:3000 and
 * 127.0.0.1:3000 so the browser doesn't block these direct POSTs.
 */
export function uploadEndpoint(path: string): string {
  const base =
    process.env.NEXT_PUBLIC_LAWSCHOOL_API_BASE ?? "http://127.0.0.1:8000";
  const trimmed = base.replace(/\/+$/, "");
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${trimmed}${suffix}`;
}

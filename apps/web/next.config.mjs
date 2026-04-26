/** @type {import('next').NextConfig} */
//
// Two build targets:
//
// 1. Tauri / static export (`pnpm build` for the desktop bundle).
//    Set `NEXT_OUTPUT=export`. Produces `out/` which Tauri ships verbatim
//    inside the .app's Resources. Disables the `/api/*` proxy (no Next dev
//    server is running in the bundled app — every API call goes direct to
//    127.0.0.1:8000 via `lib/api.ts`'s absolute base URL).
//
// 2. Local dev (default — `pnpm dev`).
//    Standard Next dev server with the rewrite proxy still in place for
//    GET-style endpoints. Long-running LLM POSTs already bypass the proxy
//    via the absolute backend URL in `lib/api.ts`.
//
// We keep both paths because (a) `pnpm dev` is faster to iterate on than a
// full Tauri rebuild, and (b) static export disallows server features
// (rewrites, headers, middleware) that `pnpm dev` happily uses.
const isStaticExport = process.env.NEXT_OUTPUT === "export";

const nextConfig = {
  reactStrictMode: true,
  ...(isStaticExport
    ? {
        output: "export",
        // Static export can't generate pages on demand — every dynamic
        // route the user can navigate to needs to be reachable. We use
        // `dynamicParams: true` on each route segment so the runtime route
        // can fetch IDs we couldn't enumerate at build time. Combined with
        // the SPA-style "soft navigation" Next does in production, this
        // gives a real desktop-app feel without a server.
        trailingSlash: false,
        images: { unoptimized: true },
      }
    : {
        async rewrites() {
          return [
            { source: "/api/:path*", destination: "http://127.0.0.1:8000/:path*" },
          ];
        },
      }),
};
export default nextConfig;

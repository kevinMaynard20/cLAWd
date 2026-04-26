/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Proxy `/api/*` to the FastAPI backend bound on 127.0.0.1:8000 (spec §7.6).
  // Note: PDF uploads bypass this rewrite because Next 15's dev server caps
  // proxied request bodies at 10 MiB. The upload page POSTs directly to
  // http://127.0.0.1:8000/uploads/pdf — see `lib/uploadEndpoint.ts` and the
  // backend CORS middleware in apps/api/src/main.py.
  async rewrites() {
    return [
      { source: "/api/:path*", destination: "http://127.0.0.1:8000/:path*" },
    ];
  },
};
export default nextConfig;

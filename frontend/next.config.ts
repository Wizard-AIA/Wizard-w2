import type { NextConfig } from "next";

// The session id lives in localStorage (see lib/api.ts) rather than an
// HttpOnly cookie -- a deliberate call for a local-first app with no backend
// auth system to issue one against. That leaves script-readable state that
// only an XSS bug would ever expose, so the mitigation that doesn't require
// re-architecting session handling is closing off the injection vectors
// instead: no framing (clickjacking into a page that then drives the app),
// no <object>/<embed> plugin content, no MIME-sniffed script execution, no
// stray <base> tag hijacking relative URLs. `script-src` is deliberately left
// unrestricted here -- the App Router injects inline hydration scripts on
// every page, and blocking those without a nonce pipeline would break
// rendering outright.
const SECURITY_HEADERS = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  {
    key: "Content-Security-Policy",
    value: "frame-ancestors 'none'; object-src 'none'; base-uri 'self'",
  },
];

const nextConfig: NextConfig = {
  // Emit a self-contained server with only the files the runtime actually
  // loads, traced from the build. The Docker image copies that instead of the
  // full node_modules, which had been carrying the entire build toolchain --
  // typescript, eslint, tailwind, the compiler -- into production.
  output: "standalone",
  async headers() {
    return [{ source: "/:path*", headers: SECURITY_HEADERS }];
  },
};

export default nextConfig;

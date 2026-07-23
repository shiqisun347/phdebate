function apiConnectSources() {
  const value = process.env.NEXT_PUBLIC_API_ORIGIN;
  if (!value) return ["'self'", "ws:", "wss:"];
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol)) throw new Error('unsupported API protocol');
    const websocketOrigin = `${url.protocol === 'https:' ? 'wss:' : 'ws:'}//${url.host}`;
    return ["'self'", url.origin, websocketOrigin, "ws:", "wss:"];
  } catch {
    throw new Error('NEXT_PUBLIC_API_ORIGIN must be an absolute HTTP(S) origin.');
  }
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  basePath: process.env.NEXT_PUBLIC_BASE_PATH || "",
  output: "standalone",
  poweredByHeader: false,
  async headers() {
    const developmentEval = process.env.NODE_ENV === "production" ? "" : " 'unsafe-eval'";
    const headers = [
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "X-Frame-Options", value: "DENY" },
      { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
      { key: "Permissions-Policy", value: "camera=(), geolocation=(), microphone=(self), payment=(), usb=()" },
      { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
      { key: "Cross-Origin-Resource-Policy", value: "same-origin" },
      {
        key: "Content-Security-Policy",
        value: `default-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'; object-src 'none'; script-src 'self' 'unsafe-inline'${developmentEval}; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' blob:; connect-src ${apiConnectSources().join(' ')}`,
      },
    ];
    if (process.env.NODE_ENV === "production") {
      headers.push({ key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" });
    }
    return [{ source: "/:path*", headers }];
  },
  async rewrites() {
    const api = process.env.API_INTERNAL_ORIGIN || "http://127.0.0.1:8200";
    return [
      { source: "/api/:path*", destination: `${api}/api/:path*` },
      { source: "/media/:path*", destination: `${api}/media/:path*` }
    ];
  }
};

export default nextConfig;

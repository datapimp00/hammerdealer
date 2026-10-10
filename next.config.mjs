/** @type {import('next').NextConfig} */
const securityHeaders = [
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
];

const adminHeaders = [
  { key: 'X-Robots-Tag', value: 'noindex, nofollow' },
  { key: 'X-Frame-Options', value: 'DENY' },
  { key: 'Cache-Control', value: 'no-store, max-age=0, must-revalidate' },
];

const nextConfig = {
  reactStrictMode: true,
  // Serve the single-file static apps from /public as-is.
  async rewrites() {
    return [
      { source: '/admin', destination: '/admin/index.html' },
      { source: '/admin/2fa', destination: '/admin/2fa.html' },
      { source: '/((?!admin/).*)', destination: '/index.html' },
    ];
  },
  async headers() {
    return [
      { source: '/admin/:path*', headers: adminHeaders },
      { source: '/(.*)', headers: securityHeaders },
    ];
  },
};

export default nextConfig;

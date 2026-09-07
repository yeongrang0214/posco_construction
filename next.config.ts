import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  // The deployed site must never call a developer workstation. `lib/api.ts`
  // concatenates this value with `/api/...`, so `.` produces same-origin URLs.
  env: {
    NEXT_PUBLIC_API_BASE_URL: '.',
  },
};

export default nextConfig;

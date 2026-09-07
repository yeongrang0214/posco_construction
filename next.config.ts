import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  // Use the always-on Railway API from every deployed browser session.
  env: {
    NEXT_PUBLIC_API_BASE_URL:
      'https://posco-construction-api-production.up.railway.app',
  },
};

export default nextConfig;

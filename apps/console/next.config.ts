import type { NextConfig } from 'next';

const config: NextConfig = {
  reactStrictMode: true,
  // The console renders nothing it has not read from the API on this
  // request. Approval queues and run traces are the two things that must
  // never be served stale: a reviewer acting on a cached queue decides
  // against a world that has moved. `dynamic: 0` is what does that; Next
  // will not accept a static window below 30s, and the pages that matter
  // here are all dynamic anyway.
  experimental: { staleTimes: { dynamic: 0 } },
};

export default config;

import type { Metadata } from 'next';
import Link from 'next/link';
import type { ReactNode } from 'react';

/*
 * Plex Sans and Plex Mono: a matched pair designed for technical interfaces,
 * with real tabular figures. The mono is not decoration here — every
 * identifier on these screens gets copied into another system, and every
 * column of money has to scan down the decimal point.
 *
 * Self-hosted through npm rather than fetched by `next/font/google`, which
 * reaches fonts.googleapis.com at build time. A build that needs a third
 * party's CDN to be reachable is a build that fails in CI, on a locked-down
 * network, and on a plane — and the failure is in the font layer, which is
 * the last place anyone looks.
 */
import '@fontsource-variable/ibm-plex-sans/standard.css';
import '@fontsource/ibm-plex-mono/400.css';
import '@fontsource/ibm-plex-mono/500.css';
import '@fontsource/ibm-plex-mono/600.css';

import './globals.css';

export const metadata: Metadata = {
  title: 'Concord reviewer',
  description: 'Approve, reject or modify the refunds an agent cannot authorise alone.',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  const tenant = process.env.NEXT_PUBLIC_TENANT ?? 'acme-payments';

  return (
    <html lang="en">
      <body>
        <header className="masthead">
          <span className="masthead__name">Concord</span>
          <nav className="masthead__nav">
            <Link href="/">Approvals</Link>
          </nav>
          <span className="masthead__tenant mono">{tenant}</span>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}

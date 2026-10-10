// app/layout.tsx — minimal root layout so Next builds. All real UI is in /public/*.html.
import type { ReactNode } from 'react';

export const metadata = {
  title: '2HEART',
  description: 'WG & Wohnungen Aggregator with secure admin',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="de">
      <body>{children}</body>
    </html>
  );
}

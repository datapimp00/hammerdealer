import { NextResponse } from 'next/server';

// Health/status endpoint for the 2HEART app.
export const dynamic = 'force-dynamic';

export async function GET() {
  return NextResponse.json({
    ok: true,
    app: '2heart-final-admin-secure-2fa',
    time: new Date().toISOString(),
    layers: ['edge-basic-auth', 'totp-2fa', 'immutable-audit'],
  });
}

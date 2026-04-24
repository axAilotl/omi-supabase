import { Suspense } from 'react';

import AuthCallbackClient from './callback-client';

export const dynamic = 'force-dynamic';

export default function AuthCallbackPage() {
  return (
    <Suspense
      fallback={
        <main className="flex min-h-screen items-center justify-center bg-[#0B0F17] px-6 text-white">
          <div className="max-w-md rounded-2xl border border-white/10 bg-white/5 p-8 text-center">
            <h1 className="mb-3 text-xl font-semibold">Omi Sign In</h1>
            <p className="text-sm text-gray-300">Preparing callback...</p>
          </div>
        </main>
      }
    >
      <AuthCallbackClient />
    </Suspense>
  );
}

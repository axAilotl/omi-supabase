"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";

import { completeAuthCallback } from "@/lib/firebase";

const AUTH_RESULT_KEY = "omi-web-app-auth-result";

export default function AuthCallbackClient() {
  const searchParams = useSearchParams();
  const [message, setMessage] = useState("Completing sign in...");

  useEffect(() => {
    const code = searchParams.get("code");
    const state = searchParams.get("state");
    const error = searchParams.get("error");

    async function finishAuth() {
      if (error) {
        window.localStorage.setItem(
          AUTH_RESULT_KEY,
          JSON.stringify({ error, state }),
        );
        setMessage("Authentication failed.");
      } else if (code && state) {
        try {
          await completeAuthCallback(code, state);
          setMessage("Authentication complete. You can close this window.");
        } catch (callbackError) {
          const callbackMessage =
            callbackError instanceof Error
              ? callbackError.message
              : "Authentication failed";
          window.localStorage.setItem(
            AUTH_RESULT_KEY,
            JSON.stringify({ error: callbackMessage, state }),
          );
          setMessage(callbackMessage);
        }
      } else {
        window.localStorage.setItem(
          AUTH_RESULT_KEY,
          JSON.stringify({ error: "Missing authorization code", state }),
        );
        setMessage("Missing authorization code.");
      }

      if (window.opener) {
        window.close();
      }
    }

    finishAuth();
  }, [searchParams]);

  return (
    <main className="flex min-h-screen items-center justify-center bg-black px-6 text-white">
      <div className="max-w-md rounded-2xl border border-white/10 bg-white/5 p-8 text-center">
        <h1 className="mb-3 text-xl font-semibold">Omi Sign In</h1>
        <p className="text-sm text-gray-300">{message}</p>
      </div>
    </main>
  );
}

'use client';

import { useEffect, useState } from 'react';
import { AuthUser, onAuthStateChange, signInWithGoogle, signOutUser } from '../lib/auth';

interface UseAuthReturn {
  user: AuthUser | null;
  loading: boolean;
  signIn: () => Promise<AuthUser | null>;
  signOut: () => Promise<void>;
  isAuthenticated: boolean;
  authError: string | null;
}

export const useAuth = (): UseAuthReturn => {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState<string | null>(null);

  useEffect(() => {
    console.log('🔧 Setting up auth state listener...');

    let unsubscribe: (() => void) | null = null;

    try {
      unsubscribe = onAuthStateChange((nextUser: AuthUser | null) => {
        setUser(nextUser);
        setLoading(false);
        setAuthError(null); // Clear any previous errors
      });
    } catch (error) {
      const e = error as Error;
      console.error('❌ Auth initialization failed:', e.message);
      setAuthError(e.message);
      setLoading(false);
      // Don't crash the app - just set user to null and continue
      setUser(null);
    }

    return () => {
      console.log('🧹 Cleaning up auth state listener...');
      try {
        if (unsubscribe) {
          unsubscribe();
        }
      } catch (error) {
        console.error('❌ Error cleaning up auth listener:', (error as Error).message);
      }
    };
  }, []);

  const signIn = async (): Promise<AuthUser | null> => {
    try {
      setLoading(true);
      setAuthError(null);
      const user = await signInWithGoogle();
      return user;
    } catch (error) {
      const e = error as Error;
      console.error('❌ Sign in failed:', e);
      setAuthError(e.message || 'Sign in failed');
      return null; // Don't throw - return null instead
    } finally {
      setLoading(false);
    }
  };

  const signOut = async (): Promise<void> => {
    try {
      setLoading(true);
      setAuthError(null);
      await signOutUser();
    } catch (error) {
      const e = error as Error;
      console.error('❌ Sign out failed:', e);
      setAuthError(e.message || 'Sign out failed');
      // Don't throw - just log the error
    } finally {
      setLoading(false);
    }
  };

  return {
    user,
    loading,
    signIn,
    signOut,
    isAuthenticated: !!user,
    authError,
  };
};

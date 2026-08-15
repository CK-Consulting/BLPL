import { useEffect, useState } from "react";
import {
  ClerkLoaded,
  ClerkLoading,
  Show,
  SignIn,
  UserButton,
  useAuth,
} from "@clerk/react";
import { AuthConfig, LockState, OnboardingState, getJSON, setLockedHandler, setTokenGetter } from "../api";
import { Logo } from "./Logo";
import { Onboarding } from "./Onboarding";
import { UnlockScreen } from "./UnlockScreen";

// The front door. Until Clerk says you are signed in, this is the only thing the
// app shows — no project list, no board, nothing.
//
// Two facts have to line up, and they are not the same fact: Clerk can tell the
// browser it holds a valid session while the backend rejects the token, because
// the backend pins the issuer and will refuse a token minted by a different
// Clerk instance. When they disagree the browser's version is the misleading
// one, so the gate reports what the *server* said rather than trusting the SDK.

type Props = { children: React.ReactNode };

export function AuthGate({ children }: Props) {
  const { isSignedIn, getToken } = useAuth();
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [rejected, setRejected] = useState(false);
  // Three states past sign-in, and they are not the same: not set up, set up
  // but locked, and ready. Collapsing any two of them produces a screen that
  // asks for the wrong thing.
  const [state, setState] = useState<{ onboarded: boolean; unlocked: boolean } | null>(null);

  const refreshState = () =>
    Promise.all([
      getJSON<OnboardingState>("/api/onboarding"),
      getJSON<LockState>("/api/auth/lock-state"),
    ])
      .then(([o, l]) => setState({ onboarded: o.complete, unlocked: l.unlocked }))
      .catch(() => setState(null));

  // Hand the API layer Clerk's token source once. Not cached beyond this:
  // getToken() refreshes short-lived tokens transparently, and holding one
  // would work until it silently expired mid-session.
  useEffect(() => {
    setTokenGetter(() => getToken());
    setLockedHandler(() => setRejected(true));
  }, [getToken]);

  // Asked unauthenticated, so a server with no Clerk issuer can say so instead
  // of showing a sign-in that cannot possibly succeed.
  useEffect(() => {
    getJSON<AuthConfig>("/api/auth/config")
      .then(setConfig)
      .catch(() => setConfig({ clerk_configured: false }));
  }, []);

  // Signing in again clears a stale rejection; without this a single expired
  // token would pin the error on screen for the rest of the session.
  useEffect(() => {
    if (isSignedIn) {
      setRejected(false);
      refreshState();
    }
  }, [isSignedIn]);

  if (config && !config.clerk_configured) {
    return (
      <div className="gate">
        <div className="gate-card">
          <Logo size={48} withText />
          <p className="gate-sub">
            This server has no Clerk issuer configured, so nobody can sign in yet. Set{" "}
            <code>BLPL_CLERK_ISSUER</code> on the backend and restart it.
          </p>
          <div className="gate-hint">
            Signing in from another browser will not help — this is a server setting.
          </div>
        </div>
      </div>
    );
  }

  return (
    <>
      <ClerkLoading>
        <div className="gate">Checking…</div>
      </ClerkLoading>
      <ClerkLoaded>
        <Show when="signed-out">
          <div className="gate gate-column">
            <Logo size={64} withText />
            <SignIn />
          </div>
        </Show>
        <Show when="signed-in">
          {rejected ? (
            <div className="gate">
              <div className="gate-card">
                <Logo size={48} withText />
                <p className="gate-sub">
                  You are signed in to Clerk, but this server rejected the session. That
                  usually means it is configured for a different Clerk instance than the one
                  this page signed in to.
                </p>
                <div className="gate-error">
                  Check that <code>BLPL_CLERK_ISSUER</code> matches the publishable key the
                  frontend was built with.
                </div>
              </div>
            </div>
          ) : state === null ? (
            <div className="gate">Checking…</div>
          ) : !state.onboarded ? (
            <Onboarding onDone={refreshState} />
          ) : !state.unlocked ? (
            <UnlockScreen onUnlocked={refreshState} />
          ) : (
            children
          )}
        </Show>
      </ClerkLoaded>
    </>
  );
}

/** The signed-in user's control, for the app header. Where sign-out lives, so
 *  the app does not reimplement it. afterSignOutUrl is set once on
 *  ClerkProvider in main.tsx rather than per component. */
export function UserControl() {
  return <UserButton />;
}

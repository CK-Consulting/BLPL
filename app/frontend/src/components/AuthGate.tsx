import { useEffect, useRef, useState } from "react";
import {
  ClerkLoaded,
  ClerkLoading,
  Show,
  SignIn,
  UserButton,
  useAuth,
} from "@clerk/react";
import {
  AuthConfig,
  LockState,
  OnboardingState,
  getJSON,
  setLockedHandler,
  setTokenGetter,
  setUnlockNeededHandler,
} from "../api";
import { Logo } from "./Logo";
import { InviteLanding } from "./InviteLanding";
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

/** /invite/123 → 123. Read once: the URL is rewritten to drop the secret from
 *  the address bar, so re-parsing later would find nothing. */
function invitationIdFromPath(): number | null {
  const m = /^\/invite\/(\d+)/.exec(window.location.pathname);
  return m ? Number(m[1]) : null;
}

export function AuthGate({ children }: Props) {
  const { isSignedIn, getToken } = useAuth();
  const [inviteId] = useState<number | null>(invitationIdFromPath);
  // Three answers, not two. "Clerk is not configured" is a claim about the
  // server's settings; "the server did not answer" is a claim about the server
  // being up. Reporting the second as the first sends whoever is on call to
  // check an environment variable that was correct all along — which is exactly
  // what it did, while the backend was in a crash loop behind it.
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  // Whether the gate has ever opened. Once it has, this screen must never come
  // back: it renders instead of `children`, so showing it would unmount the
  // whole workspace — and the editor keeps unsaved content in component state,
  // which would be gone on remount with no prompt, because nothing the user did
  // caused the unmount.
  const opened = useRef(false);
  const [rejected, setRejected] = useState(false);
  // Three states past sign-in, and they are not the same: not set up, set up
  // but locked, and ready. Collapsing any two of them produces a screen that
  // asks for the wrong thing.
  const [state, setState] = useState<{ onboarded: boolean; unlocked: boolean } | null>(null);
  // Locked *again*, after the gate had already opened — which is a different
  // situation from being locked on arrival and needs a different screen.
  //
  // The server keeps the derived key in memory only, so every restart relocks
  // every session while the workspace is still on screen with unsaved edits in
  // it. Flipping `state.unlocked` back to false would render UnlockScreen
  // instead of `children`, unmounting the whole workspace and taking the
  // editor's buffer with it — for a reason the user did not cause and cannot
  // see coming. So this is tracked separately and drawn on top instead.
  const [relocked, setRelocked] = useState(false);

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
    setUnlockNeededHandler(() => setRelocked(true));
  }, [getToken]);

  // Asked unauthenticated, so a server with no Clerk issuer can say so instead
  // of showing a sign-in that cannot possibly succeed.
  useEffect(() => {
    let cancelled = false;
    let timer = 0;
    const ask = () =>
      getJSON<AuthConfig>("/api/auth/config")
        .then((c) => {
          if (cancelled) return;
          opened.current = true;
          setConfig(c);
          setUnreachable(false);
          // Stop. This probe exists to get past the gate, not to monitor the
          // server for the rest of the session — a background poll that can
          // replace a running application is a worse failure than the blank
          // error page it was added to avoid.
          window.clearInterval(timer);
        })
        .catch(() => {
          if (!cancelled && !opened.current) setUnreachable(true);
        });
    ask();
    // A backend that is still starting comes back on its own, so keep asking
    // rather than stranding whoever is watching on an error that never clears.
    timer = window.setInterval(ask, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  // Signing in again clears a stale rejection; without this a single expired
  // token would pin the error on screen for the rest of the session.
  useEffect(() => {
    if (isSignedIn) {
      setRejected(false);
      refreshState();
    }
  }, [isSignedIn]);

  // Guarded on `opened` as well as the flag: two independent reasons this can
  // never displace a mounted workspace is the right number for a screen that
  // returns instead of the application.
  if (unreachable && !opened.current) {
    return (
      <div className="gate">
        <div className="gate-card">
          <Logo size={48} withText />
          <p className="gate-sub">
            The server is not answering. This is not a sign-in problem and not a
            setting you can change here — the backend is down, still starting, or
            failing to start.
          </p>
          <div className="gate-hint">
            <code>docker compose logs backend</code> will say why. This page retries
            on its own and will continue as soon as the server is back.
          </div>
        </div>
      </div>
    );
  }

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
            {inviteId !== null ? (
              // The invitation is shown *above* the sign-in, so someone
              // following a link knows what they are signing up for before they
              // are asked for an address that has to match.
              <InviteLanding
                invitationId={inviteId}
                signedIn={false}
                onboarded={false}
                onJoined={() => {}}
              />
            ) : (
              <Logo size={64} withText />
            )}
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
          ) : inviteId !== null ? (
            // Last, not first: redeeming needs an unlocked session, because the
            // project key is re-sealed to this account's own key as it happens.
            <InviteLanding
              invitationId={inviteId}
              signedIn
              onboarded
              onJoined={() => {
                window.history.replaceState(null, "", "/");
                window.location.reload();
              }}
            />
          ) : (
            <>
              {children}
              {relocked && (
                // Over the workspace, not instead of it. Everything underneath
                // stays mounted, so an unsaved edit is still there when the key
                // is back — the point of not reusing the `state.unlocked` path.
                <div className="gate-overlay" role="dialog" aria-modal="true">
                  <UnlockScreen
                    onUnlocked={() => {
                      setRelocked(false);
                      void refreshState();
                    }}
                  />
                </div>
              )}
            </>
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

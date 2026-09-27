import { useEffect, useState } from "react";
import { postJSON } from "../api";
import { ExternalLink } from "./Icons";

/**
 * Open the KiCad running in the compose stack, in a new tab.
 *
 * At `/kicad/` on this origin, not a port of its own. A port would be the
 * obvious thing and does not survive contact with how this gets deployed:
 * Cloudflare proxies standard ports only, so a tunnel that maps this hostname
 * cannot reach `host:3010` at all, and a second hostname would be a second
 * thing to protect.
 *
 * Being at a path is also what lets it be protected. KasmVNC has no
 * authentication of its own — it answers 200 and hands over a desktop with the
 * project directory mounted — so nginx gates the path with an auth_request
 * against this app's session. That works because Clerk's cookie rides a
 * same-origin navigation, where a Bearer token held in JavaScript would not.
 *
 * A new tab rather than an iframe: it is a VNC desktop with its own keyboard,
 * and Ctrl-S inside a workbench that also binds Ctrl-S is a way to lose work.
 */
const KICAD_PATH = "/kicad/";

export function useKicadDesktop(): string | null {
  const [ok, setOk] = useState(false);
  useEffect(() => {
    let gone = false;
    // Probed rather than assumed: the desktop is one container in a compose
    // stack somebody may not be running, and a button that leads to a 502 is
    // worse than no button.
    // GET, not HEAD. nginx issues its auth sub-request with the original
    // method, so a HEAD probe asked a GET-only gate and got 405 — read as a
    // denial, leaving the button hidden while the desktop was running. The gate
    // answers both now; the probe stays on the method that cannot surprise it.
    //
    // redirect: "manual", because the default hides the answer. A denial is a
    // 302 to the workbench, fetch follows it, and the 200 that comes back from
    // the app root sets r.ok — so the button appeared whenever the app was up,
    // whatever the gate said, and clicking it opened a new tab that bounced
    // straight back to BLPL. Which reads, from the outside, exactly like KiCad
    // failing to load. Manual turns that 302 into an opaque redirect: status 0,
    // ok false, which is the honest reading of "you may not open this".
    fetch(KICAD_PATH, { method: "GET", credentials: "same-origin", redirect: "manual" })
      .then((r) => {
        if (!gone) setOk(r.ok);
      })
      .catch(() => {
        /* not running; the button simply does not appear */
      });
    return () => {
      gone = true;
    };
  }, []);
  return ok ? KICAD_PATH : null;
}

/**
 * The control that opens KiCad, carrying the version it will open.
 *
 * The version used to sit beside this as its own static label — "KiCad 10.0.6"
 * in the middle of the navbar, between two unrelated links, saying nothing
 * anyone could act on. It is not a fact about the app; it is a fact about
 * *what this button opens*, so it belongs on the button. That also removes one
 * item from a bar that had too many.
 */
export function LaunchKicad({
  className = "",
  version = null,
  projectId = null,
  board = null,
}: {
  className?: string;
  version?: string | null;
  projectId?: string | null;
  board?: string | null;
}) {
  const url = useKicadDesktop();
  // Why the desktop could not be pointed at this board, when it could not be.
  // Swallowing that left a member of a shared project staring at an empty
  // KiCad with nothing anywhere saying why — which is the exact complaint this
  // whole change exists to answer, reintroduced one level down.
  const [note, setNote] = useState<string | null>(null);
  if (!url) return null;

  // Ask the desktop to open this board as the tab is opening. The desktop
  // autostarts a bare `kicad`, so without this it comes up with nothing loaded
  // and every file mounted but unopened — which reads as an empty install.
  //
  // Fired alongside the navigation rather than before it: the browser only
  // allows a new tab from a real click, so awaiting a round trip first would
  // get the tab blocked. The watcher polls, so arriving a moment later is
  // fine, and a failure here must not stop the desktop opening — the person
  // can still open the project by hand, which is strictly better than a dead
  // link.
  const ask = () => {
    if (!projectId) return;
    setNote(null);
    const q = board ? `?board=${encodeURIComponent(board)}` : "";
    postJSON<{ opened: boolean; reason?: string }>(
      `/api/projects/${encodeURIComponent(projectId)}/kicad/open${q}`,
      {},
    )
      .then((r) => {
        if (!r.opened && r.reason) setNote(r.reason);
      })
      .catch(() => {
        /* the desktop still opens; the project can be opened by hand */
      });
  };

  return (
    <>
    <a
      className={className || "link"}
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      onClick={ask}
      title={
        version
          ? `KiCad ${version}, running server-side in the container — for drawing a footprint or a symbol the libraries do not have`
          : "Open the KiCad desktop in a new tab — for drawing a footprint or a symbol the libraries do not have"
      }
    >
      <ExternalLink />
      <span className="kicad-launch-label">Open KiCad in New Tab</span>
      {version && <span className="kicad-ver">{version}</span>}
    </a>
    {note && (
      <span className="muted small kicad-open-note" role="status">
        Opened the desktop, but not this board: {note}
      </span>
    )}
    </>
  );
}

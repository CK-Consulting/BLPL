import { useEffect, useMemo, useState } from "react";
import { Dashboard as DashboardData, getJSON, postJSON } from "../api";
import { Logo } from "./Logo";
import { NewProjectButton } from "./ProjectControls";
import { stamp } from "../time";

/**
 * Where you land after unlocking.
 *
 * Previously the app opened straight into a project, which is fine with one and
 * wrong with several: it either guesses (and guesses wrong), or it reopens
 * whatever was last open — which quietly puts someone else's shared design on
 * screen because you happened to look at it on Friday. Landing on a list makes
 * opening a project a choice, every time.
 */

type Sort = "mine" | "recent" | "az" | "za";

const SORTS: { id: Sort; label: string; hint: string }[] = [
  { id: "mine", label: "I worked on", hint: "Where you left off — your own activity only" },
  { id: "recent", label: "Recently changed", hint: "What moved while you were away, by anyone" },
  { id: "az", label: "A–Z", hint: "By name" },
  { id: "za", label: "Z–A", hint: "By name, reversed" },
];

export function Dashboard({ onOpen }: { onOpen: (projectId: string) => void }) {
  const [data, setData] = useState<DashboardData | null>(null);
  const [sort, setSort] = useState<Sort>("mine");
  const [busy, setBusy] = useState<number | null>(null);

  const refresh = () => getJSON<DashboardData>("/api/dashboard").then(setData).catch(() => {});

  useEffect(() => {
    refresh();
  }, []);

  const respond = async (id: number, verb: "accept" | "decline") => {
    setBusy(id);
    try {
      await postJSON(`/api/invitations/${id}/${verb}`);
      await refresh();
    } finally {
      setBusy(null);
    }
  };

  const projects = useMemo(() => {
    if (!data) return [];
    const list = [...data.projects];
    // Nulls last in both time sorts: a project you have never touched is not
    // "infinitely long ago", it is unranked, and floating it to the top would
    // make the sort useless the first time you add anything.
    const byTime = (key: "last_touched_by_me" | "last_activity") => (a: any, b: any) => {
      const x = a[key],
        y = b[key];
      if (!x && !y) return a.id.localeCompare(b.id);
      if (!x) return 1;
      if (!y) return -1;
      return y.localeCompare(x);
    };
    if (sort === "mine") list.sort(byTime("last_touched_by_me"));
    else if (sort === "recent") list.sort(byTime("last_activity"));
    else if (sort === "az") list.sort((a, b) => a.id.localeCompare(b.id));
    else list.sort((a, b) => b.id.localeCompare(a.id));
    return list;
  }, [data, sort]);

  if (!data) return <div className="gate">Loading…</div>;

  return (
    <div className="dashboard">
      <header className="dash-head">
        <Logo size={30} withText />
        <span className="spacer" />
        {/* The only way into the workspace is opening a project, so creating
            one has to live here too. It used to sit solely in the workspace
            header, which a fresh account can never reach: no projects, no
            cards, no way in. */}
        <NewProjectButton onCreated={onOpen} />
        <div className="seg small">
          {SORTS.map((s) => (
            <button
              key={s.id}
              title={s.hint}
              className={sort === s.id ? "on" : ""}
              onClick={() => setSort(s.id)}
            >
              {s.label}
            </button>
          ))}
        </div>
      </header>

      {/* Burnt orange, and used nowhere else in the app. The banner alone was
          easy to look past: people read the middle of the screen, not the top
          strip, so the same invitation appears here too. */}
      {data.invitations.length > 0 && (
        <section className="notice-block">
          <h2>Waiting for you</h2>
          {data.invitations.map((inv) => (
            <div className="notice" key={inv.id}>
              <div>
                <strong className="mono">{inv.project}</strong>
                <div className="notice-sub">
                  shared by {inv.invited_by || "another user"} · expires{" "}
                  {stamp(inv.expires_at)}
                </div>
              </div>
              <span className="spacer" />
              <button disabled={busy === inv.id} onClick={() => respond(inv.id, "accept")}>
                Accept
              </button>
              <button
                className="link"
                disabled={busy === inv.id}
                onClick={() => respond(inv.id, "decline")}
              >
                Decline
              </button>
            </div>
          ))}
        </section>
      )}

      <div className="dash-body">
        <section className="dash-projects">
          <h2>Projects</h2>
          {projects.length === 0 ? (
            <div className="dash-empty">
              <p className="muted">
                Nothing here yet. Create a project or import one to get started.
              </p>
              {/* Same control as the header. People read the middle of the
                  screen, not the top strip, and this is the one screen where
                  the reader has nothing else to click. */}
              <NewProjectButton onCreated={onOpen} />
            </div>
          ) : (
            <ul className="project-cards">
              {projects.map((p) => (
                <li key={p.id}>
                  <button className="project-card" onClick={() => onOpen(p.id)}>
                    <div className="project-card-top">
                      <strong className="mono">{p.id}</strong>
                      {!p.owned && <span className="chip">shared with you</span>}
                      {p.sealed && <span className="chip chip-sealed">🔒 encrypted</span>}
                      {p.fab?.blocked && <span className="badge fail">fab blocked</span>}
                    </div>
                    {/* A sealed project's contents cannot be counted without
                        decrypting it, so the card says what it knows instead of
                        printing zeroes that would read as an empty project.
                        Opening is still one click: the card decrypts on the way
                        in. */}
                    <div className="project-card-meta muted small">
                      {p.sealed ? (
                        "encrypted at rest · opens when you do"
                      ) : (
                        <>
                          {p.markdown_files} md
                          {p.has_pcb ? " · board" : ""}
                          {p.has_schematic ? " · schematic" : ""}
                          {p.members > 1 ? ` · ${p.members} people` : ""}
                        </>
                      )}
                    </div>
                    <div className="project-card-when muted small">
                      {p.last_touched_by_me
                        ? `you: ${stamp(p.last_touched_by_me)}`
                        : "you have not opened this yet"}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="dash-activity">
          <h2>Recent activity</h2>
          {data.activity.length === 0 ? (
            <p className="muted">
              Nothing recorded yet. Activity appears here as you and anyone you share with work.
            </p>
          ) : (
            <ul className="activity-list">
              {data.activity.map((a) => (
                <li key={a.id} className={a.kind === "shared" || a.kind === "joined" ? "notable" : ""}>
                  <span className="mono">{a.project}</span>{" "}
                  <span className="muted">
                    {a.who ? `${a.who} ` : ""}
                    {a.verb}
                    {a.detail ? ` ${a.detail}` : ""}
                  </span>
                  <div className="activity-when">{stamp(a.at)}</div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}

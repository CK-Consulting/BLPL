import { useEffect, useState } from "react";
import { getJSON } from "../api";

/**
 * The reusable blocks available to this project.
 *
 * A module's *interface* is what you need to know before deciding to use one —
 * the ports are the contract a carrier board has to satisfy, and everything
 * else (how many parts, which board it came from) is context. So the ports are
 * what this shows first, rather than making them a detail you have to open.
 */

type Module = {
  name: string;
  path: string;
  root: string;
  scope: "project" | "shared";
  description: string;
  ports: string[];
  components: number;
  source_board: string;
};

export function ModuleLibrary({
  projectId,
  reloadToken,
}: {
  projectId: string;
  reloadToken: number;
}) {
  const [modules, setModules] = useState<Module[] | null>(null);
  const [sharedRoot, setSharedRoot] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    getJSON<{ modules: Module[]; shared_root: string }>(`/api/projects/${projectId}/modules`)
      .then((r) => {
        setModules(r.modules);
        setSharedRoot(r.shared_root);
        setError("");
      })
      .catch((e) => setError(String(e)));
  }, [projectId, reloadToken]);

  if (error) return <div className="pad error">{error}</div>;
  if (modules === null) return <div className="pad muted">Loading modules…</div>;

  if (modules.length === 0)
    return (
      <div className="pad muted">
        <p>No modules yet.</p>
        <p>
          A module is a proven block lifted off a finished board — a charger section, an RF
          front end — that a later design can name instead of redrawing. Ask the assistant to{" "}
          <em>read the KiCad design</em> and then <em>plan a module extraction</em> for the
          parts you want to keep.
        </p>
        <p className="small">
          Shared modules are read from <code className="mono">{sharedRoot}</code>.
        </p>
      </div>
    );

  return (
    <div className="modules">
      {modules.map((m) => (
        <section key={m.name} className="module-card">
          <header>
            <span className="mono strong">{m.name}</span>
            <span className={`badge ${m.scope}`}>{m.scope}</span>
            <span className="muted small">
              {m.components} part{m.components === 1 ? "" : "s"}
              {m.source_board ? ` · from ${m.source_board}` : ""}
            </span>
          </header>
          {m.description && <p className="muted">{m.description}</p>}
          <div className="ports">
            {m.ports.length === 0 ? (
              <span className="muted small">
                No ports — nothing crosses this module's boundary, which usually means it was
                extracted from the wrong selection.
              </span>
            ) : (
              m.ports.map((p) => (
                <span key={p} className="port mono">
                  {p}
                </span>
              ))
            )}
          </div>
          <div className="muted small mono">{m.path}</div>
        </section>
      ))}
    </div>
  );
}

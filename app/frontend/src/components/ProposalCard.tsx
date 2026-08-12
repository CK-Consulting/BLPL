import { useEffect, useMemo, useRef, useState } from "react";
import { Proposal, getJSON, postJSON } from "../api";

/**
 * One proposed file edit, as a diff you accept or reject.
 *
 * The assistant cannot write project files; it proposes, and this is where a
 * human rules on it. Showing the diff rather than the new file is the whole
 * point — "here are 400 lines, trust me" is not review.
 */

type Line = { kind: "ctx" | "add" | "del"; text: string };

/**
 * Line diff via longest-common-subsequence.
 *
 * Design documents are small (hundreds of lines), so the quadratic table is
 * free at this size and the result is a real minimal diff rather than the
 * "everything changed" blocks a naive prefix/suffix trim produces once a
 * paragraph moves.
 */
function diffLines(before: string, after: string): Line[] {
  const a = before.length ? before.split("\n") : [];
  const b = after.length ? after.split("\n") : [];
  const n = a.length;
  const m = b.length;

  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }

  const out: Line[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ kind: "ctx", text: a[i] });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      out.push({ kind: "del", text: a[i++] });
    } else {
      out.push({ kind: "add", text: b[j++] });
    }
  }
  while (i < n) out.push({ kind: "del", text: a[i++] });
  while (j < m) out.push({ kind: "add", text: b[j++] });
  return out;
}

// Long runs of unchanged text are noise in a review. Keep a few lines of
// context around each change and collapse the rest.
const CONTEXT = 3;

function collapse(lines: Line[]): (Line | { kind: "gap"; count: number })[] {
  const keep = new Array(lines.length).fill(false);
  lines.forEach((l, idx) => {
    if (l.kind === "ctx") return;
    for (let k = Math.max(0, idx - CONTEXT); k <= Math.min(lines.length - 1, idx + CONTEXT); k++) {
      keep[k] = true;
    }
  });
  const out: (Line | { kind: "gap"; count: number })[] = [];
  let run = 0;
  lines.forEach((l, idx) => {
    if (keep[idx]) {
      if (run > 0) {
        out.push({ kind: "gap", count: run });
        run = 0;
      }
      out.push(l);
    } else {
      run++;
    }
  });
  if (run > 0) out.push({ kind: "gap", count: run });
  return out;
}

type Props = {
  projectId: string;
  proposal: Proposal;
  onDecided: (id: string, status: string) => void;
};

export function ProposalCard({ projectId, proposal, onDecided }: Props) {
  const [before, setBefore] = useState<string | null>(proposal.creates_file ? "" : null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState(proposal.status);
  const cardRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (proposal.creates_file) return;
    getJSON<{ content: string }>(`/api/projects/${projectId}/files/${proposal.path}`)
      .then((f) => setBefore(f.content))
      .catch(() => setBefore("")); // gone or unreadable — show it as an addition
  }, [projectId, proposal.path, proposal.creates_file]);

  // The card grows when the diff arrives, which is after the transcript has
  // already scrolled itself. Pull the decision into view rather than leaving it
  // below the fold with nothing to say it is there.
  useEffect(() => {
    if (before === null) return;
    cardRef.current?.scrollIntoView({ block: "nearest" });
  }, [before]);

  const lines = useMemo(
    () => (before === null ? [] : collapse(diffLines(before, proposal.new_content))),
    [before, proposal.new_content],
  );

  const decide = async (action: "accept" | "reject") => {
    setBusy(true);
    setError(null);
    try {
      await postJSON(`/api/projects/${projectId}/proposals/${proposal.id}`, { action });
      setStatus(action === "accept" ? "accepted" : "rejected");
      onDecided(proposal.id, action === "accept" ? "accepted" : "rejected");
    } catch (e) {
      // The common one is a stale proposal — the file changed under it. That is
      // the safety rule working, so the message says what to do next.
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const added = lines.filter((l) => l.kind === "add").length;
  const removed = lines.filter((l) => l.kind === "del").length;

  return (
    <div className={`proposal ${status}`} ref={cardRef}>
      <div className="proposal-head">
        <span className="mono">{proposal.path}</span>
        {proposal.creates_file && <span className="status-tag added">new file</span>}
        <span className="add">+{added}</span>
        <span className="del">−{removed}</span>
        <span className="spacer" />
        {status === "pending" ? (
          <>
            <button className="link" disabled={busy} onClick={() => decide("accept")}>
              Accept
            </button>
            <button className="link" disabled={busy} onClick={() => decide("reject")}>
              Reject
            </button>
          </>
        ) : (
          <span className={`badge ${status === "accepted" ? "ok" : ""}`}>{status}</span>
        )}
      </div>
      {proposal.rationale && <div className="proposal-why">{proposal.rationale}</div>}
      {before === null ? (
        <div className="muted pad">Loading current file…</div>
      ) : (
        <pre className="diff-body">
          {lines.map((l, idx) =>
            l.kind === "gap" ? (
              <div key={idx} className="dl meta">
                ⋯ {l.count} unchanged {l.count === 1 ? "line" : "lines"}
              </div>
            ) : (
              <div
                key={idx}
                className={`dl ${l.kind === "add" ? "add-line" : l.kind === "del" ? "del-line" : "ctx"}`}
              >
                {l.kind === "add" ? "+" : l.kind === "del" ? "-" : " "}
                {l.text}
              </div>
            ),
          )}
        </pre>
      )}
      {error && <div className="gate-error">{error}</div>}
    </div>
  );
}

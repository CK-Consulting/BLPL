/**
 * Controlled impedance, as fields: each net class's impedance and the geometry
 * behind it, and the `controlled_impedance` tags that say which nets need it.
 *
 * Until this, both lived only in project.yaml, and the one mistake the
 * feature exists to catch — a class that claims 50 ohms at the Default width —
 * was only reported by Stage 8, after a full build. Here the same rules are
 * said beside the class while it is being edited, the last Stage 8 run's
 * ZC findings are shown where they belong, and anything the server refuses on
 * save lands on the row it is about rather than in a list at the bottom.
 *
 * None of this is a second validator. Saves go through the PUT, which runs
 * Stage 5's own checks (`impedance.tags` included); what is computed here only
 * predicts what Stage 5 and Stage 8 will say, so the user hears it sooner.
 */

import { useState } from "react";

import type { Impedance, ImpedanceMode, ImpedanceTag, NetClass } from "../api";

export type ZcFinding = {
  rule_id?: string;
  severity?: string;
  summary: string;
  recommendation?: string;
  nets?: string[];
};

type Spec = {
  ohms?: number;
  mode?: ImpedanceMode;
  tolerance_pct?: number;
  /** Copper layers the class's width is for. */
  layers?: string[];
  /** Plane layers the width was calculated against. */
  reference?: string[];
};

const MODES: ImpedanceMode[] = ["single_ended", "differential", "coplanar"];
const MODE_LABELS: Record<ImpedanceMode, string> = {
  single_ended: "Single-ended",
  differential: "Differential",
  coplanar: "Coplanar",
};
// core/blpl/core/impedance.py: _MODE_TAG, and the defaults a spec falls back to.
const MODE_TAG: Record<ImpedanceMode, string> = { single_ended: "SE", differential: "DIFF", coplanar: "CPW" };
const DEFAULT_OHMS = 50;
const DEFAULT_TOLERANCE = 10;
// pro.build()'s clearance for a class that declares none, which is what a
// coplanar gap is measured against (impedance.py: _DEFAULT_CLEARANCE_MM).
const DEFAULT_CLEARANCE = 0.2;

/** The copper layers of a board with `count` of them, in KiCad's names. */
export function copperLayers(count: number): string[] {
  if (count <= 1) return ["F.Cu"];
  const inner = Array.from({ length: count - 2 }, (_, i) => `In${i + 1}.Cu`);
  return ["F.Cu", ...inner, "B.Cu"];
}

const isMap = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);

/** A class's `impedance` as a spec, or null when the class is not controlled.
 *  Anything malformed reads as a mapping of nothing, so the form still opens
 *  and the server's error says what is wrong with it. */
export function readImpedance(raw: unknown): Spec | null {
  if (raw === undefined || raw === null || raw === false) return null;
  if (raw === true) return {};
  if (typeof raw === "number") return { ohms: raw };
  if (isMap(raw)) return raw as Spec;
  return {};
}

/** The most compact spelling of a spec — what someone would write by hand,
 *  so a file the form touched still reads like one a person wrote. */
export function writeImpedance(spec: Spec): Impedance {
  const kept: Spec = {};
  if (spec.ohms !== undefined) kept.ohms = spec.ohms;
  if (spec.mode !== undefined && spec.mode !== "single_ended") kept.mode = spec.mode;
  if (spec.tolerance_pct !== undefined) kept.tolerance_pct = spec.tolerance_pct;
  // The schema wants at least one layer when either list is given at all.
  if (Array.isArray(spec.layers) && spec.layers.length) kept.layers = spec.layers;
  if (Array.isArray(spec.reference) && spec.reference.length) kept.reference = spec.reference;
  const keys = Object.keys(kept);
  if (!keys.length) return true;
  if (keys.length === 1 && kept.ohms !== undefined) return kept.ohms;
  return kept;
}

const ohmsOf = (s: Spec) => s.ohms ?? DEFAULT_OHMS;
const modeOf = (s: Spec): ImpedanceMode => s.mode ?? "single_ended";
const sameTarget = (a: Spec, b: Spec) => ohmsOf(a) === ohmsOf(b) && modeOf(a) === modeOf(b);
/** impedance.Spec.meets: whether a class builds what a tag asks for. A tag
 *  that states a tolerance needs a class at least that tight — a ±10% class
 *  does not meet ±5%, and Stage 5 makes another class instead. */
const meets = (cls: Spec, tag: Spec) =>
  sameTarget(cls, tag) &&
  (tag.tolerance_pct === undefined || (cls.tolerance_pct ?? DEFAULT_TOLERANCE) <= tag.tolerance_pct + 1e-9);
const label = (s: Spec) =>
  `${ohmsOf(s)} Ω ${modeOf(s).replace("_", "-")} ±${s.tolerance_pct ?? DEFAULT_TOLERANCE}%`;

/** The class Stage 5 makes for a target no declared class has. */
export function synthesizedName(s: Spec, taken: string[] = []): string {
  // impedance.synthesized_name: a tolerance other than the default is in the
  // name, and a name already declared gets a suffix.
  let base = `Z${ohmsOf(s)}_${MODE_TAG[modeOf(s)]}`;
  const tol = s.tolerance_pct ?? DEFAULT_TOLERANCE;
  if (Math.abs(tol - DEFAULT_TOLERANCE) > 1e-9) base += `_T${tol}`;
  base = base.replaceAll(".", "p");
  let name = base;
  for (let n = 2; taken.includes(name); n++) name = `${base}_${n}`;
  return name;
}

/** Which net class a Stage 8 impedance finding is about. The report carries
 *  nets, not the class, so it is read from the summary, whose wording
 *  (core/blpl/core/impedance.py: review) names it in every rule but ZC-003. */
export function findingClass(f: ZcFinding): string | null {
  const m = /\b[Cc]lass ([^\s,]+)/.exec(f.summary ?? "");
  return m ? m[1] : null;
}

/** Server errors, by the row they are about. Stage 5's messages start with the
 *  path either as the schema spells it (`controlled_impedance.0.nets: ...`)
 *  or as impedance.py does (`controlled_impedance[0].class ...`). */
export function routeErrors(errors: string[], classNames: string[], tagCount: number) {
  const classes: Record<string, string[]> = {};
  const tags: Record<number, string[]> = {};
  const rest: string[] = [];
  // Matched against the names that have a row, longest first: a class may be
  // called USB.HS, and a pattern that stops at the dot filed its error under
  // a USB row that does not exist, where nobody saw it (Codex, PR #34).
  const known = [...classNames].sort((a, b) => b.length - a.length);
  for (const e of errors) {
    const t = /^controlled_impedance(?:\.|\[)(\d+)/.exec(e);
    const c = e.startsWith("net_classes.")
      ? known.find((n) => {
          const after = e.charAt("net_classes.".length + n.length);
          return e.startsWith(`net_classes.${n}`) && (after === "" || after === "." || after === ":" || after === " ");
        })
      : undefined;
    if (t && Number(t[1]) < tagCount) (tags[Number(t[1])] ??= []).push(e);
    else if (c !== undefined) (classes[c] ??= []).push(e);
    else rest.push(e);
  }
  return { classes, tags, rest };
}

const num = (v: string): number | undefined => (v === "" ? undefined : Number(v));

/** What each number means, shown on hover. Widths and gaps come from the
 *  fab's impedance calculator for the stackup being ordered, never a guess. */
const NUM_HINTS: Record<string, string> = {
  "Trace width": "Track width in mm. For a controlled class, from the fab's impedance calculator for this stackup.",
  Clearance: "Minimum gap in mm to copper of other nets",
  "Pair width": "Width in mm of each track of a differential pair",
  "Pair gap": "Gap in mm between the two tracks of a pair; no more than the clearance",
  "Coplanar gap": "Gap in mm to the ground pour beside the track, which sets its impedance with the width",
  Ohms: "Target impedance in ohms",
  "Tolerance %": "How far the fab may miss the target, in percent",
};

function NumField({
  label: text,
  aria,
  value,
  placeholder,
  step = "0.01",
  readOnly,
  onChange,
}: {
  label: string;
  aria: string;
  value: unknown;
  placeholder?: string;
  step?: string;
  readOnly?: boolean;
  onChange: (v: number | undefined) => void;
}) {
  return (
    <label title={NUM_HINTS[text]}>
      <span>{text}</span>
      <input
        type="number"
        step={step}
        min="0"
        aria-label={aria}
        placeholder={placeholder}
        readOnly={readOnly}
        value={typeof value === "number" ? value : ""}
        onChange={(e) => onChange(num(e.target.value))}
      />
    </label>
  );
}

/** A set of copper layers, as checkboxes over the board's own. A layer the
 *  file names that the stackup no longer has (the board went from six layers
 *  to four) stays listed and ticked, so it can be seen and unticked rather
 *  than kept invisibly. */
function LayerPicks({
  aria,
  title,
  board,
  value,
  onChange,
}: {
  aria: string;
  title: string;
  board: string[];
  value: string[];
  onChange: (v: string[]) => void;
}) {
  const all = [...board, ...value.filter((l) => !board.includes(l))];
  return (
    <span className="board-forbid" role="group" aria-label={aria}>
      <span className="muted small">{title}</span>
      {all.map((l) => (
        <label key={l} className="inline">
          <input
            type="checkbox"
            checked={value.includes(l)}
            onChange={(e) =>
              onChange(e.target.checked ? all.filter((x) => x === l || value.includes(x)) : value.filter((x) => x !== l))
            }
          />
          {l}
        </label>
      ))}
    </span>
  );
}

function Problems({ list, role = "status" }: { list: string[]; role?: string }) {
  if (!list.length) return null;
  return (
    <ul className="config-errors" role={role}>
      {list.map((m) => (
        <li key={m}>{m}</li>
      ))}
    </ul>
  );
}

function Findings({ list }: { list: ZcFinding[] }) {
  if (!list.length) return null;
  return (
    <ul className="impedance-findings">
      {list.map((f, i) => (
        <li key={i} className={`sev-${f.severity ?? "info"}`}>
          <strong>{f.rule_id}</strong> {f.summary}
          {f.recommendation && <div className="muted small">{f.recommendation}</div>}
        </li>
      ))}
    </ul>
  );
}

export function ImpedanceFields({
  netClasses,
  tags,
  errors,
  findings,
  copper,
  testPoints,
  onTestPoints,
  onChange,
}: {
  netClasses: Record<string, NetClass>;
  tags: ImpedanceTag[];
  /** The server's refusals for this config, already routed to rows. */
  errors: ReturnType<typeof routeErrors>;
  /** ZC-* findings from the last Stage 8 run, or null when there is none. */
  findings: ZcFinding[] | null;
  /** The board's copper layer count, from the stackup. */
  copper: number;
  /** test_points.include_controlled_impedance. */
  testPoints: boolean;
  onTestPoints: (v: boolean) => void;
  onChange: (netClasses: Record<string, NetClass>, tags: ImpedanceTag[]) => void;
}) {
  const [newName, setNewName] = useState("");
  const boardLayers = copperLayers(copper);
  const names = Object.keys(netClasses);
  const specs: Record<string, Spec> = {};
  for (const n of names) {
    const s = readImpedance(isMap(netClasses[n]) ? netClasses[n].impedance : undefined);
    if (s) specs[n] = s;
  }

  const byClass: Record<string, ZcFinding[]> = {};
  const unplaced: ZcFinding[] = [];
  for (const f of findings ?? []) {
    const c = findingClass(f);
    if (c && c in netClasses) (byClass[c] ??= []).push(f);
    else unplaced.push(f);
  }

  const setClass = (name: string, next: NetClass) => onChange({ ...netClasses, [name]: next }, tags);
  const setField = (name: string, rules: NetClass, k: string, v: unknown) => {
    const next: NetClass = { ...rules, [k]: v };
    if (v === undefined) delete next[k];
    setClass(name, next);
  };
  const setTag = (i: number, t: ImpedanceTag) => onChange(netClasses, tags.map((x, k) => (k === i ? t : x)));

  const trimmed = newName.trim();
  const nameProblem =
    trimmed && trimmed in netClasses ? `There is already a class called ${trimmed}.` : null;

  return (
    <>
      <fieldset className="board-features">
        <legend>Net classes</legend>
        <p className="muted small">
          Widths in mm. An impedance-controlled class's width and pair gap come from your fab's
          impedance calculator for this stackup — the form cannot know them, and a class left at
          the Default width is not that impedance.
        </p>
        {names.map((name) => {
          const rules: NetClass = isMap(netClasses[name]) ? netClasses[name] : {};
          const spec = specs[name] ?? null;
          const ran = byClass[name] ?? [];
          const ranRules = new Set(ran.map((f) => f.rule_id));
          // What Stage 8 will say about this class as it is now drafted, when
          // the last run has not already said it. The same rules as
          // impedance.review, as far as the config alone can say them: it
          // cannot see the nets, so ZC-003/005/008/011 wait for Stage 8.
          const mode = spec ? modeOf(spec) : null;
          // A pair routes at its own width when it has one (impedance.width_key).
          const widthKey = mode === "differential" && rules.diff_pair_width !== undefined ? "diff_pair_width" : "trace_width";
          const clearance = typeof rules.clearance === "number" ? rules.clearance : DEFAULT_CLEARANCE;
          const rules8: [string, boolean, string][] = spec
            ? [
                ["ZC-001", rules[widthKey] === undefined,
                  `ZC-001: no ${widthKey === "trace_width" ? "trace" : "pair"} width, so its nets get the Default width — not ${label(spec)}.`],
                ["ZC-002", mode === "differential" && rules.diff_pair_gap === undefined,
                  "ZC-002: a differential class with no pair gap. The gap sets the impedance as much as the width."],
                ["ZC-006", mode === "coplanar" && rules.coplanar_gap === undefined,
                  "ZC-006: a coplanar class with no coplanar gap. The gap to the ground pour sets the impedance as much as the width."],
                ["ZC-007", copper > 2 && !(Array.isArray(spec.layers) && spec.layers.length),
                  `ZC-007: no layers on a ${copper}-layer board. The same width is a different impedance on an outer layer and an inner one.`],
                ["ZC-009", copper === 1 && mode !== "coplanar",
                  "ZC-009: a one-layer board has no plane under the trace; only a coplanar line works there."],
                ["ZC-010", mode === "coplanar" && typeof rules.coplanar_gap === "number" && rules.coplanar_gap < clearance - 1e-9,
                  `ZC-010: the coplanar gap is less than the class's ${clearance} mm clearance, so the pour cannot come that close.`],
              ]
            : [];
          const predicted = rules8.filter(([id, hit]) => hit && !ranRules.has(id)).map(([, , msg]) => msg);
          const set = (patch: Spec) => setField(name, rules, "impedance", writeImpedance({ ...spec, ...patch }));
          return (
            <div className="board-feature" key={name}>
              <div className="field-row">
                <strong>{name}</strong>
                <NumField label="Trace width" aria={`${name} trace width`} value={rules.trace_width}
                  onChange={(v) => setField(name, rules, "trace_width", v)} />
                <NumField label="Clearance" aria={`${name} clearance`} value={rules.clearance}
                  onChange={(v) => setField(name, rules, "clearance", v)} />
                {/* Geometry for a mode the class is not in is hidden, unless the
                    file already has it — a value nobody can see is a value
                    nobody can remove. */}
                {(mode === "differential" || rules.diff_pair_width !== undefined) && (
                  <NumField label="Pair width" aria={`${name} pair width`} value={rules.diff_pair_width}
                    placeholder="trace width" onChange={(v) => setField(name, rules, "diff_pair_width", v)} />
                )}
                {(mode === "differential" || rules.diff_pair_gap !== undefined) && (
                  <NumField label="Pair gap" aria={`${name} pair gap`} value={rules.diff_pair_gap}
                    onChange={(v) => setField(name, rules, "diff_pair_gap", v)} />
                )}
                {(mode === "coplanar" || rules.coplanar_gap !== undefined) && (
                  <NumField label="Coplanar gap" aria={`${name} coplanar gap`} value={rules.coplanar_gap}
                    onChange={(v) => setField(name, rules, "coplanar_gap", v)} />
                )}
                {name !== "Default" && (
                  <button
                    className="btn"
                    type="button"
                    title="Remove this class; its nets fall back to Default unless a rule or tag places them"
                    onClick={() => {
                      const next = { ...netClasses };
                      delete next[name];
                      onChange(next, tags);
                    }}
                  >
                    Remove {name}
                  </button>
                )}
              </div>
              <div className="field-row">
                <label className="inline" title="This class is for nets that need a set impedance; Stage 8 and the release check it">
                  <input
                    type="checkbox"
                    aria-label={`${name} controlled impedance`}
                    checked={!!spec}
                    onChange={(e) => setField(name, rules, "impedance", e.target.checked ? true : undefined)}
                  />
                  Controlled impedance
                </label>
                {spec && (
                  <>
                    <NumField label="Ohms" aria={`${name} ohms`} value={spec.ohms} step="1"
                      placeholder={String(DEFAULT_OHMS)} onChange={(v) => set({ ohms: v })} />
                    <label>
                      <span>Mode</span>
                      <select aria-label={`${name} mode`} value={modeOf(spec)}
                        title="Single-ended, a differential pair, or coplanar (ground pour beside the track)"
                        onChange={(e) => set({ mode: e.target.value as ImpedanceMode })}>
                        {MODES.map((m) => (
                          <option key={m} value={m}>{MODE_LABELS[m]}</option>
                        ))}
                      </select>
                    </label>
                    <NumField label="Tolerance %" aria={`${name} tolerance`} value={spec.tolerance_pct} step="1"
                      placeholder={String(DEFAULT_TOLERANCE)} onChange={(v) => set({ tolerance_pct: v })} />
                  </>
                )}
              </div>
              {spec && (
                <div className="field-row">
                  <LayerPicks aria={`${name} layers`} title="Width is for" board={boardLayers}
                    value={Array.isArray(spec.layers) ? spec.layers : []} onChange={(v) => set({ layers: v })} />
                  <LayerPicks aria={`${name} reference`} title="Against planes" board={boardLayers}
                    value={Array.isArray(spec.reference) ? spec.reference : []} onChange={(v) => set({ reference: v })} />
                </div>
              )}
              {predicted.map((p) => (
                <p key={p} className="muted small">Stage 8 will report {p}</p>
              ))}
              <Findings list={ran} />
              <Problems list={errors.classes[name] ?? []} />
            </div>
          );
        })}
        <div className="field-row">
          <label>
            <span>New class</span>
            <input aria-label="New net class name" placeholder="RF_50Ohm" value={newName}
              title="Name of the class to add"
              onChange={(e) => setNewName(e.target.value)} />
          </label>
          <button
            className="btn"
            type="button"
            disabled={!trimmed || !!nameProblem}
            title="Add a class with its own trace width and clearance. Nets join it by name rule or impedance tag."
            onClick={() => {
              // A file with no net_classes has no Default either, and the
              // schema wants Default whenever the section exists (Codex, PR #34).
              onChange({ ...("Default" in netClasses ? {} : { Default: {} }), ...netClasses, [trimmed]: {} }, tags);
              setNewName("");
            }}
          >
            Add net class
          </button>
        </div>
        {nameProblem && <p className="config-errors" role="status">{nameProblem}</p>}
      </fieldset>

      <fieldset className="board-features">
        <legend>Controlled impedance</legend>
        <p className="muted small">
          Which nets need a controlled impedance, by a regular expression on their names. Each
          lands on the class it names, or the one class declaring the same impedance and mode;
          50 Ω single-ended unless it says otherwise.
        </p>
        {tags.map((raw, i) => {
          const tag: ImpedanceTag = isMap(raw) ? raw : { nets: "" };
          const prefix = `Tag ${i + 1}`;
          const chosen = tag.class && specs[tag.class] ? specs[tag.class] : null;
          const own: Spec = { ohms: tag.ohms, mode: tag.mode, tolerance_pct: tag.tolerance_pct };
          const shown = chosen ?? own;
          const same = names.filter((n) => specs[n] && meets(specs[n], own));
          const lands = tag.class
            ? null
            : same.length === 1
              ? `Lands on ${same[0]}.`
              : same.length > 1
                ? `${same.join(", ")} all declare ${label(own)}; choose one, or Stage 5 refuses the board.`
                : `No class declares ${label(own)}, so Stage 5 makes ${synthesizedName(own, names)} with the Default width, and Stage 8 reports ZC-001 until a class with the stackup's width is declared.`;
          const classOptions = names.filter((n) => specs[n]);
          if (tag.class && !classOptions.includes(tag.class)) classOptions.push(tag.class);
          const setOwn = (k: "ohms" | "mode" | "tolerance_pct", v: unknown) => {
            const next = { ...tag, [k]: v } as ImpedanceTag;
            // The default is left unsaid, as it would be by hand.
            if (v === undefined || (k === "mode" && v === "single_ended")) delete next[k];
            setTag(i, next);
          };
          return (
            <div className="board-feature" key={i}>
              <div className="field-row">
                <label>
                  <span>{prefix} nets</span>
                  <input aria-label={`${prefix} nets`} placeholder="^ANT_|_RF_(IN|OUT)$" value={tag.nets ?? ""}
                    title="A regular expression over net names; every net it matches needs this impedance"
                    onChange={(e) => setTag(i, { ...tag, nets: e.target.value })} />
                </label>
                <label>
                  <span>Class</span>
                  <select
                    aria-label={`${prefix} class`}
                    title="The class these nets go on, or By impedance to use the class declaring the same target"
                    value={tag.class ?? ""}
                    onChange={(e) => {
                      // A tag that names a class takes its target from it;
                      // keeping its own would only be a second place to
                      // disagree, which Stage 5 refuses.
                      const next: ImpedanceTag = { nets: tag.nets };
                      if (e.target.value) next.class = e.target.value;
                      else {
                        // Back to "by impedance": keep the target it had, which
                        // was the class's. Starting from the stripped tag would
                        // quietly make a 90 Ω pair 50 Ω single-ended (Codex, PR #34).
                        const from = chosen ?? own;
                        if (from.ohms !== undefined) next.ohms = from.ohms;
                        if (from.mode && from.mode !== "single_ended") next.mode = from.mode;
                        if (from.tolerance_pct !== undefined) next.tolerance_pct = from.tolerance_pct;
                      }
                      setTag(i, next);
                    }}
                  >
                    <option value="">By impedance</option>
                    {classOptions.map((n) => (
                      <option key={n} value={n}>{n}</option>
                    ))}
                  </select>
                </label>
                <NumField label="Ohms" aria={`${prefix} ohms`} value={shown.ohms} step="1"
                  placeholder={String(DEFAULT_OHMS)} readOnly={!!chosen} onChange={(v) => setOwn("ohms", v)} />
                <label>
                  <span>Mode</span>
                  <select aria-label={`${prefix} mode`} value={modeOf(shown)} disabled={!!chosen}
                    onChange={(e) => setOwn("mode", e.target.value)}>
                    {MODES.map((m) => (
                      <option key={m} value={m}>{MODE_LABELS[m]}</option>
                    ))}
                  </select>
                </label>
                <NumField label="Tolerance %" aria={`${prefix} tolerance`} value={shown.tolerance_pct} step="1"
                  placeholder={String(DEFAULT_TOLERANCE)} readOnly={!!chosen}
                  onChange={(v) => setOwn("tolerance_pct", v)} />
                <button className="btn" type="button" title="Remove this tag; its nets lose their impedance requirement"
                  onClick={() => onChange(netClasses, tags.filter((_, k) => k !== i))}>
                  Remove tag
                </button>
              </div>
              {chosen && <p className="muted small">Target from {tag.class}: {label(chosen)}.</p>}
              {lands && <p className="muted small">{lands}</p>}
              <Problems list={errors.tags[i] ?? []} />
            </div>
          );
        })}
        <button className="btn" type="button" title="Mark nets, by name pattern, as needing a controlled impedance"
          onClick={() => onChange(netClasses, [...tags, { nets: "" }])}>
          Add impedance tag
        </button>
        <div className="field-row">
          <label className="inline" title="Off by default: a test pad on a controlled line is a stub">
            <input
              type="checkbox"
              aria-label="Test points on controlled-impedance nets"
              checked={testPoints}
              onChange={(e) => onTestPoints(e.target.checked)}
            />
            Test points on controlled-impedance nets
          </label>
        </div>
        <p className="muted small">
          Off by default: a test pad on a controlled line is a stub that changes its impedance.
        </p>
        {findings && unplaced.length > 0 && (
          <>
            <p className="muted small">From the last Stage 8 review:</p>
            <Findings list={unplaced} />
          </>
        )}
      </fieldset>
    </>
  );
}

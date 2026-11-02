/**
 * What the fab builds the bare board from, beyond what the gerbers show.
 *
 * None of it is read from a KiCad file by the sites that quote a board: copper
 * weight, laminate, Tg, colours, gold fingers, via protection and castellation
 * are boxes on their order forms, and each one left at its default is a guess
 * the fab makes for you. The release package carries them in board-specs.md;
 * this is where they are set.
 *
 * Every field has a "not specified" choice, and choosing it removes the key
 * rather than writing a default. board-specs.md lists what is unset, so "the
 * fab's standard" stays a visible decision instead of a value nobody chose.
 * That is also why the yes/no fields are lists and not checkboxes: an
 * unticked box cannot tell "no" from "not said".
 */

import type { Fabrication } from "../api";

const COPPER_OZ = [0.5, 1, 2, 3, 4];
const MASK = ["green", "red", "blue", "purple", "black", "white", "yellow", "none"];
const SILK = ["white", "black", "yellow", "none"];
const BEVELS = [0, 20, 30, 45];
const VIA_PROTECTION: [string, string][] = [
  ["none", "None (open vias)"],
  ["tented", "Tented (IPC-4761 Type I)"],
  ["plugged", "Plugged (Type III)"],
  ["filled", "Filled (Type V)"],
  ["filled_capped", "Filled and capped (Type VII)"],
];

/** A value from the file that the list does not offer is shown, not lost. */
const withCurrent = <T,>(list: T[], current: T | undefined): T[] =>
  current === undefined || list.includes(current) ? list : [...list, current];

export function FabricationFields({
  fab,
  copper,
  onChange,
}: {
  fab: Fabrication;
  /** Copper layer count: inner copper is only asked about when there is some. */
  copper: number;
  onChange: (fab: Fabrication) => void;
}) {
  const set = <K extends keyof Fabrication>(k: K, v: Fabrication[K] | undefined) => {
    const next: Fabrication = { ...fab };
    if (v === undefined) delete next[k];
    else next[k] = v;
    onChange(next);
  };
  const copperOz = fab.copper_oz ?? {};
  const setCopper = (side: "outer" | "inner", v: string) => {
    const next = { ...copperOz };
    if (v) next[side] = Number(v);
    else delete next[side];
    set("copper_oz", Object.keys(next).length ? next : undefined);
  };
  const fingers =
    fab.gold_fingers === false
      ? "no"
      : fab.gold_fingers && typeof fab.gold_fingers === "object"
        ? String(fab.gold_fingers.bevel_deg ?? 0)
        : "";
  const yesNo = (v: boolean | undefined) => (v === undefined ? "" : v ? "yes" : "no");

  return (
    <fieldset className="board-fabrication">
      <legend>Fabrication</legend>
      <p className="muted small">
        Printed in <code>board-specs.md</code> at the root of every release package, for the quote
        forms that read none of it from KiCad files. Anything not specified is listed there as left to
        the fab's standard.
      </p>
      <div className="field-row">
        <label>
          <span>Outer copper (oz)</span>
          <select
            value={copperOz.outer ?? ""}
            onChange={(e) => setCopper("outer", e.target.value)}
            title="Finished copper weight on the outer layers. 1 oz is the standard; 2 oz for high current."
          >
            <option value="">not specified (1 oz)</option>
            {withCurrent(COPPER_OZ, copperOz.outer).map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        {copper > 2 && (
          <label>
            <span>Inner copper (oz)</span>
            <select
              value={copperOz.inner ?? ""}
              onChange={(e) => setCopper("inner", e.target.value)}
              title="Finished copper weight on the inner layers. 0.5 oz is the standard."
            >
              <option value="">not specified (0.5 oz)</option>
              {withCurrent(COPPER_OZ, copperOz.inner).map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
        )}
        <label>
          <span>Laminate</span>
          <input
            value={fab.material ?? ""}
            placeholder="FR-4"
            onChange={(e) => set("material", e.target.value.trim() ? e.target.value : undefined)}
            title="Board material, e.g. FR-4, FR-4 High Tg, Rogers RO4350B, polyimide, aluminium"
          />
        </label>
        <label>
          <span>Tg (°C)</span>
          <input
            type="number"
            step="1"
            min="100"
            max="400"
            value={fab.tg_c ?? ""}
            placeholder="not specified"
            onChange={(e) => set("tg_c", e.target.value === "" ? undefined : Number(e.target.value))}
            title="Minimum glass transition temperature of the laminate: 130 is standard FR-4, 170 is high Tg"
          />
        </label>
      </div>
      <div className="field-row">
        <label>
          <span>Solder mask</span>
          <select
            value={fab.mask_color ?? ""}
            onChange={(e) => set("mask_color", e.target.value || undefined)}
            title="Solder mask colour. Green is the cheapest and quickest at most fabs."
          >
            <option value="">not specified</option>
            {withCurrent(MASK, fab.mask_color).map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Silkscreen</span>
          <select
            value={fab.silk_color ?? ""}
            onChange={(e) => set("silk_color", e.target.value || undefined)}
            title="Silkscreen (legend) colour"
          >
            <option value="">not specified</option>
            {withCurrent(SILK, fab.silk_color).map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Via protection</span>
          <select
            value={fab.via_protection ?? ""}
            onChange={(e) => set("via_protection", e.target.value || undefined)}
            title="How vias are covered, per IPC-4761. Filled and capped is what via-in-pad needs."
          >
            <option value="">not specified</option>
            {VIA_PROTECTION.map(([v, label]) => (
              <option key={v} value={v}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="field-row">
        <label>
          <span>Gold fingers</span>
          <select
            value={fingers}
            onChange={(e) => {
              const v = e.target.value;
              set("gold_fingers", v === "" ? undefined : v === "no" ? false : { bevel_deg: Number(v) });
            }}
            title="Hard gold edge connector contacts, and the bevel angle on the edge they plug in by"
          >
            <option value="">not specified</option>
            <option value="no">no</option>
            {BEVELS.map((b) => (
              <option key={b} value={b}>
                {b ? `yes, ${b}° bevel` : "yes, no bevel"}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Castellated edges</span>
          <select
            value={yesNo(fab.castellated)}
            onChange={(e) => set("castellated", e.target.value === "" ? undefined : e.target.value === "yes")}
            title="Plated half-holes on the board edge, for a module soldered onto another board"
          >
            <option value="">not specified</option>
            <option value="no">no</option>
            <option value="yes">yes</option>
          </select>
        </label>
      </div>
    </fieldset>
  );
}

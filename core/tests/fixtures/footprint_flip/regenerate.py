"""Regenerate kicad_placed.kicad_pcb: FlipProbe placed and flipped by pcbnew itself.

The emitter's flip is checked against this file rather than against rules
written down from reading KiCad's source, because the rules are not what one
would guess (a flipped reference text keeps itself upright by turning 180 deg
and swapping both justifications). Run it with KiCad's Python:

    /usr/bin/python3 tests/fixtures/footprint_flip/regenerate.py

FlipProbe is authored for this test, not taken from a library: one of every
kind of thing a flip has to carry — rotated, chamfered, trapezoid and custom
pads, a drilled oval, an arc, a circle, a polygon, a courtyard rect, a
back-side text, justified texts, a plain and an angled text box and a keepout
zone. The placements follow the KiCad plugin's hdm_to_pcb: orient, then flip
left to right.
"""

from pathlib import Path

import pcbnew

HERE = Path(__file__).resolve().parent
PLACEMENTS = [  # refdes, board x, board y, rot, side
    ("U1", 10, 10, 0, "bottom"),
    ("U2", 30, 10, 30, "bottom"),
    ("U3", 50, 10, 200, "bottom"),
    ("U4", 70, 10, 30, "top"),
]
SHEET_ORIGIN = 12.5  # the emitter's offset from board to sheet coordinates


def main() -> None:
    board = pcbnew.BOARD()
    for ref, x, y, rot, side in PLACEMENTS:
        fp = pcbnew.FootprintLoad(str(HERE / "BLPL_Test.pretty"), "FlipProbe")
        fp.SetFPID(pcbnew.LIB_ID("BLPL_Test", "FlipProbe"))
        board.Add(fp)
        fp.SetReference(ref)
        fp.SetPosition(pcbnew.VECTOR2I_MM(x + SHEET_ORIGIN, y + SHEET_ORIGIN))
        if rot:
            fp.SetOrientationDegrees(rot)
        if side == "bottom":
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
    pcbnew.SaveBoard(str(HERE / "kicad_placed.kicad_pcb"), board)


if __name__ == "__main__":
    main()

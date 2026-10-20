# Checking the toolpaths by Fusion's simulation

Fusion's own simulation is the third check, next to `check_cam.py` and the written review ([review.md](review.md)).
It reports what Fusion itself flags, such as the tool, its holder or the machine's Z axis hitting the vise, and it leaves a model of the machined stock, which `sim_check.py` compares with the part, face by face.
Where the stock differs from the part by more than the simulation's accuracy, a toolpath leaves copper or cuts into the part.

The Fusion API can neither run a simulation nor read its stock, so `scripts/sim_ui.ps1` drives the simulation's window, on Windows, through window messages and UI Automation (Windows' interface for reading and pressing controls).
It does not use the real mouse or keyboard, so it neither needs the screen nor disturbs whoever is using it.

## Running it

Simulate each setup on its own, in run order.
With several setups selected, Fusion warns that their work offsets differ, and reports collisions with the vise that each setup's own simulation does not have (on the 30 × 20 × 4 mm test part: 103 errors in the bottom setup together, none alone).
A setup simulated on its own starts from the whole stock, not from what the setups before it left, so each setup's saved stock has only its own cuts in it; since each setup only removes copper, the part they leave between them is the intersection of those stocks, and `compare` takes that intersection.

For each setup:

1. Start the simulation, with its Issues panel open (a script that is not read-only, so no simulation or other command dialog may be open):

   ```python
   def run(_context: str):
       runpy.run_path(SCRIPTS + r'\sim_check.py')['simulate']({'setup': 'Top (carvera-cam)'})
   ```

2. Run the UI script from a shell, with an STL path in a scratch folder:

   ```
   powershell -ExecutionPolicy Bypass -File <SCRIPTS>\sim_ui.ps1 -Stl <scratch>\top.stl
   ```

   It waits until the simulation has verified the whole setup, prints the Issues panel's counts and every issue under its operation, moves the simulation to its end, and saves the stock there to the STL ("Save Stock…" in the canvas's right-click menu, which saves the stock at the playhead), to this computer only.
   It prints JSON: `errors`, `warnings`, `issues` (each with the `operation`, the `count`, and the `events`, each a list of what collided, such as `Fixture+Holder`) and any `notices` Fusion showed.

3. Exit the simulation (read-only): `runpy.run_path(SCRIPTS + r'\sim_check.py')['stop']({})`.
   Exiting takes effect after the call returns, so start the next setup's simulation in a separate call.

Then write the part's body to an STL (read-only): `runpy.run_path(SCRIPTS + r'\sim_check.py')['export_part']({'path': r'<scratch>\part.stl'})`.
It picks the machined body, the copy in `carvera-cam part` when `build` turned the part over; pass `body` for a design with several visible bodies.
Compare, outside Fusion or inside it:

```
python <SCRIPTS>\sim_check.py <scratch>\part.stl <scratch>\top.stl <scratch>\bottom.stl
```

It takes a few seconds for a 30 × 20 mm part at the default 0.05 mm grid; pass `pitch` (in `compare(args)`) to coarsen it for a large part.

If a call times out while the simulation starts, Fusion has opened a notice that blocks every MCP call; `sim_ui.ps1` closes notices titled "Info" and reports their text, so run it, then go on.

## Reading the comparison

`compare` casts a vertical ray through each column of a grid over the part's footprint, plus 1 mm round it, and compares where each mesh is solid along Z.
It reports two kinds of region, each with its volume, area, deepest difference along Z (`depth_mm`), width across, Z range and centre, in the design's frame, in mm:

- `left`: copper the part does not have, which a toolpath did not remove;
- `overcut`: copper the part has, which a toolpath removed.

Regions narrower than 0.1 mm or smaller than 0.001 mm³ are only counted (`*_small_regions`, `*_small_volume_mm3`): along each wall the two meshes facet the curve differently, which leaves strips one grid column wide.
Differences under 0.02 mm along Z are the simulation's own accuracy and are left out.

Some differences are by design; match each region against `build`'s warnings and the features from `analyze`:

| Region | Expected where |
|---|---|
| overcut, through the part, about 0.2 mm wide round a hole | a thread milled into a hole modelled at its minor diameter |
| overcut, a cone below a blind hole's bottom, as deep as the drill's point | a blind drilled hole without a modelled point |
| overcut, a cone 1.59 mm across and 0.37 mm deep in a pocket floor | a pocket entry hole's dimple |
| overcut, 0.1–0.2 mm deep along an edge | the deburr chamfer |
| left, in an inside corner, out to the corner radius of the smallest end mill that fits | a sharp or tight inside corner |

Anything else is a finding: a floor left high or cut low, a wall left thick or cut back, a feature not cut, or stock left beside the part, such as a tab.
Report it with its place and size, find the operation that cut or should have cut there (`check_cam.py`'s `summary`), and treat it as a gap in the checks too (see "Comparing with the other checks").

## Reading the issues

Every error or warning in the Issues panel needs attention, except those of a simulation run on several setups at once, which is why each setup runs alone.
An event names what met: the tool's `Cutter`, `Shaft`, `Shoulder` or `Holder`, the `Fixture` (the modelled vise and fixture plate), the stock, or a machine axis such as `Z`.
A `Fixture` event in a setup's own simulation means a tool or the spindle reaches the vise: look at the operation's moves near the jaws (rule X1) and at where `build` placed that setup's vise.

## Comparing with the other checks

The simulation, `check_cam.py` and the written review should agree.
A region or issue that neither of the others found is a gap in them: add the rule to `check_cam.py` and its row to [rules.md](rules.md), or the item to [review.md](review.md), as SKILL.md's "Improving the skill" says.
A finding of the others that the simulation contradicts, such as a cut into the part that the stock does not show, points at a wrong rule.

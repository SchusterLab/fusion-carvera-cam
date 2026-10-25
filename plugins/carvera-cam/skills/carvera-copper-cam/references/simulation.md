# Checking the toolpaths by Fusion's simulation

Fusion's own simulation is the third check, next to `check_cam.py` and the written review ([review.md](review.md)).
It reports what Fusion itself flags, such as the tool, its holder or the machine's Z axis hitting the vise, and it leaves a model of the machined stock, which `sim_check.py` compares with the part, face by face.
Where the stock differs from the part by more than the simulation's accuracy, a toolpath leaves copper or cuts into the part.

The Fusion API can neither run a simulation nor read its stock, so `scripts/sim_ui.ps1` drives the simulation's window, on Windows, through window messages and UI Automation (Windows' interface for reading and pressing controls).
It does not use the real mouse or keyboard, so it does not disturb whoever is using the computer.
Saving the stock needs Windows unlocked, though, since the canvas's right-click menu does not open on a locked screen; the issues can be read either way.
Only the lock screen of the session Fusion runs in counts: under Remote Desktop, the computer's own console has a lock screen of its own, which does not stop the menu.
The menu also opens only where Fusion's canvas shows on screen: with another window over it, `sim_ui.ps1` says so under `stock_error`, and the user has to bring Fusion to the front for the few minutes the saves take.

## Running it

Read the issues from each setup on its own, in run order, and save the stock from all the setups simulated together.
With several setups selected, Fusion warns that their work offsets differ, and reports collisions with the vise that each setup's own simulation does not have (on the 30 × 20 × 4 mm test part: 103 errors in the bottom setup together, none alone); those errors touch only the collision checks, so the stock it leaves is right, and its issues are ignored.
A setup simulated on its own starts from the whole stock, not from what the setups before it left, so its stock has only its own cuts in it; the setups together leave the part as they cut it between them, in one save.

For each setup, and then for all of them together (`{'setup': 'all'}`):

1. Start the simulation, with its Issues panel open (a script that is not read-only, so no simulation or other command dialog may be open).
   It refuses a setup with a toolpath not yet generated or out of date, and lists under `empty` the operations Fusion generated but found nothing to cut; report each of those, since an empty operation is one `build` made for a feature it then did not machine:

   ```python
   def run(_context: str):
       runpy.run_path(SCRIPTS + r'\sim_check.py')['simulate']({'setup': 'Top (carvera-cam)'})
   ```

2. Run the UI script from a shell; for all the setups together, with an STL path in a scratch folder, so that it saves the stock:

   ```
   powershell -ExecutionPolicy Bypass -File <SCRIPTS>\sim_ui.ps1
   powershell -ExecutionPolicy Bypass -File <SCRIPTS>\sim_ui.ps1 -Stl <scratch>\stock.stl
   ```

   It waits until the simulation has verified the whole setup, prints the Issues panel's counts and every issue under its operation, and with `-Stl` moves the simulation to its end and saves the stock there to the STL ("Save Stock…" in the canvas's right-click menu, which saves the stock at the playhead), to this computer only.
   After the move to the end, Fusion builds the stock a little at a time, and Save Stock saves it as far as it has got: on a 120 × 250 mm part, a save 3 s after the move left out every operation from the deburr on.
   So the script saves the stock again every few seconds until two saves in a row have the same number of triangles, and lists each save's count under `saves`.
   It prints JSON: `errors`, `warnings`, `issues` (each with the `operation`, the `count`, and the `events`, each a list of what collided, such as `Fixture+Holder`) and any `notices` Fusion showed.
   When it cannot save the stock, such as on a locked screen, it still prints the issues, gives the reason under `stock_error`, and exits with 1: ask the user to unlock the screen, then run it again on the same simulation.

3. Exit the simulation (read-only): `runpy.run_path(SCRIPTS + r'\sim_check.py')['stop']({})`.
   Exiting takes effect after the call returns, so start the next setup's simulation in a separate call; if Fusion refuses that call because a command dialog is open, make it again.

Then write the part's body to an STL (read-only): `runpy.run_path(SCRIPTS + r'\sim_check.py')['export_part']({'path': r'<scratch>\part.stl'})`.
It picks the machined body, the copy in `carvera-cam part` when `build` turned the part over; pass `body` for a design with several visible bodies.
It meshes the faces to within 0.005 mm (`deviation`), not by Fusion's preset refinements, which coarsen with the body's size and put false rings of overcut round the holes of a long part.
Compare, outside Fusion or inside it:

```
python <SCRIPTS>\sim_check.py <scratch>\part.stl <scratch>\stock.stl
```

`compare` takes several stocks too, and compares the part with what they share, as for stocks saved one setup at a time.

It takes a few seconds for a 30 × 20 mm part at the default 0.05 mm grid; pass `pitch` (in `compare(args)`) to coarsen it for a large part.

To find where an issue starts, simulate some of a setup's operations alone, from the uncut stock, with `operations`: `simulate({'setup': 'Top (carvera-cam)', 'operations': ['Outline', 'Tab entry holes']})`.
An issue that goes away when an earlier operation is left out comes from the stock that operation leaves.

If a call times out while the simulation starts, Fusion has opened a notice that blocks every MCP call; `sim_ui.ps1` closes notices titled "Info" and reports their text, so run it, then go on.

## Reading the comparison

`compare` casts a vertical ray through each column of a grid over the part's footprint and up to 1 mm round it, and compares where each mesh is solid along Z.
The stock further out, such as the frame round a part cut out of larger stock, is left out.
It reports two kinds of region, each with its volume, area, deepest difference along Z (`depth_mm`), width across, Z range and centre, in the design's frame, in mm:

- `left`: copper the part does not have, which a toolpath did not remove;
- `overcut`: copper the part has, which a toolpath removed.

Regions narrower than 0.1 mm or smaller than 0.001 mm³ are only counted (`*_small_regions`, `*_small_volume_mm3`): along each wall the two meshes facet the curve differently, which leaves strips one grid column wide.
Differences under 0.02 mm along Z are the simulation's own accuracy and are left out.

A region is a connected patch of columns, so a deburr chamfer round an island's top and a sliver at the island's wall can join into one region whose `depth_mm` is the wall's height; look at its `z_mm` and place before calling it a wall cut back.

Some differences are by design; match each region against `build`'s warnings and the features from `analyze`:

| Region | Expected where |
|---|---|
| overcut, through the part, about 0.2 mm wide round a hole | a thread milled into a hole modelled at its minor diameter |
| overcut, a cone below a blind hole's bottom, as deep as the drill's point | a blind drilled hole without a modelled point |
| overcut, a cone 1.59 mm across and 0.37 mm deep in a pocket floor | a pocket entry hole's dimple |
| overcut, 0.1–0.2 mm deep along an edge | the deburr chamfer |
| left, in an inside corner, out to the corner radius of the smallest end mill that fits | a sharp or tight inside corner |
| left, a hole's width across, part of the way up the part, under a hole not along Z | a hole into the part's side, which `build` warns that nothing cuts |
| left, a wedge along an edge, as wide as the face is across | a sloped face other than a 45° chamfer up to 1.4375 mm wide, such as a chamfer at another angle, which `build` warns that nothing cuts |
| left, a cone at the bottom of a blind threaded hole | a drill point modelled in a hole that is bored with a flat bottom for the thread mill |

Anything else is a finding: a floor left high or cut low, a wall left thick or cut back, a feature not cut, or stock left beside the part, such as a tab.
The comparison cannot see a missing deburr, since the model's edges are sharp and an edge left sharp matches them; rule DB1 checks the deburr.
Report it with its place and size, find the operation that cut or should have cut there (`check_cam.py`'s `summary`), and treat it as a gap in the checks too (see "Comparing with the other checks").

## Reading the issues

Every error or warning in the Issues panel needs attention, except those of a simulation run on several setups at once, which is why each setup runs alone.
An event names the two things that met: the tool's `Cutter`, `Shaft`, `Shoulder` or `Holder`; the `Stock`; the `Fixture`, which is the modelled vise and fixture plate together; or a part of the machine, named by the axis that carries it: `Z` for the spindle head, `Y` for the bed, which rides on the Y axis, and `Base` for the frame that stays put.
A `Fixture` event in a setup's own simulation means the tool, its holder or the spindle head reaches the vise or the plate: look at the operation's moves near the jaws (rule X1) and at where `build` placed that setup's vise.
`Rapid move into stock` means a rapid, sideways or straight down, meets copper, or grazes it: a drill as wide as the slot it rapids into counts too.
In the bottom setup's `Cut tabs`, it has also come up once or twice on parts whose every rapid there runs in columns the bottom outline had cleared across the tool's width (`PackageMount_K`, `sample-lid-with-tabs-v2-6port10x10`); the move it flags is not yet found, and rule H2 reports nothing there.
`Base+Stock` means the stock itself meets the machine's frame: the bed carries it along the machine's Y axis, so a cut near one end of a long bar moves the bed far enough that the bar's other end reaches the frame (see SKILL.md's "Limits").

Toolpaths made to collide on purpose, on the 30 × 20 × 4 mm test part, show what the simulation catches.
It reports the cutter in the insert on a jaw's face or in the fixture plate beside the vise (`Fixture+Cutter`), the holder and the spindle head beside a jaw on a deep plunge (`Fixture+Holder`, `Fixture+Z`), and the cutter in the raised end of the bed, past the plate (`Y+Cutter`).
With the tool past the bed's edge along the machine's Y axis, where the tool itself meets nothing, it reports `Fixture+Base`, most likely the fixture plate, which the bed carries, running into the frame.
It does not check the machine's travel: the machine model sets no limits on its axes, and a toolpath 600 mm from the part, far beyond the Carvera's travel, raised no issue.
Nor does it check the stock against the vise: stock 250 mm across a vise that opens to about 185 mm simulated without an issue, so only `build`'s warning catches that stock.

## Comparing with the other checks

The simulation, `check_cam.py` and the written review should agree.
A region or issue that neither of the others found is a gap in them: add the rule to `check_cam.py` and its row to [rules.md](rules.md), or the item to [review.md](review.md), as SKILL.md's "Improving the skill" says.
A finding of the others that the simulation contradicts, such as a cut into the part that the stock does not show, points at a wrong rule.

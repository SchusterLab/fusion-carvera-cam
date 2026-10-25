---
name: carvera-copper-cam
description: Generate CAM toolpaths in Autodesk Fusion for machining a copper part on SchusterLab's Makera Carvera, following the lab's conventions (Makera and McMaster-Carr tools with the "Copper" preset, the Carvera Community machine and post, a top setup and, when the part needs it, a flipped bottom setup, pre-drilled entry holes, and, when the stock is larger than the part, four holding tabs cut by a separate program), and check existing Carvera toolpaths for crash, spindle-stall and tool-wear risks against the lab's rules and by Fusion's simulation, comparing the simulated stock with the part. Use when the user asks to program, CAM, toolpath or machine a part on the Carvera or Makera, in copper or aluminum, or to check, review or verify Carvera toolpaths. Needs the Fusion MCP server.
---

# Carvera copper toolpaths

This skill turns a Fusion design of a 2.5-axis part into a ready-to-post Carvera program, in the lab's style.
It makes one setup or two (the top, then the part flipped), every operation with tools from Makera and McMaster-Carr and the lab's feeds, and one NC program per setup, `<part>_top` and `<part>_bottom`, plus `<part>_tabs` when the part is cut out of larger stock.
How it machines the part depends on the stock (see "Machining strategy").
The same programs machine aluminum: run them with the Carvera's feed and spindle overrides at 200%.
It also checks existing toolpaths, the lab's own as well as those it generates, against the lab's rules for avoiding crashes, spindle stalls and tool wear (see "Checking existing toolpaths").
By default it then runs Fusion's simulation of each setup, reads the collisions Fusion flags, and compares the stock the simulation leaves with the part (see [references/simulation.md](references/simulation.md)).

A 2.5-axis part is one whose features are all reached straight down the Z axis from the top or from the bottom: pockets, holes, outlines and through windows.
The skill does not cut side features, undercuts or 3D surfaces.

## Machining strategy

The stock decides how the part is machined, and `build` works it out from the stock's size and the part's features:

| Stock | How the part is held | Programs, two setups | Programs, one setup |
|---|---|---|---|
| larger than the part in both X and Y, by 6.175 mm or more on every side | by four tabs to the stock round it | `_top`, `_bottom`, `_tabs` | `_top`, `_tabs` |
| the part's own size along one side, longer along the other, and the part longer than the jaws are wide (95.5 mm) | by its own sides, in the jaws | `_top`, `_bottom` | `_top` |
| the part's own X and Y size | by its own sides, in the jaws | `_top`, `_bottom` | `_top` |

`build` refuses any other stock.

**Window machining** cuts the part out of larger stock: an outline cut round the part leaves four tabs holding it to the stock, and the last program, `_tabs`, cuts them. Before `_tabs` runs, the operator hot-glues the part to the stock round it, so that the freed part stays put. With two setups the tabs sit at mid-thickness, and with one on the part's bottom face.

**Stock that fits the part along one side** holds the part by those sides in the jaws; cuts through the whole stock free its ends first, and there are no tabs.

**Stock of the part's own size** needs no outline cut and no tabs: the vise holds the part itself, so its outline must be that rectangle.

**One setup** is enough when nothing opens at the bottom face, since through holes and windows are cut from the top; the bottom face is then left as the stock was, and not deburred. Pass `"sides": 2` to machine it anyway, to face it or to deburr its edges.

**No tool may enter the vise**, its jaws or the 1.9 mm ledge under the stock along each jaw: `build` tests each planned cut against the modelled vise and stops with an error naming the cut, and the check's rule X1 tests the G-code.

[references/conventions.md](references/conventions.md), "Setups", has the details of each.

## What the generator does

`scripts/carvera_cam.py` runs inside Fusion's Python; its functions each take one dict of arguments:

| Entry point | What it does |
|---|---|
| `analyze(args)` | Recognizes the part's holes, tapped holes, pocket floors, through windows and faces, and prints them as JSON. Changes nothing. |
| `build(args)` | Deletes what an earlier build made (setups ending in `(carvera-cam)`, and the `carvera-cam` sketches and planes), then creates the setups, tools, operations and NC programs. It orders each setup's operations for the fewest tool changes, keeping each one after those it depends on (conventions.md, "Run order"). Does not compute toolpaths. |
| `generate(args)` | Computes the toolpaths within a time budget and reports each operation's errors, warnings and machining time. |

`scripts/check_cam.py` has `check(args)`, the toolpath check, and `summary(args)`, which lists each operation's tool, time, cut length, depth, extent and plunge points from the G-code, for comparing two builds.

`scripts/sim_check.py` and `scripts/sim_ui.ps1` check the toolpaths by Fusion's own simulation: they run it on each setup, read the collisions and other issues Fusion flags, save the stock it leaves, and compare that stock with the part (see [references/simulation.md](references/simulation.md)).

The toolpaths are checked by `scripts/check_cam.py`'s rules, by reading them for what no rule covers as [references/review.md](references/review.md) describes, and then by Fusion's simulation, which shows whether the stock it leaves is the part.
By default the skill does all of these, compares the results, and improves whatever falls short (see "Improving the skill").

The lab's conventions it applies are in [references/conventions.md](references/conventions.md); read that file before changing any parameter.

## Workflow

### 1. Open the part

Find the document with the Fusion MCP `read` tool (`queryType: "document"`, `operation: "search"`) and open it with `execute` (`featureType: "document"`, `operation: "open"`).
Never save the document unless the user asks; the user reviews the toolpaths first.

### 2. Run the analysis

Every call to the generator goes through a short stub passed to the Fusion MCP `execute` tool as a `script`.
`SCRIPTS` is this skill's `scripts` folder, as an absolute Windows path:

```python
import runpy
SCRIPTS = r'<absolute path of this skill>\scripts'

def run(_context: str):
    runpy.run_path(SCRIPTS + r'\carvera_cam.py')['analyze']({})
```

Pass `readOnly: true` for `analyze`, since it changes nothing.
If the design has more than one visible solid body, pass `{"body": "<name>"}`.
A body in the root component goes by its name, and a body inside a component by its path, `<occurrence>/<body>`, such as `Nut:1/Body1`, so that a part named like the bodies of the screws and nuts inserted round it can still be picked.

The JSON lists, among other things:

- `size_mm`: the part's bounding box, with Z its thickness;
- `holes`: groups of holes by diameter, side (`top`, `bottom`, or `none` when closed at both ends), through or blind, and `thread`, the thread read from a tapped Hole feature;
- `tap_candidate`: the hole's thread: the one its tapped Hole feature names, or, for a plain hole, a thread whose minor diameter matches it;
- `floors`: pocket floors and the faces level with the part's top or bottom (the two outer faces, and any other face at that height, such as a boss's top), each with its smallest inside-corner radius (0 for a sharp corner, null when the walls have no inside corners), `side_open` for a pocket open to the side of the part, and `shelf` for a floor that steps down into a deeper pocket;
- `windows`: through openings that are not round holes;
- `side_holes`: holes whose axis does not run along Z, which nothing cuts, each with its diameter, the middle of its axis and the axis's direction;
- `chamfers`: the 45° chamfers and countersinks the design models, flat or cones about Z, each with a level face along its edge on the side it faces and walls along its other edge, and at most 1.4375 mm wide, with its side (`top` or `bottom`), width and heights; `build` cuts them with the chamfering bit (see Limits);
- `sloped_faces`: the other faces neither level nor upright, which nothing cuts, such as chamfers at another angle or wider, each with a point on it, its area, its slope from level and its width;
- `tab_z`: the height of the tab band's centre that `build` will use with two setups: halfway between the part's top and bottom, unless a pocket open to the side is at that height and within an end mill's width of a tab. `tab_z_one_sided` is the same with one setup: the band on the part's bottom face, or the lowest height above it clear of such pockets.
- `top`, `turned_over` and `features_per_face`: the design's face machined first, as the top (`+Z`, `-Z`, `+X`, `-X`, `+Y` or `-Y`), whether that face is the design's bottom (`-Z`), and the features (pocket floors and blind holes; through holes and windows count for neither face) on the top and bottom faces as the part is machined. Without a `top` argument, whichever of the design's top and bottom faces has more features goes up, so `turned_over` is true when the bottom has more. When the part is turned, the rest of the JSON describes the turned part, turned about its centre: turned over (`-Z`), its y and z are mirrored about the centre; with a side up, the part is turned a quarter about the design's Y axis (`+X`, `-X`) or X axis (`+Y`, `-Y`).

### 3. Agree the plan with the user

Before building, confirm with the user, briefly and in one message:

- **Stock.** Ask whether the stock has the same X and Y size as the part, and whether it has the same thickness, and, for each that differs, the stock's dimensions. The answers and `analyze`'s features decide the machining strategy (see "Machining strategy"): tell the user which one `build` will use, the setups and programs it makes, and, with one setup, that the bottom face stays as the stock left it and is not deburred. For stock of the part's size along a side, pass the part's `size_mm` value for that side, as `analyze` printed it, in the design's axes. The lab's usual stock is a 2″ × 150 mm bar (50.8 × 150 mm), 6.35 mm or 19 mm thick, or a 150 × 150 mm plate about 6.2 mm thick. Where the part sits in the stock is in [references/conventions.md](references/conventions.md), "Setups"; with two setups, `bottom` (below) moves it in the stock's thickness, which shifts facing time between the top and bottom programs, so the user can choose when to come back to flip the stock.
- **Taps.** Lab parts mark tapped holes with Fusion's Hole command, Tapped type, threads not modeled, which leaves a plain hole at the minor diameter. `build` reads that thread data itself. For a plain hole with a `tap_candidate` (one whose `thread` is empty), ask whether it is tapped, and pass the answer as `taps`, keyed by the diameter exactly as `analyze` printed it, for example `{"1.623": "M2"}`. Supported threads: M2, M2.5, M3 and M4; their pre-drilling and thread-milling settings are in [references/threads.toml](references/threads.toml).
- **Tab height**, in window machining, if `analyze` moved `tab_z` (or, with one setup, `tab_z_one_sided`) because a pocket opens to the side; the user may prefer another height, passed as `tab_z`.
- **A Part design becomes a Hybrid one.** `build` adds components, which a Part design cannot hold, so it switches the design to Hybrid, which keeps its modelling, and warns. Saving the document keeps the change.
- **What `build` adds to the design**: a `carvera-cam` sketch (entry points, corner chains and tab spans), section sketches for shelves, planar patches for the facing (`carvera-cam faces`), the tabs as bodies (`carvera-cam tabs`), and a model of the lab's Saunders fixture and vise per setup, so that the simulation checks the tool against them. It redraws them on every build, and hides all but the part.
- **The machine and post** come from the Carvera Community's repository on GitHub. The first build after each new upstream machine uploads its 3D model, with the lab's bed recess, to a Fusion project named `carvera-cam` in the user's hub; say so the first time.
- **Which face is the top.** If `top` is not `+Z`, say which of the design's faces is machined first. `build` then machines a copy of the body, turned in place, in a `carvera-cam part` component, and hides the design's body. Pass `top` (`+Z`, `-Z`, `+X`, `-X`, `+Y` or `-Y`) to choose the face yourself, as for a part modelled lying on its side; give `stock` in the design's axes all the same. `build` likewise turns the copy a quarter about Z when stock fits the part along its X only, and reports `quarter_turned`.
- **Dimples.** A closed pocket's entry hole, and the pre-drill of a blind hole the end mill bores, leave a cone-shaped dimple in the floor, 1.59 mm across and 0.37 mm deep; a blind drilled hole leaves the drill's point below its bottom unless the design models it. `build` lists each. Say so, and go on unless the user objects.
- **Anything skipped or doubtful**, such as sharp inside corners, holes no tool fits, or more than six tools (the Carvera's automatic tool changer holds six; the seventh and later need a manual change).

### 4. Build

Always do steps 7 and 8 after this one, without asking, unless the user asks to skip them: every run of the skill checks the toolpaths, reviews them and compares the simulated stock with the part.
Build the part in the user's document:

```python
def run(_context: str):
    runpy.run_path(SCRIPTS + r'\carvera_cam.py')['build']({
        'stock': {'x': 50.8, 'y': 150, 'z': 6.35},
        'taps': {'1.623': 'M2'},
        'name': '<part name for the NC programs>',
    })
```

Other arguments: `body`, `sides` (1 or 2 setups; by default 2 when the bottom has features to cut, such as pocket floors, holes or the design's chamfers, or stock to face, else 1), `face` (face off the stock above and below the part, default true), `chamfer` (deburr the machined edges: the outer faces', the pocket floors' where walls fall away from them, and those of holes under counterbores; default true; the design's own chamfers are cut either way), `fixture` (model the vise, default true), `tab_width` (default 5 mm), `top` (the design's face to machine first; see "Which face is the top"), `tab_z` (the tab band's centre, in mm, along the design's axis that `top` names), and in `stock`, `y_mode` (default `back`), `y_offset` (default 10 mm) and `bottom`.
`bottom` is the stock left below the part, in mm; the default centres the part in the stock's thickness with two setups, and is 0 with one.
With two setups, it sets how long the top program runs before the operator flips the stock: more stock below the part moves facing time from the top program to the bottom one.
`build` prints the machining strategy (`strategy`: the number of `sides`, whether there is an `outline` cut with tabs, and whether the vise holds the part by its sides while `ends` cuts free its ends), whether it turned the part a quarter (`quarter_turned`), the tool slots, the operations, the tab layout, the number of pocket entry holes and any warnings; it takes about 10 seconds.
Call `build` and `generate` in separate MCP calls: a call that runs longer than about a minute times out.

### 5. Look at the part in Fusion

Look at the part with a screenshot (the MCP `read` tool), to see any feature `build` warned about, and the toolpaths of the operations in question.
After `build` the Manufacture workspace is active, so a script reaches the design with `doc.products.itemByProductType('DesignProductType')`, not `app.activeProduct`.
The default screenshot shows the whole fixture plate, with the part small; aim the camera at the part first:

```python
app = adsk.core.Application.get()
des = adsk.fusion.Design.cast(app.activeDocument.products.itemByProductType('DesignProductType'))
body = des.rootComponent.bRepBodies.itemByName('<the body's name>')
vp = app.activeViewport
c = vp.camera
bb = body.boundingBox
t = adsk.core.Point3D.create((bb.minPoint.x + bb.maxPoint.x) / 2, (bb.minPoint.y + bb.maxPoint.y) / 2, bb.maxPoint.z)
c.target, c.eye, c.upVector = t, adsk.core.Point3D.create(t.x, t.y, t.z + 20), adsk.core.Vector3D.create(0, 1, 0)
c.isFitView = False
c.viewExtents = 1.2 * max(bb.maxPoint.x - bb.minPoint.x, bb.maxPoint.y - bb.minPoint.y)  # cm
vp.camera = c
```

When `build` turned the part over, the body is the one in the `carvera-cam part` component.
To show an operation's toolpath in the screenshot, select it: `app.userInterface.activeSelections.add(op)`.

### 6. Generate the toolpaths

```python
def run(_context: str):
    runpy.run_path(SCRIPTS + r'\carvera_cam.py')['generate']({'budget': 50})
```

If it prints `"status": "running"`, call it again; it resumes where it stopped.
`{'limit': 8}` starts at most 8 operations per call, which keeps each call short on a large part.
It prints `"status": "partial"`, with the operations not yet generated under `pending`, until every operation has a toolpath.
When done, it reports every operation. If Fusion discards thread-mill passes, it lowers that operation's pass count below the one `build` computed from `threads.toml` and lists the change under `adjusted`; report any such change to the user.
Any operation with `"toolpath": false`, `hasError` or `hasWarning` needs attention before the program is used.

### 7. Check and review

Always do this step, without asking, unless the user asks to skip it.

1. Run the toolpath check (see "Checking existing toolpaths").
2. Review the toolpaths as [references/review.md](references/review.md) describes: judge whether each of the checker's findings is real, and look at what no rule checks, the run order, the times, the screenshots and the machine's travel.

Where a check finds a problem, fix the skill (see "Improving the skill"), then build the part again.
To compare a build with an earlier one, as after a change to the skill, `check_cam.py`'s `summary({})` lists every operation's tool, time, cut length, deepest Z, extent and plunge points; run it on both and compare operation by operation.

### 8. Simulate in Fusion

Always do this step, without asking, unless the user asks to skip it.
In the user's document, once its toolpaths pass both checks, follow [references/simulation.md](references/simulation.md): simulate each setup on its own with the machine and read the issues Fusion flags, then simulate all the setups together, save the stock they leave, and compare it with the part (that simulation's issues are ignored: with several setups, Fusion's collision checks are wrong).
Every issue, and every difference between the stock and the part that is not by design (that table lists those), needs attention before the program is used: compare it with the two checks' findings, and fix the toolpath or the skill.
This step needs Windows, where `sim_ui.ps1` runs; elsewhere, ask the user to run Fusion's simulation with collision checking on each setup.

### 9. Report

Report to the user, plainly:

- the tool list by slot (T1, T2, …), and which slots need a manual tool change;
- the NC programs and their order on the machine: run `_top`; with two setups, flip the part and run `_bottom`; in window machining, hot-glue the part to the stock round it, then run `_tabs` last, which cuts the four tabs and frees the part (in Fusion they are named `<part>_top (carvera-cam)` and so on; the files they write are `<part>_top.nc` and so on);
- the total machining time, and that aluminum runs the same programs at 200% feed and spindle override;
- every warning from `build`, `generate` and the check, and every feature the generator did not machine;
- what the simulation flagged in each setup, and where the simulated stock differs from the part beyond the differences by design;
- any change you made to the skill.

Post-process (write the G-code) only when the user asks.

## Improving the skill

When a check shows the toolpaths falling short, fix the skill, not just this part:

- **The generator was wrong:** fix `scripts/carvera_cam.py`, or the convention in [references/conventions.md](references/conventions.md) it follows.
- **The checker missed something, or flagged something wrong:** fix the rule in `scripts/check_cam.py` and its row in [references/rules.md](references/rules.md).
- **The written review missed something:** add it to [references/review.md](references/review.md).
- **The simulation found something both checks missed:** add the rule or the review item that catches it, as [references/simulation.md](references/simulation.md) says.

Then build, generate and check this part again, and the other parts the change touches, to show that the fix works and changes nothing else.
Keep each fix small and about one thing, and never weaken a rule to make a finding go away.

Offer each improvement to the skill's repository, [SchusterLab/fusion-carvera-cam](https://github.com/SchusterLab/fusion-carvera-cam), as a pull request, so that every user gets it.
Ask the user before opening one: it publishes the change.
If the skill's folder is in a clone of that repository, make a branch there; otherwise clone the repository into a scratch folder and make the same change there.
Commit with a message that says what was wrong, the evidence, and the fix, and open the pull request with `gh pr create`.
An installed plugin's folder is overwritten when the plugin updates, so a fix made only there does not last.

## Checking existing toolpaths

`scripts/check_cam.py` checks the toolpaths of the active document against the lab's rules for avoiding crashes, spindle stalls and tool wear, which [references/rules.md](references/rules.md) lists with their thresholds and evidence.
It works on any milling setups: ones the lab programmed by hand, ones `build` made, and either kind after someone has edited them.
For an edited toolpath, rules G1, U1 and TB1 compare every setup's cuts with the part's model, so they catch an edit that cuts into the part, leaves a face uncut or oversize, or cuts a tab before the tab program.
Open the document (Workflow, step 1), then run:

```python
def run(_context: str):
    runpy.run_path(SCRIPTS + r'\check_cam.py')['check']({})
```

`{'setups': ['Setup1']}` checks only the named setups, and then skips G1, U1 and TB1, which need every setup's cuts.
The check takes a few seconds per setup, plus a few seconds at the end to compare the cuts with the part's faces (`(part surfaces)`); if it prints `"status": "running"`, call it again, and it goes on with the steps in `pending`.
It prints every finding with its rule id, its severity (`error` or `warning`), the setup, the operation and a message.
To read each setup's moves, it adds a temporary NC program to the document and deletes it again, so the document then counts as changed: close it without saving unless the user asks otherwise.

Report the errors first, grouped by rule, then the warnings, with the operations each applies to and what rules.md says each guards against.
Errors P1, P2 and P3 mean the toolpaths need regenerating before any other finding can be trusted.
Then, unless the user asks to skip it, check the toolpaths by Fusion's simulation as well ([references/simulation.md](references/simulation.md)), which also works on any milling setups.

## Working with Fusion

**Dialogs.**
- While the user has a command dialog open, such as Sketch or Extrude, `execute` refuses any script that is not read-only: "Cannot perform 'script' while a command dialog is open".
- A read-only script still runs, but only `analyze` is one. `build`, `generate` and `check` all change the document, `check` by adding a temporary NC program.
- Ask the user to close the dialog, and wait for them to say so.
- A modal window, such as Recovered Documents after a crash, blocks every call, read-only ones included: each one times out. Ask the user to close it.

**Long calls.**
- A call that runs past about a minute times out, but its script goes on running in Fusion, and the next call waits behind it. Send one call at a time, and after a timeout, wait for the script to finish before sending another.
- A call that keeps Fusion busy much longer can crash it. One call that ran `build` and then `generate` on the sample puck did both. Keep `build`, `generate` and `check` in separate calls, and let `generate` resume over several.
- Don't make Fusion regenerate one operation over and over in a call, as when trying a parameter at several values: one that regenerated three chamfers at nine tip offsets each kept Fusion busy for 45 minutes, until it was stopped. Bound every wait for a toolpath, as `generate` does, and try a few values per call.
- A call that may run past a minute, such as `build` on a large part, should write what it prints to a file, with `faulthandler` on, so that a crash still leaves the Python line it crashed at:

  ```python
  import faulthandler, io, contextlib
  LOG = r'<scratch folder>\build'

  def run(_context: str):
      faulthandler.enable(open(LOG + '_fault.txt', 'w'), all_threads=True)
      out = io.StringIO()
      with contextlib.redirect_stdout(out):
          runpy.run_path(SCRIPTS + r'\carvera_cam.py')['build']({...})
      open(LOG + '.txt', 'w', encoding='utf-8').write(out.getvalue())
      faulthandler.disable()
  ```

  Then wait for `build.txt` outside Fusion, with a shell loop that also stops when Fusion is gone (`tasklist | grep -i fusion360`).

**A failed call.**
- When a script fails, Fusion undoes what it did. A script that made the document's CAM product and then failed, such as a `build` that stopped on the part's shape, or a call that ran `build` and then failed in code of its own, leaves the document with no working workspace: every later `build` in it fails with "InternalValidationError : pCurrentSession". Close the document without saving and make a new copy.
- `build` checks the stock against the part before it makes the CAM product, so a stock it refuses does not do this.

**A crash or a hang.**
`scripts/fusion_restart.ps1` (Windows only) tells what state Fusion is in, and starts it again.
1. When calls keep timing out, or the file a long call writes never appears, run `powershell -ExecutionPolicy Bypass -File fusion_restart.ps1 -Status` twice, a few seconds apart:
   - Fusion runs and its CPU seconds climb: it is busy, so wait. A window that does not respond only means that a script is still running.
   - Fusion runs, but its CPU seconds hardly move for minutes: it hangs. Stopping it loses every unsaved document apart from its crash recovery, so ask the user first, unless only throwaway copies are open.
   - Fusion is gone, or `error_dialog` is true (the "Fusion Error Report" window, `cer_dialog.exe`): it crashed. Unsaved documents are lost apart from their crash recovery.
2. Restart it with `fusion_restart.ps1 -Throwaway <creationId>,<creationId>`, adding `-Stop` for a Fusion that hangs. It closes the error report, clears the crash-recovery entries of the throwaway copies you name, by the `creationId` you noted when you made each, starts Fusion from its Start-menu shortcut, and waits until it is ready. Otherwise the Recovered Documents window opens on start and blocks every call.
   - It lists every other recovery entry under `kept`, and leaves it. Such an entry may hold the user's own unsaved work: ask the user what to do with it before the next call, since its window blocks every call until someone closes it.
   - The entries are in `%LOCALAPPDATA%\Autodesk\Autodesk Fusion 360\<user id>\CrashRecovery`. `AutoSavedFileList.xml` has one element per document, with its `Name`, its `DocGuid` (the document's `creationId`) and its `RecoveryPath`, a `.backup` file. The documents open in a running Fusion have entries too (`Fusion_PID` is that process's id), and stopping Fusion turns them into recoveries.
3. Open the part again, make a new throwaway copy where one was lost, and go on from the step that crashed.
4. If it was a call that crashed, read the `faulthandler` file it wrote: its first lines name the Python line Fusion crashed at. A crash that comes back at the same line is a bug in the skill: fix it (see "Improving the skill"). It may follow an update: Fusion updates itself when it starts, and the version number (`app.version`) and the `webdeploy` folder in the trace change. Fusion 2706.1.14 crashed with an access violation where `build` set a height's reference geometry, or an open chain of sketch curves, on an operation's input; `build` now sets those on the operation once it is made. To find such a line, log each operation's name to a file before `Builder.make` makes it, and apply a doubtful selection to a throwaway operation one variant at a time, logging each before Fusion runs it.

**API facts the skill relies on.**
- A Part design holds only one component, so `build` switches it to Hybrid (`design.designIntent`).
- Each setup shows or hides the machine through `setup.visibilityManager` (`machineVisible`, `machineBaseVisible`); hidden, it stays the setup's machine.
- A new 2D pocket keeps a 0.2 mm `maximumStepdown` even with the tool's stepdown in use; set it to `tool_stepdown`.
- A 2D pocket's rest machining (`useRestMachining`) goes by an earlier tool's diameter, not by the stock earlier operations left. It cannot skip stock that is already gone.
- A 2D pocket's stock contours (`useStockContours` and the `stockContours` closed chains) limit it to where stock is left; a closed chain inside another one takes its area out. They work with the `predrill` ramp.
- An operation's feed height defaults to 5 mm above its top height (`feedHeight_offset`), and the tool feeds down from there at the plunge rate. `build` sets 1 mm. Fusion raises the feed height to the ramp clearance height (`rampClearanceHeight`, 2.5 mm by default) plus the vertical lead-in radius, so `build` sets the ramp clearance to 0.5 mm.
- A script cannot read a pattern feature's `inputEntities`: it fails with "Didn't roll editing feature back". The skill matches patterned faces by radius instead.
- `HoleFeature` has no `isTapped`; use `holeTapType`.

## Limits

How each feature is cut is in [references/conventions.md](references/conventions.md). The skill leaves these undone or imperfect, and `build` warns about each one it meets:

- **The machining axis** must be one of the design's axes: the top setup looks down at the face `top` names, and the bottom setup at the opposite face.
- **Features it does not cut:** holes whose axis does not run along Z; holes closed at both ends, or that no tool in the library fits; openings through the part that are not open all the way through over their whole outline; pockets with no room even for the 1 mm end mill's entry (0.55 mm round the centre), whose faces rule U1 then reports; sloped faces other than the design's 45° chamfers and countersinks up to 1.4375 mm wide, and chamfers on the bottom face with one setup.
- **Inside corners** finish at the radius of the smallest end mill that fits: R0.5 with the 1 mm end mill.
- **Burrs near walls:** the chamfering bit's body keeps it 1.59 mm, its radius, from a wall rising beside an edge, so the last 1.59 mm of an edge that runs into such a wall keep their burr.
- **Pockets open to the side:** a pass `build` cannot make is left out; tell the user where copper stays, as the simulated stock shows. In stock of the part's own size, `build` refuses a pocket open to a side the vise grips.
- **The stock:** stock of the part's own size needs a part whose outline is that rectangle, and stock that fits along one side a part longer than the jaws' 95.5 mm the other way, with straight sides where they grip. The tabs follow the largest outer loop of any level face, so a part wider than that, such as one whose widest part is a sloped wall, gets a warning.
- **What nothing checks but the simulation:** the stock against the machine's frame. The stock's X size runs along the machine's Y axis, which the bed moves on, so a cut near one end of a long bar moves its other end far out: end cuts 100 mm from a 300 mm bar's middle brought its far end into the frame (`Base+Stock` in the simulation).
- **What nothing checks at all:** that the toolpaths stay within the machine's travel, since the machine model sets no limits on its axes; the review compares each setup's span with the travel by eye.

## Re-learning the conventions

`scripts/dump_cam.py` dumps every CAM setup of a Fusion project to JSON (one document per few seconds; call `dump` repeatedly until it prints `ALL DONE`).
Use it when the lab's practice changes, compare its output with [references/conventions.md](references/conventions.md), and update `carvera_cam.py` to match.
To rebuild the tool library, [assets/carvera-tools.json](assets/carvera-tools.json), run `python -I scripts/tool_library.py` outside Fusion; it fetches Makera's tool files again and remakes the drills from McMaster-Carr's table (see [references/conventions.md](references/conventions.md), "Machine, post and material").
When the lab's practice changes, update the thresholds at the top of `check_cam.py`, the evidence in [references/rules.md](references/rules.md) and the written review in [references/review.md](references/review.md) to match.
`scripts/make_test_part.py` builds test parts in new, unsaved documents, for testing changes: `make({})` a 30 × 20 × 4 mm part with one of each feature (`{"angled_notch": true}` adds a notch open to the side with slanted walls, `{"pocket_hole": true}` a hole through the top pocket's floor, and `{"side_slot": false}` leaves out the slot open to the side), `make_open_pockets({})` a 40 × 30 × 4 mm plate with three pockets open to the side, one with a wall 10° off the outline (`{"lip": true}` adds one whose wall meets the outline at 12° with a sliver of the part outside it, and `{"island": true}` a pin in another's floor), and `make_open_floor({})` a 30 × 20 × 3 mm plate whose top floor runs out to every side round a boss and a pin, to build in stock larger than the part.

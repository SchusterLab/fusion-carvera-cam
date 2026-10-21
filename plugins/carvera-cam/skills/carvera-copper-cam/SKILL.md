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

**Window machining** cuts the part out of larger stock.
An outline cut, one end mill (3.175 mm) wide, runs round the part and leaves four tabs holding it to the stock, and the last program, `_tabs`, cuts them and frees the part.
Before `_tabs` runs, the operator hot-glues the part to the stock round it, so that the freed part stays put instead of being thrown by the end mill.
The stock must reach at least 6.175 mm past the part on every side: the outline cut, plus a 3 mm frame of stock round it that holds the part through the tabs and takes the vise's grip.
With two setups, the tabs sit at mid-thickness, and the bottom setup cuts them from below.
With one, the tabs sit on the part's bottom face, and the top setup cuts them.

**Stock that fits the part along one side** holds the part by those two sides in the jaws, which grip them over the jaws' 95.5 mm width.
The part must be longer than that the other way, so that the cuts that free its ends run outside the jaws.
Those end cuts come first, through the whole stock, each starting and ending in the air beyond the gripped sides; the offcuts beyond them fall away, and there are no tabs.
The jaws close along the setups' X axis, the design's Y, so when the stock fits along the design's X, `build` machines a copy of the part turned a quarter about Z.
The part's sides must be straight wherever the jaws reach.

**Stock of the part's own size** needs no outline cut and no tabs: the vise holds the part itself.
The part's outline must then be the rectangle the stock is; a part with rounded or notched outer corners needs larger stock.

**One setup** is enough when nothing opens at the bottom face (no pocket floor facing down, no hole open only at the bottom): through holes and windows are cut from the top.
The part's bottom face is then the stock's bottom, left as it is, and its edges, and the holes and windows where they come out at the bottom, are not deburred.
Likewise, where the stock fits the part and its face is not faced, the edges on the stock's own sides are not deburred: no tool cuts them.
Stock thicker than the part is faced on top only.
Pass `"sides": 2` to machine the bottom face anyway, for example to face it or to deburr its edges.
When every feature opens at the design's bottom face, the part is turned over and machined from that face, in one setup.

A face with no stock above it, in stock as thick as the part, is not faced.

**The tools stay out of the vise.**
The part may be thinner than the jaws' grip, as long as no tool reaches the jaws.
Each jaw also has a ledge under the stock along its gripped side, 1.9 mm wide, so a cut through the stock's bottom there would cut into the jaw.
`build` tests where each planned cut reaches (the facing, the outline cut, the end cuts, the passes in pockets open to the side, the entry holes, the windows, the through holes and the deburr) against the modelled vise, and stops with an error naming the cut and where it would hit.
The check's rule X1 then tests the G-code itself, every 0.5 mm along each move.

## What the generator does

`scripts/carvera_cam.py` runs inside Fusion's Python; its functions each take one dict of arguments:

| Entry point | What it does |
|---|---|
| `analyze(args)` | Recognizes the part's holes, tapped holes, pocket floors, through windows and faces, and prints them as JSON. Changes nothing. |
| `build(args)` | Deletes what an earlier build made (setups ending in `(carvera-cam)`, and the `carvera-cam` sketches and planes), then creates the setups, tools, operations and NC programs. Does not compute toolpaths. |
| `generate(args)` | Computes the toolpaths within a time budget and reports each operation's errors, warnings and machining time. |
| `features`, `walls`, `pass_path`, `coverage`, `tools`, `add_operation`, `add_contour`, `renumber_tools` | The primitives the recipes use; each recipe says which it needs and how to call them. |

`scripts/check_cam.py` has `check(args)`, the toolpath check, and `summary(args)`, which lists each operation's tool, time, cut length, depth, extent and plunge points from the G-code, for comparing two builds.

`scripts/sim_check.py` and `scripts/sim_ui.ps1` check the toolpaths by Fusion's own simulation: they run it on each setup, read the collisions and other issues Fusion flags, save the stock it leaves, and compare that stock with the part (see [references/simulation.md](references/simulation.md)).

The whole part can be programmed two ways: by `build` itself, the script's way, or by the recipes, written methods in [references/recipes/](references/recipes/) that you carry out with smaller functions in the same script, the primitives, such as `features`, `add_operation` and `pass_path`.
[part.md](references/recipes/part.md) makes every operation, in run order, from the lab's conventions; it hands pockets open to the side to [open-pocket.md](references/recipes/open-pocket.md) and tight inside corners to [corners.md](references/recipes/corners.md), features whose shapes vary too much for one fixed method.
Each recipe says when it applies, how to cut the feature, and which shapes it does not cover.
The toolpaths are likewise checked two ways, by `scripts/check_cam.py` and by reading them as [references/review.md](references/review.md) describes, and then by Fusion's simulation, which shows whether the stock it leaves is the part.
By default the skill does all of these, compares the results, and improves whichever falls short (see "Improving the skill").

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

- **Stock.** Ask whether the stock has the same X and Y size as the part, and whether it has the same thickness, and, for each that differs, the stock's dimensions. The answers and `analyze`'s features decide the machining strategy (see "Machining strategy"): tell the user which one `build` will use, the setups and programs it makes, and, with one setup, that the bottom face stays as the stock left it and is not deburred. For stock of the part's size along a side, pass the part's `size_mm` value for that side, as `analyze` printed it, in the design's axes. The lab's usual stock is a 2″ × 150 mm bar (50.8 × 150 mm) of 6.35 mm (0.25″) or 19 mm thickness, or a 150 × 150 mm copper plate about 6.2 mm thick. Larger stock holds the part 10 mm from its back edge along Y, and centred along X; with `y_mode` `center` it is centred along Y too, which suits a short bar. The stock is at least as thick as the part, and stock above and below the part is faced off. With two setups the part sits, by default, centred in the stock's thickness, and moving it up or down (`bottom`, below) shifts facing time between the top and bottom programs, so the user can choose when to come back to flip the stock. With one setup it sits on the stock's bottom.
- **Taps.** Lab parts mark tapped holes with Fusion's Hole command, Tapped type, threads not modeled, which leaves a plain hole at the minor diameter. `build` reads that thread data itself. For a plain hole with a `tap_candidate` (one whose `thread` is empty), ask whether it is tapped, and pass the answer as `taps`, keyed by the diameter exactly as `analyze` printed it, for example `{"1.623": "M2"}`. Supported threads: M2, M2.5, M3 and M4; their pre-drilling and thread-milling settings are in [references/threads.toml](references/threads.toml).
- **Tab height**, in window machining, if `analyze` moved `tab_z` (or, with one setup, `tab_z_one_sided`) because a pocket opens to the side; the user may prefer another height, passed as `tab_z`.
- **A Part design becomes a Hybrid one.** A Part design holds only one component, and `build` adds several, so it switches a Part design to Hybrid, which keeps its modelling, and warns. Saving the document keeps the change.
- **The design gets a sketch and the vise.** `build` adds a sketch named `carvera-cam` (entry points, corner chains and tab spans) to the design, plus a section sketch and construction plane per shelf height, the way the lab sketches these by hand. It adds a planar patch at its top and at its bottom, filling the part's outline and one end mill diameter (plus 0.05 mm) past it, in a `carvera-cam faces` component, for the facing to select, so that it faces over the openings in the faces and over the outline cut too. The finishing face selects a second patch at each face that stops at the outline, since the outline cut has cleared the stock past it by then. The `carvera-cam faces` component is hidden; the facing selects its patches all the same. With larger stock, it models the four tabs as bodies in a `carvera-cam tabs` component, and the cuts around them take their heights from the tabs' top and bottom faces. It also adds a copy of the lab's fixture per setup, `carvera-cam vise top` and `carvera-cam vise bottom`, so that simulation checks the tool against the vise and the machine. The fixture is the Saunders Machine Works fixture plate and vise, downloaded from their site; it leaves out the 4th-axis module, so simulation does not check that. It redraws all of these on every build. The sketches, the construction planes, both vises and the machine start hidden, out of the way of the part; the setups still carry the vise and the machine for simulation.
- **The machine and post.** `build` fetches the latest Carvera 3-axis machine and Carvera post from the Carvera Community's profiles repository on GitHub. The first build after each new upstream machine uploads the machine's 3D model, with the lab's bed recess for the fixture plate, to a Fusion project named `carvera-cam` in the user's hub (see [references/conventions.md](references/conventions.md)); say so the first time.
- **The stock across the vise.** The jaws grip the stock's Y edges, so the vise opens to the stock's Y size; it opens to about 185 mm. The jaws close along the Carvera's X axis, so both setups' X axis is the part's Y axis, and the G-code is in that frame; the origin is the stock top's corner at the part's +X and +Y.
- **Which face is the top.** If `top` is not `+Z`, say which of the design's faces is machined first: with `turned_over`, its bottom face. `build` then machines a copy of the body turned in place, in a `carvera-cam part` component, and hides the design's body behind it; the next build shows it again. Pass `top` to choose the face yourself, such as a side of a part whose machined faces are modelled facing sideways: `+Z`, `-Z`, `+X`, `-X`, `+Y` or `-Y`. With a side up, give `stock` in the design's axes as always, so the stock's thickness is its size along that side's axis; `stock`'s `y_mode`, `y_offset` and `bottom` keep their meaning as the part is machined. `tab_z` is along the design's axis that `top` names, Z by default. Likewise, when stock fits the part along the turned part's X only, `build` turns the copy a quarter about Z and reports `quarter_turned`; say so.
- **Dimples in pocket floors.** Each closed pocket's entry hole is drilled at the pocket's centre where the end mill fits there, else at the point farthest from its walls, with the drill's point only as far below its floor as the end mill needs: the end mill enters copper only down a hole at least half its width, the drill's cone included (rule TH4), so the point stops where the cone is half the end mill wide at the floor. That leaves the smallest dimple the rule allows, a cone 1.59 mm across and 0.37 mm deep in the floor. `build` lists where they are. Blind drilled holes are drilled the same way, so the drill's point goes its length below the hole's bottom; `build` warns about those too, unless the hole has a modelled point that matches the drill's angle, and asks the user to model it. Say so, and go on unless the user objects.
- **Anything skipped or doubtful**, such as sharp inside corners, holes no tool fits, or more than six tools (the Carvera's automatic tool changer holds six; the seventh and later need a manual change).

### 4. Build both ways

Build the part twice: the script's way in the user's document, and the recipes' way in a fresh copy of the part.

```python
def run(_context: str):
    runpy.run_path(SCRIPTS + r'\carvera_cam.py')['build']({
        'stock': {'x': 50.8, 'y': 150, 'z': 6.35},
        'taps': {'1.623': 'M2'},
        'name': '<part name for the NC programs>',
    })
```

Other arguments: `body`, `sides` (1 or 2 setups; by default 2 when the bottom has features to cut, such as pocket floors, holes or the design's chamfers, or stock to face, else 1), `face` (face off the stock above and below the part, default true), `chamfer` (deburr the machined edges: the outer faces', the pocket floors' where walls fall away from them, and those of holes under counterbores; default true; the design's own chamfers are cut either way), `fixture` (model the vise, default true), `tab_width` (default 5 mm), `top` (the design's face to machine first; see "Which face is the top"), `tab_z` (the tab band's centre, in mm, along the design's axis that `top` names), `mode` (`"script"`, the default, or `"recipe"` for the recipes' way, below), and in `stock`, `y_mode` (default `back`), `y_offset` (default 10 mm) and `bottom`.
`bottom` is the stock left below the part, in mm; the default centres the part in the stock's thickness with two setups, and is 0 with one.
With two setups, it sets how long the top program runs before the operator flips the stock: more stock below the part moves facing time from the top program to the bottom one.
`build` prints the machining strategy (`strategy`: the number of `sides`, whether there is an `outline` cut with tabs, and whether the vise holds the part by its sides while `ends` cuts free its ends), whether it turned the part a quarter (`quarter_turned`), the tool slots, the operations, the tab layout, the number of pocket entry holes and any warnings; it takes about 10 seconds.
In recipe mode it prints the strategy, whether it turned the part, the setups' names, the machine and post, and the warnings, and `next`, what to call next.
Call `build` and `generate` in separate MCP calls: a call that runs longer than about a minute times out.

For the recipes' way, copy the part's body into a new, unsaved document and build there with `"mode": "recipe"` and the same other arguments.
The copy keeps the shape but not the Hole features that mark tapped holes, so pass every tapped hole `analyze` found in the user's document under `taps`.
Both documents are named "Untitled" when the part is unsaved, so tell them apart by `doc.creationId`: note each one's when you make it, and bring a document forward with `doc.activate()` before each call, since every function works on the active document.

```python
import adsk.core, adsk.fusion

def run(_context: str):
    app = adsk.core.Application.get()
    print('user document', app.activeDocument.creationId)
    src = adsk.fusion.Design.cast(app.activeDocument.products.itemByProductType('DesignProductType'))
    body = adsk.fusion.TemporaryBRepManager.get().copy(<the part's body in src>)
    doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    des = adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType'))
    des.designType = adsk.fusion.DesignTypes.ParametricDesignType
    root = des.rootComponent
    bf = root.features.baseFeatures.add()
    bf.startEdit()
    root.bRepBodies.add(body, bf)
    bf.finishEdit()
    root.bRepBodies.item(0).name = '<the body's name>'
    print('copy', doc.creationId)
```

The copy is a throwaway: never save it.
`build` with `"mode": "recipe"` makes the setups, the layout sketch, the tabs, the face patches and the vise, and no operations.

### 5. Follow the recipes

In the copy, follow [part.md](references/recipes/part.md): it reads the part with `features({})` and adds every operation in run order, with [open-pocket.md](references/recipes/open-pocket.md) and [corners.md](references/recipes/corners.md) where it sends you.
Look at the feature in Fusion first (a screenshot with the MCP `read` tool), since a recipe lists the shapes it does not cover.
When a feature fits none of them, leave it out and tell the user.

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

In each document:

```python
def run(_context: str):
    runpy.run_path(SCRIPTS + r'\carvera_cam.py')['generate']({'budget': 50})
```

If it prints `"status": "running"`, call it again; it resumes where it stopped.
`{'limit': 8}` starts at most 8 operations per call, which keeps each call short on a large part.
It prints `"status": "partial"`, with the operations not yet generated under `pending`, until every operation has a toolpath.
When done, it reports every operation. If Fusion discards thread-mill passes, it lowers that operation's pass count below the one `build` computed from `threads.toml` and lists the change under `adjusted`; report any such change to the user.
Any operation with `"toolpath": false`, `hasError` or `hasWarning` needs attention before the program is used.

### 7. Check both ways and compare

In each document:

1. Read the toolpaths as [references/review.md](references/review.md) describes, and write down what you find, before you run the checker.
2. Run the toolpath check (see "Checking existing toolpaths").
3. Compare the checker's findings with yours, as review.md's "Comparing with the checker" says.

Then compare the two documents.
`check_cam.py`'s `summary({})` lists every operation's tool, time, cut length, deepest Z, extent and plunge points; run it in each document and compare operation by operation.
Where the two builds agree, and both pass both checks, the result stands.
Where they differ, decide which is better from the evidence: the plunge points against what has cleared the stock, `coverage` for pockets cut by passes, the times, and screenshots of the operations that differ.
The better one is the one that cuts every feature fully, never plunges into uncut copper, keeps the part held and clear of the vise, and, those being equal, takes less time.
When the evidence does not settle it, for example a faster toolpath against a better finish, show the user the two and ask.
Then improve whichever way fell short (see "Improving the skill"), and leave the better toolpaths in the user's document: if the recipes' way won, build the user's document again with `"mode": "recipe"` and follow the recipes there too.

In window machining, take a screenshot with the outline operation selected and check the tabs and the stock position; with stock that fits along one side, select the `Ends` operation instead and check that the end cuts run outside the jaws; to check the vise as well, show `carvera-cam vise top` for the screenshot and hide it again after.

### 8. Simulate in Fusion

Always do this step, without asking, unless the user asks to skip it.
In the user's document, once its toolpaths are the ones that stand, follow [references/simulation.md](references/simulation.md): simulate each setup on its own with the machine, read the issues Fusion flags, save the stock each setup leaves, and compare the stock with the part.
Every issue, and every difference between the stock and the part that is not by design (that table lists those), needs attention before the program is used: compare it with the two checks' findings, and fix the toolpath or the skill.
This step needs Windows, where `sim_ui.ps1` runs; elsewhere, ask the user to run Fusion's simulation with collision checking on each setup.

### 9. Report

Report to the user, plainly:

- the tool list by slot (T1, T2, …), and which slots need a manual tool change;
- the NC programs and their order on the machine: run `_top`; with two setups, flip the part and run `_bottom`; in window machining, hot-glue the part to the stock round it, then run `_tabs` last, which cuts the four tabs and frees the part (in Fusion they are named `<part>_top (carvera-cam)` and so on; the files they write are `<part>_top.nc` and so on);
- the total machining time, and that aluminum runs the same programs at 200% feed and spindle override;
- every warning from `build`, `generate` and the check, and every feature the generator did not machine;
- what the simulation flagged in each setup, and where the simulated stock differs from the part beyond the differences by design;
- how the two ways compared, and any change you made to the skill.

Post-process (write the G-code) only when the user asks.

## Improving the skill

When the comparison shows one way falling short, fix the skill, not just this part:

- **The script's way was worse:** fix `scripts/carvera_cam.py`, or the convention in [references/conventions.md](references/conventions.md) it follows.
- **The recipes' way was worse:** fix the recipe's text, or the primitive it calls.
- **The checker missed something, or flagged something wrong:** fix the rule in `scripts/check_cam.py` and its row in [references/rules.md](references/rules.md).
- **The written review missed something:** add it to [references/review.md](references/review.md).
- **The simulation found something both checks missed:** add the rule or the review item that catches it, as [references/simulation.md](references/simulation.md) says.

Then build, generate and check this part both ways again, to show that the fix works and changes nothing else.
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
- A call that runs past about a minute times out, and one that keeps Fusion busy much longer can crash it. One call that ran `build` and then `generate` on the sample puck did both.
- Keep `build`, `generate` and `check` in separate calls, and let `generate` resume over several.

**A failed call.**
- When a script fails, Fusion undoes what it did. A script that made the document's CAM product and then failed, such as a `build` that stopped on the part's shape, or a call that ran `build` and then failed in code of its own, leaves the document with no working workspace: every later `build` in it fails with "InternalValidationError : pCurrentSession". Close the document without saving and make a new copy.
- `build` checks the stock against the part before it makes the CAM product, so a stock it refuses does not do this.

**After a crash.**
1. Fusion's window is gone and only a "Fusion Error Report" window is left (`cer_dialog.exe`). Close it; stopping the process is fine.
2. Unsaved documents are lost apart from Fusion's crash recovery. That includes a throwaway copy, so make a new copy for the recipes' way.
3. Before starting Fusion again, clear the recovery entries of throwaway documents, so the Recovered Documents window does not block the next calls (user, 2026-10-17):
   - The entries are in `%LOCALAPPDATA%\Autodesk\Autodesk Fusion 360\<user id>\CrashRecovery`. `AutoSavedFileList.xml` has one element per document, with its `Name`, its `DocGuid` (the document's `creationId`) and its `RecoveryPath`, a `.backup` file.
   - With Fusion stopped, delete each throwaway's element and its `.backup` file. Match a throwaway by the `creationId` you noted when you made it.
   - The documents open in a running Fusion have entries too (`Fusion_PID` is that process's id), and stopping Fusion turns them into recoveries. Clear an entry for the user's own document only if it holds no unsaved work of theirs, for example when it was opened just for this skill and only the skill changed it. Otherwise ask the user first.
4. Start Fusion from its Start-menu shortcut, `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Autodesk\Autodesk Fusion.lnk`. A `FusionLauncher.exe` under `%LOCALAPPDATA%\Autodesk\webdeploy\production` may belong to an older version, and fails with "Configuration file … is missing or incomplete".
5. Wait until the window title no longer starts with "Initializing" before calling the MCP tools.

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

- The part's machining axis must be one of the design's axes: the top setup looks down at the face `top` names, the design's top face by default or its bottom face when the part is turned over, and the bottom setup at the opposite face.
- A closed pocket less than 3.275 mm wide is too narrow for a pocket toolpath to enter, since Fusion refuses an entry with less room round the 3.175 mm end mill; a 2D contour cuts it instead, entering through the pocket's entry hole. A straight slot as wide as a small end mill and open at one end is cut along its centreline from the open end, and a pocket reached only through it is milled by that end mill entering at the slot's closed end.
  A closed pocket with no room anywhere for the 3.175 mm end mill's entry hole is pocketed by a small end mill instead, entering down a hole drilled as wide as itself; the hole must clear the part all the way up, past the walls of the pockets above that it passes through.
  A pocket with no room even for the 1 mm end mill's entry (0.55 mm round its centre), or a shelf region with no room for the 3.175 mm end mill's, gets no pocket, and `build` warns; the small end mills' corner passes cut only what they reach of it, so check `check_cam.py`'s rule U1 for faces left uncut.
- The tabs follow the part's outline: the largest outer loop of any level face, usually the top or bottom face's, but a floor's on a plate whose top and bottom faces are small pads standing proud of it.
  A part wider than every level face's outer loop, such as one whose widest part is a sloped wall, gets a warning.
- Stock of the part's own X and Y size works only for a part whose outline is that rectangle, and stock that fits along one side only for a part longer than the jaws' 95.5 mm width the other way, with straight sides where the jaws grip.
- A pocket open to the side is cut by contour passes along its walls, each starting and ending in the outline cut beside it, or, in stock of the part's size, beside the part, where there is no stock. A floor open on every side, such as the floor round a boss, is cut by passes round its islands instead, and a pass that closes on itself enters through a pocket entry hole, which leaves a dimple in the floor. In stock of the part's own size, `build` refuses a pocket open to a side the vise grips, where the passes would run into the jaws. A wall that runs within a few degrees of the outline, so that a pass along it never crosses the outline cut, and an island in a floor that also has walls along its sides are not cut; [references/recipes/open-pocket.md](references/recipes/open-pocket.md) says what to tell the user. The chamfering bit's body keeps it 1.59 mm, its radius, from a wall rising beside an edge, so the last 1.59 mm of an edge that runs into such a wall, as where a pocket's wall meets the part's side, keep their burr.
  An island is any inner loop of the floor with a wall rising from it, such as a pad that a slot cuts through, and the passes keep the tool off its whole loop.
  A floor that no wall rises from, such as a ring between a groove and the outline, has its passes run out from its inner loops.
- A pocket's inside corners finish at the radius of the smallest end mill that fits: 1 mm diameter leaves R0.5 in a sharp corner.
- A through window's inside corners below R1.59 finish the same way, with a small end mill from the face through the part.
- An opening through the part that is not open all the way through over its whole outline gets no window cut, and `build` warns.
  Examples are a window ringed by pockets that stop at a floor, and one that a tongue juts into partway up.
  The pockets on each side cut what they reach of it, so check rule U1 and the simulation for copper left in it.
- A round wall is a hole when it goes all the way round at one of its ends, as one circular edge or as arcs where several faces meet that end, such as a hole whose bottom opens into sloped walls; a round wall that a slot opens all the way down is the slot's rounded end, not a hole.
- A hole closed at both ends, or one no tool in the library fits, is skipped with a warning.
- A round wall with something standing up from the floor inside it, such as a groove round a boss, is a pocket with an island, not a hole.
- A hole whose axis does not run along Z, such as a hole into the part's side, is not cut; `build` warns about each one.
- The only sloped faces cut are the design's 45° chamfers and countersinks (`analyze`'s `chamfers`), by the chamfering bit, whose 45° cone reaches 1.5375 mm out from the edge of its 0.1 mm point. Its point goes 0.1 to 0.5 mm below the chamfer, as far as the chamfer's width and the floors and tabs below allow, so a chamfer can be at most 1.4375 mm wide. The bit cuts it 0.2 mm deeper at each pass, along the sharp edge `build` sketches for it, and the last pass follows the modelled chamfer.
  Any other sloped face, such as a chamfer at another angle, a wider one, or an 82° countersink, is left square, and `build` warns about it; so is a chamfer on the bottom face with one setup.
- Nothing checks that a toolpath stays within the machine's travel: the machine model sets no limits on its axes, so the simulation reports none, and `check_cam.py` has no rule for travel.
- Nor do `build` and `check_cam.py` check the stock against the machine's frame; only the simulation does.
  The stock's X size runs along the machine's Y axis, the axis the bed moves on, so a cut near one end of a long bar moves its other end far out: on a 200 mm part in a 300 mm bar, the end cuts 100 mm from the bar's middle brought the far end into the frame (`Base+Stock` in the simulation), while a 146 mm part in a 305 mm bar, cut only out to 73 mm, cleared it.

## Re-learning the conventions

`scripts/dump_cam.py` dumps every CAM setup of a Fusion project to JSON (one document per few seconds; call `dump` repeatedly until it prints `ALL DONE`).
Use it when the lab's practice changes, compare its output with [references/conventions.md](references/conventions.md), and update `carvera_cam.py` and the recipes in [references/recipes/](references/recipes/) to match, so that both ways of cutting a feature keep agreeing.
To rebuild the tool library, [assets/carvera-tools.json](assets/carvera-tools.json), run `python -I scripts/tool_library.py` outside Fusion; it fetches Makera's tool files again and remakes the drills from McMaster-Carr's table (see [references/conventions.md](references/conventions.md), "Machine, post and material").
When the lab's practice changes, update the thresholds at the top of `check_cam.py`, the evidence in [references/rules.md](references/rules.md) and the written review in [references/review.md](references/review.md) to match.
`scripts/make_test_part.py` builds test parts in new, unsaved documents, for testing changes: `make({})` a 30 × 20 × 4 mm part with one of each feature (`{"angled_notch": true}` adds a notch open to the side with slanted walls, `{"pocket_hole": true}` a hole through the top pocket's floor, and `{"side_slot": false}` leaves out the slot open to the side), `make_open_pockets({})` a 40 × 30 × 4 mm plate with three pockets open to the side, among them one the open-pocket recipe cannot fully cut, and `make_open_floor({})` a 30 × 20 × 3 mm plate whose top floor runs out to every side round a boss and a pin, to build in stock larger than the part.

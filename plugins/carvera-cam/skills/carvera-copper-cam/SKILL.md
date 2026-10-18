---
name: carvera-copper-cam
description: Generate CAM toolpaths in Autodesk Fusion for machining a copper part on SchusterLab's Makera Carvera, following the lab's conventions (Makera and McMaster-Carr tools with the "Copper" preset, the Carvera Community machine and post, top and flipped bottom setups, pre-drilled entry holes, four holding tabs cut by a separate program), and check existing Carvera toolpaths for crash, spindle-stall and tool-wear risks against the lab's rules. Use when the user asks to program, CAM, toolpath or machine a part on the Carvera or Makera, in copper or aluminum, or to check, review or verify Carvera toolpaths. Needs the Fusion MCP server.
---

# Carvera copper toolpaths

This skill turns a Fusion design of a 2.5-axis part into a ready-to-post Carvera program, in the lab's style.
It makes two setups (top, then the part flipped), every operation with tools from Makera and McMaster-Carr and the lab's feeds, and three NC programs: `<part>_top`, `<part>_bottom` and `<part>_tabs`.
The same programs machine aluminum: run them with the Carvera's feed and spindle overrides at 200%.
It also checks existing toolpaths, the lab's own as well as those it generates, against the lab's rules for avoiding crashes, spindle stalls and tool wear (see "Checking existing toolpaths").

A 2.5-axis part is one whose features are all reached straight down the Z axis from the top or from the bottom: pockets, holes, outlines and through windows.
The skill does not cut side features, undercuts or 3D surfaces.

## What the generator does

`scripts/carvera_cam.py` runs inside Fusion's Python; its functions each take one dict of arguments:

| Entry point | What it does |
|---|---|
| `analyze(args)` | Recognizes the part's holes, tapped holes, pocket floors, through windows and faces, and prints them as JSON. Changes nothing. |
| `build(args)` | Deletes what an earlier build made (setups ending in `(carvera-cam)`, and the `carvera-cam` sketches and planes), then creates the setups, tools, operations and NC programs. Does not compute toolpaths. |
| `generate(args)` | Computes the toolpaths within a time budget and reports each operation's errors, warnings and machining time. |
| `recipes`, `walls`, `pass_path`, `coverage`, `tools`, `add_operation`, `add_contour`, `renumber_tools` | The primitives the recipes use; each recipe says which it needs and how to call them. |

`scripts/check_cam.py` has `check(args)`, the toolpath check, and `summary(args)`, which lists each operation's tool, time, cut length, depth, extent and plunge points from the G-code, for comparing two builds.

Some features can be cut two ways: by `build` itself, the script's way, or by a recipe, a written method in [references/recipes/](references/recipes/) that you carry out with smaller functions in the same script, the primitives, such as `walls`, `pass_path` and `add_operation`.
A recipe covers a feature whose shapes vary too much for one fixed method; each says when it applies, how to cut the feature, and which shapes it does not cover.
The toolpaths are likewise checked two ways: by `scripts/check_cam.py`, and by reading them as [references/review.md](references/review.md) describes.
By default the skill does both, compares the results, and improves whichever falls short (see "Improving the skill").

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

The JSON lists, among other things:

- `size_mm`: the part's bounding box, with Z its thickness;
- `holes`: groups of holes by diameter, side (`top`, `bottom`, or `none` when closed at both ends), through or blind, and `thread`, the thread read from a tapped Hole feature;
- `tap_candidate`: the hole's thread: the one its tapped Hole feature names, or, for a plain hole, a thread whose minor diameter matches it;
- `floors`: pocket floors and the two outer faces, each with its smallest inside-corner radius (0 for a sharp corner, null when the walls have no inside corners), `side_open` for a pocket open to the side of the part, and `shelf` for a floor that steps down into a deeper pocket;
- `windows`: through openings that are not round holes;
- `tab_z`: the height of the tab band's centre that `build` will use: halfway between the part's top and bottom, unless a pocket open to the side is at that height and within an end mill's width of a tab.
- `turned_over` and `features_per_face`: the face with more features (pocket floors and blind holes; through holes and windows count for neither face) is machined first, as the top. When the design's bottom face has more, `turned_over` is true and the rest of the JSON describes the part turned over about its X axis: `top` is the design's bottom face, and y and z are mirrored about the part's centre.

### 3. Agree the plan with the user

Before building, confirm with the user, briefly and in one message:

- **Stock.** The lab's usual stock is a 2″ × 150 mm bar (50.8 × 150 mm) of 6.35 mm (0.25″) or 19 mm thickness, or a 150 × 150 mm copper plate about 6.2 mm thick. The part sits 10 mm from the back of the stock along Y and, by default, centred in its thickness. The stock must be thicker than the part, and the margins above and below the part are faced off. Moving the part up or down in the stock (`bottom`, below) shifts facing time between the top and bottom programs, so the user can choose when to come back to flip the stock.
- **Taps.** Lab parts mark tapped holes with Fusion's Hole command, Tapped type, threads not modeled, which leaves a plain hole at the minor diameter. `build` reads that thread data itself. For a plain hole with a `tap_candidate` (one whose `thread` is empty), ask whether it is tapped, and pass the answer as `taps`, keyed by the diameter exactly as `analyze` printed it, for example `{"1.623": "M2"}`. Supported threads: M2, M2.5, M3 and M4; their pre-drilling and thread-milling settings are in [references/threads.toml](references/threads.toml).
- **Tab height**, if `analyze` moved `tab_z` off the halfway plane because a pocket opens to the side; the user may prefer another height, passed as `tab_z`.
- **A Part design becomes a Hybrid one.** A Part design holds only one component, and `build` adds several, so it switches a Part design to Hybrid, which keeps its modelling, and warns. Saving the document keeps the change.
- **The design gets a sketch and the vise.** `build` adds a sketch named `carvera-cam` (entry points, corner chains and tab spans) to the design, plus a section sketch and construction plane per shelf height, the way the lab sketches these by hand. It adds a planar patch at its top and at its bottom, filling the part's outline and one end mill diameter (plus 0.05 mm) past it, in a `carvera-cam faces` component, for the facing to select, so that it faces over the openings in the faces and over the outline cut too. The finishing face selects a second patch at each face that stops at the outline, since the outline cut has cleared the stock past it by then. The `carvera-cam faces` component is hidden; the facing selects its patches all the same. It models the four tabs as bodies in a `carvera-cam tabs` component, and the cuts around them take their heights from the tabs' top and bottom faces. It also adds two copies of the lab's fixture, `carvera-cam vise top` and `carvera-cam vise bottom` (the second hidden), so that simulation checks the tool against the vise and the machine. The fixture is the Saunders Machine Works fixture plate and vise, downloaded from their site; it leaves out the 4th-axis module, so simulation does not check that. It redraws all of these on every build.
- **The machine and post.** `build` fetches the latest Carvera 3-axis machine and Carvera post from the Carvera Community's profiles repository on GitHub. The first build after each new upstream machine uploads the machine's 3D model, with the lab's bed recess for the fixture plate, to a Fusion project named `carvera-cam` in the user's hub (see [references/conventions.md](references/conventions.md)); say so the first time.
- **The stock across the vise.** The jaws grip the stock's Y edges, so the vise opens to the stock's Y size; it opens to about 185 mm. The jaws close along the Carvera's X axis, so both setups' X axis is the part's Y axis, and the G-code is in that frame; the origin is the stock top's corner at the part's +X and +Y.
- **Which face is the top.** If `turned_over` is true, say that the design's bottom face is machined first. `build` then machines a copy of the body turned over in place, in a `carvera-cam part` component, and hides the design's body behind it; the next build shows it again. Pass `flip` (true or false) to choose the face yourself. `tab_z` is always in the design's Z.
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

Other arguments: `body`, `face` (face the stock off both sides, default true), `chamfer` (deburr the outer faces' edges, default true), `fixture` (model the vise, default true), `tab_width` (default 5 mm), `tab_z` (model Z of the tab band's centre, in mm), `recipes` (default false), and in `stock`, `y_mode` (default `back`), `y_offset` (default 10 mm) and `bottom`.
`bottom` is the stock left below the part, in mm; the default centres the part in the stock's thickness.
It sets how long the top program runs before the operator flips the stock: more stock below the part moves facing time from the top program to the bottom one.
`build` prints the tool slots, the operations, the tab layout, the number of pocket entry holes and any warnings; it takes about 10 seconds.
Call `build` and `generate` in separate MCP calls: a call that runs longer than about a minute times out.

For the recipes' way, copy the part's body into a new, unsaved document and build there with `"recipes": true` and the same other arguments.
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
`build` with `"recipes": true` cuts everything else as usual and lists the features it left to a recipe under `recipes`.

### 5. Follow the recipes

In the copy, carry out each recipe `build` listed, in run order.
`recipes({})` reads the list back from the document in a later call.
For each kind of entry, read its recipe in [references/recipes/](references/recipes/) and carry it out: `"recipe": "open-pocket"` is [open-pocket.md](references/recipes/open-pocket.md), and `"recipe": "corners"` is [corners.md](references/recipes/corners.md).
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
Then improve whichever way fell short (see "Improving the skill"), and leave the better toolpaths in the user's document: if the recipes' way won, build the user's document again with `"recipes": true` and follow the recipes there too.

Take a screenshot with the outline operation selected and check the tabs, the stock position and the vise.
Ask the user to run Fusion's simulation with collision checking on each setup before machining; the API cannot run it.

### 8. Report

Report to the user, plainly:

- the tool list by slot (T1, T2, …), and which slots need a manual tool change;
- the NC programs and their order on the machine: run `_top`, flip the part, run `_bottom`, then run `_tabs`, which cuts the four tabs and frees the part (in Fusion they are named `<part>_top (carvera-cam)` and so on; the files they write are `<part>_top.nc` and so on);
- the total machining time, and that aluminum runs the same programs at 200% feed and spindle override;
- every warning from `build`, `generate` and the check, and every feature the generator did not machine;
- how the two ways compared, and any change you made to the skill.

Post-process (write the G-code) only when the user asks.

## Improving the skill

When the comparison shows one way falling short, fix the skill, not just this part:

- **The script's way was worse:** fix `scripts/carvera_cam.py`, or the convention in [references/conventions.md](references/conventions.md) it follows.
- **The recipes' way was worse:** fix the recipe's text, or the primitive it calls.
- **The checker missed something, or flagged something wrong:** fix the rule in `scripts/check_cam.py` and its row in [references/rules.md](references/rules.md).
- **The written review missed something:** add it to [references/review.md](references/review.md).

Then build, generate and check this part both ways again, to show that the fix works and changes nothing else.
Keep each fix small and about one thing, and never weaken a rule to make a finding go away.

Offer each improvement to the skill's repository, [SchusterLab/fusion-carvera-cam](https://github.com/SchusterLab/fusion-carvera-cam), as a pull request, so that every user gets it.
Ask the user before opening one: it publishes the change.
If the skill's folder is in a clone of that repository, make a branch there; otherwise clone the repository into a scratch folder and make the same change there.
Commit with a message that says what was wrong, the evidence, and the fix, and open the pull request with `gh pr create`.
An installed plugin's folder is overwritten when the plugin updates, so a fix made only there does not last.

## Checking existing toolpaths

`scripts/check_cam.py` checks the toolpaths of the active document against the lab's rules for avoiding crashes, spindle stalls and tool wear, which [references/rules.md](references/rules.md) lists with their thresholds and evidence.
It works on any milling setups, including ones the lab programmed by hand.
Open the document (Workflow, step 1), then run:

```python
def run(_context: str):
    runpy.run_path(SCRIPTS + r'\check_cam.py')['check']({})
```

`{'setups': ['Setup1']}` checks only the named setups.
The check takes a few seconds per setup; if it prints `"status": "running"`, call it again, and it goes on with the setups in `pending`.
It prints every finding with its rule id, its severity (`error` or `warning`), the setup, the operation and a message.
To read each setup's moves, it adds a temporary NC program to the document and deletes it again, so the document then counts as changed: close it without saving unless the user asks otherwise.

Report the errors first, grouped by rule, then the warnings, with the operations each applies to and what rules.md says each guards against.
Errors P1, P2 and P3 mean the toolpaths need regenerating before any other finding can be trusted.
The check does not replace Fusion's simulation with collision checking, which the user still runs on each setup.

## Limits

- The part's machining axis must be the design's Z axis; the top setup looks down −Z at the design's top face, or at its bottom face when the part is turned over.
- A closed pocket less than 3.275 mm wide is too narrow for a pocket toolpath to enter, since Fusion refuses an entry with less room round the 3.175 mm end mill; a 2D contour cuts it instead, entering through the pocket's entry hole. A pocket narrower than the end mill is cut only by the small end mills, rest machining. A straight slot as wide as a small end mill and open at one end is cut along its centreline from the open end, and a pocket reached only through it is milled by that end mill entering at the slot's closed end.
- The tabs follow the outline of the part's top or bottom face, whichever encloses more area; a part wider in the middle than at both faces gets a warning.
- A pocket open to the side is cut by contour passes along its walls, each starting and ending in the outline cut beside it. A wall that runs within a few degrees of the outline, so that a pass along it never crosses the outline cut, and an island in such a pocket's floor are not cut; [references/recipes/open-pocket.md](references/recipes/open-pocket.md) says what to tell the user.
- A pocket's inside corners finish at the radius of the smallest end mill that fits: 1 mm diameter leaves R0.5 in a sharp corner.
- A through window's inside corners below R1.59 finish the same way, with a small end mill from the face through the part.
- A hole closed at both ends, or one no tool in the library fits, is skipped with a warning.

## Re-learning the conventions

`scripts/dump_cam.py` dumps every CAM setup of a Fusion project to JSON (one document per few seconds; call `dump` repeatedly until it prints `ALL DONE`).
Use it when the lab's practice changes, compare its output with [references/conventions.md](references/conventions.md), and update `carvera_cam.py` and the recipes in [references/recipes/](references/recipes/) to match, so that both ways of cutting a feature keep agreeing.
To rebuild the tool library, [assets/carvera-tools.json](assets/carvera-tools.json), run `python -I scripts/tool_library.py` outside Fusion; it fetches Makera's tool files again and remakes the drills from McMaster-Carr's table (see [references/conventions.md](references/conventions.md), "Machine, post and material").
When the lab's practice changes, update the thresholds at the top of `check_cam.py`, the evidence in [references/rules.md](references/rules.md) and the written review in [references/review.md](references/review.md) to match.
`scripts/make_test_part.py` builds test parts in new, unsaved documents, for testing changes: `make({})` a 30 × 20 × 4 mm part with one of each feature (`{"angled_notch": true}` adds a notch open to the side with slanted walls), and `make_open_pockets({})` a 40 × 30 × 4 mm plate with three pockets open to the side, among them one the open-pocket recipe cannot fully cut.

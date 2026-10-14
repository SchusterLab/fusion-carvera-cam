---
name: carvera-copper-cam
description: Generate CAM toolpaths in Autodesk Fusion for machining a copper part on SchusterLab's Makera Carvera, following the lab's conventions (the lab's tools with the "Copper 101-H02" preset, the Carvera Community machine and post, top and flipped bottom setups, pre-drilled entry holes, four holding tabs cut by a separate program), and check existing Carvera toolpaths for crash, spindle-stall and tool-wear risks against the lab's rules. Use when the user asks to program, CAM, toolpath or machine a part on the Carvera or Makera, in copper or aluminum, or to check, review or verify Carvera toolpaths. Needs the Fusion MCP server.
---

# Carvera copper toolpaths

This skill turns a Fusion design of a 2.5-axis part into a ready-to-post Carvera program, in the lab's style.
It makes two setups (top, then the part flipped), every operation with the lab's tools and feeds, and three NC programs: `<part>_top`, `<part>_bottom` and `<part>_tabs`.
The same programs machine aluminum: run them with the Carvera's feed and spindle overrides at 200%.
It also checks existing toolpaths, the lab's own as well as those it generates, against the lab's rules for avoiding crashes, spindle stalls and tool wear (see "Checking existing toolpaths").

A 2.5-axis part is one whose features are all reached straight down the Z axis from the top or from the bottom: pockets, holes, outlines and through windows.
The skill does not cut side features, undercuts or 3D surfaces.

## What the generator does

`scripts/carvera_cam.py` runs inside Fusion's Python and has three entry points, each taking one dict of arguments:

| Entry point | What it does |
|---|---|
| `analyze(args)` | Recognizes the part's holes, tapped holes, pocket floors, through windows and faces, and prints them as JSON. Changes nothing. |
| `build(args)` | Deletes what an earlier build made (setups ending in `(carvera-cam)`, and the `carvera-cam` sketches and planes), then creates the setups, tools, operations and NC programs. Does not compute toolpaths. |
| `generate(args)` | Computes the toolpaths within a time budget and reports each operation's errors, warnings and machining time. |

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
- `tap_candidate`: a thread whose minor diameter matches a plain hole that carries no thread data;
- `floors`: pocket floors and the two outer faces, each with its smallest inside-corner radius (0 for a sharp corner, null when the walls have no inside corners), `side_open` for a pocket open to the side of the part, and `shelf` for a floor that steps down into a deeper pocket;
- `windows`: through openings that are not round holes;
- `tab_z`: the height of the tab band's centre that `build` will use: halfway between the part's top and bottom, unless a pocket open to the side is at that height.

### 3. Agree the plan with the user

Before building, confirm with the user, briefly and in one message:

- **Stock.** The lab's usual stock is a 2″ × 150 mm bar (50.8 × 150 mm) of 6.35 mm (0.25″) or 19 mm thickness, or a 150 × 150 mm copper plate about 6.2 mm thick. The part sits 10 mm from the back of the stock along Y and, by default, centred in its thickness. The stock must be thicker than the part, and the margins above and below the part are faced off. Moving the part up or down in the stock (`bottom`, below) shifts facing time between the top and bottom programs, so the user can choose when to come back to flip the stock.
- **Taps.** Lab parts mark tapped holes with Fusion's Hole command, Tapped type, threads not modeled, which leaves a plain hole at the minor diameter. `build` reads that thread data itself. For a hole with only a `tap_candidate`, ask whether it is tapped, and pass the answer as `taps`, keyed by the diameter exactly as `analyze` printed it, for example `{"1.623": "M2"}`. Supported threads: M2, M2.5, M3 and M4; their pre-drilling and thread-milling settings are in [references/threads.toml](references/threads.toml).
- **Tab height**, if `analyze` moved `tab_z` off the halfway plane because a pocket opens to the side; the user may prefer another height, passed as `tab_z`.
- **The design gets a sketch and the vise.** `build` adds a sketch named `carvera-cam` (entry points, corner chains and tab spans) to the design, plus a section sketch and construction plane per shelf height, the way the lab sketches these by hand. It also adds two copies of the lab's fixture, `carvera-cam vise top` and `carvera-cam vise bottom` (the second hidden), so that simulation checks the tool against the vise and the machine. The fixture is the Saunders Machine Works fixture plate and vise, downloaded from their site; it leaves out the 4th-axis module, so simulation does not check that. It redraws all of these on every build.
- **The machine and post.** `build` fetches the latest Carvera 3-axis machine and Carvera post from the Carvera Community's profiles repository on GitHub. The first build after each new upstream machine uploads the machine's 3D model, with the lab's bed recess for the fixture plate, to a Fusion project named `carvera-cam` in the user's hub (see [references/conventions.md](references/conventions.md)); say so the first time.
- **The stock across the vise.** The jaws grip the stock's Y edges, so the vise opens to the stock's Y size; it opens to about 185 mm. The jaws close along the Carvera's X axis, so both setups' X axis is the part's Y axis, and the G-code is in that frame; the origin is the stock top's corner at the part's +X and +Y.
- **Anything skipped or doubtful**, such as sharp inside corners, holes no tool fits, or more than six tools (the Carvera's automatic tool changer holds six; the seventh and later need a manual change).

### 4. Build

```python
def run(_context: str):
    runpy.run_path(SCRIPTS + r'\carvera_cam.py')['build']({
        'stock': {'x': 50.8, 'y': 150, 'z': 6.35},
        'taps': {'1.623': 'M2'},
        'name': '<part name for the NC programs>',
    })
```

Other arguments: `body`, `face` (face the stock off both sides, default true), `chamfer` (deburr the outer faces' edges, default true), `fixture` (model the vise, default true), `tab_width` (default 5 mm), `tab_z` (model Z of the tab band's centre, in mm), and in `stock`, `y_mode` (default `back`), `y_offset` (default 10 mm) and `bottom`.
`bottom` is the stock left below the part, in mm; the default centres the part in the stock's thickness.
It sets how long the top program runs before the operator flips the stock: more stock below the part moves facing time from the top program to the bottom one.
`build` prints the tool slots, the operations, the tab layout, the number of pocket entries and any warnings; it takes about 10 seconds.
Call `build` and `generate` in separate MCP calls: a call that runs longer than about a minute times out.

### 5. Generate the toolpaths

```python
def run(_context: str):
    runpy.run_path(SCRIPTS + r'\carvera_cam.py')['generate']({'budget': 50})
```

If it prints `"status": "running"`, call it again; it resumes where it stopped.
`{'limit': 8}` starts at most 8 operations per call, which keeps each call short on a large part.
It prints `"status": "partial"`, with the operations not yet generated under `pending`, until every operation has a toolpath.
When done, it reports every operation. If Fusion discards thread-mill passes, it lowers that operation's pass count below the one in `threads.toml` and lists the change under `adjusted`; report any such change to the user.
Any operation with `"toolpath": false`, `hasError` or `hasWarning` needs attention before the program is used.

### 6. Check the result

Run the toolpath check (see "Checking existing toolpaths") and fix or report every error it finds.
Take a screenshot with the Fusion MCP `read` tool (`queryType: "screenshot"`), with the outline operation selected, and check the tabs, the stock position and the vise.
Ask the user to run Fusion's simulation with collision checking on each setup before machining; the API cannot run it.
Report to the user, plainly:

- the tool list by slot (T1, T2, …), and which slots need a manual tool change;
- the NC programs and their order on the machine: run `_top`, flip the part, run `_bottom`, then run `_tabs`, which cuts the four tabs and frees the part;
- the total machining time, and that aluminum runs the same programs at 200% feed and spindle override;
- every warning from `build`, `generate` and the check, and every feature the generator did not machine.

Post-process (write the G-code) only when the user asks.

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

- The part's machining axis must be the design's Z axis, with the top setup looking down −Z.
- A pocket narrower than the 3.175 mm end mill gets no entry hole; the small end mills clear it by rest machining, plunging as the lab does.
- The tabs follow the outline of the part's top or bottom face, whichever encloses more area; a part wider in the middle than at both faces gets a warning.
- A pocket's inside corners finish at the radius of the smallest end mill that fits: 1 mm diameter leaves R0.5 in a sharp corner.
- A through window with inside corners below R1.59 keeps material in its corners; `build` warns about it.
- A hole closed at both ends, or one no tool in the library fits, is skipped with a warning.

## Re-learning the conventions

`scripts/dump_cam.py` dumps every CAM setup of a Fusion project to JSON (one document per few seconds; call `dump` repeatedly until it prints `ALL DONE`).
Use it when the lab's practice changes, compare its output with [references/conventions.md](references/conventions.md), and update `carvera_cam.py` to match.
When the lab changes its tool library `Carvera Tools/My Tools` in Fusion, call `export_tools({})` in `carvera_cam.py` to rewrite the lab's tools in [assets/carvera-tools.json](assets/carvera-tools.json), the tools `build` uses; it leaves out the drills in `My Tools` and keeps the added tools: the McMaster-Carr drills, which are the library's only drills, and Makera's tools.
To rebuild the added tools, fetching Makera's again from the Carvera Community repository, run `python -I scripts/tool_library.py` outside Fusion (see [references/conventions.md](references/conventions.md), "Machine, post and material").
When the lab's practice changes, update the thresholds at the top of `check_cam.py` and the evidence in [references/rules.md](references/rules.md) to match.
`scripts/make_test_part.py` builds a 30 × 20 × 4 mm test part with one of each feature in a new, unsaved document, for testing changes.

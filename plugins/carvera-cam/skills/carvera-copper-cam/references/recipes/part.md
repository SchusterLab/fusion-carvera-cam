# Recipe: the whole part

Make every operation of the part by hand, one `add_operation` call each, so that the whole toolpath can be compared operation by operation with the one `build` makes itself.
`build({"mode": "recipe"})` makes everything round the operations: the setups, the layout sketch with its entry points and chains, the tabs, the face patches, the part's turned copy and the vise.
It adds no operations and no NC programs.
This recipe then adds the operations in the order they run, from the lab's conventions ([conventions.md](../conventions.md)), and hands two features to their own recipes: pockets open to the side ([open-pocket.md](open-pocket.md)) and inside corners tighter than the end mill ([corners.md](corners.md)).

The comparison tests the choice of operations and their settings: which tool cuts each feature, in what order, from which height to which, entering where.
It does not test the layout, which both ways share: where the entry points lie, where the tabs sit, and the chains along the outline.

## When it applies

Use it for the recipes' way of SKILL.md's "Build both ways", in the copy of the part.
Call `build` with the same arguments as the script's build, and with `"mode": "recipe"`.

## Steps

1. **Read the part.**
   `features({})` lists what this recipe works from (see "What features gives").
   Look at the part in Fusion too, with a screenshot aimed as SKILL.md's "Follow the recipes" shows, to see any pocket open to the side or tight corner the recipes for those would cut.
2. **Sort the holes** (see "Holes").
3. **Add the top setup's operations**, in the order of "The operations in order", each with `add_operation` (see "Calling add_operation").
   Each operation goes last in its setup, so add them in run order.
4. **Add the bottom setup's**, when `setups` has a bottom one, in the same order, with the differences "The bottom setup" lists.
5. **Renumber the tools**: `renumber_tools({})`.
6. **Generate, check and compare**, as SKILL.md says, against the script's build in the user's document.

## What features gives

- `setups`: the setups' names, `top` and, with two sides, `bottom`.
- `strategy`: `sides` (1 or 2), `outline` (window machining: an outline cut with four tabs) and `ends` (the vise holds the part by its sides, and an end cut frees its ends).
- `stock_above_mm`: the stock above the part's face each setup machines, the margin m below.
  The part's face is faced only when m is above 0 and `face` is true.
- `part`: `ztop`, `zbot` and `thickness_mm`, in the design's Z, in mm.
- `tab`: in window machining, the tab band: `top_z`, `bottom_z`, `overlap` (how far the cuts round the tabs go past the band) and the modelled tab faces, `tab:top` and `tab:bottom`.
- `layout`: the main entry point (`entry`), the corner entries (`corner:<k>`; `corner:0` is the main entry's chain), the number of corner, tab and end chains, the shelf `regions` and the `slots`.
- `floors`: every pocket floor and both outer faces, with `side`, `role` (`pocket` or `face`), `depth` below that side's face, `corner_radius`, `side_open`, `shelf`, `falls_away` (a wall falls away from the floor somewhere: at the part's sides, into a deeper pocket, or round a hole or window through it), and what build made for it: `entry` (its entry point, whether it is `drilled`, and the hole's depth), `narrow` (too little room round the end mill for a pocket to enter), `region` (the shelf region it is pocketed in), `inside_region` (the deepest other shelf region it lies inside, whose pocket clears it down to that region's floor), `slot` and `fed_by_slot`.
- `holes`: each hole on its own, with `dia_mm`, `depth_mm`, `side` (`top`, `bottom`, or `none` when closed at both ends), `through`, `thread`, `point_angle` (a modelled drill point), `opens_in` (`face`, the index of the pocket floor its rim lies in, or `other`), `hole_below` (the narrower hole that goes on down from its floor, for a counterbore) and `centre` (its centre point).
- `windows`: each through opening that is not a round hole, with `z` (where it opens), `corner_radius`, `entry` (its point, and `pocket_fits`: whether a pocket can enter there) and `starts_at_floor` (the shelf floor over it, which the pockets clear it down to).
- `library`: the drills (`dia_mm`, `flute_mm`, `point_mm`, how far the point reaches below the full diameter), the 3.175 mm end mill, the small end mills, the chamfering bit and the thread mills (`tool`, `dia_mm`).

## Names

`add_operation` takes the part's geometry by name.

**Heights**: a parameter `<top|bottom>Height_ref` set to `{"ref": "<name>"}`, with `<top|bottom>Height_mode` `'from point'` and an offset along the tool axis (positive is up in the setup):
`face:<top|bottom>`, `floor:<i>`, `hole_bottom:<i>` (a blind hole's bottom face), `window_top:<j>` (the face a window opens in), `patch:<top|bottom>` and `patch:<top|bottom> finish` (the face patches), `tab:<top|bottom>`.

**What it cuts**, in `select`:
- `pockets`, `contours` and `stock_contours`, lists of: `floor:<i>`, `floor_outline:<i>` (the floor's outer loop, the tool inside), `floor_inner:<i>` (its inner loops), `hole_floor_inner:<i>` (the inner loops of a counterbore's floor), `face:<top|bottom>`, `face_deburr:<top|bottom>` (the edges of the face a deburr takes; see Deburr), `floor_deburr:<i>` (the edges where walls fall away from floor i), `patch:<name>`, `patch_outline:<top|bottom>` (a patch's outer edge as a closed boundary), `region:<k>`, `window:<j>`, `hole_rim:<i>`, `chains:<corner|tabs|ends>`, `slot:<k>`, `silhouette`, `path:<id>`.
- `holes`: hole indices as integers (`[2, 3]`), for a drill, bore or thread mill, which then keeps its default hole mode.
- `points` (a drill's points), `predrill` (a pocket's entry holes) and `entries` (a contour's entry points): point names, `entry`, `corner:<k>`, `pocket:<floor>`, `window:<j>`, `hole:<i>`, `slot_end:<k>`.

## Heights

Every height comes from geometry, and every offset is one of the lab's fixed numbers: the 0.2 mm the rough face and rough pockets leave, the 0.5 mm past a face a cut breaks through, or a thread's offsets from [threads.toml](../threads.toml).
The exception is a cut deeper than T1 reaches, which goes down in stages measured from the stock top (see "Deep cuts").

**The stock's highest point, H.**
Each setup keeps track of where the stock is highest: the stock top until the rough face, then 0.2 mm above the part's face until the finishing face, then the face.
Without facing (m = 0, or `face` false) it is the face throughout.
An operation that starts at the face starts at H instead, so its first pass cuts stock and no air:
- before the rough face: `topHeight_mode` `'from stock top'`, offset `0mm`;
- after it: `topHeight_mode` `'from point'`, `topHeight_ref` `{"ref": "patch:<side>"}`, offset `0.2mm` until the finishing face, `0mm` after it;
- without facing: the same patch, offset `0mm` throughout (build makes the patches whether or not the face is faced).
This is "top at H" below.

**Hole tops.** Drills, bores and thread mills that start at a hole's top keep their default `'from hole top'` and take `topHeight_offset` 0.2 mm while the face carries its allowance, else 0 mm.

**Through the part.** A cut through the part ends 0.5 mm past the far face: `bottomHeight_mode` `'from point'`, `bottomHeight_ref` `{"ref": "face:<far side>"}`, offset `-0.5mm`.

**Below the stock top.** A height d mm below the part's face, without geometry there, is `'from stock top'` with offset −(m + d) mm.

## Holes

Sort each hole of the setup's side (`side` `top` in the top setup, `bottom` in the bottom one; `none` is skipped, with a warning):

1. **Tapped**: `thread` is set (M2, M2.5, M3 or M4).
   Its pre-drill and thread mill are in [threads.toml](../threads.toml); M4 has no matching drill, so it is pre-drilled by T1 and bored to size.
2. **Drilled**: a library drill within −0.03 to +0.05 mm of the hole's diameter whose flute is at least 0.5 mm longer than the hole's depth; the closest in diameter.
3. **Bored by the 3.175 mm end mill**: wider than 3.275 mm.
   Its radial stock r = (diameter − 3.175) / 2.
   When r is above 0.6 mm (two 0.3 mm passes), and the hole is no deeper than T1 reaches, it is roughed by a pocket instead of a bore ("hole pocket"), unless its rim has no flat face round it, or a blind hole no flat bottom, for the pocket to select (`opens_in` null, or `has_bottom_face` false): then it is bored.
4. **Bored by a small end mill**: the largest small end mill whose radius is at most the hole's radius less 0.05 mm and whose flute is at least 0.5 mm longer than the hole.
   With none, skip the hole and warn.

T1, the 3.175 mm drill, reaches 6.9 mm (its 7.4 mm flute less 0.5 mm) into the material it starts in.

## Values and names

Parameters take Fusion expressions as strings: a length with its unit (`'0.2mm'`), an enumeration in single quotes inside the string (`"'from point'"`), a flag as `'true'` or `'false'`, a count as `'2'`.
`add_operation` puts the lab's settings for each strategy under them (see the end of "The operations in order"), so leave out what this recipe does not name.
A tool is named by its description as `tools({})` or features' `library` gives it; the start of a description is enough when only one tool begins with it, as features' truncated `Chamfering Bit - 1/8`.
An operation's diameter goes in its name with two decimals (`Bore 4.20`, `Hole pocket 7.00`), an M4 tap hole's with three (`Bore M4 3.332`), a drill's as the drill's own description begins (`Drill 2.7`; T1's is `Drill 3.175`, whatever the hole).
Operations of the same name, such as one `Window` per window, take Fusion's suffixes (`Window (2)`, …) in the order they are added.

## The operations in order

The tools: T1 the 3.175 mm drill (`3.175*7.4mm Drill (1/8")`), T2 the roughing end mill and T3 the finishing one (both `Spiral O Metal 3.175*12mm`; pass `"T": 2` or `"T": 3`), the rest by their description.
Every operation goes in the setup's NC program (`"program": "top"` or `"bottom"`), except the tab cut (`"tabs"`).
Each name below is the operation's name; the multi-depth ones (`doMultipleDepths` `true`) are the rough passes, and each finishing pass runs in one pass at full depth (`doMultipleDepths` `false`).

### Before the end mill

1. **Entry hole** (window machining only): T1, `drill`, `holeMode` `'selection-points'`, `points` `[entry]`, top at H, `drillTipThroughBottom` `true`.
   It goes through the whole stock when T1 reaches that far: `bottomHeight_mode` `'from stock bottom'`, offset `0mm`, `breakThroughDepth` `0.5mm`.
   Otherwise see "Deep cuts".
2. **Ends** (when `ends`): T2, `contour2d`, multi-depth, `contours` `[chains:ends]`, no entry points, top `'from stock top'` 0 mm, bottom `'from stock bottom'` −0.5 mm.
   It frees the part's ends through the whole stock, starting and ending in the air past the gripped sides.
3. **Face** (when the face is faced): T2, `pocket2d`, multi-depth, `pockets` `[patch_outline:<side>]`, `predrill` `[entry]`, top at H (the stock top), `bottomHeight_mode` `'from contour'`, and the 0.2 mm allowance: `useStockToLeave` `true`, `simpleStockToLeave` `false`, `stockToLeave` `0mm`, `verticalStockToLeave` `0.2mm`.
   H is now 0.2 mm above the face.

### Rough passes

4. **Outline** (window machining): T2, `contour2d`, multi-depth, `contours` `[silhouette]`, `entries` `[entry]`, top at H, bottom at the top of the tab band: `bottomHeight_mode` `'from point'`, `bottomHeight_ref` `{"ref": "tab:top"}` (the bottom setup: `tab:bottom`), offset 0.
5. **Tab entry holes** (window machining, top setup, more than one corner chain): T1, `drill`, `holeMode` `'selection-points'`, `points` `[corner:1, corner:2, …]` (not `corner:0`, the main entry), `breakThroughDepth` `0mm`, `drillTipThroughBottom` `true`, top at `tab:top` (offset 0), bottom at `tab:bottom` with offset −`overlap`.
6. **Pocket entry holes <depth> mm**: T1, `drill`, `holeMode` `'selection-points'`, top at H, `breakThroughDepth` `0mm`, at the entry points of the floors whose `entry.drilled` is true, each point once, one operation per hole depth.
   A floor open on every side of the part, round islands, adds the entries of its closed passes (`path_entry:<id>`), at its depth: sketch its passes before this step, as [open-pocket.md](open-pocket.md)'s "A floor open on every side" says.
   A hole that ends at a floor of that depth (reference any one floor of that depth) stops where the drill's cone is half the end mill wide (1.5875 mm) at the floor: `bottomHeight_mode` `'from point'`, `bottomHeight_ref` that floor, offset −(T1's `point_mm` / 2) mm, `drillTipThroughBottom` `false`.
   That leaves a cone-shaped dimple 1.59 mm across in the floor; warn about each one.
7. **Window entry holes**: T1, `drill`, `holeMode` `'selection-points'`, `points` every window's entry, `breakThroughDepth` `0mm`, `drillTipThroughBottom` `true`, top at H, bottom through the part.
8. **Pre-drill bores**: T1 for the holes the 3.175 mm end mill bores or pockets and the M4 tap holes; for a hole a small end mill bores, the widest drill no wider than the hole less 0.03 mm that reaches its depth (`Pre-drill bores <drill diameter>`).
   `drill`, `holes` the holes, top at the hole top, the drilling cycle of threads.toml's `[drill]` (below), and:
   - a through hole: the default bottom (the hole's bottom), `breakThroughDepth` `0.5mm`, `drillTipThroughBottom` `true`;
   - a blind hole (` (blind)` added to the name): `breakThroughDepth` `0mm`, `drillTipThroughBottom` `false`;
   - a counterbore (a blind hole with a `hole_below`): the drill's point goes on below the floor until its cone meets the floor at the lower hole's edge, (lower hole's diameter / 2) / tan(65°) mm, or to the lower hole's bottom if it is blind and that comes first: `bottomHeight_offset` −that; with `drillTipThroughBottom` `true` instead when the lower hole is at least 3.175 mm wide.
   One operation per drill, through or blind, and bottom offset, with the holes of every diameter it pre-drills; T1's is named plain `Pre-drill bores`.
9. **Pockets**: T2, `pocket2d`, multi-depth, top at H, `rampType` `'predrill'` (`'plunge'` if no floor of the setup has an entry), `bottomHeight_mode` `'from contour'`, the 0.2 mm floor allowance as in Face, `pockets` the closed pocket floors (`floor:<i>`) and one `region:<k>` per shelf region, `predrill` their entry points.
   Leave out the narrow floors, those cut through a slot (`slot` or `fed_by_slot` set) and those open to the side.
   A region or floor inside a shallower shelf region (its `inside_region`) starts at that region's rough floor instead (top `'from point'` at that region's shelf floor, offset 0.2 mm): one operation per such starting height, named `Pockets from <depth> mm`.
   Pockets open to the side are cut by [open-pocket.md](open-pocket.md), in their place here.
10. **Bores**, for each group of holes the 3.175 mm end mill cuts (same diameter, same ends), in the order of the groups' first holes in `holes`:
    - a hole pocket, `Hole pocket <diameter>`: T2, `pocket2d`, multi-depth, the default `'predrill'` ramp, `pockets` `[hole_rim:<i>, …]`, `predrill` `[hole:<i>, …]`; top at H for a hole that opens in the face, else at the floor it opens in plus 0.2 mm; bottom through the part, or for a blind hole at `hole_bottom:<i>` plus 0.2 mm;
    - otherwise `Bore <diameter>`: T2, `bore`, `stepover` `0.3mm`, `numberOfStepovers` ⌈r / 0.3⌉ (at least 1), `doMultiplePasses` `true`; top at H for a hole that opens in the face, else `'from hole top'` with offset 0.2 mm; `bottomHeight_offset` `-0.5mm` through, `0.2mm` blind.
11. **Bore <thread> <diameter>** (M4 tap holes): the end mill and settings of threads.toml's `[M4.bore]`: `bore`, `stepover` and `numberOfStepovers` from it, `doMultiplePasses` `true`, top as for Bores, `bottomHeight_offset` its `through_bottom_offset` through, 0 blind.
12. **Narrow pocket**: T2, `contour2d`, multi-depth, `contours` `[floor_outline:<i>]`, `entries` `[pocket:<i>]`, top at H, bottom at the floor (offset 0).
13. **Window**, one per window: top at H when it opens in the face, at its `starts_at_floor` floor when it has one, else at `window_top:<j>`; bottom through the part.
    T2, multi-depth: a `pocket2d` (default `'predrill'` ramp) with `pockets` `[window:<j>]` and `predrill` `[window:<j>]` when `pocket_fits` (or it has no entry), else a `contour2d` with `contours` `[window:<j>]` and `entries` `[window:<j>]`.
14. **Outline between tabs** (window machining, top setup): T2, `contour2d`, multi-depth, `contours` `[chains:corner]`, no entry points (each chain starts beside its corner entry, which Tab entry holes and the main entry drilled), top at `tab:top` (offset 0), bottom at `tab:bottom` with offset −`overlap`.

### Finishing passes

T3, each in one pass, in this order, with the drills of step 16a among them:

15. **Ends (finish)**, as Ends.
16. **Outline (finish)**, as Outline.
16a. **Drill <diameter>** for the holes whose `opens_in` is a pocket floor: as in step 25, with `topHeight_offset` `0.2mm` (the floor still carries the rough pocket's allowance), so that the finishing pocket cleans up the burrs they leave on the floor.
17. **Pockets (finish)**: as Pockets, with top at H and no floor allowance; a shelf region that holds deeper ones is finished on its own (`Pockets (finish) at <depth> mm`) with `useStockContours` `true` and `stock_contours` that region less the deeper regions and floors inside it, so the tool runs over them only as far as its radius.
18. **Bore M4 <diameter> (finish)**, for blind M4 holes only (threads.toml's `blind_finish_pass`).
19. **Narrow pocket (finish)**, as Narrow pocket.
20. **Window (finish)**, one per window: a `contour2d`, `contours` `[window:<j>]`, with the entry and heights of its rough pass (the middle is cleared, so a contour along the walls finishes it).
21. **Outline between tabs (finish)**, as Outline between tabs.

### Small tools, holes and threads

22. **Slots <width>** and **Pockets through slots <width>**, for each slot width, that is, each slot end mill (see conventions.md's "Slots"): the slot's end mill, `contour2d`, multi-depth, `compensationType` `'off'`, `contours` every `slot:<k>` of that width, top at H, bottom at the first of those slots' floors; then, when those slots feed pockets, `pocket2d`, multi-depth, `pockets` the floors they feed, `predrill` the `slot_end:<k>` of each slot that feeds one, top at H, `bottomHeight_mode` `'from contour'`.
23. **Corners**: pockets and windows whose `corner_radius` is more than 0.001 mm below 1.5875 mm (an R1.5875 corner is the end mill's own), by [corners.md](corners.md), in their place here.
    The heights it needs: a pocket's corner cut starts at H, or, for a floor inside a shelf region, at that region's floor; its bottom is `'from contour'`; a window's runs from H through the part.
24. **Bore <diameter>** for the holes small end mills bore: that end mill, `bore`, `stepover` `0.2mm`, `numberOfStepovers` `2`, top at the hole top, `bottomHeight_offset` `-0.5mm` through, `0mm` blind.
25. **Drill <diameter>**, one per drill and through or blind (` (blind)`), for the holes not drilled before the finishing passes: the plain drilled holes first, then the tap drills of threads.toml.
    `drill`, `holes` (T1's with `"T": 1`), top at the hole top (so `topHeight_offset` 0.2 mm while the face carries its allowance, which it does until Face (finish)), the `[drill]` cycle, through holes `breakThroughDepth` `0.5mm` and `drillTipThroughBottom` `true`, blind holes `breakThroughDepth` `0mm` and `drillTipThroughBottom` `true` (full diameter to the bottom: the point goes its length deeper).
    Warn about each blind hole with no modelled point, or one whose `point_angle` differs from the drill's by more than 0.5°.
26. **Thread <size>**, one per size and through or blind: the size's thread mill, `thread`, `holes`, with
    `direction` `'climb'`, `threadPitch` the pitch, `pitchDiameterOffset`, `stepover` `0.1mm`, `useHelicalLeads` `false`, `doMultiplePasses` `true`,
    `numberOfStepovers` ⌊h / 0.1⌋ + 1 with h = (modelled hole + pitch diameter offset − the thread mill's diameter) / 2 (the thread mill's `dia_mm` in features' `library`),
    `topHeight_offset` threads.toml's `top_offset` plus 0.2 mm while the face carries its allowance,
    `bottomHeight_offset` its `through_bottom_offset` or `blind_bottom_offset`.
27. **Bore <diameter> (finish)**, for each group of Bores and hole pockets, in the same order: T3, `bore`, `doMultiplePasses` `false`, with `stepover` `0.3mm` and `numberOfStepovers` ⌈r / 0.3⌉ as for a rough bore (a hole pocket's finishing bore too); top at H for a hole that opens in the face, else `'from hole top'` with offset 0; `bottomHeight_offset` `-0.5mm` through, `0mm` blind.
28. **Face (finish)**: T3, `pocket2d` (default `'predrill'` ramp), one pass, `pockets` `[patch:<side> finish]`, `predrill` `[entry]`, top at H (0.2 mm above the face), `bottomHeight_mode` `'from contour'`, no allowance.
    H is now the face.

### Deburring and the tabs

29. **Deburr** (when `chamfer` and the setup's side has a flat face): the chamfering bit, `chamfer2d`, `contours` `[face_deburr:<side>, floor_deburr:<i>, …]`, `chamferTipOffset` t, and no heights: a chamfer sits at the edges it follows.
    `face_deburr` takes every edge of the face, but where the stock fits the part and the face is not faced, it leaves out the edges on the stock's own sides: no tool cuts them, so they have no burrs.
    `floor_deburr:<i>` takes the edges where walls fall away from floor i; add one for every other floor of the side whose `role` is `face`, such as the top of an island level with the face.
    The bit's point sits t below the 0.1 mm chamfer, and Fusion leaves out an edge where the point would go below a floor.
    Room: the smallest of the setup's pocket depths, its blind holes' depths, in window machining the depth below the part's face of the tab band's face on this setup's side (the band's top in the top setup, its bottom in the bottom one), and 0.7 mm.
    t = room − 0.2 mm, kept between 0.1 and 0.5 mm; warn when even 0.1 mm leaves the point within 0.02 mm of a floor or the tabs.
30. **Deburr pocket floors** (when `chamfer` and the setup's side has a flat face), for each depth of the side's pocket floors with `falls_away` set: the chamfering bit, `chamfer2d`, `contours` `[floor_deburr:<i>, …]` for those floors, `chamferTipOffset` t, no heights; named `Deburr pocket floors`, then ` <depth> mm` when those floors have more than one depth.
    These floors' edges fall away below the face, where step 29 does not reach: where a floor open to the side meets the part's sides, where a shelf steps down into a deeper pocket, and round a hole or window through a floor.
    t follows step 29's rule with the room measured from the floor down: the depths of the deeper pockets and blind holes less the floor's depth, in window machining the tab band's depth less the floor's depth, and 0.7 mm.
31. **Deburr counterbores** (when `chamfer`), for each hole with a `hole_below`: the chamfering bit, `chamfer2d`, `contours` `[hole_floor_inner:<i>]`, `chamferTipOffset` 0.5 mm when the lower hole is through, else its depth less 0.2 mm, kept between 0.1 and 0.5 mm; one operation per tip offset.
32. **Cut tabs** (window machining, in the last setup: the bottom one with two sides, the top with one): T2, `contour2d`, multi-depth, `maximumStepdown` `0.2mm`, `contours` `[chains:tabs]`, `"program": "tabs"`.
    Top setup: top at `tab:top` (offset 0), bottom at `tab:bottom` with offset −`overlap`.
    Bottom setup: top at `tab:bottom` (offset 0), bottom at `tab:top` with offset −`overlap`.

The drilling cycle, in every drill operation: `cycleType` `'chip-breaking'`, `peckingDepth` `0.1mm`, `accumulatedPeckingDepth` `1mm`, `chipBreakDistance` `0.1mm` (threads.toml's `[drill]`).

`add_operation` sets the rest of the lab's settings for each strategy: tolerances, stepover, compensation, the feed height 1 mm above the top height and the chamfer's width and leads.

## The bottom setup

The same operations for the bottom side's features, with these differences:
- no Entry hole when the top one goes through the stock, and no Ends, Tab entry holes or Outline between tabs;
- the Outline stops at `tab:bottom`;
- windows and through holes are not cut again: the top setup cut them through the part;
- Cut tabs runs here, last, when there are two setups.

## Deep cuts

A cut deeper than T1's 6.9 mm reach goes down in stages, each drilled as deep as T1 reaches and then milled down to there, before T1 drills on from the milled floor (conventions.md, "Entry holes").
Its stage heights are measured from the stock top (`'from stock top'`, offset −(m + d)), and each stage's name gives its depths, `Outline 0 to 6.9 mm`.
The parts the lab makes are mostly thinner than that; when one is not, follow conventions.md's staging and say which cuts are staged.

## Calling add_operation

Run it in Fusion like `build`, through a stub (SKILL.md, "Run the analysis"), with a helper that returns what each function prints:

```python
import io, json, contextlib, runpy

def call(fn, args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        runpy.run_path(SCRIPTS + r'\carvera_cam.py')[fn](args)
    return json.loads(buf.getvalue())
```

Every argument is in its docstring in `scripts/carvera_cam.py`.
This one adds the rough pockets of floors 0 and 1:

```python
call('add_operation', {
    'setup': 'top', 'program': 'top', 'strategy': 'pocket2d',
    'tool': 'Spiral O Metal 3.175*12mm', 'T': 2, 'name': 'Pockets',
    'params': {'doMultipleDepths': 'true', 'rampType': "'predrill'",
               'topHeight_mode': "'from point'", 'topHeight_ref': {'ref': 'patch:top'},
               'topHeight_offset': '0.2mm', 'bottomHeight_mode': "'from contour'",
               'useStockToLeave': 'true', 'simpleStockToLeave': 'false',
               'stockToLeave': '0mm', 'verticalStockToLeave': '0.2mm'},
    'select': {'pockets': ['floor:0', 'floor:1'], 'predrill': ['pocket:0', 'pocket:1']}})
```

A call adds one operation and takes about a second, so a part's operations fit in a few MCP calls of ten to fifteen each, inside the minute an MCP call may take.
It prints `rejected` when Fusion refused a point or face, which leaves that cut without its entry: fix the call before going on.

## Comparing

`check_cam.py`'s `summary({})` in each document gives every operation's tool, time, cut length, deepest Z, extent and plunges.
Pair the operations in run order, which is also the order of their names; where both ways made the same operations with the same settings, the summaries agree to the 0.001 mm and the second.
For each difference, decide which way is right, from conventions.md and the checks, and fix the recipe or the script (SKILL.md, "Improving the skill").

Report to the user, as SKILL.md's "Report" says, that in window machining the part must be hot-glued to the stock round it before the `_tabs` program runs, the warnings this recipe raises (the dimples the pocket entry holes leave, blind holes with no modelled point, holes skipped) and the check's: a tool past T6 needs a manual change (N3).

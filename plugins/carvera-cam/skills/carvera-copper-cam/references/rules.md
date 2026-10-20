# Toolpath checks

The `carvera-copper-cam` skill checks the toolpaths of an open Fusion document against the lab's rules for avoiding crashes, spindle stalls and tool wear on the Carvera, and lists each breach as a finding.
Ask Claude to check a document's Carvera toolpaths, or run `check({})` from `scripts/check_cam.py` inside Fusion as SKILL.md shows.
The check covers every milling setup in the document: setups the lab programmed by hand as well as those made by `build`, the skill's toolpath generator, and those toolpaths after someone has edited them.
Its written counterpart, [review.md](review.md), judges the same toolpaths by reading their G-code and screenshots; the skill runs both and compares them, so that each catches what the other misses.

Each finding gives a rule id from the tables below, a severity, the setup, the operation and a message with the numbers behind it; in short, for example:
`H5 error | Setup1 | Drill3 | cuts 9.50 mm deep with a 7.40 mm flute`.
An **error** means a likely crash, broken tool or spoiled part: fix it before cutting.
A **warning** means a setting outside the lab's usual practice: change it, or keep it if there is a reason, such as a tool or part the lab has proven in that setting.

## Where the rules come from

The rules and their thresholds come from the lab's own toolpaths: 1004 operations in 88 setups of the 49 part documents in the Fusion project `2026-01-06_smpm-sample-package-6port`, made from January to October 2026.
Each rule's evidence is the number of operations that follow it, out of the operations it applies to, and "all" means every one.
Some counts cover only the current parts, the documents at the top of the project, and leave out the older versions kept in its folders `old-versions` and `v1`, where many of the exceptions sit.
The evidence for depth rules counts only the operations whose heights are set from the stock top or bottom or from a point, since the rest set them relative to a selected contour or hole, whose position the parameters do not give.

Most thresholds sit at or just above the value the lab's operations use, so a finding marks a setting the lab has not used in copper, not one known to fail.
Where a rule also flags some of the lab's own operations, its evidence says how many.
Rule ids keep the numbers of the list the rules were mined from, so the numbering has gaps.

## How the check reads a toolpath

The check reads each operation's parameters for its feeds, speeds, stepdowns, entry type and drilling cycle.
Many of an operation's heights are set relative to a selected contour or hole, so the parameters do not say how deep the tool goes.
For depths and heights, the check therefore posts each setup to temporary G-code, with the Carvera Community post processor that the skill also uses, and follows the tool's moves.

To post, the check adds an NC program to the document and deletes it again, so the document counts as changed afterwards.
Save your own changes before running the check, and close the document without saving after it.

The check sees each toolpath as it was computed, which may no longer match the design.
Fusion marks some outdated toolpaths itself (rule P1), but not those computed for a stock or model that has since changed; rule P3 catches those.

## Rules

Each table gives the rule's id, the condition that makes a finding, its severity, the lab's evidence and the failure the rule guards against.
Diameters, depths and heights are in mm, feeds in mm/min.
The thresholds are constants at the top of `check_cam.py`.

### Toolpath state

Fix these findings first: other findings on a stale toolpath mean nothing.

| Id | Finding when | Severity | Evidence | Guards against |
|---|---|---|---|---|
| P1 | the operation has no toolpath, or Fusion marks it out of date | error | — | cutting with a toolpath for an older design |
| P2 | Fusion reports an error or a warning on the operation, such as a lost hole selection | error for Fusion's errors, warning for its warnings | — | features left uncut, or cut from a broken selection |
| P3 | the toolpath's moves across the part run below the operation's current retract height (checked where the retract height is set from the stock top) | error | found in `hanger-for-bluefors-sd`, whose every toolpath runs 11.4 mm below its current retract height | a toolpath computed for a stock or model that has changed since; regenerate it |

### Feeds and speeds

The lab runs every end mill at 6000 rpm and 200 mm/min, every drill at 10000 rpm with a 50 mm/min plunge, and every thread mill at 100 (M2) to 200 mm/min (M3 and M4); the skill runs the thread mills at 10000 rpm, where the lab's parts ran them at 15000 rpm.
These settings are the feed presets named "Copper", for the copper the lab machines (C10100, H02 half-hard temper), which every tool in the skill's tool library (`assets/carvera-tools.json`) carries.
The lab cuts aluminum with the same programs, at 200% feed and spindle override.

| Id | Finding when | Severity | Evidence | Guards against |
|---|---|---|---|---|
| F0 | the spindle speed is 0 | error | — | feeding a stopped tool into the stock |
| F1 | the spindle speed, cutting feed or plunge feed differs from the tool's Copper preset | warning | the feeds match the tool's Copper preset in all 916 operations whose tool had one; the other 88 use tools without one (rule N5) | settings the lab has not used with that tool |

### How much the tool cuts at once

The stepdown is the depth an end mill cuts in one pass; the stepover is how far it moves sideways between passes.
The lab keeps the stepdown small, 0.3 mm for end mills of 3 mm and up and 0.2 mm below, and its contours cut a slot the full width of the tool on their first pass.
A roughing pass below means a multi-depth pass that cuts the feature to size in such steps; the lab then runs a finishing pass, a second end mill of the same size over the full depth at once.

| Id | Finding when | Severity | Evidence | Guards against |
|---|---|---|---|---|
| D1 | a roughing pass steps down more than 0.3 mm with an end mill of 3 mm or more, or more than 0.2 mm or 0.2 × the diameter, whichever is less, with a smaller one | warning | 168 of 169 roughing passes in the current parts and 134 of 161 in the older versions; the exceptions step 0.5 mm | stall and broken end mills |
| D4 | a pocket (or contour) cuts its full depth in one pass with no roughing pass of a pocket (or of a contour or pocket, for a contour) by an end mill of the same diameter earlier in the setup; rest machining is exempt. A contour may finish what a pocket roughed: a window is roughed by a pocket and finished by a contour along its walls | warning | 174 of 197 single-pass operations follow such a roughing pass | a full-depth cut into solid stock |
| D6 | a pocket's stepover is more than 0.6 × the tool's diameter | warning | 261 of 263 pockets step over 0.6 × the diameter | an overloaded end mill |
| D8 | a bore's radial stepover is more than 0.3 mm | warning | 23 of 33 bores in the current parts step over 0.1 mm, the rest 0.3 to 0.5 mm | stall and broken end mills |
| CH1 | a deburr (2D chamfer) keeps the chamfering bit's point less than 0.1 mm off the edge (Fusion's tip offset) | warning | all 104 deburr operations keep it at least 0.1 mm off, most 0.3 to 0.5 mm | the bit's sharp point doing the cutting and chipping |
| CH2 | a deburr's chamfer width is more than 0.2 mm | warning | the generator deburrs 0.1 mm wide | a chamfer the design does not have |

### How the tool enters the cut

The lab's end mills enter pockets through holes a drill makes first (Fusion's `predrill` entry), not by ramping into solid metal.
The checker warns about a ramp, whatever the feed, and about every plunge into stock that is neither cleared nor drilled, since an end mill plunging into copper can stall or break.
The checker reads the plunges from the G-code, whatever the operation's entry setting; it follows an arc as straight moves at most 5° round and at most 0.002 mm inside the arc, so a cut round a corner clears the corner, not its chord, and a cut round a large arc does not seem to cut into the part. Earlier milling passes, in the same setup or by an end mill in an earlier one, must have cleared the end mill's whole footprint at that spot down to some depth, which counts stock cleared by a pocket, by the outline cut at a pocket's side entry, or by a larger end mill round a pocket's corners. Below that depth, an earlier drill must have made a hole there, at least as wide as the end mill and full width down to the plunge's depth, which is the drill point's depth less the point's length. A hole drilled in an earlier setup counts too, wherever it is full width over that stretch of the plunge.

| Id | Finding when | Severity | Evidence | Guards against |
|---|---|---|---|---|
| E1 | a pocket or 2D contour ramps into the copper (a helix, profile or other ramp) instead of entering a pre-drilled hole, or a pocket plunges outside the stock (Fusion's "Plunge outside stock") with no stock contours, so outside the setup's stock block | warning | the lab's rule: copper is always entered at a pre-drilled hole; 259 of 263 pockets enter by `predrill` (213) or by plunging (46) | the end mill rubbing and loading up as it ramps into copper; an entry Fusion chooses, into solid stock |
| E4 | a pocket (a facing pocket included) or 2D contour by an end mill plunges into stock that no earlier end mill pass has cleared, in its setup or an earlier one, and that no earlier drill, in its setup or an earlier one, made a hole in as wide as the end mill and full width over the rest of the plunge, but for the drill's cone at its bottom, which TH4 checks. A plunge with the whole tool outside the setup's stock box is in the air and passes, as where the facing and the passes of a pocket open to the side enter beside stock of the part's own size | warning | 86 of 111 `predrill` pockets in the current parts have a drill before them in their setup (whether its tip goes through was not counted); most of the rest are bottom-side pockets, which the check accepts when an earlier setup drilled through | stall and broken end mills from plunging into solid copper |

### Heights, depth and reach

These rules read the depths from the posted G-code.
A tool's stick-out is its length below the holder, Fusion's body length: 28 mm for the lab's end mills and 26 mm for its drills.

| Id | Finding when | Severity | Evidence | Guards against |
|---|---|---|---|---|
| H1 | the retract height, set from the stock top, is less than 2 mm above it | warning | 992 of 1004 operations retract 5 mm above the stock top and clear 15 mm above it | rapid moves grazing the stock or the clamps |
| H2 | a rapid move runs below the stock top through stock that no earlier pass, in its setup or an earlier one or earlier in the same operation, has cleared to below it: a move sideways sampled every 0.5 mm, a move straight down at its end, where a hole an earlier drill in the setup made counts as cleared down to where its point starts; a sample with the whole tool outside the setup's stock box is in the air and passes | error | none in the generator's builds or in `sample-clamp-6port7x7` and `sample-puck-v2-2port10x10`; a drill rapiding down to 1 mm above a hole's top on a shelf no pass had cut (`kiutra-puck55-adapter`), and a pre-drill rapiding into a 15.5 mm part's uncut face (`eccosorb-clamp-6port`), both of which Fusion's simulation flagged as rapid moves into stock | the tool crashing into the stock at rapid speed |
| H3 | the tool goes more than 0.5 mm below the stock bottom, not counting a drill's point | error | none of the 426 part operations whose depth is known goes further, and through-cuts end 0.5 mm below; blanks squared in the project's folder `stock` go up to 2 mm below | cutting into the vise or the fixture plate |
| H5 | an operation cuts deeper than the tool's flute length, or than its neck's length for a tool whose neck is narrower than its cut (a thread mill), measured from its top height | error for a drill, warning otherwise | 196 of the 210 operations in the current parts whose depth is known; deeper holes are drilled by two operations, each at most 7.5 mm deep | packed chips and broken drills; the shank rubbing the wall |
| H6 | the tool goes deeper below the stock top than 0.75 × its stick-out (0.95 × is an error) | warning | 433 of 435 operations stay within 0.75 ×, and all but those in `hanger-for-bluefors-sd` within 0.68 × | the holder hitting the stock |
| X1 | the tool, sampled every 0.5 mm along each move as its tip's centre and eight points on a circle of its diameter (for a drill, where its point reaches full diameter), lies inside a body of the setup's fixture: a jaw beside the stock, or the ledge each jaw has under the stock's gripped edge, which a cut through the stock's bottom reaches; checked only in setups with a fixture (rule M2) | error | — | crashing into the vise or the fixture plate |

### The part's shape

These rules compare every setup's cuts with the part's model, so they catch a toolpath that no longer cuts what the design shows: after an edit to an operation's heights, stock to leave, tool, compensation or geometry, after an operation was suppressed or deleted, or after the design changed.
They run once every milling setup has been checked, on sample points spread over the part's faces: the flat faces square to the Z axis (floors), the walls parallel to it, flat or round, and, for G1 only, the other flat faces, at most 4000 points per face and 0.5 mm apart at the closest.
Each tool is taken as a cylinder of its diameter from its tip up to the stock top, a drill's from where its point reaches full width; a chamfering bit is left out, since a deburr cuts into the part by design.
On a large face the points lie up to a few millimetres apart, so G1 also tests each end of every cut: a tool tip 0.02 mm above it, along the tool, inside the part is a gouge however small.

| Id | Finding when | Severity | Evidence | Guards against |
|---|---|---|---|---|
| G1 | a tool's cylinder takes in a point on the part's surface more than 0.02 mm deep (0.03 mm sideways for a drill, which may be up to 0.05 mm wider than its hole); a thread mill, which cuts the thread into the wall, and a face a thread mill threads are left out | error | no finding on the generator's builds of the two test parts or on the lab's `sample-clamp-6port7x7` and `sample-puck-v2-2port10x10`; a finishing pocket set 0.3 mm deeper, and a window finish with −0.2 mm stock to leave, each gave G1 at the edited depth | a spoiled part: a floor cut too deep, a wall cut past its line, a hole drilled oversize or in the wrong place |
| U1 | no tool reaches some point of a floor or a wall, at least 0.3 mm (a floor) or 0.55 mm (a wall) from the face's edges, within 0.03 mm; faces of the stock itself, at its top or bottom or on a side of its box, are left out | warning | `sample-puck-v2-2port10x10`: four Ø3.8 mm holes, blind 2.4 mm into the bottom face, that no operation cuts; on the generator's builds, a suppressed finishing pass, corner pass, finishing face or tab cut, a smaller drill in a hole, and roughing and finishing passes both left 0.2 mm oversize each gave U1. The 0.55 mm margin on walls leaves out the uncut strip a 1 mm end mill leaves in a sharp inside corner | a feature left uncut, too shallow or oversize, which the part then lacks |
| TB1 | an operation outside a tab program (an NC program whose name ends in `_tabs`) cuts more than 0.02 mm into a tab (a body in the generator's `carvera-cam tabs` component) | error | an outline cut taken down through the stock's bottom gave TB1. Hand-programmed documents have no tab bodies, so the rule does not check them | the part cut free before the tab program, loose in the outline cut under a spinning end mill |

### Drilling and threading

| Id | Finding when | Severity | Evidence | Guards against |
|---|---|---|---|---|
| K1 | a drill's cycle is not chip breaking (partial retract), its pecking depth is over 0.1 mm, or its accumulated pecking depth, after which it retracts fully, is over 1 mm | warning | all 224 drill operations chip-break; 222 peck 0.1 mm, and 193 retract fully every 1 mm, the rest at Fusion's default of 2.5 × the diameter | chips packing in the hole and breaking the drill |
| K2 | a drill that selects a through hole's faces and goes to the hole's bottom does not send its tip through the bottom and 0.5 mm past it | warning | 64 of the 65 drills that select a through hole's faces do | a thin cap of copper left at the bottom, which the thread mill or bore then runs into |
| K3 | a drill is wider than a hole it selects, and no thread mill in the document threads that hole | warning | the drills wider than their hole are threads' pre-drills, such as the 1.7 mm drill in M2's 1.623 mm hole, except 2 operations drilling 3.175 mm into 3.0 mm holes | an oversized hole from the wrong drill |
| TH1 | a thread mill cuts conventional rather than climb | warning | all 27 thread operations climb | thread mill wear |
| TH2 | a thread mill (error), or a bore or circular cut (warning), goes down, at a rapid or the feed, into stock that no earlier pass, in its setup or an earlier one, has cleared and that no earlier drill made a hole in as wide as the tool, as E4 checks, the drill's cone left to TH4 | as given | every thread and all 72 bores follow a drill in their setup | the tool cutting a hole into solid copper |
| TH3 | a thread mill goes more than 0.01 mm deeper than the hole it threads, along the setup's tool axis: deeper than an earlier drill cut it at full width, or than its modelled bottom if that is deeper | error | the lab's rule; its through holes are drilled 0.5 mm past the bottom and thread-milled 0.3 mm past it; a helical lead-in would start about 0.05 mm below the bottom height, so the generator uses a flat one | the thread mill cutting solid copper: a blind hole's floor, or stock the drill never reached |
| TH4 | an end mill enters copper, by plunging (pockets, faces, contours, bores, circular cuts) or by boring down a hole (bores and circular cuts), deeper than a hole drilled before it is at least half as wide as the end mill, centred on it: below where the drill's cone narrows to half the end mill's diameter, or where no drill went, into copper no earlier pass has cleared | warning | the user's rule (2026-10-17), for every operation that enters at a pre-drilled hole (user, 2026-10-17): the end mill needs a pre-drilled hole or cone at least half its diameter; bobbin-top-v2-6port10x10 had bored a counterbore 0.37 mm below that, from a pre-drill stopped at the floor, which TH2 missed because the bore spirals down rather than plunging. The generator stops pocket entry holes where the cone is half the end mill wide at the floor, which leaves the smallest dimple the rule allows | the end mill cutting solid copper below a pre-drill |

### Deburring

| Id | Finding when | Severity | Evidence | Guards against |
|---|---|---|---|---|
| DB1 | an edge the setup should deburr is not chamfered: a convex edge where a flat face facing up in the setup meets a wall falling away below it (the face's outline, and the rims of its holes and pockets, a counterbore's floor included), at any of five points along it no chamfering bit's cone in the setup passes through it. An edge of the stock itself, at the stock's top and on a side of the stock box, is left out: no tool cuts it, so it has no burr. So is an edge where the face meets a slope falling away, such as a modelled chamfer: the edge is obtuse, and the chamfer's own cut takes its burr. So is a point within the bit's radius (1.59 mm for the 1/8" bit) of a wall rising from the same face, which the bit's body keeps it from reaching | warning | the user's rule (2026-10-17): every edge is deburred. Read from the G-code, not the selections, because Fusion silently leaves out an edge where the bit's point would go below a floor: with a 0.5 mm tip offset it skipped bobbin-top-v2-6port10x10's counterbore rim; the face's deburr also never reached the hole under the counterbore. On `make_open_pockets`, the deburr stops 1.59 mm short of where each pocket's wall meets the part's side, and with those points counted every such edge gave DB1 | burrs left on the part |
| DB2 | a chamfering bit's point goes below the top of a tab (a body in the generator's `carvera-cam tabs` component) while its cone reaches over the tab | error | the user's rule (2026-10-17); the tabs still stand in the outline cut when the deburr runs. Hand-programmed documents have no tab bodies, so the rule does not check them | the bit cutting into a tab, weakening it or breaking the bit |

### Tools and setups

The Carvera's automatic tool changer holds six tools, numbered 1 to 6, and the operator loads them by number.

| Id | Finding when | Severity | Evidence | Guards against |
|---|---|---|---|---|
| N1 | one tool number names two different tools in a setup (error), or in two setups of the document (warning) | as given | never within one of the lab's 88 setups; across setups, in 3 documents | the wrong tool in the changer, and a crash at its first move |
| N3 | a tool number is above 6 | warning | 12 current setups use tools 7 to 10 | a missed manual tool change |
| N4 | the document's NC programs hold some of a setup's operations but leave one out (error), or an operation is suppressed (warning) | as given | `build` puts every operation in a program; an operation added after it, by hand or by a recipe, must be added to the program too. A suppressed operation is left out of the posted G-code | an operation the machine never runs, leaving its feature uncut |
| N5 | the tool is not in the skill's tool library, or has no holder | warning | a few of the lab's tools have no preset, such as the 1/8″ two-flute end mill in `sample-clamp-6port7x7`, which also has no holder | feeds unchecked against a preset; holder collisions unchecked in simulation |
| N6 | a finishing pass (a single-depth pocket or contour after a multi-depth one of the same kind, as in D4) uses the tool number of the roughing pass before it | warning | the lab's finishing passes run on T3, a second 3.175 mm end mill, and never on T2, the roughing one | a worn roughing edge spoiling the finished wall and floor |
| M1 | the setup has no machine | warning | 3 of the 88 setups have none | simulation not checking the machine |
| M2 | the setup has no fixture | warning | only 10 of the 88 setups set one, all in older versions such as `2026-01-20_sample-puck-6port7x7`, whose fixture setup the skill copies | simulation, and rule X1, not checking the vise |
| G0 | no posted G-code could be matched to the operation, or the check ran out of time while comparing the cuts with the part's faces | warning | — | an operation left out of the depth and fixture checks; G1, U1 and TB1 left incomplete |

## What the check does not cover

Fusion's simulation with collision checking still has to run on each setup before machining, because the API cannot run it.
The check does not test the tool, its shank or its holder against the machine, against the stock, or against the fixture above the tool's tip.
G1 and U1 do not sample round faces other than walls parallel to the Z axis, such as fillets, modelled chamfers and drill points, and U1 does not check slanted faces, which no flat end mill cuts to size.
If the check runs out of time while comparing the cuts with the part's faces, it says so (G0) and leaves G1, U1 and TB1 incomplete.

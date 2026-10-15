# Toolpath checks

The `carvera-copper-cam` skill checks the toolpaths of an open Fusion document against the lab's rules for avoiding crashes, spindle stalls and tool wear on the Carvera, and lists each breach as a finding.
Ask Claude to check a document's Carvera toolpaths, or run `check({})` from `scripts/check_cam.py` inside Fusion as SKILL.md shows.
The check covers every milling setup in the document: setups the lab programmed by hand as well as those made by `build`, the skill's toolpath generator.

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
These settings are the feed presets named "Copper 101-H02", after the copper the lab machines (C10100, H02 half-hard temper), which every tool in the skill's tool library (`assets/carvera-tools.json`) carries.
The lab cuts aluminum with the same programs, at 200% feed and spindle override.
Chip load is the feed per cutting edge per revolution: feed ÷ (rpm × flutes).

| Id | Finding when | Severity | Evidence | Guards against |
|---|---|---|---|---|
| F0 | the spindle speed is 0 | error | — | feeding a stopped tool into the stock |
| F1 | the spindle speed, cutting feed or plunge feed differs from the tool's Copper 101-H02 preset | warning | the feeds match the tool's Copper 101-H02 preset in all 916 operations whose tool had one; the other 88 use tools without one (rule N5) | settings the lab has not used with that tool |
| F2 | an end mill or chamfering bit cuts at more than 0.034 mm chip load | warning | all 647 end-mill operations cut at 0.033 mm (one flute) or 0.017 mm (two flutes), and all 112 chamfering operations at 0.017 mm | stall and broken small end mills |

### How much the tool cuts at once

The stepdown is the depth an end mill cuts in one pass; the stepover is how far it moves sideways between passes.
The lab keeps the stepdown small, 0.3 mm for end mills of 3 mm and up and 0.2 mm below, and its contours cut a slot the full width of the tool on their first pass.
A roughing pass below means a multi-depth pass that cuts the feature to size in such steps; the lab then runs a finishing pass, a second end mill of the same size over the full depth at once.

| Id | Finding when | Severity | Evidence | Guards against |
|---|---|---|---|---|
| D1 | a roughing pass steps down more than 0.3 mm with an end mill of 3 mm or more, or more than 0.2 mm or 0.2 × the diameter, whichever is less, with a smaller one | warning | 168 of 169 roughing passes in the current parts and 134 of 161 in the older versions; the exceptions step 0.5 mm | stall and broken end mills |
| D4 | a pocket (or contour) cuts its full depth in one pass with no roughing pass of a pocket (or contour) by an end mill of the same diameter earlier in the setup; rest machining, and contours that cut sideways in steps of at most 0.3 mm, are exempt | warning | 174 of 197 single-pass operations follow such a roughing pass | a full-depth cut into solid stock |
| D6 | a pocket's stepover is more than 0.6 × the tool's diameter | warning | 261 of 263 pockets step over 0.6 × the diameter | an overloaded end mill |
| D8 | a bore's radial stepover is more than 0.3 mm | warning | 23 of 33 bores in the current parts step over 0.1 mm, the rest 0.3 to 0.5 mm | stall and broken end mills |
| CH1 | a deburr (2D chamfer) keeps the chamfering bit's point less than 0.1 mm off the edge (Fusion's tip offset) | warning | all 104 deburr operations keep it at least 0.1 mm off, most 0.3 to 0.5 mm | the bit's sharp point doing the cutting and chipping |

### How the tool enters the cut

The lab's end mills enter pockets through holes a drill makes first (Fusion's `predrill` entry), not by ramping into solid metal.
The checker warns about every other entry, whatever the feed, since an end mill plunging into copper can stall or break.
It allows one exception: an end mill rest machining a pocket may plunge, since it cuts only what a larger end mill left, such as the pocket's corners.
A 2D contour around a closed loop must likewise enter through a hole, at its entry positions; an open contour starts at its open end, which the checker takes to be clear.
The checker reads the entries from the G-code: an end mill that should enter through a hole must plunge down a hole an earlier drill made at that spot, at least as wide as the end mill there and full width down to the plunge's depth, which is the drill point's depth less the point's length. A hole drilled in an earlier setup counts too, wherever it is full width over the plunge, and so does stock that an earlier milling pass in the same setup has already cleared, as at a pocket's side entry.

| Id | Finding when | Severity | Evidence | Guards against |
|---|---|---|---|---|
| E1 | a pocket or 2D contour ramps into the copper (a helix, profile or other ramp) instead of entering a pre-drilled hole, or a pocket's entry (Fusion's ramp type) is set to none | warning | the lab's rule: copper is always entered at a pre-drilled hole; 259 of 263 pockets enter by `predrill` (213) or by plunging (46) | the end mill rubbing and loading up as it ramps into copper; an entry Fusion chooses, into solid stock |
| E2 | a pocket enters by plunging, other than in rest machining | warning | — (46 of the lab's 263 pockets plunge, 39 of them with a 1 or 2 mm end mill or into a pocket already roughed) | stall and broken end mills from plunging into solid copper |
| E4 | a pocket entering by `predrill`, or a 2D contour entering at entry positions, plunges where no earlier drill, in its setup or an earlier one, made a hole as wide as the end mill and full width over the plunge, and no earlier pass in its setup has cleared the stock | warning | 86 of 111 `predrill` pockets in the current parts have a drill before them in their setup (whether its tip goes through was not counted); most of the rest are bottom-side pockets, which the check accepts when an earlier setup drilled through | the end mill plunging where the entry hole should be |
| E5 | a 2D contour around a closed loop has no entry position | warning | — (200 of the lab's 318 contours have neither; whether their loops are closed, or start where an earlier cut has cleared the stock, was not recorded) | stall and broken end mills from plunging into solid copper |

### Heights, depth and reach

These rules read the depths from the posted G-code.
A tool's stick-out is its length below the holder, Fusion's body length: 28 mm for the lab's end mills and 26 mm for its drills.

| Id | Finding when | Severity | Evidence | Guards against |
|---|---|---|---|---|
| H1 | the retract height, set from the stock top, is less than 2 mm above it | warning | 992 of 1004 operations retract 5 mm above the stock top and clear 15 mm above it | rapid moves grazing the stock or the clamps |
| H3 | the tool goes more than 0.5 mm below the stock bottom, not counting a drill's point | error | none of the 426 part operations whose depth is known goes further, and through-cuts end 0.5 mm below; blanks squared in the project's folder `stock` go up to 2 mm below | cutting into the vise or the fixture plate |
| H5 | an operation cuts deeper than the tool's flute length, measured from its top height | error for a drill, warning otherwise | 196 of the 210 operations in the current parts whose depth is known; deeper holes are drilled by two operations, each at most 7.5 mm deep | packed chips and broken drills; the shank rubbing the wall |
| H6 | the tool goes deeper below the stock top than 0.75 × its stick-out (0.95 × is an error) | warning | 433 of 435 operations stay within 0.75 ×, and all but those in `hanger-for-bluefors-sd` within 0.68 × | the holder hitting the stock |
| X1 | the tool's tip, sampled as eight points on a circle of the tool's diameter at the end and middle of each move, lies inside a body of the setup's fixture; checked only in setups with a fixture (rule M2) | error | — | crashing into the vise or the fixture plate |

### Drilling and threading

| Id | Finding when | Severity | Evidence | Guards against |
|---|---|---|---|---|
| K1 | a drill's cycle is not chip breaking (partial retract), its pecking depth is over 0.1 mm, or its accumulated pecking depth, after which it retracts fully, is over 1 mm | warning | all 224 drill operations chip-break; 222 peck 0.1 mm, and 193 retract fully every 1 mm, the rest at Fusion's default of 2.5 × the diameter | chips packing in the hole and breaking the drill |
| K2 | a drill that selects a through hole's faces and goes to the hole's bottom does not send its tip through the bottom and 0.5 mm past it | warning | 64 of the 65 drills that select a through hole's faces do | a thin cap of copper left at the bottom, which the thread mill or bore then runs into |
| K3 | a drill is wider than a hole it selects, and no thread mill in the document threads that hole | warning | the drills wider than their hole are threads' pre-drills, such as the 1.7 mm drill in M2's 1.623 mm hole, except 2 operations drilling 3.175 mm into 3.0 mm holes | an oversized hole from the wrong drill |
| TH1 | a thread mill cuts conventional rather than climb | warning | all 27 thread operations climb | thread mill wear |
| TH2 | a thread mill (error), or a bore or circular cut (warning), runs with no drill before it in its setup | as given | every thread and all 72 bores follow a drill in their setup | the tool cutting a hole into solid copper |
| TH3 | a thread mill goes more than 0.01 mm deeper than the hole it threads, along the setup's tool axis: deeper than an earlier drill cut it at full width, or than its modelled bottom if that is deeper | error | the lab's rule; its through holes are drilled 0.5 mm past the bottom and thread-milled 0.3 mm past it; a helical lead-in would start about 0.05 mm below the bottom height, so the generator uses a flat one | the thread mill cutting solid copper: a blind hole's floor, or stock the drill never reached |

### Tools and setups

The Carvera's automatic tool changer holds six tools, numbered 1 to 6, and the operator loads them by number.

| Id | Finding when | Severity | Evidence | Guards against |
|---|---|---|---|---|
| N1 | one tool number names two different tools in a setup (error), or in two setups of the document (warning) | as given | never within one of the lab's 88 setups; across setups, in 3 documents | the wrong tool in the changer, and a crash at its first move |
| N3 | a tool number is above 6 | warning | 12 current setups use tools 7 to 10 | a missed manual tool change |
| N5 | the tool is not in the skill's tool library, or has no holder | warning | a few of the lab's tools have no preset, such as the 1/8″ two-flute end mill in `sample-clamp-6port7x7`, which also has no holder | feeds unchecked against a preset; holder collisions unchecked in simulation |
| N6 | a finishing pass (a single-depth pocket or contour after a multi-depth one of the same kind, as in D4) uses the tool number of the roughing pass before it | warning | the lab's finishing passes run on T3, a second 3.175 mm end mill, and never on T2, the roughing one | a worn roughing edge spoiling the finished wall and floor |
| M1 | the setup has no machine | warning | 3 of the 88 setups have none | simulation not checking the machine |
| M2 | the setup has no fixture | warning | only 10 of the 88 setups set one, all in older versions such as `2026-01-20_sample-puck-6port7x7`, whose fixture setup the skill copies | simulation, and rule X1, not checking the vise |
| G0 | no posted G-code could be matched to the operation | warning | — | an operation left out of the depth and fixture checks |

## What the check does not cover

Fusion's simulation with collision checking still has to run on each setup before machining, because the API cannot run it.
The check does not test the tool, its shank or its holder against the machine, against the stock, or against the fixture above the tool's tip.
It does not compare a drill's diameter with the hole's, and it does not know whether a rapid move between cuts crosses uncut stock; Fusion plans those moves against its own model of the stock, which rule P3 shows can be out of date.

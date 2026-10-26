# Reviewing toolpaths by reading them

`check_cam.py` checks the toolpaths against its rules ([rules.md](rules.md)); this review covers what those rules cannot.
Run it after the checker, on the same toolpaths: judge each of the checker's findings, then look at what no rule checks.
The simulation ([simulation.md](simulation.md)) comes after both.

## What to read

- `check_cam.py`'s `summary({})`: for every operation, in run order, its tool and slot, machining time, cut length, deepest Z, the area its cuts span (`xy_extent`) and every plunge (`plunge_points`), in its setup's coordinates, in mm; `plunge_points_design` gives the plunges in the design's frame, the one `analyze` gives the features in.
  Both lists stop at 40 points, and every peck of a drill counts as one, so pass `{"max_points": 1000}` to see them all.
- `analyze`'s features: the floors' depths, the holes, the windows and the part's size.
- Screenshots of each setup, with the operation in question selected; SKILL.md's "Look at the part in Fusion" says how to aim the camera and select one.

## What to look at

1. **Is each finding real?**
   For every finding the checker reports, look at the geometry or the G-code it names and decide whether the part would come out wrong.
   A rule can misfire on a shape its author did not test: on parts turned over for machining, DB1 once flagged 145 inside corners as edges with no deburr, because it took the floor's side of an edge from the order of the face's edges.
   A finding that is wrong is a bug in the rule: fix it in `check_cam.py`.
2. **The order makes sense.**
   `build` orders each setup's operations for the fewest tool changes, keeping each one after the operations it depends on (conventions.md, "Run order"), and no rule checks the order as a whole.
   Drills come before the cuts that enter their holes, the outline cut before anything that enters from its groove, every roughing pass before its finishing pass, the drills in a pocket floor and the slots before the finishing pockets, the deburr last but for the tab cut, and the tab cut last of all, in its own program.
3. **The times are plausible.**
   Facing usually takes the longest.
   An operation that takes many times longer than a similar one, or whose cut length is far longer than its feature needs, often means a toolpath that wanders, such as one that feeds in from the stock's edge.
4. **The screenshots look right.**
   With the outline selected, four tabs stand at the outline, and the part sits in the stock where the plan put it, clear of the jaws.
   Each pocket's toolpath fills the pocket.
   With stock that fits along one side, the end cuts run outside the jaws.
   To see the vise, show `carvera-cam vise top` for the screenshot and hide it again after.
5. **The toolpath stays within the machine's travel.**
   Nothing else checks this: the machine model sets no limits on its axes, so the simulation reports none.
   Compare each setup's `xy_extent` and deepest Z with the Carvera's travel.

## When the review finds something

A problem the checker did not report is a gap in it: if a rule could catch it, add the rule to `check_cam.py` and its row to [rules.md](rules.md); if not, add what to look for here.
When you cannot tell whether a finding is right, show the user the evidence, the G-code lines and a screenshot, and ask.
Then follow SKILL.md's "Improving the skill".

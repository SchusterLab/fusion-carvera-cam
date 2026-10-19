# Reviewing toolpaths by reading them

This is the written counterpart of `check_cam.py`: a way to judge the toolpaths from their G-code and from screenshots, without the checker's rules.
Run it alongside the checker, then compare the two (see "Comparing with the checker").
Each item names the checker rule that covers the same ground, from [rules.md](rules.md), so that a disagreement points at one rule.

## What to read

- `check_cam.py`'s `summary({})`: for every operation, in run order, its tool and slot, machining time, cut length, deepest Z, the area its cuts span (`xy_extent`) and every plunge (`plunge_points`, a straight move down at the feed), all in its setup's coordinates, in mm; `plunge_points_design` gives the plunges again in the design's frame, the one `analyze` gives the features in.
  Both lists stop at 40 points, and every peck of a drill counts as one, so pass `{"max_points": 1000}` to see them all.
- `analyze`'s features: the floors' depths, the holes, the windows and the part's size.
- Screenshots of each setup, with the operation in question selected; SKILL.md's "Follow the recipes" says how to aim the camera and select one.
- For a pocket cut by passes, `coverage({"floor": i})`.

## What to look for

1. **Every plunge goes into cleared stock or down a hole** (E4, TH2).
   Every rapid move below the stock top likewise runs through stock already cleared (H2).
   For each plunge point, find what made room for it: an earlier operation whose cuts pass within its tool's radius at that depth or deeper, in this setup or the other one, or a drill whose plunge lands on the same point at least as deep.
   A plunge with nothing under it plunges into solid copper, unless the whole tool is outside the stock, as where the facing enters beside stock of the part's own size.
   A plunge that ends in the drill's cone, below the hole's full width, is fine only where the cone is at least half the end mill wide, centred on it (TH4); a pocket entry hole stops exactly there at the floor, so its finishing pass plunges to the depth where the cone is half the end mill wide.
   A bore spirals down rather than plunging, so also check the hole under it (TH4): all the way to the bore's deepest Z, a hole drilled before it must be at least half as wide as its end mill, so below the depth where the drill's cone narrows to half the end mill's diameter the bore cuts copper the drill never reached.
2. **Every floor and wall is cut, and only to its line** (U1 checks for faces left uncut or oversize, G1 for cuts into the part, H3 for the depth below the stock).
   After an edit to an operation, look hardest at what it cuts: a changed height, stock to leave, tool or compensation moves the cut off the model, and a suppressed or deleted operation leaves its feature uncut.
   An operation's deepest Z should match its feature: a pocket's floor, a rough pass 0.2 mm above it, a through cut 0.5 mm past the far face.
   Nothing should go deeper than 0.5 mm below the stock's bottom, besides a drill's point: the main entry hole is drilled through the stock, 0.5 mm past it with the tip through, so that the bottom setup can enter it from the other side; with one setup, the through cuts, the outline between tabs and the tab cut also go 0.5 mm below the stock's bottom, since the part's bottom face is the stock's.
   For a pocket cut by passes, `coverage` must report no uncut area; for others, the screenshot should show the toolpath filling the pocket.
3. **The order makes sense** (E4, N6).
   Drills come before the cuts that enter their holes, the outline cut before anything that enters from its groove, every roughing pass before its finishing pass, the deburr last but for the tab cut, and the tab cut last of all.
4. **The tools are right** (N1, N3, N6).
   Each slot holds one tool throughout the job; finishing passes run on T3, not on T2, which roughs; a slot above 6 needs a manual tool change.
5. **Every tool reaches its depth** (H5, H6).
   An operation's deepest cut, below where it starts, fits within the tool's flute, or within its neck for a tool with a neck narrower than its cut, such as a thread mill; and the depth below the stock top stays under three quarters of how far the tool sticks out of its holder.
6. **Every edge is deburred, and the bit stays out of the tabs** (DB1, DB2).
   Every outer edge of a face facing up in the setup, and the rim of every hole and pocket in it, a counterbore's floor included, should have a chamfering pass along it; select the chamfer operations and look.
   That includes the edges where a pocket floor falls away (at the part's sides, into a deeper pocket, and round a hole or window through the floor) and the rim of an island's top.
   The exception is an edge on the stock's own sides, where the stock fits the part and the face is not faced: no tool cuts it, so it is not deburred (DB1 skips it too).
   The bit also cannot reach within its radius, 1.59 mm, of a wall rising beside an edge, so the pass stops that short of the wall (DB1 skips those points too).
   Fusion silently leaves out an edge where the bit's point would go below a floor, so an edge with no pass is likely one of those.
   At the outline, the bit's deepest Z must stay above the tabs' top.
7. **The part stays held** (TB1 checks that only the tab program cuts the tabs).
   With the outline selected, the screenshot shows four tabs left standing, and only the tab program cuts them.
   Stock of the part's own size, and stock that fits the part along one side (cut free by the Ends cut), have no outline or tabs; there the vise holds the part, so check item 8 with care.
8. **The tool stays clear of the vise** (X1).
   Every operation's `xy_extent` lies inside the stock, and the screenshot shows no toolpath in the jaws.
   With stock of the part's own size, the facing and the passes of a pocket open to the side run past the stock's edge by design, and with stock that fits along one side, the end cuts do; there look at the jaws instead: no cut beside a gripped edge may go below the jaws' top, and the end cuts must lie outside the jaws' width.
   No cut through the stock's bottom may lie within 1.9 mm of a gripped edge, over the jaw's ledge under the stock.
9. **The times are plausible** (no rule checks this).
   Facing usually takes the longest; an operation that takes many times longer than a similar one, or a cut length far longer than its feature needs, often means a toolpath that wanders, such as one that feeds in from the stock's edge.

## Comparing with the checker

Write down your findings before reading the checker's, then compare:

- **A finding the checker also made**: agreed; report it.
- **A finding only you made**: check it against the G-code once more.
  If it holds, the checker has a gap: add or fix the rule in `check_cam.py`, and its row in [rules.md](rules.md).
- **A finding only the checker made**: work out whether it is right.
  If it is, this review missed it: add what to look for here.
  If it is not, the rule is wrong: fix it in `check_cam.py`.
- When you cannot tell which is right, show the user the evidence, the G-code lines and a screenshot, and ask.

Then follow SKILL.md's "Improving the skill".

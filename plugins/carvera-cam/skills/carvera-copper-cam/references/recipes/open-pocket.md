# Recipe: a pocket open to the side

Cut a pocket that opens to the side of the part with 2D contour passes along its walls, each pass starting and ending on the outline cut's path.
The outline cut is the contour that cuts round the part, one end mill wide, and its path is where the centre of its tool runs.
It has already cleared the stock there, so every plunge goes into empty space.
In stock of the part's own size there is no outline cut, and that path lies beside the part, outside the stock, so the plunges are clear all the same.
A pocket toolpath is the wrong tool here: Fusion picks where it enters an open pocket, whatever the ramp type or lead-in, and it plunges with the edge of the tool in copper that nothing has cut.
A floor open on every side of the part has no walls along its sides to follow, so its passes run round the islands standing on it instead (see "A floor open on every side").

## When it applies

[part.md](part.md) sends here for every pocket floor that `features` marks `side_open`, in the setup of its `side`: the rough operations go right after `Pockets` (part.md's step 9), the finishing ones right after `Pockets (finish)` (step 17).
One operation cuts the open floors of one depth, in one pass (`rough` or `finish`):

- **Tool**: T2 (`Spiral O Metal 3.175*12mm`, `"T": 2`) roughs, T3 (`"T": 3`) finishes.
- **Name**: `Open pockets`, or `Pockets` when every pocket of the setup opens to the side; then ` (finish)` for the finishing pass; then, with open floors of more than one depth, ` <depth> mm` (`Open pockets (finish) 1.5 mm`).
- **Heights**: top at H (part.md's "Heights"); `bottom_at_floor` one of the floors.
- **Rough**: `doMultipleDepths` `true`, and the 0.2 mm floor allowance: `useStockToLeave` `true`, `simpleStockToLeave` `false`, `stockToLeave` `0mm`, `verticalStockToLeave` `0.2mm`.
- **Finish**: `doMultipleDepths` `false`, no allowance.

## Steps

1. **Read the walls.**
   `walls({"floor": i})` lists the walls that rise from the floor, as runs between the pocket's openings, and `reach_mm`, the farthest any part of the floor lies from its walls.
   The farthest point often lies on the opening, where nothing else cuts inside the part, so the passes must reach it.
   `walls` also lists `islands`: for each island standing on the floor, the walls all round it.
   A floor with no runs but with islands, such as the floor round a boss that runs out to every side of the part, is cut by passes round its islands instead (see "A floor open on every side").
2. **Plan the passes.**
   The first pass runs one radius of the 3.175 mm end mill (1.5875 mm) off the walls, so it finishes them.
   Each further pass runs 0.6 × the tool diameter (1.905 mm) farther out, until the passes reach the whole floor: with diameter D, that takes n = ⌈(reach_mm + 0.1 − D) / 0.6D⌉ + 1 passes, and at least one.
3. **Sketch each pass.**
   `pass_path({"floor": i, "run": k, "offset_mm": d})` draws the tool centre's path d mm off run k of the walls.
   At each end it runs the path on, along the walls' direction there, to the outline cut's path; where the offset walls already reach past that path, it cuts the path back to it.
   It returns the path's id, its two ends and its length.
   It returns `"ok": false`, and leaves nothing sketched, when an end run on 10 mm does not reach the outline cut's path, when the walls offset that far leave no path, or when the pass would come within a tool radius of another wall rising from the floor, such as an island's; see "Shapes the steps do not cover".
   Each call returns a new id, so two passes along the same walls have different ids.
4. **Check the passes cover the floor.**
   `coverage({"floor": i})` samples the floor every 0.1 mm against every pass sketched for it and returns `uncut_area_mm2`, split into `in_corners_mm2`, stock within a tool radius of the walls, and `elsewhere_mm2`, with the extent of what stays uncut and its point farthest from a pass.
   Rule E4 of the check cannot see uncut floor, so this is the test: `elsewhere_mm2` must be 0.
   `in_corners_mm2` is what the end mill's radius leaves in inside corners tighter than 1.5875 mm; the corners recipe cuts it, so it is expected wherever `analyze` gives the floor a smaller `corner_radius`.
   If `elsewhere_mm2` is not 0, add a pass where the uncut area lies, or see "Shapes the steps do not cover".
5. **Add the operations.**
   For each depth and pass, call `add_contour` with the setup, `"program"` the setup's, the tool and `T`, the name, the `params` and `bottom_at_floor` above, and `paths`, the ids of every pass for that depth's floors.
   The rough and the finishing operation follow the same paths: the rough one in steps, leaving 0.2 mm on the floor, and the finishing one in one pass at the floor.
   The contour follows each path exactly, with no tool-radius offset.
   `program` puts it, last, in the setup's NC program.
6. **Generate and check**, as in SKILL.md.
   Rule E4 must report nothing for the new operations: it accepts a plunge only into stock that an end mill in this setup or an earlier one has cleared, or down a drilled hole.

## A floor open on every side

A floor that runs out to every side of the part, round one or more islands, has no runs of walls, so the passes go round the islands instead: the first one tool radius (1.5875 mm) off them, each further one 1.905 mm farther out, n passes as in step 2.

1. **Sketch the passes.**
   `island_passes({"floor": i})` sketches all n passes at once, as `build` cuts them.
   Each pass offsets every island's walls and is cut back to the outline cut's path, and to where it goes inside another island's loop.
   Where two islands' loops cross, the pieces join there into one, so no two passes cross: Fusion splits a contour wherever any of its curves cross, even curves of different chains, and plunges again at each piece, into copper nothing has cut.
   A pass that never reaches the outline cut's path, such as one island's whole loop, closes on itself.
   `island_passes` joins it to a pass farther out, so that the end mill enters on the outline cut's path, not down a hole drilled in the floor (user, 2026-10-21): the tool cuts the pass farther out, follows a line from it into the closed pass, goes round, and leads on into the next closed pass in.
   Each such run, from the outline cut's path in to the islands, is one chain, because Fusion orders the chains of a contour as it likes, so a pass can rely only on what its own chain has cut before it.
   A line in is at most two passes' spacing long, keeps a tool radius off the walls and crosses no pass.
   It returns `paths`, one per chain, each with its id, its length and `entry`, and under `left_out` each pass it left out, with why.
   A chain that starts on the outline cut's path has `entry` null.
   A closed pass that no line can reach, such as the second of two islands inside one pass, starts a chain of its own, and `entry` names its start, `path_entry:<id>`: the end mill enters there down a hole drilled to the floor.
2. **Drill the entries.**
   Sketch these passes before part.md's step 6, Pocket entry holes, and add every `entry` to that step's points, so that the holes are drilled to the floor with the other pocket entry holes and leave the same 1.59 mm dimple; warn about each one.
3. **Check and add the operations** as in steps 4 to 6, with `paths` every chain's id.

## Calling the primitives

They are functions in `scripts/carvera_cam.py`, run in Fusion like `build`.
Each takes a dict and prints its result as JSON.
`features`, `walls` and `coverage` change nothing and may run with `readOnly: true`; `pass_path`, `island_passes` and `add_contour` change the design and may not.
The calls for one part fit in one MCP call.
In this example, `SCRIPTS` is the skill's `scripts` folder:

```python
import io, json, contextlib, math, runpy

def call(fn, args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        runpy.run_path(SCRIPTS + r'\carvera_cam.py')[fn](args)
    return json.loads(buf.getvalue())

D = 3.175  # the end mill's diameter, mm
f = call('features', {})
side = 'top'
pockets = [x for x in f['floors'] if x['role'] == 'pocket' and x['side'] == side]
by_depth = {}  # depth -> the open floors of that depth
for x in pockets:
    if x['side_open']:
        by_depth.setdefault(round(x['depth'], 3), []).append(x['floor'])
paths = {}
entries = []  # where chains round islands start in the floor: add them to part.md's step 6
for dep, floors in by_depth.items():
    ids = []
    for i in floors:
        w = call('walls', {'floor': i})
        n = max(1, math.ceil((w['reach_mm'] + 0.1 - D) / (0.6 * D)) + 1)
        for k in range(len(w['runs'])):
            for j in range(n):
                p = call('pass_path', {'floor': i, 'run': k, 'offset_mm': D / 2 + j * 0.6 * D})
                if p['ok']:
                    ids.append(p['path'])
                else:
                    print('floor', i, 'run', k, 'pass', j, 'failed')  # see below
        if not w['runs'] and w['islands']:  # open on every side: round the islands
            p = call('island_passes', {'floor': i})
            ids += [q['path'] for q in p['paths']]
            entries += [q['entry'] for q in p['paths'] if q['entry']]
            for q in p['left_out']:
                print('floor', i, 'pass', q['offset_mm'], 'mm left out:', q['why'])
        print('floor', i, call('coverage', {'floor': i}))
    paths[dep] = ids

def add_open_pockets(which, top_at_h):
    """Right after Pockets (which='rough') or Pockets (finish) ('finish');
    top_at_h: part.md's "top at H" params at that point."""
    name = 'Pockets' if all(x['side_open'] for x in pockets) else 'Open pockets'
    for dep, floors in sorted(by_depth.items()):
        params = dict(top_at_h, doMultipleDepths='true' if which == 'rough' else 'false')
        if which == 'rough':
            params.update(useStockToLeave='true', simpleStockToLeave='false',
                          stockToLeave='0mm', verticalStockToLeave='0.2mm')
        call('add_contour', {
            'setup': side, 'program': side, 'tool': 'Spiral O Metal 3.175*12mm',
            'T': 2 if which == 'rough' else 3,
            'name': name + (' (finish)' if which == 'finish' else '')
                    + (' %g mm' % dep if len(by_depth) > 1 else ''),
            'params': params, 'bottom_at_floor': floors[0], 'paths': paths[dep]})
```

## Shapes the steps do not cover

The steps assume the walls meet the outline steeply, as they do where a pocket is cut straight in from the side, so that a pass run on along them crosses the outline cut.
Look at the part before trusting them, and after `generate`, look at the operations' toolpaths in Fusion.

- **A pass that cannot reach the outline cut.**
  `pass_path` fails when a wall runs so nearly along the outline that the pass, run on 10 mm, never crosses the outline cut's path.
  Never let a pass end anywhere else: there it would plunge into uncut copper.
  A pass the floor does not need can be left out: `coverage` shows whether the floor is covered without it.
  When the floor needs it, add the operations with the passes that work, and tell the user where copper stays (the uncut extent `coverage` gives) and that the pocket needs another way in, such as a drilled entry.
- **A pocket open on two sides that are not next to each other**, such as a channel across the part, has two runs of walls; give each run its own passes.
- **An island in a floor that also has runs of walls** is not cut round: the passes follow only the runs, and `pass_path` refuses a pass that would come within a tool radius of the island.
  Tell the user where copper stays round the island (the uncut extent `coverage` gives).

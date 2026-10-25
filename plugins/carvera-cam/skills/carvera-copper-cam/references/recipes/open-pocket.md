# Recipe: a pocket open to the side

Cut a pocket that opens to the side of the part with 2D contour passes along its walls, each pass starting and ending on the outline cut's path, or, on a ledge that also opens into a through window, ending in the window.
The outline cut is the contour that cuts round the part, one end mill wide, and its path is where the centre of its tool runs.
It has already cleared the stock there, so every plunge goes into empty space.
In stock of the part's own size there is no outline cut, and that path lies beside the part, outside the stock, so the plunges are clear all the same.
A pocket toolpath is the wrong tool here: Fusion picks where it enters an open pocket, whatever the ramp type or lead-in, and it plunges with the edge of the tool in copper that nothing has cut.
A floor with islands standing on it, such as the floor round a boss, has its passes run round the islands instead, and round its walls as well if it has any (see "Floors with islands, and walls a pass cannot follow").

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
   `walls({"floor": i})` lists the walls that rise from the floor, as runs between the pocket's openings, and `reach_mm`, the farthest any part of the floor lies from its walls, along its openings or inside it.
   The farthest point often lies on the opening, where nothing else cuts inside the part, so the passes must reach it.
   `walls` also lists `islands`: for each island standing on the floor, the walls all round it.
   A floor with islands is cut by passes round them instead (see "Floors with islands, and walls a pass cannot follow").
2. **Plan the passes.**
   The first pass runs one radius of the 3.175 mm end mill (1.5875 mm) off the walls, so it finishes them.
   Each further pass runs 0.6 × the tool diameter (1.905 mm) farther out, until the passes reach the whole floor: with diameter D, that takes n = ⌈(reach_mm + 0.1 − D) / 0.6D⌉ + 1 passes, and at least one.
3. **Sketch each pass.**
   `pass_path({"floor": i, "run": k, "offset_mm": d})` draws the tool centre's path d mm off run k of the walls.
   At each end it runs the path on, along the walls' direction there, to the outline cut's path; where the offset walls already reach past that path, it cuts the path back to it.
   Where that line would run on more than 10 mm, or into a wall, as along a wall that meets the outline at a shallow angle with a sliver of the part outside it, the path runs on only until the tool can turn straight out of the part a radius clear of every wall, and turns there.
   An end that runs into a through window instead, as on a ledge open both to the outside and to a window, stops where the tool is a radius inside the window, clear of the ledge and of the window's walls; there it cuts copper that the window's pocket removes anyway.
   The path then starts at its other end, on the outline cut's path, since the tool would plunge into uncut copper where the window is not yet cut.
   The path keeps a tool radius off every wall above the floor, not only off the walls rising from it.
   Where the floor drops into a deeper pocket beside it, the offset walls run on over that pocket, which leaves the tool room to cut the floor right up to the walls; where the path then comes within a radius of that pocket's own walls near an end, it is cut back from that end.
   Where the path strays past the outline cut's path between its ends, as it does round the outside of a corner where two walls meet, it follows the outline cut's path there instead, since the outline cut has cleared that.
   It returns the path's id, its two ends and its length.
   It returns `"ok": false`, and leaves nothing sketched, when an end reaches neither the outline cut's path nor a window, when both ends reach windows, when the walls offset that far leave no path, or when the pass would come within a tool radius of a wall above the floor anywhere but near an end.
   If any pass of a floor fails, cut that floor with `island_passes` instead (see "Floors with islands, and walls a pass cannot follow"), and pass `add_contour` only the ids `island_passes` returns: an offset of an open run of walls keeps a tool radius off them only square to them, so near the run's ends, such as the tip of a sliver of the part, it can cut into the part.
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

## Floors with islands, and walls a pass cannot follow

A floor with islands standing on it is cut by passes round the islands: the first one tool radius (1.5875 mm) off them, each further one 1.905 mm farther out, n passes as in step 2.
So is a floor where a pass along its walls fails (step 3).
A floor that runs out to every side of the part, such as the floor round a boss, has no runs of walls.
A floor that has runs has each run drawn as one more island, closed round the outside of the part, far enough out that the passes round it are cut back before they get there.
The passes round a run then follow its walls and turn out to the outline cut's path, and where they meet an island's they join it rather than cross.

Openings in the floor that other tools cut are patched (user, 2026-10-24); `walls` and `island_passes` do it themselves:
- An island some of whose walls fall away, to a slot, a hole or a deeper pocket, such as a pad a slot cuts through, is its loop's walls that rise from the floor, with a straight line across each opening between them. Its passes run over the openings instead of round them.
- A closed floor joined to this one (its `joined_to` in `features` is this floor; `walls` lists it under `joined`) is cut with it, as one floor: it lies at the same height, separated only by an opening the 3.175 mm end mill cannot enter, such as a groove a small end mill cuts. Its islands are this floor's too, and the passes run over the opening into it. It gets no pocket and no entry hole of its own.

1. **Sketch the passes.**
   `island_passes({"floor": i})` sketches all n passes at once, as `build` cuts them.
   Each pass offsets every island's walls and is cut back to the outline cut's path, and to where it goes inside another island's loop.
   Where a pass vanishes because the floor is too narrow for it there, the pass before it can leave a strip uncut; `island_passes` then adds a pass one tool radius beyond that one, which reaches the strip.
   Where two islands' loops cross, the pieces join there into one, so no two passes cross: Fusion splits a contour wherever any of its curves cross, even curves of different chains, and plunges again at each piece, into copper nothing has cut.
   A pass that never reaches the outline cut's path, such as one island's whole loop, closes on itself.
   When every pass of the n closes so, `island_passes` adds passes farther out, up to four, until one reaches the outline cut's path.
   `island_passes` joins it to a pass farther out, so that the end mill enters on the outline cut's path, not down a hole drilled in the floor (user, 2026-10-21): the tool cuts the pass farther out, follows a line from it into the closed pass, goes round, and leads on into the next closed pass in.
   Where no pass farther out reaches, it joins one of the same offset or one nearer the islands: so it does for an island beside a run of walls, and for a pass pinched off inside a run's walls, which lies farther from the run than the pass it joins.
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
        mine, failed = [], False  # this floor's passes along its runs
        for k in range(0 if w['islands'] else len(w['runs'])):
            for j in range(n):
                p = call('pass_path', {'floor': i, 'run': k, 'offset_mm': D / 2 + j * 0.6 * D})
                if p['ok']:
                    mine.append(p['path'])
                else:
                    failed = True
        if w['islands'] or failed:  # round the islands, and round the runs as islands
            p = call('island_passes', {'floor': i})
            mine = [q['path'] for q in p['paths']]
            entries += [q['entry'] for q in p['paths'] if q['entry']]
            for q in p['left_out']:
                print('floor', i, 'pass', q['offset_mm'], 'mm left out:', q['why'])
        ids += mine
        print('floor', i, call('coverage', {'floor': i, 'paths': mine}))
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

The steps cover the shapes `build` was tested on: walls that meet the outline steeply or at a shallow angle, islands with or without walls beside them, ledges open to a window, and ledges beside a deeper pocket.
Look at the part before trusting them, and after `generate`, look at the operations' toolpaths in Fusion.

- **A pass left out.**
  `island_passes` lists under `left_out` each pass it could not make, with why.
  Never let a pass end anywhere but the outline cut's path or a window: elsewhere it would plunge into uncut copper.
  A pass the floor does not need can be left out: `coverage` shows whether the floor is covered without it.
  When the floor needs it, add the operations with the passes that work, and tell the user where copper stays (the uncut extent `coverage` gives).
- **A pocket open on two sides that are not next to each other**, such as a channel across the part, has two runs of walls; give each run its own passes.

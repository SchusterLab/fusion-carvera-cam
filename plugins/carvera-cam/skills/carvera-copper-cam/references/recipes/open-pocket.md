# Recipe: a pocket open to the side

Cut a pocket that opens to the side of the part with 2D contour passes along its walls, each pass starting and ending on the outline cut's path.
The outline cut is the contour that cuts round the part, one end mill wide, and its path is where the centre of its tool runs.
It has already cleared the stock there, so every plunge goes into empty space.
In stock of the part's own size there is no outline cut, and that path lies beside the part, outside the stock, so the plunges are clear all the same.
A pocket toolpath is the wrong tool here: Fusion picks where it enters an open pocket, whatever the ramp type or lead-in, and it plunges with the edge of the tool in copper that nothing has cut.

## When it applies

`build` with `"recipes": true` leaves these pockets uncut and lists them in its output under `recipes`, with `"recipe": "open-pocket"`; `recipes({})` reads the same list back from the document in a later call.
Each entry is one operation to add, and gives:

- `floors`: the pockets' floors, as indices into `analyze`'s `floors` list; one operation cuts them all.
- `depth_mm`: how deep the floors lie below the face; an operation covers floors of one depth.
- `pass`: `rough` or `finish`; every pocket gets one entry of each.
- `setup`, `after`: the setup, and the operation the new one goes right after.
  Entries that share `after` go in the order listed: the second one after the first.
- `tool_from`: an operation that uses the right tool and feeds; the new one copies them.
- `name`: the operation's name.
- `params`: the heights and allowance the operation takes: where it starts, whether it goes down in steps, and the 0.2 mm the rough pass leaves on the floor.
  Pass it to `add_contour` exactly as given; a height's reference face comes as `{"entity_tokens": [...]}`.
- `bottom_at_floor`: the floor the operation goes down to.

## Steps

1. **Read the walls.**
   `walls({"floor": i})` lists the walls that rise from the floor, as runs between the pocket's openings, and `reach_mm`, the farthest any part of the floor lies from its walls.
   The farthest point often lies on the opening, where nothing else cuts inside the part, so the passes must reach it.
2. **Plan the passes.**
   The first pass runs one radius of the 3.175 mm end mill (1.5875 mm) off the walls, so it finishes them.
   Each further pass runs 0.6 × the tool diameter (1.905 mm) farther out, until the passes reach the whole floor: with diameter D, that takes n = ⌈(reach_mm + 0.1 − D) / 0.6D⌉ + 1 passes, and at least one.
3. **Sketch each pass.**
   `pass_path({"floor": i, "run": k, "offset_mm": d})` draws the tool centre's path d mm off run k of the walls.
   At each end it runs the path on, along the walls' direction there, to the outline cut's path; where the offset walls already reach past that path, it cuts the path back to it.
   It returns the path's id, its two ends and its length.
   It returns `"ok": false`, and leaves nothing sketched, when an end run on 10 mm does not reach the outline cut's path, or the walls offset that far leave no path; see "Shapes the steps do not cover".
   Each call returns a new id, so two passes along the same walls have different ids.
4. **Check the passes cover the floor.**
   `coverage({"floor": i})` samples the floor every 0.1 mm against every pass sketched for it and returns `uncut_area_mm2`, split into `in_corners_mm2`, stock within a tool radius of the walls, and `elsewhere_mm2`, with the extent of what stays uncut and its point farthest from a pass.
   Rule E4 of the check cannot see uncut floor, so this is the test: `elsewhere_mm2` must be 0.
   `in_corners_mm2` is what the end mill's radius leaves in inside corners tighter than 1.5875 mm; the corners recipe cuts it, so it is expected wherever `analyze` gives the floor a smaller `corner_radius`.
   If `elsewhere_mm2` is not 0, add a pass where the uncut area lies, or see "Shapes the steps do not cover".
5. **Add the operations.**
   For each entry, call `add_contour` with the entry's `setup`, `after`, `tool_from`, `name`, `params` and `bottom_at_floor`, and `paths`, the ids of every pass for the entry's floors.
   The rough and the finishing operation follow the same paths: the rough one in steps, leaving 0.2 mm on the floor, and the finishing one in one pass at the floor.
   The contour follows each path exactly, with no tool-radius offset.
   `add_contour` also puts the operation in the NC program that holds `after`.
6. **Generate and check**, as in SKILL.md.
   Rule E4 must report nothing for the new operations: it accepts a plunge only into stock that an end mill in this setup or an earlier one has cleared, or down a drilled hole.

## Calling the primitives

They are functions in `scripts/carvera_cam.py`, run in Fusion like `build`.
Each takes a dict and prints its result as JSON.
`recipes`, `walls` and `coverage` change nothing and may run with `readOnly: true`; `pass_path` and `add_contour` change the design and may not.
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
entries = [e for e in call('recipes', {}) if e['recipe'] == 'open-pocket']
paths = {}
for e in [e for e in entries if e['pass'] == 'rough']:
    ids = []
    for i in e['floors']:
        w = call('walls', {'floor': i})
        n = max(1, math.ceil((w['reach_mm'] + 0.1 - D) / (0.6 * D)) + 1)
        for k in range(len(w['runs'])):
            for j in range(n):
                p = call('pass_path', {'floor': i, 'run': k, 'offset_mm': D / 2 + j * 0.6 * D})
                if p['ok']:
                    ids.append(p['path'])
                else:
                    print('floor', i, 'run', k, 'pass', j, 'failed')  # see below
        print('floor', i, call('coverage', {'floor': i}))
    paths[tuple(e['floors'])] = ids
last = {}  # (pass, after) -> the operation added there last
for e in entries:
    after = last.get((e['pass'], e['after']), e['after'])
    o = call('add_contour', {k: e[k] for k in ('setup', 'tool_from', 'name', 'params', 'bottom_at_floor')}
             | {'after': after, 'paths': paths[tuple(e['floors'])]})
    last[(e['pass'], e['after'])] = o['operation']
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
- **An island in the floor** has walls of its own that no run includes; the steps do not cut round it, so tell the user.

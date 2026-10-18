# Recipe: inside corners the 3.175 mm end mill cannot reach

Cut the inside corners of pockets and windows that are tighter than the 3.175 mm end mill's radius (1.5875 mm) with a smaller end mill, by rest machining: a 2D pocket that cuts only the stock the larger end mill left behind.
The 3.175 mm end mill leaves a corner of radius 1.5875 mm wherever the part's corner is tighter, so the small end mill takes only the slivers in the corners.

## When it applies

`build` with `"recipes": true` leaves these corners uncut and lists them in its output under `recipes`, with `"recipe": "corners"`, one entry per setup; `recipes({})` reads the same list back from the document in a later call.
Each entry gives `setup` and `after`, the operation the new ones go right after, and `features`, one per pocket or window with tight corners:

- `kind`: `pocket` or `window` (a through opening that is not a round hole);
- `floor` (for a pocket) or `window`: its index in `analyze`'s `floors` or `windows` list;
- `corner_radius_mm`: its tightest inside corner, 0 for a sharp one;
- `depth_mm`: how deep the cut goes: the pocket's depth, or the part's thickness for a window;
- `top_mm` (pockets): how far below the part's face the cut starts: 0, or, for a floor inside a shelf's opening, that shelf's depth, since the shelf's pocket has cleared everything above it, corners too;
- `params`: its heights; pass them to `add_operation` exactly as given.

## Steps

1. **List the tools.**
   `tools({})` gives `in_use`, the tools the part's operations use already, with their slots, and `library`, the skill's tools.
   The small end mills are the library's flat end mills (`type` `flat end mill`) narrower than 3.175 mm.
2. **Choose a tool for each feature.**
   A tool fits a feature when its radius is no larger than the corner radius and its flute is at least 0.5 mm longer than `depth_mm`.
   Of the tools that fit, take one the part uses already, if any, so the part needs fewer tools; otherwise take the largest.
   A corner tighter than the smallest end mill's radius (0.5 mm for the 1 mm end mill) cannot be cut to size: take the smallest end mill, and tell the user the corner stays at that radius.
   When no tool's flute is long enough, leave the feature out and tell the user its corners keep the 3.175 mm end mill's radius.
3. **Add one operation per tool and kind**, in setup `setup`, after `after`:
   - pockets: `add_operation` with `strategy` `pocket2d`, the tool's description as `tool`, `name` `Pocket corners <diameter>` (for example `Pocket corners 2`), followed by ` from <top_mm> mm` when `top_mm` is not 0, `select` `{"pocket_floors": [...]}`, and as `params` the feature's `params` plus `doMultipleDepths` `true`, `useRestMachining` `true` and `rampType` `'plunge'`;
   - windows: the same, with `name` `Window corners <diameter>` and `select` `{"window_loops": [...]}`.
   Features of one kind that share a tool and the same `params` go in one operation.
   Put windows' operations after the pockets' ones: pass the name of the pocket operation as `after`.
4. **Renumber the tools.**
   `renumber_tools({})` puts any new tool in its slot: tools after T3 are numbered in the order the programs first use them.
   Then report the slots: the Carvera's tool changer holds six, and the seventh and later tools need a manual change.
5. **Generate and check**, as in SKILL.md.
   Rest machining plunges into the corners, where the larger end mill has cleared all but a sliver, so rule E4 accepts it.

## Calling the primitives

They are functions in `scripts/carvera_cam.py`, run like the open-pocket recipe's (see that recipe for `call`).
`recipes` and `tools` change nothing; `add_operation` and `renumber_tools` change the document.

```python
lib = call('tools', {})
used = {t['tool'] for t in lib['in_use']}
small = [t for t in lib['library'] if t['type'] == 'flat end mill' and t['diameter_mm'] < 3.175]
for e in [e for e in call('recipes', {}) if e['recipe'] == 'corners']:
    after = e['after']
    for kind, key, sel in (('pocket', 'floor', 'pocket_floors'), ('window', 'window', 'window_loops')):
        groups = {}  # tool -> features
        for f in [f for f in e['features'] if f['kind'] == kind]:
            fits = [t for t in small if t['diameter_mm'] / 2 <= max(f['corner_radius_mm'], 0.5) + 1e-3
                    and t['flute_mm'] >= f['depth_mm'] + 0.5]
            if not fits:
                print('no tool for', kind, f[key])  # tell the user
                continue
            t = max([t for t in fits if t['tool'] in used] or fits, key=lambda t: t['diameter_mm'])
            used.add(t['tool'])
            groups.setdefault((t['tool'], f.get('top_mm', 0)), []).append(f)
        for (tool, top), fs in sorted(groups.items(), key=lambda g: g[0][1]):
            d = [t['diameter_mm'] for t in small if t['tool'] == tool][0]
            o = call('add_operation', {
                'setup': e['setup'], 'after': after, 'strategy': 'pocket2d', 'tool': tool,
                'name': '%s corners %g' % (kind.capitalize(), d) + (' from %g mm' % top if top else ''),
                'params': dict(fs[0]['params'], doMultipleDepths='true', useRestMachining='true',
                               rampType="'plunge'"),
                'select': {sel: [f[key] for f in fs]}})
            after = o['operation']
call('renumber_tools', {})
```

The example puts features of one kind, tool and `top_mm` in one operation with the first feature's `params`, shallowest first; give a feature whose `params` differ otherwise its own operation.

## Shapes the steps do not cover

- **A corner deeper than any small end mill's flute** stays at the 3.175 mm end mill's radius; tell the user.
- **A slot narrower than 3.175 mm, and a pocket reached only through one,** are not listed here: `build` cuts them, as conventions.md's "Slots" describes.

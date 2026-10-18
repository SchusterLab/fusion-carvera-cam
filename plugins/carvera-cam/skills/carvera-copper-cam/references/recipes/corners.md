# Recipe: inside corners the 3.175 mm end mill cannot reach

Cut the inside corners of pockets and windows that are tighter than the 3.175 mm end mill's radius (1.5875 mm) with a smaller end mill, by rest machining: a 2D pocket that cuts only the stock the larger end mill left behind.
The 3.175 mm end mill leaves a corner of radius 1.5875 mm wherever the part's corner is tighter, so the small end mill takes only the slivers in the corners.

## When it applies

[part.md](part.md) sends here at its step 23, after the slots, for the setup's features whose `corner_radius` (in `features`) is more than 0.001 mm below 1.5875 mm, 0 for a sharp corner:

- each pocket floor of the setup's side, unless a slot cuts it (`slot` or `fed_by_slot` set);
- in the top setup, each window (a through opening that is not a round hole).

For each, the cut runs:

- a pocket: from H (part.md's "Heights"), or, for a floor with an `inside_region`, from that region's floor (`'from point'` at any of the region's floors, offset 0), since the region's pocket has cleared everything above it, corners too; down to the floor (`bottomHeight_mode` `'from contour'`); its depth is the floor's `depth`;
- a window: from H through the part, 0.5 mm past its far face (part.md's "Through the part"); its depth is the part's thickness plus that 0.5 mm.

## Steps

1. **List the tools.**
   `features({})` gives the small end mills under `library`, with their diameters and flutes.
2. **Choose a tool for each feature.**
   A tool fits a feature when its radius is no larger than the corner radius and its flute is at least 0.5 mm longer than `depth_mm`.
   Of the tools that fit, take one already chosen for another corner, in either setup, if any, so the part needs fewer tools; otherwise take the largest.
   A corner tighter than the smallest end mill's radius (0.5 mm for the 1 mm end mill) cannot be cut to size: take the smallest end mill, and tell the user the corner stays at that radius.
   When no tool's flute is long enough, leave the feature out and tell the user its corners keep the 3.175 mm end mill's radius.
3. **Add one operation per tool and kind**, in the setup, in its NC program:
   - pockets: `add_operation` with `strategy` `pocket2d`, the tool's description as `tool`, `name` `Pocket corners <diameter>` (for example `Pocket corners 2`), followed by ` from <depth> mm` for a cut that starts at a region's floor, `select` `{"pockets": ["floor:<i>", ...]}`, and as `params` its heights plus `doMultipleDepths` `true`, `useRestMachining` `true` and `rampType` `'plunge'`;
   - windows: the same, with `name` `Window corners <diameter>` and `select` `{"pockets": ["window:<j>", ...]}`.
   Features of one kind that share a tool and the same start go in one operation, those starting highest first, and the windows' operations after the pockets'.
4. **Renumber the tools.**
   `renumber_tools({})` puts any new tool in its slot: tools after T3 are numbered in the order the programs first use them.
   Then report the slots: the Carvera's tool changer holds six, and the seventh and later tools need a manual change.
5. **Generate and check**, as in SKILL.md.
   Rest machining plunges into the corners, where the larger end mill has cleared all but a sliver, so rule E4 accepts it.

## Calling the primitives

They are functions in `scripts/carvera_cam.py`, run like the open-pocket recipe's (see that recipe for `call`).
`features` changes nothing; `add_operation` and `renumber_tools` change the document.
In this example `top_at_h` and `through` are part.md's "top at H" and "through the part" heights at this point, and `side` the setup's side:

```python
f = call('features', {})
small = f['library']['small_end_mills']
used = set()  # the end mills chosen for corners so far; carry it over to the bottom setup
region_floor = {r['region']: r for r in f['layout']['regions']}
tight = lambda r: r is not None and r < 1.5875 - 1e-3
feats = [('pocket', x['floor'], x['corner_radius'], x['depth'], x['inside_region'])
         for x in f['floors'] if x['role'] == 'pocket' and x['side'] == side
         and x['slot'] is None and x['fed_by_slot'] is None and tight(x['corner_radius'])]
if side == 'top':
    feats += [('window', w['window'], w['corner_radius'], f['part']['thickness_mm'] + 0.5, None)
              for w in f['windows'] if tight(w['corner_radius'])]
for kind in ('pocket', 'window'):
    groups = {}  # (tool, region) -> features
    for _, i, r, depth, reg in [x for x in feats if x[0] == kind]:
        fits = [t for t in small if t['dia_mm'] / 2 <= max(r, 0.5) + 1e-3
                and t['flute_mm'] >= depth + 0.5]
        if not fits:
            print('no tool for', kind, i)  # tell the user
            continue
        t = max([t for t in fits if t['tool'] in used] or fits, key=lambda t: t['dia_mm'])
        used.add(t['tool'])
        groups.setdefault((t['tool'], t['dia_mm'], reg), []).append(i)
    # the cuts that start highest first: from H, then from the shallowest region floor
    for (tool, d, reg), items in sorted(groups.items(), key=lambda g: (
            g[0][2] is not None, 0 if g[0][2] is None else abs(region_floor[g[0][2]]['z']))):
        name = '%s corners %g' % (kind.capitalize(), d)
        if kind == 'pocket':
            if reg is None:
                heights = dict(top_at_h)
            else:
                heights = {'topHeight_mode': "'from point'", 'topHeight_offset': '0mm',
                           'topHeight_ref': {'ref': 'floor:%d' % region_floor[reg]['floors'][0]}}
                name += ' from %g mm' % f['floors'][region_floor[reg]['floors'][0]]['depth']
            heights['bottomHeight_mode'] = "'from contour'"
        else:
            heights = dict(top_at_h, **through)
        call('add_operation', {
            'setup': side, 'program': side, 'strategy': 'pocket2d', 'tool': tool, 'name': name,
            'params': dict(heights, doMultipleDepths='true', useRestMachining='true',
                           rampType="'plunge'"),
            'select': {'pockets': ['%s:%d' % ('floor' if kind == 'pocket' else 'window', i)
                                   for i in items]}})
call('renumber_tools', {})
```

For a bottom-setup region, sort by the region floor's depth below the bottom face instead of its z.

## Shapes the steps do not cover

- **A corner deeper than any small end mill's flute** stays at the 3.175 mm end mill's radius; tell the user.
- **A slot narrower than 3.175 mm, and a pocket reached only through one,** are not cut here: part.md's step 22 cuts them, as conventions.md's "Slots" describes.

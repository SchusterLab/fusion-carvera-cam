"""Check the toolpaths by Fusion's own simulation: its collisions, and the stock it leaves.

The API cannot run a simulation or read its stock, so this works with sim_ui.ps1,
which drives the simulation's window:

  simulate(args)     in Fusion: simulates one setup with the machine (Simulate with
                     Machine), with its Issues panel open for sim_ui.ps1 to read. A setup
                     simulated on its own starts from the whole stock.
  stop(args)         in Fusion: exits the simulation.
  export_part(args)  in Fusion: writes the machined body as an STL.
  compare(args)      anywhere: compares the stock sim_ui.ps1 saved with the part's STL.

compare is plain Python with no packages, so it runs inside Fusion (through runpy)
or outside it:

    python sim_check.py <part .stl> <stock .stl, one per setup> ...

Both files must be in the same frame and in mm: the simulation's Save Stock
writes the stock in the design's frame, and export_part() writes the part
there too.

A vertical ray through each column of a grid (pitch 0.05 mm by default) finds
where each mesh is solid along Z; the difference of the two is, per column,
the copper left that the part does not have ("left") and the copper cut away
that the part has ("overcut"). Columns that differ by more than `tol` group
into regions, reported largest first with their volume, area, deepest
difference, width, Z range and centre, in the design's frame (mm).

Only the part's footprint and `margin` round it are compared, so the stock
round an outline cut (which starts 3.175 mm out) stays out of it.
"""

import json
import math
import struct
import sys
from collections import deque


def read_stl(path):
    """Triangles of a binary or ASCII STL, as ((x, y, z), (x, y, z), (x, y, z))."""
    data = open(path, 'rb').read()
    if len(data) >= 84:
        n = struct.unpack_from('<I', data, 80)[0]
        if 84 + 50 * n == len(data):
            tris = []
            for k in range(n):
                f = struct.unpack_from('<12f', data, 84 + 50 * k)
                tris.append((f[3:6], f[6:9], f[9:12]))
            return tris
    pts = [tuple(float(v) for v in line.split()[1:4])
           for line in data.decode('ascii', 'replace').splitlines()
           if line.strip().startswith('vertex')]
    return [tuple(pts[i:i + 3]) for i in range(0, len(pts) - 2, 3)]


def bounds(tris):
    xs = [p[0] for t in tris for p in t]
    ys = [p[1] for t in tris for p in t]
    zs = [p[2] for t in tris for p in t]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def volume(tris):
    v = 0.0
    for a, b, c in tris:
        v += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
              + a[2] * (b[0] * c[1] - b[1] * c[0]))
    return v / 6


class Grid:
    # Column centres sit off the round numbers by an irrational fraction of the
    # pitch, so that no ray runs exactly through a mesh edge or vertex.
    def __init__(self, x0, y0, nx, ny, h):
        self.x0 = x0 + h * (0.5 + 0.0137 * math.sqrt(2))
        self.y0 = y0 + h * (0.5 + 0.0119 * math.sqrt(3))
        self.nx, self.ny, self.h = nx, ny, h

    def centre(self, i, j):
        return self.x0 + i * self.h, self.y0 + j * self.h


def column_hits(tris, g):
    """{(i, j): sorted Z where the ray up column (i, j) crosses the mesh}."""
    hits = {}
    for a, b, c in tris:
        d = (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])
        if abs(d) < 1e-12:
            continue  # a wall: vertical rays miss it
        lo_i = max(0, math.ceil((min(a[0], b[0], c[0]) - g.x0) / g.h))
        hi_i = min(g.nx - 1, math.floor((max(a[0], b[0], c[0]) - g.x0) / g.h))
        lo_j = max(0, math.ceil((min(a[1], b[1], c[1]) - g.y0) / g.h))
        hi_j = min(g.ny - 1, math.floor((max(a[1], b[1], c[1]) - g.y0) / g.h))
        for i in range(lo_i, hi_i + 1):
            x = g.x0 + i * g.h
            for j in range(lo_j, hi_j + 1):
                y = g.y0 + j * g.h
                u = ((b[0] - x) * (c[1] - y) - (c[0] - x) * (b[1] - y)) / d
                v = ((c[0] - x) * (a[1] - y) - (a[0] - x) * (c[1] - y)) / d
                w = 1 - u - v
                if u < 0 or v < 0 or w < 0:
                    continue
                hits.setdefault((i, j), []).append(u * a[2] + v * b[2] + w * c[2])
    for z in hits.values():
        z.sort()
    return hits


def intervals(zs):
    """Solid spans along a column; None when the ray crossed the mesh an odd number of times."""
    if len(zs) % 2:
        return None
    return [(zs[k], zs[k + 1]) for k in range(0, len(zs), 2) if zs[k + 1] > zs[k]]


def minus(a, b):
    """Spans of a not in b."""
    out = []
    for lo, hi in a:
        cur = lo
        for blo, bhi in b:
            if bhi <= cur or blo >= hi:
                continue
            if blo > cur:
                out.append((cur, blo))
            cur = max(cur, bhi)
            if cur >= hi:
                break
        if cur < hi:
            out.append((cur, hi))
    return out


def intersect(a, b):
    """Spans in both a and b."""
    out = []
    for lo, hi in a:
        for blo, bhi in b:
            l, u = max(lo, blo), min(hi, bhi)
            if u > l:
                out.append((l, u))
    out.sort()
    return out


def regions(cells, h, tol):
    """Group columns whose difference exceeds tol, each with its size and place."""
    keep = {k: v for k, v in cells.items() if sum(hi - lo for lo, hi in v) > tol}
    seen, out = set(), []
    for start in keep:
        if start in seen:
            continue
        seen.add(start)
        group, queue = [], deque([start])
        while queue:
            i, j = queue.popleft()
            group.append((i, j))
            for n in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if n in keep and n not in seen:
                    seen.add(n)
                    queue.append(n)
        # Width: twice the largest number of steps from a column to the region's edge.
        gset = set(group)
        dist = {}
        queue = deque()
        for i, j in group:
            if any(n not in gset for n in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1))):
                dist[(i, j)] = 1
                queue.append((i, j))
        while queue:
            i, j = queue.popleft()
            for n in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if n in gset and n not in dist:
                    dist[n] = dist[(i, j)] + 1
                    queue.append(n)
        depth = [sum(hi - lo for lo, hi in keep[c]) for c in group]
        zlo = min(lo for c in group for lo, _ in keep[c])
        zhi = max(hi for c in group for _, hi in keep[c])
        out.append({'cells': group, 'volume': sum(depth) * h * h, 'area': len(group) * h * h,
                    'depth': max(depth), 'width': (2 * max(dist.values()) - 1) * h,
                    'z': (zlo, zhi)})
    out.sort(key=lambda r: -r['volume'])
    return out


def compare(args):
    """Compare simulated-stock STLs with a part STL; prints and returns a JSON report.

    args: stock (a path, or a list of paths: the stock each setup's own simulation left,
    whose intersection is what all of them leave, since each only removes copper), part
    (path), pitch (mm, default 0.05), tol (mm, default 0.02: smaller
    differences along Z are the simulation's own accuracy), margin (mm round the part's
    footprint, default 1), min_volume (mm3, default 0.001) and min_width (mm, default 0.1:
    a region one column wide is where the two meshes facet a wall differently), below
    either of which a region is only counted, max_regions (default 40).
    """
    paths = args['stock'] if isinstance(args['stock'], (list, tuple)) else [args['stock']]
    stocks, part = [read_stl(p) for p in paths], read_stl(args['part'])
    h = float(args.get('pitch', 0.05))
    tol = float(args.get('tol', 0.02))
    margin = float(args.get('margin', 1.0))
    (px0, py0, pz0), (px1, py1, pz1) = bounds(part)
    (sx0, sy0, sz0), (sx1, sy1, sz1) = bounds([t for st in stocks for t in st])
    x0, y0 = px0 - margin, py0 - margin
    g = Grid(x0, y0, int(math.ceil((px1 - px0 + 2 * margin) / h)),
             int(math.ceil((py1 - py0 + 2 * margin) / h)), h)
    ph, shs = column_hits(part, g), [column_hits(st, g) for st in stocks]
    left, over, odd = {}, {}, 0
    for key in set(ph).union(*shs):
        a = intervals(ph.get(key, []))
        bs = [intervals(sh.get(key, [])) for sh in shs]
        if a is None or None in bs:
            odd += 1
            continue
        b = bs[0]
        for other in bs[1:]:
            b = intersect(b, other)
        lft, ovr = minus(b, a), minus(a, b)
        if lft:
            left[key] = lft
        if ovr:
            over[key] = ovr
    min_volume = float(args.get('min_volume', 0.001))
    min_width = float(args.get('min_width', 0.1))
    max_regions = int(args.get('max_regions', 40))
    report = {
        'pitch_mm': h, 'tol_mm': tol, 'margin_mm': margin, 'min_width_mm': min_width,
        'part_volume_mm3': round(volume(part), 3),
        'stock_volumes_mm3': [round(volume(st), 3) for st in stocks],
        'part_bbox_mm': [round(v, 3) for v in (px0, py0, pz0, px1, py1, pz1)],
        'stock_bbox_mm': [round(v, 3) for v in (sx0, sy0, sz0, sx1, sy1, sz1)],
        'columns_skipped': odd,
    }
    for name, cells in (('left', left), ('overcut', over)):
        rs = regions(cells, h, tol)
        big = [r for r in rs if r['volume'] >= min_volume and r['width'] >= min_width - 1e-9]
        report[name + '_volume_mm3'] = round(sum(r['volume'] for r in rs), 4)
        report[name + '_small_regions'] = len(rs) - len(big)
        report[name + '_small_volume_mm3'] = round(sum(r['volume'] for r in rs) - sum(r['volume'] for r in big), 4)
        report[name] = []
        for r in big[:max_regions]:
            xs = [g.centre(i, j)[0] for i, j in r['cells']]
            ys = [g.centre(i, j)[1] for i, j in r['cells']]
            report[name].append({
                'volume_mm3': round(r['volume'], 4), 'area_mm2': round(r['area'], 4),
                'depth_mm': round(r['depth'], 3), 'width_mm': round(r['width'], 3),
                'z_mm': [round(r['z'][0], 3), round(r['z'][1], 3)],
                'centre_mm': [round(sum(xs) / len(xs), 2), round(sum(ys) / len(ys), 2)],
                'xy_extent_mm': [round(min(xs), 2), round(min(ys), 2), round(max(xs), 2), round(max(ys), 2)],
            })
        report[name + '_more_regions'] = max(0, len(big) - max_regions)
    print(json.dumps(report, indent=1))
    return report


def export_part(args):
    """In Fusion: write a body as a binary STL in mm, in the design's frame.

    args: path; deviation (mm, default 0.005: how far the mesh may stand off the faces);
    body (name, or '<occurrence>/<body>'), default the machined body: the
    copy in the 'carvera-cam part' component when build turned the part over, else the
    one visible solid body in the root component.
    """
    import adsk.core
    import adsk.fusion
    app = adsk.core.Application.get()
    des = adsk.fusion.Design.cast(app.activeDocument.products.itemByProductType('DesignProductType'))
    root = des.rootComponent
    body = None
    name = args.get('body')
    if name and '/' in name:
        occ, bname = name.split('/', 1)
        body = root.allOccurrences.itemByName(occ) and root.allOccurrences.itemByName(occ).bRepBodies.itemByName(bname)
    elif name:
        body = root.bRepBodies.itemByName(name)
    else:
        for occ in root.allOccurrences:
            if occ.component.name == 'carvera-cam part':
                body = next((b for b in occ.bRepBodies if b.isSolid), None)
        if body is None:
            solids = [b for b in root.bRepBodies if b.isSolid and b.isVisible]
            if len(solids) != 1:
                raise ValueError('pass body: %d visible solid bodies in the root' % len(solids))
            body = solids[0]
    if body is None:
        raise ValueError('no body %r' % name)
    em = des.exportManager
    o = em.createSTLExportOptions(body, args['path'])
    # Fusion's preset refinements scale with the body's size: High gave a 120 x 30 mm plate
    # 932 triangles, and the comparison then found full-depth rings of overcut round its
    # 3.4 mm holes that a mesh bound to 0.005 mm does not show.
    o.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementCustom
    o.surfaceDeviation = float(args.get('deviation', 0.005)) / 10  # mm -> cm
    o.normalDeviation = math.radians(5)
    o.isBinaryFormat = True
    o.unitType = adsk.fusion.DistanceUnits.MillimeterDistanceUnits
    if not em.execute(o):
        raise RuntimeError('STL export failed')
    print(json.dumps({'body': body.name, 'path': args['path'], 'volume_mm3': round(body.volume * 1000, 3)}))


SIM_COMMANDS = ('IronMachineSimulation', 'IronSimulation')


def _cam():
    import adsk.cam
    import adsk.core
    app = adsk.core.Application.get()
    return app.userInterface, adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType('CAMProductType'))


def simulate(args):
    """In Fusion: start Simulate with Machine on one setup, with its Issues panel open.

    args: setup (its name). Simulate one setup at a time: with several selected, Fusion
    warns that their work offsets differ and reports collisions with the fixture that the
    setups' own simulations do not have. With a simulation open, Fusion refuses this script
    (it is not read-only): stop() that one first, in a call of its own. The simulation
    opens after the script returns; sim_ui.ps1 then reads it.
    """
    ui, cam = _cam()
    setup = cam.setups.itemByName(args['setup'])
    if setup is None:
        raise ValueError('no setup %r; setups: %s' % (args['setup'], [s.name for s in cam.setups]))
    ops = [o for o in setup.allOperations if not o.isSuppressed]
    bad = [o.name for o in ops if not o.isToolpathValid]
    if bad:
        raise RuntimeError('generate these toolpaths first: %s' % bad)
    ui.activeSelections.clear()
    ui.activeSelections.add(setup)
    ui.commandDefinitions.itemById(SIM_COMMANDS[0]).execute()
    ui.commandDefinitions.itemById('SimulationIssues').execute()
    # An operation Fusion generated but found nothing to cut has no toolpath; the
    # simulation passes over it.
    print(json.dumps({'simulating': setup.name, 'empty': [o.name for o in ops if not o.hasToolpath]}))


def stop(args):
    """In Fusion: exit the simulation, if one is open."""
    ui, _ = _cam()
    was = ui.activeCommand
    if was in SIM_COMMANDS:
        ui.commandDefinitions.itemById('SimulationStop').execute()
    print(json.dumps({'stopped': was in SIM_COMMANDS}))


if __name__ == '__main__':
    if len(sys.argv) < 3:
        sys.exit('usage: python sim_check.py <part .stl> <stock .stl> [<stock .stl> ...]')
    compare({'part': sys.argv[1], 'stock': sys.argv[2:]})

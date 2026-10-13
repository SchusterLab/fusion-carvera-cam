"""Toolpaths for copper parts on SchusterLab's Makera Carvera, in Autodesk Fusion.

This module runs inside Fusion's Python interpreter (through the Fusion MCP
`script` feature or a Fusion script). It encodes the lab's CAM conventions,
learned from the parts in the project 2026-01-06_smpm-sample-package-6port:

- the Carvera 3-axis machine and the Carvera post, the latest versions in the
  Carvera Community's profiles repository on GitHub;
- the lab's tools, exported from its Fusion library "Carvera Tools/My Tools"
  to assets/carvera-tools.json, each with the feed preset "Copper 101-H02"
  (aluminum runs the same program at 200% override);
- a top setup and a flipped bottom setup, the part held to the stock by four
  tabs that a separate "_tabs" program cuts last.

Entry points, each taking one dict of arguments:

    analyze(args)   recognize the part's features and print them as JSON
    build(args)     create the setups, operations and NC programs
    generate(args)  generate the toolpaths and report errors and warnings

Lengths in args are millimetres. Fusion's internal unit is the centimetre.
"""

import json
import math
import os
import re
import time
import tomllib
import urllib.request

import adsk.cam
import adsk.core
import adsk.fusion

SKILL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
# The lab's tools; export_tools() rewrites this file from the Fusion library.
TOOLS_FILE = os.path.join(SKILL_DIR, 'assets', 'carvera-tools.json')
TOOL_LIBRARY_URL = 'cloud://Carvera Tools/My Tools'
# The machine and post: the newest files on the Carvera Community's main
# branch, cached so that a build without network uses the last ones fetched.
COMMUNITY_API = ('https://api.github.com/repos/Carvera-Community/Carvera_Community_Profiles'
                 '/contents/CAM_Post_Processors/Fusion360-profiles?ref=main')
COMMUNITY_POST = 'Carvera.cps'
COMMUNITY_MACHINE = re.compile(r'Makera Carvera 3-axis Community Version ([\d.]+)\.f3d$')
CACHE_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
                         'carvera-cam', 'community')
# Fusion project that holds the machine's uploaded 3D model; see _model_urn().
MODEL_PROJECT = 'carvera-cam'
PRESET_NAMES = ('Copper 101-H02', 'Copper')
SETUP_TAG = '(carvera-cam)'

DRILL_3175 = '3.175*7.4mm Drill'
DRILL_3175_LONG = '3.175*12.7mm Drill'
ENDMILL = 'Spiral O Metal 3.175*12mm'
ENDMILL_D = 3.175
# Smaller end mills, largest first: (description, diameter, flute length).
SMALL_ENDMILLS = [
    ('Spiral O Metal 2.5*12mm', 2.5, 12.0),
    ('Spiral O Metal 2*8mm', 2.0, 8.0),
    ('Spiral O Metal 1.5*6mm', 1.5, 6.0),
    ('Spiral O Metal 1*3mm', 1.0, 3.0),
]
# matched by prefix: the library stores this name with a mis-encoded inch mark
CHAMFER = 'Chamfering Bit - 1/8'
# Tapped holes: the lab's pre-drilling and thread-milling recipe, per thread
# size, lives in references/threads.toml, which is read on every run.
THREADS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'references', 'threads.toml')


def _load_threads(path=THREADS_FILE):
    with open(path, 'rb') as f:
        data = tomllib.load(f)
    common = {k: data.pop(k) for k in ('drill', 'thread')}
    return common, data


THREAD_COMMON, THREAD_RECIPES = _load_threads()
# name: (modelled diameter, tap drill or None to bore, thread mill, pitch)
THREADS = {name: (r['modelled_hole'], None if 'bore' in r else r['drill'],
                  r['thread_mill'], r['pitch'])
           for name, r in THREAD_RECIPES.items()}

EPS = 1e-4  # cm


# ---------------------------------------------------------------- helpers

def _app():
    return adsk.core.Application.get()


def mm(v_cm):
    return round(v_cm * 10.0, 4)


def _normal(face):
    ok, n = face.evaluator.getNormalAtPoint(face.pointOnFace)
    return n


def _other_face(edge, face):
    for f in edge.faces:
        if f.tempId != face.tempId:
            return f
    return None


def _is_vertical_cylinder(face):
    g = face.geometry
    return isinstance(g, adsk.core.Cylinder) and abs(abs(g.axis.z) - 1.0) < 1e-6


def _is_concave_cylinder(face):
    """True when the material is outside the cylinder (a hole or an inside corner)."""
    g = face.geometry
    p = face.pointOnFace
    v = g.origin.vectorTo(p)
    a = g.axis.copy()
    a.normalize()
    a.scaleBy(v.dotProduct(a))
    v.subtract(a)
    return _normal(face).dotProduct(v) < 0


def _is_full_circle_face(face):
    return any(isinstance(e.geometry, adsk.core.Circle3D) for e in face.edges)


def _zrange(entity):
    bb = entity.boundingBox
    return bb.minPoint.z, bb.maxPoint.z


def _set(params, name, expression):
    p = params.itemByName(name)
    if p is None:
        raise KeyError('no CAM parameter named %r' % name)
    p.expression = expression


def _try_set(params, name, expression):
    p = params.itemByName(name)
    if p is not None:
        p.expression = expression
        return True
    return False


def _design():
    # Not products.itemByProductType: once CAM has a machine, the document also
    # holds the machine's design (Spindle, Bed, ...) as a second Design product.
    return adsk.fusion.FusionDocument.cast(_app().activeDocument).design


def _cam():
    doc = _app().activeDocument
    try:
        cam = doc.products.itemByProductType('CAMProductType')
    except RuntimeError:
        cam = None
    if cam is None:
        # activating the Manufacture workspace creates the CAM product; if it is
        # already active (say after a failed script), leave it and come back
        ws = _app().userInterface.workspaces
        if _app().userInterface.activeWorkspace.id == 'CAMEnvironment':
            ws.itemById('FusionSolidEnvironment').activate()
        ws.itemById('CAMEnvironment').activate()
        cam = doc.products.itemByProductType('CAMProductType')
    return adsk.cam.CAM.cast(cam)


def _find_body(args):
    design = _design()
    bodies = []
    for b in design.rootComponent.bRepBodies:
        bodies.append(b)
    for occ in design.rootComponent.allOccurrences:
        if occ.component.name.startswith(VISE_PREFIX):
            continue  # the vise an earlier build added
        for b in occ.bRepBodies:
            bodies.append(b)
    bodies = [b for b in bodies if b.isSolid and b.isVisible]
    name = args.get('body')
    if name:
        bodies = [b for b in bodies if b.name == name]
    if len(bodies) != 1:
        raise RuntimeError('expected one visible solid body%s, found %s; pass args["body"]'
                           % (' named %r' % name if name else '', [b.name for b in bodies]))
    return bodies[0]


# --------------------------------------------------------------- analysis

def _thread_faces(design):
    """Map face tempId -> thread name (e.g. 'M2') for tapped holes and thread features.

    The lab models tapped holes with the Hole command's Tapped type (not modeled),
    which leaves a plain hole at the minor diameter and stores the thread on the
    hole feature.
    """
    out = {}
    comps = [design.rootComponent] + [o.component for o in design.rootComponent.allOccurrences]
    for comp in comps:
        for hf in comp.features.holeFeatures:
            try:
                if hf.holeTapType != adsk.fusion.HoleTapTypes.TappedHoleTapType:
                    continue
                name = hf.tappedHoleInfo.threadDesignation.split('x')[0]
                for f in hf.sideFaces:
                    out[f.tempId] = name
            except Exception:
                pass
        for tf in comp.features.threadFeatures:
            try:
                name = tf.threadInfo.threadDesignation.split('x')[0]
                for f in tf.faces:
                    out[f.tempId] = name
            except Exception:
                pass
    return out


def _end_open(hole, edge, at_top):
    """Is the hole open (not floored) at this circular end edge?"""
    other = _other_face(edge, hole)
    if other is None:
        return True
    g = other.geometry
    if isinstance(g, adsk.core.Plane):
        nz = _normal(other).z
        return nz > 0.5 if at_top else nz < -0.5
    if isinstance(g, adsk.core.Cone):
        # A chamfer or countersink widens away from the hole and keeps it open;
        # a drill point narrows and closes it.
        bb = other.boundingBox
        r = hole.geometry.radius
        wider = (bb.maxPoint.x - bb.minPoint.x) / 2 > r + EPS
        return wider
    return True


def _classify_hole(face, ztop, zbot, threads):
    g = face.geometry
    z0, z1 = _zrange(face)
    top_edges = [e for e in face.edges if isinstance(e.geometry, adsk.core.Circle3D)
                 and abs(e.geometry.center.z - z1) < EPS]
    bot_edges = [e for e in face.edges if isinstance(e.geometry, adsk.core.Circle3D)
                 and abs(e.geometry.center.z - z0) < EPS]
    top_open = bool(top_edges) and _end_open(face, top_edges[0], True)
    bot_open = bool(bot_edges) and _end_open(face, bot_edges[0], False)
    side = 'top' if top_open else ('bottom' if bot_open else 'none')
    return {
        'face': face,
        'dia': mm(2 * g.radius),
        'z0': mm(z0), 'z1': mm(z1),
        'depth': mm(z1 - z0),
        'side': side,
        'through': top_open and bot_open,
        'x': mm(g.origin.x), 'y': mm(g.origin.y),
        'thread': threads.get(face.tempId),
    }


def _is_vertical_plane(face):
    return isinstance(face.geometry, adsk.core.Plane) and abs(_normal(face).z) < 1e-6


def _sharp_inside_corner(body, w1, w2, edge):
    """Two vertical planar walls meeting at a vertical edge: is the corner concave?"""
    g = edge.geometry
    mid = adsk.core.Point3D.create((g.startPoint.x + g.endPoint.x) / 2,
                                   (g.startPoint.y + g.endPoint.y) / 2,
                                   (g.startPoint.z + g.endPoint.z) / 2)
    n1, n2 = _normal(w1), _normal(w2)
    if abs(n1.dotProduct(n2)) > 0.999:
        return False
    step = 0.002  # 20 um
    p = adsk.core.Point3D.create(mid.x + step * (n1.x - n2.x), mid.y + step * (n1.y - n2.y), mid.z)
    return body.pointContainment(p) == adsk.fusion.PointContainment.PointInsidePointContainment


def _floor_corner_radius(floor, upward, body):
    """Smallest inside-corner radius of the vertical walls around a floor face (mm).

    Returns 0.0 for a sharp inside corner and None when there are no inside corners.
    """
    rmin = None
    fz = floor.pointOnFace.z
    walls = []
    for e in floor.edges:
        w = _other_face(e, floor)
        if w is None:
            continue
        wz0, wz1 = _zrange(w)
        if not ((upward and wz1 > fz + EPS) or (not upward and wz0 < fz - EPS)):
            continue
        walls.append(w)
        if _is_vertical_cylinder(w) and not _is_full_circle_face(w) and _is_concave_cylinder(w):
            r = mm(w.geometry.radius)
            rmin = r if rmin is None else min(rmin, r)
    wall_ids = {w.tempId for w in walls}
    for w in walls:
        if not _is_vertical_plane(w):
            continue
        for e in w.edges:
            g = e.geometry
            if not isinstance(g, adsk.core.Line3D) or abs(g.startPoint.z - g.endPoint.z) < EPS:
                continue
            o = _other_face(e, w)
            if o is not None and o.tempId in wall_ids and _is_vertical_plane(o):
                if _sharp_inside_corner(body, w, o, e):
                    return 0.0
    return rmin


def _walls_of_loop(loop, face):
    return [_other_face(e, face) for e in loop.edges]


def _loop_ring(lp):
    """XY polygon (cm) of a loop, walked in co-edge order."""
    ring = []
    for pc in _loop_pieces(lp):
        n = 2 if isinstance(pc['edge'].geometry, adsk.core.Line3D) else 24
        ring += [(p.x, p.y) for p in (_point_on(pc, pc['len'] * k / n) for k in range(n))]
    return ring


def _ring_area(ring):
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]))) / 2


def _outline_loop(faces):
    """The part's outline: the outer loop of the top or bottom face enclosing more area.

    A pocket that opens to the side notches one face's loop; the other face's
    loop is then the outline at the tabs' height.
    """
    best = None
    for f in faces:
        for lp in f.loops:
            if lp.isOuter:
                ring = _loop_ring(lp)
                area = _ring_area(ring)
                if best is None or area > best[0] + 1e-6:
                    best = (area, lp, ring)
    return (best[1], best[2]) if best else (None, None)


def _outline_polygon(faces):
    """XY polygon (cm) of the part's outline."""
    return _outline_loop(faces)[1]


def _inside(poly, x, y):
    """Even-odd point-in-polygon test."""
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _opens_sideways(floor, outline):
    """Where an outer edge of the floor leads out of the part's outline, if anywhere.

    Returns (x, y, dx, dy) in cm: the edge's midpoint and the unit direction out
    of the part, or None.
    """
    fev = floor.evaluator
    z = floor.pointOnFace.z
    for lp in floor.loops:
        if not lp.isOuter:
            continue
        for e in lp.edges:
            ev = e.evaluator
            ok, t0, t1 = ev.getParameterExtents()
            tm = (t0 + t1) / 2
            ok, p = ev.getPointAtParameter(tm)
            ok, d = ev.getFirstDerivative(tm)
            n = math.hypot(d.x, d.y)
            if n < 1e-9:
                continue
            for sgn in (1, -1):
                qx, qy = p.x - sgn * d.y / n * 0.05, p.y + sgn * d.x / n * 0.05
                ok, uv = fev.getParameterAtPoint(adsk.core.Point3D.create(qx, qy, z))
                if ok and fev.isParameterOnFace(uv):
                    continue  # this side is the floor itself
                if not _inside(outline, qx, qy):
                    return (p.x, p.y, -sgn * d.y / n, sgn * d.x / n)
    return None


def _steps_down(floor, upward, hole_ids):
    """Does an outer edge of the floor meet a wall that continues below it (above, for the
    bottom side)?"""
    z = floor.pointOnFace.z
    for lp in floor.loops:
        if not lp.isOuter:
            continue
        for e in lp.edges:
            w = _other_face(e, floor)
            if w is None or w.tempId in hole_ids or abs(_normal(w).z) > 0.1:
                continue
            wz0, wz1 = _zrange(w)
            if (upward and wz0 < z - EPS) or (not upward and wz1 > z + EPS):
                return True
    return False


def _tab_height(a, band, overlap, requested=None):
    """Model Z (mm) of the tab band's centre: the mid-plane unless a side opening is there."""
    ztop, zbot = a['ztop'], a['zbot']
    openings = []
    for f in a['floors']:
        if f.get('side_open'):
            openings.append((f['z'], ztop) if f['side'] == 'top' else (zbot, f['z']))

    def clear(zc):
        lo, hi = zc - band / 2, zc + band / 2
        return all(hi <= o_lo + 1e-6 or lo >= o_hi - 1e-6 for o_lo, o_hi in openings)

    if requested is not None:
        return requested, clear(requested), openings
    mid = (ztop + zbot) / 2
    lo, hi = zbot + band / 2 + overlap, ztop - band / 2 - overlap
    for k in range(int((hi - lo) / 0.05) + 2):
        for zc in (mid + k * 0.05, mid - k * 0.05):
            if lo - 1e-6 <= zc <= hi + 1e-6 and clear(zc):
                return round(zc, 3), True, openings
    return mid, False, openings


def analyze_body(body, design=None):
    design = design or _design()
    threads = _thread_faces(design)
    bb = body.boundingBox
    ztop, zbot = bb.maxPoint.z, bb.minPoint.z
    holes, floors, windows = [], [], []
    hole_ids = set()

    for f in body.faces:
        if _is_vertical_cylinder(f) and _is_full_circle_face(f) and _is_concave_cylinder(f):
            h = _classify_hole(f, ztop, zbot, threads)
            holes.append(h)
            hole_ids.add(f.tempId)

    top_face = bot_face = None
    for f in body.faces:
        if not isinstance(f.geometry, adsk.core.Plane):
            continue
        nz = _normal(f).z
        if abs(nz) < 0.99:
            continue
        others = [_other_face(e, f) for e in f.edges]
        if others and all(o is not None and o.tempId in hole_ids for o in others):
            continue  # floor of a hole or counterbore; the bore/drill handles it
        z = f.pointOnFace.z
        up = nz > 0
        depth = (ztop - z) if up else (z - zbot)
        entry = {
            'face': f,
            'side': 'top' if up else 'bottom',
            'z': mm(z),
            'depth': mm(depth),
            'area': round(f.area * 100, 2),
            'corner_radius': _floor_corner_radius(f, up, body),
            'role': 'face' if depth < EPS else 'pocket',
        }
        floors.append(entry)
        if up and depth < EPS and (top_face is None or f.area > top_face.area):
            top_face = f
        if not up and depth < EPS and (bot_face is None or f.area > bot_face.area):
            bot_face = f
        # through windows: inner loops whose walls run down to the bottom of the part
        if up:
            for loop in f.loops:
                if loop.isOuter:
                    continue
                walls = _walls_of_loop(loop, f)
                if any(w is None or w.tempId in hole_ids for w in walls):
                    continue
                wz0 = min(_zrange(w)[0] for w in walls)
                if wz0 <= zbot + EPS:
                    rmin = None
                    for w in walls:
                        if _is_vertical_cylinder(w) and _is_concave_cylinder(w):
                            r = mm(w.geometry.radius)
                            rmin = r if rmin is None else min(rmin, r)
                    windows.append({'face': f, 'loop': loop, 'z': mm(z),
                                    'depth': mm(z - zbot), 'corner_radius': rmin})

    # pockets open to the side of the part: the tool reaches them sideways, so the
    # tabs must not sit at their height
    outline = _outline_polygon([f for f in (top_face, bot_face) if f is not None])
    for fl in floors:
        fl['opening'] = None
        if fl['role'] == 'pocket' and outline is not None:
            fl['opening'] = _opens_sideways(fl['face'], outline)
        fl['side_open'] = fl['opening'] is not None
        # a shelf: a floor that steps down into a deeper pocket inside the part;
        # Fusion pockets it as one region with the deeper pocket (see _make_layout)
        fl['shelf'] = fl['role'] == 'pocket' and not fl['side_open'] and \
            _steps_down(fl['face'], fl['side'] == 'top', hole_ids)

    return {
        'body': body,
        'size': [mm(bb.maxPoint.x - bb.minPoint.x), mm(bb.maxPoint.y - bb.minPoint.y),
                 mm(ztop - zbot)],
        'ztop': mm(ztop), 'zbot': mm(zbot),
        'holes': holes, 'floors': floors, 'windows': windows,
        'top_face': top_face, 'bottom_face': bot_face,
    }


def _tap_candidate(dia):
    for name, (d, _, _, _) in THREADS.items():
        if abs(dia - d) < 0.03:
            return name
    return None


def _strip(feat):
    return {k: v for k, v in feat.items() if k not in ('face', 'loop', 'body', 'opening')}


def analyze(args=None):
    """Print the recognized features of the part as JSON."""
    args = args or {}
    body = _find_body(args)
    a = analyze_body(body)
    groups = {}
    for h in a['holes']:
        key = (h['dia'], h['side'], h['through'], round(h['depth'], 2), h['thread'])
        g = groups.setdefault(key, dict(_strip(h), count=0, at=[]))
        g['count'] += 1
        g['at'].append([h['x'], h['y']])
        g['tap_candidate'] = h['thread'] or _tap_candidate(h['dia'])
    out = {
        'body': body.name,
        'size_mm': a['size'],
        'ztop': a['ztop'], 'zbot': a['zbot'],
        'holes': list(groups.values()),
        'floors': [_strip(f) for f in a['floors']],
        'windows': [_strip(w) for w in a['windows']],
        'tab_z': _tab_height(a, min(1.0, 0.4 * a['size'][2]), min(0.5, 0.2 * a['size'][2]))[0],
        'has_top_face': a['top_face'] is not None,
        'has_bottom_face': a['bottom_face'] is not None,
    }
    print(json.dumps(out, indent=1))
    return out


# ------------------------------------------------------------- libraries

class Tools:
    """The lab's tools from TOOLS_FILE, numbered into Carvera ATC slots."""

    def __init__(self):
        with open(TOOLS_FILE, encoding='utf-8') as f:
            lib = adsk.cam.ToolLibrary.createFromJson(f.read())
        self.by_desc = {}
        for t in lib:
            d = t.parameters.itemByName('tool_description').value.value
            self.by_desc.setdefault(d, t)
        self.slots = {}   # key -> (tool, preset)
        self.order = []

    def has(self, desc):
        return desc in self.by_desc

    def drills(self):
        out = []
        for d, t in self.by_desc.items():
            p = t.parameters
            if p.itemByName('tool_type').value.value == 'drill' and 'Spot' not in d:
                out.append((d, mm(p.itemByName('tool_diameter').value.value),
                            mm(p.itemByName('tool_fluteLength').value.value)))
        return sorted(out, key=lambda x: x[1])

    def get(self, desc, key=None):
        """Return (tool, preset) for a slot; `key` lets one tool fill two slots (T2/T3)."""
        key = key or desc
        if key in self.slots:
            return self.slots[key]
        src = self.by_desc.get(desc)
        if src is None:
            hits = [d for d in self.by_desc if d.startswith(desc)]
            if len(hits) != 1:
                raise RuntimeError('tool %r is not in %s' % (desc, TOOLS_FILE))
            src = self.by_desc[hits[0]]
        tool = adsk.cam.Tool.createFromJson(src.toJson())
        number = len(self.order) + 1
        tool.parameters.itemByName('tool_number').expression = str(number)
        preset = None
        for name in PRESET_NAMES:
            for i in range(tool.presets.count):
                if tool.presets.item(i).name == name:
                    preset = tool.presets.item(i)
                    break
            if preset:
                break
        self.slots[key] = (tool, preset)
        self.order.append((number, desc, preset.name if preset else None))
        return self.slots[key]


def _fetch_community(warnings):
    """Return local paths of the newest community post and 3-axis machine.

    Downloads only files whose git hash changed since the last fetch. Without
    network, falls back to the cached files, with a warning.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    index_path = os.path.join(CACHE_DIR, 'index.json')
    try:
        with open(index_path, encoding='utf-8') as f:
            index = json.load(f)
    except (OSError, ValueError):
        index = {}
    try:
        req = urllib.request.Request(COMMUNITY_API, headers={'User-Agent': 'carvera-cam'})
        with urllib.request.urlopen(req, timeout=15) as r:
            listing = json.load(r)
        machines = [(tuple(int(n) for n in m.group(1).split('.')), e) for e in listing
                    for m in [COMMUNITY_MACHINE.match(e['name'])] if m]
        wanted = {'post': next(e for e in listing if e['name'] == COMMUNITY_POST),
                  'machine': max(machines, key=lambda x: x[0])[1]}
        for key, e in wanted.items():
            path = os.path.join(CACHE_DIR, e['name'])
            if index.get(key, {}).get('sha') != e['sha'] or not os.path.exists(path):
                with urllib.request.urlopen(e['download_url'], timeout=60) as r:
                    data = r.read()
                with open(path, 'wb') as f:
                    f.write(data)
            index[key] = {'name': e['name'], 'sha': e['sha']}
        with open(index_path, 'w', encoding='utf-8') as f:
            json.dump(index, f, indent=1)
    except Exception as e:  # offline, rate-limited, or the repository moved
        if not all(k in index for k in ('post', 'machine')):
            raise RuntimeError('cannot fetch the Carvera Community machine and post: %s' % e)
        warnings.append('could not reach the Carvera Community repository (%s); using the '
                        'machine and post fetched earlier' % e)
    out = {k: os.path.join(CACHE_DIR, index[k]['name']) for k in ('post', 'machine')}
    out['machine_sha'] = index['machine']['sha']
    return out


def _model_urn(path, sha, log):
    """Version URN of the machine's 3D model, uploaded to the user's Fusion hub.

    The community .f3d links its 3D model to a file in its author's hub, which
    other users cannot open, and Fusion then refuses the machine. So the .f3d is
    uploaded once per hub and per upstream version, to the project MODEL_PROJECT.
    """
    data = _app().data
    hub = data.activeHub.id
    models_path = os.path.join(CACHE_DIR, 'models.json')
    try:
        with open(models_path, encoding='utf-8') as f:
            models = json.load(f)
    except (OSError, ValueError):
        models = {}
    known = models.get(hub)
    if known and known['sha'] == sha:
        try:
            if data.findFileById(known['urn']) is not None:
                return known['urn']
        except RuntimeError:
            pass
    projects = data.dataProjects
    project = next((p for p in projects if p.name == MODEL_PROJECT), None) or projects.add(MODEL_PROJECT)
    future = project.rootFolder.uploadFile(path)
    t0 = time.time()
    while future.uploadState == adsk.core.UploadStates.UploadProcessing and time.time() - t0 < 40:
        adsk.doEvents()
        time.sleep(0.2)
    if future.uploadState != adsk.core.UploadStates.UploadFinished:
        raise RuntimeError('upload of the machine model did not finish')
    urn = future.dataFile.versionId
    models[hub] = {'sha': sha, 'urn': urn}
    with open(models_path, 'w', encoding='utf-8') as f:
        json.dump(models, f, indent=1)
    log.append('uploaded the machine model to the Fusion project %r' % MODEL_PROJECT)
    return urn


def _machine(community, log, warnings):
    """The community machine, with its 3D model in the user's hub when possible."""
    loc = adsk.cam.LibraryLocations.ExternalLibraryLocation
    machine = adsk.cam.Machine.createFromFile(loc, community['machine'])
    try:
        urn = _model_urn(community['machine'], community['machine_sha'], log)
    except Exception as e:
        warnings.append('the machine has no 3D model, so simulation does not check the tool '
                        'against the machine frame: %s' % e)
        machine.clearSimulationModel()
        return machine
    mch = os.path.join(CACHE_DIR, 'machine.mch')
    machine.save(loc, mch)
    with open(mch, encoding='utf-8') as f:
        d = json.load(f)
    d['fusion']['default']['model_urn'] = urn
    with open(mch, 'w', encoding='utf-8') as f:
        json.dump(d, f, indent=1)
    return adsk.cam.Machine.createFromFile(loc, mch)


def _post(path):
    with open(path, encoding='utf-8') as f:
        return adsk.cam.PostConfiguration.createFromContent(f.read())


def export_tools(args=None):
    """Rewrite TOOLS_FILE from the lab's Fusion tool library, after it changes."""
    libs = adsk.cam.CAMManager.get().libraryManager.toolLibraries
    lib = libs.toolLibraryAtURL(adsk.core.URL.create(TOOL_LIBRARY_URL))
    if lib is None:
        raise RuntimeError('tool library not found: ' + TOOL_LIBRARY_URL)
    data = json.loads(lib.toJson())
    with open(TOOLS_FILE, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.write('\n')
    print('wrote %d tools to %s' % (len(data['data']), TOOLS_FILE))


# ------------------------------------------------------------ operations

class Builder:
    """Creates operations; with dry=True it only records which tools they need."""

    def __init__(self, cam, tools, log, dry=False):
        self.cam = cam
        self.tools = tools
        self.log = log
        self.dry = dry
        self.needed = []

    def op(self, setup, strategy, tool_key, name, params, select=None):
        desc, key = tool_key if isinstance(tool_key, tuple) else (tool_key, None)
        if self.dry:
            if (desc, key) not in self.needed:
                self.needed.append((desc, key))
            return None
        tool, preset = self.tools.get(desc, key)
        oi = setup.operations.createInput(strategy)
        oi.tool = tool
        if preset is not None:
            oi.toolPreset = preset
        oi.displayName = name
        modes = getattr(adsk.cam, 'AutomaticGenerationModes', None)
        skip = [getattr(modes, n) for n in dir(modes) if 'Skip' in n] if modes else []
        if skip:
            oi.generationMode = skip[0]
        for k, v in params.items():
            _set(oi.parameters, k, v)
        if select:
            select(oi.parameters)
        op = setup.operations.add(oi)
        self.log.append('%s / %s [%s] T%s %s' % (setup.name, name, strategy,
                        tool.parameters.itemByName('tool_number').expression, desc))
        return op


def _sel_pocket(faces):
    def f(params):
        v = params.itemByName('pockets').value
        sels = v.getCurveSelections()
        for face in faces:
            s = sels.createNewPocketSelection()
            s.inputGeometry = [face]
        v.applyCurveSelections(sels)
    return f


def _sel_silhouette():
    # The setup-model flag, not inputGeometry=[body]: a body silhouette comes out
    # empty in the flipped bottom setup.
    def f(params):
        v = params.itemByName('contours').value
        sels = v.getCurveSelections()
        s = sels.createNewSilhouetteSelection()
        s.isSetupModelSelected = True
        s.loopType = adsk.cam.LoopTypes.OnlyOutsideLoops
        s.sideType = adsk.cam.SideTypes.AlwaysOutsideSideType
        v.applyCurveSelections(sels)
    return f


def _sel_chains(loops):
    def f(params):
        v = params.itemByName('contours').value
        sels = v.getCurveSelections()
        for loop in loops:
            s = sels.createNewChainSelection()
            s.inputGeometry = [e for e in loop.edges]
            try:
                s.sideType = adsk.cam.SideTypes.AlwaysInsideSideType
            except Exception:
                pass
        v.applyCurveSelections(sels)
    return f


def _sel_face_contours(faces):
    def f(params):
        v = params.itemByName('contours').value
        sels = v.getCurveSelections()
        for face in faces:
            s = sels.createNewFaceContourSelection()
            s.inputGeometry = [face]
        v.applyCurveSelections(sels)
    return f


REJECTED = []  # (parameter, point) pairs Fusion refused, reported by build()


def _sel_faces(param, faces):
    def f(params):
        if not faces:
            return  # Fusion refuses an empty list ("Do not have a valid body")
        v = params.itemByName(param).value
        try:
            v.value = list(faces)
        except RuntimeError:
            # keep what Fusion accepts, one at a time, and report the rest
            ok = []
            for e in faces:
                try:
                    v.value = ok + [e]
                    ok.append(e)
                except RuntimeError:
                    g = getattr(e, 'worldGeometry', None) or getattr(e, 'geometry', None)
                    REJECTED.append((param, None if g is None else
                                     (round(mm(g.x), 2), round(mm(g.y), 2), round(mm(g.z), 2))))
            if ok:
                v.value = ok
    return f


def _sel_pocket_regions(faces, regions):
    # Floors and closed chains go in one applyCurveSelections: a second apply on
    # the same parameter drops the pocket selections made by the first.
    # The API takes a chain's side from its first curve's direction: a clockwise
    # chain pockets everything outside it, so clockwise chains are reverted.
    def f(params):
        v = params.itemByName('pockets').value
        sels = v.getCurveSelections()
        for face in faces:
            s = sels.createNewPocketSelection()
            s.inputGeometry = [face]
        for r in regions:
            s = sels.createNewChainSelection()
            s.inputGeometry = list(r['curves'])
            s.isOpen = False
            s.isReverted = r['clockwise']
        v.applyCurveSelections(sels)
    return f


def _both(*selectors):
    def f(params):
        for sel in selectors:
            sel(params)
    return f


# ------------------------------------------------------------ parameters
# Values below are the lab's, read from its existing setups. Feeds and speeds
# come from the tool preset, not from here.

POCKET = {
    'tolerance': '0.1mm',
    'maximumStepover': 'tool_diameter * 0.6',
    'useStockToLeave': 'false',
    'doRamp': 'true',
    'rampType': "'helix'",
    'compensation': "'left'",
}
CONTOUR = {
    'tolerance': '0.01mm',
    'useStockToLeave': 'false',
    'doRamp': 'false',
    'compensation': "'left'",
    'numberOfFinishingStepovers': '2',
}
DRILL = {  # every drill op, entry holes included, pecks as threads.toml says
    'cycleType': "'%s'" % THREAD_COMMON['drill']['cycle'],
    'incrementalDepth': '%gmm' % THREAD_COMMON['drill']['peck'],
    'chipBreakDistance': '%gmm' % THREAD_COMMON['drill']['chip_break'],
}
BORE = {
    'tolerance': '0.01mm',
    'direction': "'climb'",
    'doMultiplePasses': 'true',
}
THREAD = {
    'doMultiplePasses': 'true',
}


def _drill_ends(through):
    """Peck and breakthrough of a drill op, from threads.toml (the lab drills every hole alike)."""
    d = THREAD_COMMON['drill']
    brk = d['through_breakthrough'] if through else d['blind_breakthrough']
    return {'cycleType': "'%s'" % d['cycle'], 'incrementalDepth': '%gmm' % d['peck'],
            'chipBreakDistance': '%gmm' % d['chip_break'], 'breakThroughDepth': '%gmm' % brk,
            'drillTipThroughBottom': 'true' if through else 'false'}


def _thread_params(name, through):
    """Thread-mill parameters for one thread size, from threads.toml."""
    r, c = THREAD_RECIPES[name], THREAD_COMMON['thread']
    r = dict(r, **r.get('through' if through else 'blind', {}))
    stepover = 'tool_stepover' if r['stepover'] == 'tool' else '%gmm' % r['stepover']
    return dict(THREAD, direction="'%s'" % c['direction'], threadPitch='%gmm' % r['pitch'],
                pitchDiameterOffset='%gmm' % r['pitch_diameter_offset'],
                numberOfStepovers=str(r['passes']), stepover=stepover,
                topHeight_offset='%gmm' % c['top_offset'],
                bottomHeight_offset='%gmm' % (c['through_bottom_offset'] if through
                                              else c['blind_bottom_offset']))
CHAMFER_OP = {
    'tolerance': '0.01mm',
    'chamferWidth': '0.1mm',
    'chamferTipOffset': '0.5mm',
    'chamferClearance': '0.1mm',
    'doLeadIn': 'true',
    'doLeadOut': 'true',
    'entry_radius': '0',
}


def _bottom_below_part_top(depth_mm, margin):
    """Bottom height at `depth_mm` below the part's top face (in this setup's view).

    Fusion has no "from model top" mode, so heights are measured from the stock
    top; the part sits centred in the stock's thickness, `margin` below its top.
    """
    return {'bottomHeight_mode': "'from stock top'",
            'bottomHeight_offset': '%.4fmm' % (-(margin + depth_mm))}


def _top_below_part_top(depth_mm, margin):
    return {'topHeight_mode': "'from stock top'",
            'topHeight_offset': '%.4fmm' % (-(margin + depth_mm))}


def _small_endmill(corner_radius, depth):
    """Largest small end mill that fits the corner radius and reaches the depth."""
    for desc, d, flute in SMALL_ENDMILLS:
        if d / 2 <= corner_radius + 1e-3 and flute >= depth + 0.5:
            return desc, d
    return None, None


# ------------------------------------------------------------- the vise
# The lab's fixture, from Saunders Machine Works' published models: the Carvera
# fixture plate (inch) and a Gen3 hobby mod vise (1/4") with two smooth insert
# bars, assembled as in the lab's design 2026-01-18_slab-fixture-3-axis (less
# its 4th-axis module). In fixture coordinates (mm) the plate spans x 0..357,
# y 0..240, z -17.78..0; the fixed jaw's insert bar faces -X at x = 242.93, the
# adjustable one faces +X at x = 186.72, both bars span y 124.64..220.14 and
# top out at z = 29.53. The lab grips the stock 4.3 mm deep with the jaws on the
# stock's Y edges, the fixed jaw on the back (+Y) edge, and attaches the plate's
# corner (0, 0, -17.78) to the Carvera's table.

SMW_URL = 'https://saundersmachineworks.com/cdn/shop/files/'
SMW_FILES = {'plate': 'Makera_Carvera_Fixture_Plate.step',
             'vise': 'Gen3_Hobby_Mod_Vise_Inch.step',
             'bar': 'S1822_Gen3_Smooth_Insert_Bar.step'}
# (file, bodies whose path contains this, turn about Z in degrees, shift in mm,
#  moves with the adjustable jaw), measured from the lab's fixture design.
# The vise's own file has the adjustable side where the lab's design has it.
SMW_PLACEMENTS = [
    ('plate', 'INCH Plate', 0, (2.0, 0.0, 1.27), False),
    ('vise', 'Fixed Side', -90, (280.354, 172.388, 0.0), False),
    ('vise', 'Adjustable Side', -90, (280.354, 172.388, 0.0), True),
    ('bar', '', 90, (249.283, 172.388, 29.528), False),
    ('bar', '', 90, (180.369, 172.388, 29.528), True),
]
FIXTURE_CACHE = os.path.join(os.path.dirname(CACHE_DIR), 'fixture')
FX_FIXED_FACE = 242.93
FX_ADJ_FACE = 186.72
FX_JAW_CENTRE = (124.64 + 220.14) / 2
FX_JAW_TOP = 29.53
FX_PLATE_CORNER = (0.0, 0.0, -17.78)
FX_MAX_OPENING = 185.0  # the adjustable base reaches the plate's edge beyond this
GRIP = 4.3


def _step_bodies(path):
    """[(occurrence path, temporary body)] of a STEP file, via a throwaway document."""
    app = _app()
    keep = app.activeDocument
    im = app.importManager
    doc = im.importToNewDocument(im.createSTEPImportOptions(path))
    tb = adsk.fusion.TemporaryBRepManager.get()
    out = []
    try:
        root = adsk.fusion.FusionDocument.cast(doc).design.rootComponent
        out += [('', tb.copy(b)) for b in root.bRepBodies]
        for occ in root.allOccurrences:
            out += [(occ.fullPathName, tb.copy(b)) for b in occ.bRepBodies]
    finally:
        doc.close(False)
        keep.activate()
    return out


def _assemble_fixture():
    """Download the Saunders models and place them in fixture coordinates.

    Returns [(body, moves_with_jaw)]. Checks that the insert bars' faces land
    on FX_FIXED_FACE and FX_ADJ_FACE, in case Saunders changes a model.
    """
    os.makedirs(FIXTURE_CACHE, exist_ok=True)
    parts = {}
    for key, name in SMW_FILES.items():
        path = os.path.join(FIXTURE_CACHE, name)
        if not os.path.exists(path):
            req = urllib.request.Request(SMW_URL + name, headers={'User-Agent': 'carvera-cam'})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            with open(path, 'wb') as f:
                f.write(data)
        parts[key] = _step_bodies(path)
    tb = adsk.fusion.TemporaryBRepManager.get()
    out = []
    for key, match, turn, shift, moves in SMW_PLACEMENTS:
        m = adsk.core.Matrix3D.create()
        m.setToRotation(math.radians(turn), adsk.core.Vector3D.create(0, 0, 1),
                        adsk.core.Point3D.create(0, 0, 0))
        t = adsk.core.Matrix3D.create()
        t.translation = adsk.core.Vector3D.create(*[v / 10 for v in shift])
        m.transformBy(t)
        for path, b in parts[key]:
            if match in path:
                c = tb.copy(b)
                tb.transform(c, m)
                out.append((c, moves))
    # the insert bars are the bodies that reach the jaws' top
    faces = {False: [], True: []}
    for c, moves in out:
        bb = c.boundingBox
        if abs(bb.maxPoint.z * 10 - FX_JAW_TOP) < 0.05:
            faces[moves].append(round((bb.maxPoint.x if moves else bb.minPoint.x) * 10, 3))
    if not (faces[False] and faces[True]
            and all(abs(x - FX_FIXED_FACE) < 0.05 for x in faces[False])
            and all(abs(x - FX_ADJ_FACE) < 0.05 for x in faces[True])):
        raise RuntimeError('the Saunders vise models no longer line up with the lab fixture '
                           '(jaw faces at %s); update SMW_PLACEMENTS' % faces)
    return out


def _fixture_bodies():
    """The fixture's bodies as temporary B-Reps in fixture coordinates: [(body, moves_with_jaw)].

    Assembled once from the Saunders models, then cached as an .smt file.
    """
    smt = os.path.join(FIXTURE_CACHE, 'fixture.smt')
    meta = os.path.join(FIXTURE_CACHE, 'fixture.json')
    key = json.dumps([SMW_FILES, SMW_PLACEMENTS])
    tb = adsk.fusion.TemporaryBRepManager.get()
    try:
        with open(meta, encoding='utf-8') as f:
            m = json.load(f)
        if m['key'] == key and os.path.exists(smt):
            bodies = tb.createFromFile(smt)
            if bodies.count == len(m['moves']):
                return [(bodies.item(i), mv) for i, mv in enumerate(m['moves'])]
    except (OSError, ValueError, KeyError, RuntimeError):
        pass
    out = _assemble_fixture()
    tb.exportToFile([c for c, _ in out], smt)
    with open(meta, 'w', encoding='utf-8') as f:
        json.dump({'key': key, 'moves': [mv for _, mv in out]}, f)
    return out


def _stock_box(body, stock):
    """The top setup's stock box in model coordinates, in cm: ((x0, y0, z0), (x1, y1, z1))."""
    bb = body.boundingBox
    lo, hi = bb.minPoint, bb.maxPoint
    cx = (lo.x + hi.x) / 2
    sx, sy, sz = stock['x'] / 10, stock['y'] / 10, stock['z'] / 10
    if stock.get('y_mode', 'back') == 'back':
        y1 = hi.y + stock.get('y_offset', 10) / 10
        y0 = y1 - sy
    elif stock.get('y_mode') == 'front':
        y0 = lo.y - stock.get('y_offset', 10) / 10
        y1 = y0 + sy
    else:
        cy = (lo.y + hi.y) / 2
        y0, y1 = cy - sy / 2, cy + sy / 2
    z0 = lo.z - stock['bottom'] / 10
    return (cx - sx / 2, y0, z0), (cx + sx / 2, y1, z0 + sz)


def _vise_matrices(box, held_bottom, held_top, flipped):
    """Placements (fixed side, adjustable side) of the fixture for one setup.

    held_bottom..held_top is the stock's Z extent in the setup's own upright
    frame. For the bottom setup that frame is the model turned 180 degrees about
    X through the stock's centre, as the setup's WCS turns it.
    """
    (x0, y0, _), (x1, y1, _) = box
    cy = (y0 + y1) / 2
    grip = min(GRIP, (held_top - held_bottom) * 10 - 1.0) / 10
    # fixture X (towards the fixed jaw) -> +Y, fixture Y -> -X, Z -> Z
    origin = adsk.core.Point3D.create((x0 + x1) / 2 + FX_JAW_CENTRE / 10,
                                      y1 - FX_FIXED_FACE / 10,
                                      held_bottom - (FX_JAW_TOP / 10 - grip))
    m = adsk.core.Matrix3D.create()
    m.setWithCoordinateSystem(origin, adsk.core.Vector3D.create(0, 1, 0),
                              adsk.core.Vector3D.create(-1, 0, 0),
                              adsk.core.Vector3D.create(0, 0, 1))
    opening = (y1 - y0) * 10
    shift = adsk.core.Matrix3D.create()
    shift.translation = adsk.core.Vector3D.create(
        (FX_FIXED_FACE - opening - FX_ADJ_FACE) / 10, 0, 0)
    adj = shift.copy()
    adj.transformBy(m)
    if flipped:
        flip = adsk.core.Matrix3D.create()
        flip.setToRotation(math.pi, adsk.core.Vector3D.create(1, 0, 0),
                           adsk.core.Point3D.create(0, cy, 0))
        m.transformBy(flip)
        adj.transformBy(flip)
    return m, adj


def _add_vise(design, name, bodies, fixed, adj):
    """Add the fixture as one component named `name`; returns (occurrence, table attach point)."""
    root = design.rootComponent
    occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = name
    tb = adsk.fusion.TemporaryBRepManager.get()
    parametric = design.designType == adsk.fusion.DesignTypes.ParametricDesignType
    bf = comp.features.baseFeatures.add() if parametric else None
    if bf:
        bf.startEdit()
    try:
        for b, moves in bodies:
            c = tb.copy(b)
            tb.transform(c, adj if moves else fixed)
            comp.bRepBodies.add(c, bf) if bf else comp.bRepBodies.add(c)
    finally:
        if bf:
            bf.finishEdit()
    # the plate's corner is rounded, so the attach point is a construction
    # point there, as in the fixture design (Point1)
    corner = adsk.core.Point3D.create(*[v / 10 for v in FX_PLATE_CORNER])
    corner.transformBy(fixed)
    sk = comp.sketches.add(comp.xYConstructionPlane)
    sk.name = LAYOUT_SKETCH + ' table attach'
    sp = sk.sketchPoints.add(sk.modelToSketchSpace(corner))
    ci = comp.constructionPoints.createInput()
    ci.setByPoint(sp)
    cp = comp.constructionPoints.add(ci)
    cp.name = 'table attach'
    sk.isVisible = False
    return occ, cp.createForAssemblyContext(occ)


def _setup(cam, name, body, machine, stock, top, vise=None):
    si = cam.setups.createInput(adsk.cam.OperationTypes.MillingOperation)
    si.name = name
    si.models = [body]
    if machine is not None:
        si.machine = machine
    if vise is not None:
        si.fixtureEnabled = True
        si.fixtures = [vise[0]]
    setup = cam.setups.add(si)
    p = setup.parameters
    if vise is not None:
        # put the setup on the Carvera's table through the plate's corner, so
        # simulation checks the machine as well as the vise
        _try_set(p, 'job_position', 'true')
        _try_set(p, 'job_positionReference_origin_mode', "'point'")
        p.itemByName('job_positionAttach').value.value = [vise[1]]
        p.itemByName('job_positionReference_origin_point').value.value = [vise[1]]
    if top:
        _set(p, 'job_stockMode', "'fixedbox'")
        _set(p, 'job_stockFixedX', '%gmm' % stock['x'])
        _set(p, 'job_stockFixedY', '%gmm' % stock['y'])
        _set(p, 'job_stockFixedZ', '%gmm' % stock['z'])
        _set(p, 'job_stockFixedXMode', "'center'")
        _set(p, 'job_stockFixedYMode', "'%s'" % stock.get('y_mode', 'back'))
        _set(p, 'job_stockFixedYOffset', '%gmm' % stock.get('y_offset', 10))
        _set(p, 'job_stockFixedZMode', "'bottom'")
        _set(p, 'job_stockFixedZOffset', '%gmm' % stock['bottom'])
        _set(p, 'wcs_origin_mode', "'stockPoint'")
        _set(p, 'wcs_origin_boxPoint', "'top 4'")
    else:
        _set(p, 'job_stockMode', "'previoussetup'")
        _set(p, 'wcs_orientation_mode', "'axesZX'")
        _set(p, 'wcs_orientation_flipZ', 'true')
        _set(p, 'wcs_origin_mode', "'stockPoint'")
        _set(p, 'wcs_origin_boxPoint', "'top 2'")
    return setup


# ------------------------------------------------------- tabs and entries
# The lab holds the part with four bridges ("tabs") at mid-thickness and enters
# every cut through holes the T1 drill makes first, at points in a sketch:
#   - one point just outside the outline at a tab edge, drilled through the
#     stock: the entry of both facing pockets and both outline contours;
#   - a point at the start of each other "corner chain" (the outline between
#     two tabs), drilled through the tab band;
#   - a point inside each pocket, drilled to the pocket floor.
# build() redraws that sketch, named LAYOUT_SKETCH, every time it runs.

LAYOUT_SKETCH = 'carvera-cam'
VISE_PREFIX = LAYOUT_SKETCH + ' vise '
TOOL_R = ENDMILL_D / 2 / 10  # cm


def _flat(p):
    return adsk.core.Point3D.create(p.x, p.y, 0)


def _loop_pieces(loop):
    pieces = []
    for ce in loop.coEdges:
        e = ce.edge
        ev = e.evaluator
        ok, t0, t1 = ev.getParameterExtents()
        ok, length = ev.getLengthAtParameter(t0, t1)
        pieces.append({'edge': e, 'rev': ce.isOpposedToEdge, 'ev': ev, 't0': t0, 't1': t1,
                       'len': length})
    return pieces


def _point_on(pc, s):
    """Point at arc length s (cm) along the piece, in traversal direction."""
    se = min(max(pc['len'] - s if pc['rev'] else s, 0.0), pc['len'])
    if se < 1e-7:
        t = pc['t0']
    elif se > pc['len'] - 1e-7:
        t = pc['t1']
    else:
        ok, t = pc['ev'].getParameterAtLength(pc['t0'], se)
        if not ok:
            t = pc['t0'] + (pc['t1'] - pc['t0']) * se / pc['len']
    ok, p = pc['ev'].getPointAtParameter(t)
    return p


def _clockwise(pieces):
    """Reorder the pieces so the loop runs clockwise seen from +Z."""
    pts = []
    for pc in pieces:
        pts += [_point_on(pc, 0), _point_on(pc, pc['len'] / 2)]
    area = sum(p.x * q.y - q.x * p.y for p, q in zip(pts, pts[1:] + pts[:1]))
    if area > 0:  # counter-clockwise
        pieces = [dict(pc, rev=not pc['rev']) for pc in reversed(pieces)]
    return pieces


def _place_tabs(pieces, width, n=4):
    """Arc-length centres of n tabs, at the middle of the longest lines and arcs."""
    ok_types = (adsk.core.Line3D, adsk.core.Arc3D, adsk.core.Circle3D)
    cand = [i for i, pc in enumerate(pieces)
            if isinstance(pc['edge'].geometry, ok_types) and pc['len'] >= width + 0.1]
    tabs = {i: [] for i in range(len(pieces))}
    if not cand:
        return None
    if len(cand) >= n:
        for i in sorted(cand, key=lambda i: -pieces[i]['len'])[:n]:
            tabs[i].append(pieces[i]['len'] / 2)
        return tabs
    total = sum(pieces[i]['len'] for i in cand)
    for i in cand:
        k = max(1, round(n * pieces[i]['len'] / total))
        k = max(1, min(k, int(pieces[i]['len'] // (width + 0.2))))
        tabs[i] += [pieces[i]['len'] * (j + 0.5) / k for j in range(k)]
    return tabs


def _sketch_curve(sk, pc, a, b):
    g = pc['edge'].geometry
    p0, p1 = _flat(_point_on(pc, a)), _flat(_point_on(pc, b))
    if isinstance(g, adsk.core.Line3D):
        return [sk.sketchCurves.sketchLines.addByTwoPoints(p0, p1)]
    if isinstance(g, (adsk.core.Arc3D, adsk.core.Circle3D)):
        pm = _flat(_point_on(pc, (a + b) / 2))
        return [sk.sketchCurves.sketchArcs.addByThreePoints(p0, pm, p1)]
    if a < 1e-6 and b > pc['len'] - 1e-6:
        return list(sk.project(pc['edge']))
    raise RuntimeError('a tab landed on an edge that is neither a line nor an arc')


def _direction(pc, a, b):
    d = min(0.001, (b - a) / 10)
    p, q = _point_on(pc, a), _point_on(pc, a + d)
    v = adsk.core.Vector3D.create(q.x - p.x, q.y - p.y, 0)
    v.normalize()
    return v


def _is_reverted(curve, start, direction):
    """Must the chain be reverted to run along `direction` from `start`?"""
    ev = curve.geometry.evaluator
    ok, t = ev.getParameterAtPoint(start)
    ok, der = ev.getFirstDerivative(t)
    return der.x * direction.x + der.y * direction.y < 0


def _thin(pts, n=240):
    step = max(1, len(pts) // n)
    return pts[::step]


def _most_open_point(inside, edge_pts, bbox, n=24):
    """Grid search, then a local refinement, for the point farthest from edge_pts."""
    edge_pts = _thin(edge_pts)
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0

    def clearance(x, y):
        if not inside(x, y):
            return -1.0
        return min(math.hypot(x - u, y - v) for u, v in edge_pts)

    best = (-1.0, 0.0, 0.0)
    for i in range(n + 1):
        for j in range(n + 1):
            x, y = x0 + w * i / n, y0 + h * j / n
            c = clearance(x, y)
            if c > best[0]:
                best = (c, x, y)
    bx, by = best[1], best[2]
    for i in range(-4, 5):
        for j in range(-4, 5):
            x, y = bx + w / n * i / 4, by + h / n * j / 4
            c = clearance(x, y)
            if c > best[0]:
                best = (c, x, y)
    return best


def _pocket_entry(face):
    """Point on a pocket floor farthest from its edges, and that clearance (cm)."""
    bb = face.boundingBox
    z = face.pointOnFace.z
    edge_pts = []
    for lp in face.loops:
        for e in lp.edges:
            ev = e.evaluator
            ok, t0, t1 = ev.getParameterExtents()
            ok, pts = ev.getStrokes(t0, t1, 0.0005)
            edge_pts += [(q.x, q.y) for q in pts]
    fev = face.evaluator

    def inside(x, y):
        ok, uv = fev.getParameterAtPoint(adsk.core.Point3D.create(x, y, z))
        return ok and fev.isParameterOnFace(uv)

    return _most_open_point(inside, edge_pts,
                            (bb.minPoint.x, bb.minPoint.y, bb.maxPoint.x, bb.maxPoint.y))


def _make_layout(design, a, plan, warnings):
    """Sketch the corner chains, tab segments and entry points; return them for the ops."""
    root = design.rootComponent
    width = plan['tab']['width'] / 10
    faces = [f for f in (a['top_face'], a['bottom_face']) if f is not None]
    if not faces:
        raise RuntimeError('the part has no flat top or bottom face to take its outline from')

    loop = _outline_loop(faces)[0]
    lb, bb = loop.boundingBox, a['body'].boundingBox
    if (lb.maxPoint.x - lb.minPoint.x) < (bb.maxPoint.x - bb.minPoint.x) - 0.005 or \
            (lb.maxPoint.y - lb.minPoint.y) < (bb.maxPoint.y - bb.minPoint.y) - 0.005:
        warnings.append('the part is wider in the middle than at its top and bottom faces; '
                        'the tab chains follow the face outline, so check them')
    pieces = _clockwise(_loop_pieces(loop))
    tabs = _place_tabs(pieces, width)
    if tabs is None:
        raise RuntimeError('no straight or arc edge of the outline is long enough for a '
                           '%g mm tab' % plan['tab']['width'])

    sk = root.sketches.add(root.xYConstructionPlane)
    sk.name = LAYOUT_SKETCH
    segs = []  # (curves, is_tab, start point, direction)
    for i, pc in enumerate(pieces):
        pos = 0.0
        for c in sorted(tabs[i]):
            lo, hi = c - width / 2, c + width / 2
            if lo - pos > 1e-5:
                segs.append((_sketch_curve(sk, pc, pos, lo), False,
                             _flat(_point_on(pc, pos)), _direction(pc, pos, lo)))
            segs.append((_sketch_curve(sk, pc, lo, hi), True,
                         _flat(_point_on(pc, lo)), _direction(pc, lo, hi)))
            pos = hi
        if pc['len'] - pos > 1e-5:
            segs.append((_sketch_curve(sk, pc, pos, pc['len']), False,
                         _flat(_point_on(pc, pos)), _direction(pc, pos, pc['len'])))

    # corner chains: the runs of outline between tabs, each starting just after a tab
    first_tab = [k for k, sg in enumerate(segs) if sg[1]][0]
    chains, run = [], []
    for sg in segs[first_tab + 1:] + segs[:first_tab + 1]:
        if sg[1]:
            if run:
                chains.append(run)
            run = []
        else:
            run.append(sg)

    def outside(pt, d):
        # a clockwise loop has its outside on the left of the direction of travel
        return adsk.core.Point3D.create(pt.x - d.y * TOOL_R, pt.y + d.x * TOOL_R, 0)

    corner = [{'curves': [c for sg in ch for c in sg[0]], 'start': ch[0][2], 'dir': ch[0][3],
               'entry': sk.sketchPoints.add(outside(ch[0][2], ch[0][3]))} for ch in chains]
    tab_chains = [{'curves': sg[0], 'start': sg[2], 'dir': sg[3]} for sg in segs if sg[1]]

    pocket_entries = {}
    for f in a['floors']:
        if f['role'] != 'pocket':
            continue
        if f['opening']:
            # enter sideways, from the outline cut beside the opening; no drilling
            x, y, dx, dy = f['opening']
            off = TOOL_R + 0.002
            pocket_entries[f['face'].tempId] = {
                'point': sk.sketchPoints.add(adsk.core.Point3D.create(x + dx * off, y + dy * off, 0)),
                'side': f['side'], 'depth': f['depth'], 'drill': False}
            continue
        c, x, y = _pocket_entry(f['face'])
        if c >= TOOL_R + 0.005:
            pocket_entries[f['face'].tempId] = {
                'point': sk.sketchPoints.add(adsk.core.Point3D.create(x, y, 0)),
                'side': f['side'], 'depth': f['depth'], 'drill': True}
    shelves = _shelf_regions(root, a, sk, pocket_entries)
    _merge_entries(pocket_entries)
    return {'sketch': sk, 'corner': corner, 'tabs': tab_chains, 'shelves': shelves,
            'entry': corner[0]['entry'], 'pocket_entries': pocket_entries}


def _profile_rings(profile):
    """(outer ring, [inner rings], outer loop) of a sketch profile, as XY polygons in cm."""
    outer, inner, outer_loop = None, [], None
    for lp in profile.profileLoops:
        pieces = []
        for pc in lp.profileCurves:
            ev = pc.geometry.evaluator
            ok, t0, t1 = ev.getParameterExtents()
            ok, pts = ev.getStrokes(t0, t1, 0.0005)
            pieces.append([(q.x, q.y) for q in pts])
        # profileCurves come in no particular order or direction: chain them end to end
        ring = pieces.pop(0) if pieces else []
        while pieces:
            ex, ey = ring[-1]
            k, rev = min(((k, rev) for k in range(len(pieces)) for rev in (False, True)),
                         key=lambda kr: math.hypot(pieces[kr[0]][-1 if kr[1] else 0][0] - ex,
                                                   pieces[kr[0]][-1 if kr[1] else 0][1] - ey))
            pc = pieces.pop(k)
            ring += pc[::-1] if rev else pc
        if lp.isOuter:
            outer, outer_loop = ring, lp
        else:
            inner.append(ring)
    return outer, inner, outer_loop


def _in_profile(rings, x, y):
    outer, inner, _ = rings
    return _inside(outer, x, y) and not any(_inside(r, x, y) for r in inner)


def _shelf_regions(root, a, sk, pocket_entries):
    """For each shelf floor, the pocket opening just above it, sketched as a closed chain.

    A shelf is pocketed with the deeper pocket it steps down into, as one region
    bounded by the walls above the shelf, the way the lab selects such pockets by
    hand; its entry point is the region's most open point, which lands in the
    deeper pocket and is drilled to that pocket's floor.
    """
    out = {}
    by_level = {}
    for f in a['floors']:
        if f.get('shelf'):
            by_level.setdefault((f['side'], f['z']), []).append(f)
    for (side, z), floors in by_level.items():
        dz = 0.0001 if side == 'top' else -0.0001  # 1 um into the opening
        pi = root.constructionPlanes.createInput()
        pi.setByOffset(root.xYConstructionPlane, adsk.core.ValueInput.createByReal(z / 10 + dz))
        plane = root.constructionPlanes.add(pi)
        plane.name = LAYOUT_SKETCH + ' z=%g' % z
        sec = root.sketches.add(plane)
        sec.name = LAYOUT_SKETCH + ' section z=%g' % z
        sec.intersectWithSketchPlane([a['body']])
        profiles = [(p, _profile_rings(p)) for p in sec.profiles]
        regions = {}  # profile index -> (curves, entry point or None)
        for f in floors:
            p0 = f['face'].pointOnFace
            hit = [(k, r) for k, (p, r) in enumerate(profiles)
                   if r[0] and _in_profile(r, p0.x, p0.y)]
            if not hit:
                continue
            k, rings = min(hit, key=lambda kr: profiles[kr[0]][0].areaProperties().area)
            if k not in regions:
                curves = [pc.sketchEntity for pc in rings[2].profileCurves]
                xs, ys = [q[0] for q in rings[0]], [q[1] for q in rings[0]]
                c, x, y = _most_open_point(lambda x, y: _in_profile(rings, x, y),
                                           rings[0] + [q for r in rings[1] for q in r],
                                           (min(xs), min(ys), max(xs), max(ys)))
                pt = (sk.sketchPoints.add(adsk.core.Point3D.create(x, y, 0))
                      if c >= TOOL_R + 0.005 else None)
                # the ring starts along the first profile curve's own direction;
                # the chain starts along its sketch curve's
                r0 = rings[0]
                ccw = sum(r0[i - 1][0] * r0[i][1] - r0[i][0] * r0[i - 1][1]
                          for i in range(len(r0))) > 0
                pc0 = rings[2].profileCurves.item(0)
                s0 = pc0.sketchEntity.startSketchPoint.geometry
                if math.hypot(s0.x - r0[0][0], s0.y - r0[0][1]) > 1e-4:
                    ccw = not ccw
                regions[k] = ({'curves': curves, 'clockwise': not ccw}, pt)
            region, pt = regions[k]
            out[f['face'].tempId] = region
            if pt is not None:
                pocket_entries[f['face'].tempId] = {
                    'point': pt, 'side': side, 'depth': f['depth'], 'drill': True}
    return out


def _merge_entries(entries):
    """Entries within 0.5 mm of each other share one hole, drilled to the deepest floor."""
    clusters = []
    for e in entries.values():
        if not e['drill']:
            continue
        p = e['point'].geometry
        for cl in clusters:
            q = cl[0]['point'].geometry
            if cl[0]['side'] == e['side'] and math.hypot(p.x - q.x, p.y - q.y) < 0.05:
                cl.append(e)
                break
        else:
            clusters.append([e])
    for cl in clusters:
        keep = max(cl, key=lambda e: e['depth'])
        for e in cl:
            e['point'], e['depth'] = keep['point'], keep['depth']


def _sel_layout_chains(chains, top):
    """Chain selections that keep a left-compensated tool outside the outline.

    The API sets the tool side from the first curve's direction, so each chain
    is reverted where its first curve runs against the clockwise direction seen
    along the setup's tool axis.
    """
    def f(params):
        v = params.itemByName('contours').value
        sels = v.getCurveSelections()
        for ch in chains:
            s = sels.createNewChainSelection()
            s.inputGeometry = list(ch['curves'])
            s.isOpen = True
            d = ch['dir'] if top else adsk.core.Vector3D.create(-ch['dir'].x, -ch['dir'].y, 0)
            s.isReverted = _is_reverted(ch['curves'][0], ch['start'], d)
        v.applyCurveSelections(sels)
    return f


def _group(holes):
    """Group holes that one bore operation can share: same diameter, depth, ends."""
    groups = {}
    for h in holes:
        groups.setdefault((h['dia'], h['z0'], h['z1'], h['through']), []).append(h)
    return list(groups.values())


def _slot_order(needed):
    """The lab's slot order: T1 drill, T2/T3 end mill, small end mills, drills, threads, chamfer."""
    def rank(item):
        desc, key = item
        if desc == DRILL_3175 and key is None:
            return (0, 0)
        if desc == ENDMILL:
            return (1, 0 if key == 'rough' else 1)
        small = [d for d, _, _ in SMALL_ENDMILLS]
        if desc in small:
            return (2, small.index(desc))
        if 'Drill' in desc:
            return (3, desc)
        if 'Threadmill' in desc:
            return (4, desc)
        if desc.startswith('Chamfer'):
            return (6, 0)
        return (5, desc)
    return sorted(needed, key=rank)


def _side_ops(b, setup, a, side, plan, warnings):
    """Roughing, finishing, small-tool, hole, thread and chamfer ops for one side."""
    body = a['body']
    top = side == 'top'
    is_side = lambda f: f['side'] == side
    holes = [h for h in a['holes'] if is_side(h)]
    floors = [f for f in a['floors'] if is_side(f)]
    face = a['top_face'] if top else a['bottom_face']
    thickness = a['ztop'] - a['zbot']
    zc = (a['ztop'] + a['zbot']) / 2
    taps = plan['taps']
    tab = plan['tab']
    # stock above the part's face this setup machines: the top margin, or the bottom one
    m = plan['margin_top'] if top else plan['margin_bottom']
    ops = {'main': [], 'tabs': []}

    def depth_of(z):
        return (a['ztop'] - z) if top else (z - a['zbot'])

    # classify holes
    drilled, bored, tapped = [], [], []
    drills = b.tools.drills()
    for h in holes:
        t = h['thread'] or taps.get('%g' % h['dia'])
        if t:
            if t not in THREADS:
                warnings.append('unknown thread %r on %.2f mm hole; skipped' % (t, h['dia']))
                continue
            tapped.append((h, t))
            continue
        match = [d for d in drills if h['dia'] - 0.02 <= d[1] <= h['dia'] + 0.05
                 and d[2] >= h['depth'] + 0.5]
        if match:
            drilled.append((h, match[0][0]))
        elif h['dia'] > ENDMILL_D + 0.1:
            bored.append((h, (ENDMILL, 'rough'), (ENDMILL, 'finish')))
        else:
            em, d = _small_endmill(h['dia'] / 2 - 0.05, h['depth'])
            if em:
                bored.append((h, (em, None), None))
            else:
                warnings.append('no tool for %.2f mm hole at (%g, %g); skipped'
                                % (h['dia'], h['x'], h['y']))
    # taps with no matching drill (M4) are drilled, then bored to the modelled
    # diameter, as threads.toml says
    tap_bored = [(h, t) for h, t in tapped if THREADS[t][1] is None]

    def drill_for(depth):
        # the lab drills up to the flute length (7.4 mm, or 12.7 mm for the long drill)
        if depth + 0.5 > 12.7:
            warnings.append('%.1f mm entry hole is deeper than the long 3.175 mm drill reaches'
                            % depth)
        return DRILL_3175 if depth + 0.5 <= 7.4 else DRILL_3175_LONG

    lay = plan['layout']
    band_top, band_bot = tab['z'] + tab['band'] / 2, tab['z'] - tab['band'] / 2
    chain_bot = band_bot - tab['overlap']
    stock_z = thickness + plan['margin_top'] + plan['margin_bottom']
    points = lambda pts: _sel_faces('holePoints', pts)
    entry_ops = {'holeMode': "'selection-points'", 'topHeight_mode': "'from stock top'",
                 'topHeight_offset': '0mm'}

    # T1 entry holes, before anything the end mill cuts
    if top:
        ops['main'].append(b.op(setup, 'drill', drill_for(stock_z), 'Entry hole', dict(
            DRILL, **entry_ops, bottomHeight_mode="'from stock bottom'",
            bottomHeight_offset='0mm', breakThroughDepth='0.5mm', drillTipThroughBottom='true'),
            points([lay['entry']] if lay else [])))
        if lay is None or len(lay['corner']) > 1:
            depth = a['ztop'] - chain_bot
            params = dict(DRILL, **entry_ops, breakThroughDepth='0mm', drillTipThroughBottom='true')
            params.update(_bottom_below_part_top(depth, m))
            ops['main'].append(b.op(setup, 'drill', drill_for(m + depth), 'Tab entry holes',
                                    params, points([c['entry'] for c in lay['corner'][1:]]
                                                   if lay else [])))
    side_entries = {}
    if lay:
        side_entries = {k: e for k, e in lay['pocket_entries'].items() if e['side'] == side}
    else:  # dry run: the entries are not sketched yet
        side_entries = {f['face'].tempId: {'point': None, 'depth': f['depth'],
                                           'drill': not f['side_open']}
                        for f in floors if f['role'] == 'pocket'}
    by_depth, seen = {}, []
    for e in side_entries.values():
        if e['drill'] and (e['point'] is None or all(e['point'] is not q for q in seen)):
            seen.append(e['point'])
            by_depth.setdefault(round(e['depth'], 3), []).append(e['point'])
    for depth, pts in sorted(by_depth.items()):
        params = dict(DRILL, **entry_ops, breakThroughDepth='0mm', drillTipThroughBottom='false')
        params.update(_bottom_below_part_top(depth, m))
        ops['main'].append(b.op(setup, 'drill', drill_for(m + depth),
                                'Pocket entry holes %g mm' % depth, params, points(pts)))

    # T1: pre-drill everything the 3.175 end mill will bore
    pre = [h for h, rough, _ in bored if rough[0] == ENDMILL] + [h for h, _ in tap_bored]
    for through in (True, False):
        hs = [h for h in pre if h['through'] == through]
        if not hs:
            continue
        ops['main'].append(b.op(setup, 'drill', drill_for(max(h['depth'] for h in hs)),
                                'Pre-drill bores' + ('' if through else ' (blind)'),
                                dict(DRILL, **_drill_ends(through)),
                                _sel_faces('holeFaces', [h['face'] for h in hs])))

    pockets = sorted([f for f in floors if f['role'] == 'pocket'], key=lambda f: f['depth'])
    facing = [f for f in floors if f['role'] == 'face']
    windows = [w for w in a['windows']] if top else []

    passes = (('rough', 'true', ''), ('finish', 'false', ' (finish)'))
    for which, multi, suffix in passes:
        tk = (ENDMILL, which)
        entry = [lay['entry']] if lay else []
        if facing and plan['face']:
            ops['main'].append(b.op(setup, 'pocket2d', tk, 'Face' + suffix, dict(
                POCKET, doMultipleDepths=multi, topHeight_mode="'from stock top'",
                topHeight_offset='0mm', bottomHeight_mode="'from contour'",
                rampType="'predrill'"),
                _both(_sel_pocket([f['face'] for f in facing]),
                      _sel_faces('predrillPositions', entry))))
        # outline: the top setup cuts down to the tab band, the bottom setup up to it
        params = dict(CONTOUR, doMultipleDepths=multi, topHeight_mode="'from stock top'",
                      topHeight_offset='0mm')
        if top:
            params.update(_bottom_below_part_top(a['ztop'] - band_top, m))
        else:
            params.update(_bottom_below_part_top(band_bot - a['zbot'], m))
        ops['main'].append(b.op(setup, 'contour2d', tk, 'Outline' + suffix, params, _both(
            _sel_silhouette(), _sel_faces('entryPositions', entry))))
        if pockets:
            entries = [side_entries[f['face'].tempId]['point'] for f in pockets
                       if f['face'].tempId in side_entries]
            shelves = lay['shelves'] if lay else {}
            plain = [f['face'] for f in pockets if f['face'].tempId not in shelves]
            chains = []  # one chain per region, though several shelves share it
            for f in pockets:
                c = shelves.get(f['face'].tempId)
                if c is not None and all(c is not d for d in chains):
                    chains.append(c)
            unique = []
            for e in entries:
                if e is not None and all(e is not u for u in unique):
                    unique.append(e)
            ops['main'].append(b.op(setup, 'pocket2d', tk, 'Pockets' + suffix, dict(
                POCKET, doMultipleDepths=multi, bottomHeight_mode="'from contour'",
                rampType="'predrill'" if entries else "'plunge'",
                **_top_below_part_top(0, m)),
                _both(_sel_pocket_regions(plain, chains),
                      _sel_faces('predrillPositions', unique))))
        for hs in _group([h for h, rough, _ in bored if rough[0] == ENDMILL]):
            h = hs[0]
            radial = max(0.1, (h['dia'] - ENDMILL_D) / 2)
            params = dict(BORE, stepover='0.3mm',
                          numberOfStepovers=str(max(1, math.ceil(radial / 0.3))))
            params.update(_top_below_part_top(depth_of(h['z1'] if top else h['z0']), m))
            params.update({'bottomHeight_offset': '-0.5mm' if h['through'] else '0mm'})
            ops['main'].append(b.op(setup, 'bore', tk, 'Bore %.2f%s' % (h['dia'], suffix),
                                    params, _sel_faces('circularFaces', [x['face'] for x in hs])))
        # tap holes bored to size: one light pass, plus a finishing pass in blind holes
        for hs in _group([h for h, _ in tap_bored]):
            h = hs[0]
            t = [t for x, t in tap_bored if x is h][0]
            bore = THREAD_RECIPES[t]['bore']
            if which == 'finish' and (h['through'] or not bore.get('blind_finish_pass')):
                continue
            params = dict(BORE, stepover='%gmm' % bore['stepover'],
                          numberOfStepovers=str(bore['passes']), doMultiplePasses=multi)
            params.update(_top_below_part_top(depth_of(h['z1'] if top else h['z0']), m))
            params.update({'bottomHeight_offset': '%gmm' % (
                bore['through_bottom_offset'] if h['through'] else 0.0)})
            ops['main'].append(b.op(setup, 'bore', (bore['end_mill'], which),
                                    'Bore %s %.3f%s' % (t, h['dia'], suffix), params,
                                    _sel_faces('circularFaces', [x['face'] for x in hs])))
        if windows:
            for w in windows:
                params = dict(CONTOUR, doMultipleDepths=multi)
                params.update(_top_below_part_top(depth_of(w['z']), m))
                params.update(_bottom_below_part_top(thickness + 0.5, m))
                ops['main'].append(b.op(setup, 'contour2d', tk, 'Window' + suffix, params,
                                        _sel_chains([w['loop']])))
        # top setup: the outline between the tabs, down through the tab band
        if top:
            params = dict(CONTOUR, doMultipleDepths=multi)
            params.update(_top_below_part_top(a['ztop'] - band_top, m))
            params.update(_bottom_below_part_top(a['ztop'] - chain_bot, m))
            ops['main'].append(b.op(setup, 'contour2d', tk, 'Outline between tabs' + suffix,
                                    params, _sel_layout_chains(lay['corner'] if lay else [], True)))

    # small end mills: rest machining in pockets whose corners the 3.175 can't reach,
    # one operation per tool
    corners = {}
    for f in pockets:
        r = f['corner_radius']
        if r is None or r >= ENDMILL_D / 2 - 1e-3:
            continue
        smallest = SMALL_ENDMILLS[-1][1] / 2
        if r < smallest:
            warnings.append('pocket at z=%g mm has sharp or R%.2f inside corners; the %g mm '
                            'end mill leaves R%g' % (f['z'], r, 2 * smallest, smallest))
        em, d = _small_endmill(max(r, smallest), f['depth'])
        if not em:
            warnings.append('pocket at z=%g mm: corner radius %.2f mm needs a smaller tool '
                            'than the library has, or is deeper than its flute' % (f['z'], r))
            continue
        corners.setdefault((em, d), []).append(f['face'])
    for (em, d), faces in corners.items():
        ops['main'].append(b.op(setup, 'pocket2d', em, 'Pocket corners %g' % d, dict(
            POCKET, doMultipleDepths='true', rampType="'plunge'", useRestMachining='true',
            bottomHeight_mode="'from contour'", **_top_below_part_top(0, m)),
            _sel_pocket(faces)))
    for w in windows:
        r = w['corner_radius']
        if r is not None and r < ENDMILL_D / 2 - 1e-3:
            warnings.append('window at x/y loop: corner radius %.2f mm is below 1.59 mm; '
                            'the 3.175 end mill leaves material in its corners' % r)
    small = {}
    for h, rough, _ in bored:
        if rough[0] != ENDMILL:
            small.setdefault(rough[0], []).append(h)
    for em, group in small.items():
        for hs in _group(group):
            params = dict(BORE, stepover='0.2mm', numberOfStepovers='2')
            params.update({'bottomHeight_offset': '-0.5mm' if hs[0]['through'] else '0mm'})
            ops['main'].append(b.op(setup, 'bore', em, 'Bore %.2f' % hs[0]['dia'], params,
                                    _sel_faces('circularFaces', [x['face'] for x in hs])))

    # drills (plain holes, then tap drills), grouped by tool
    by_drill = {}
    for h, desc in drilled:
        by_drill.setdefault(desc, []).append(h)
    for h, t in tapped:
        if THREADS[t][1]:
            by_drill.setdefault(THREADS[t][1], []).append(h)
    for desc, hs in by_drill.items():
        for through in (True, False):
            sub = [h for h in hs if h['through'] == through]
            if sub:
                ops['main'].append(b.op(
                    setup, 'drill', desc,
                    'Drill ' + desc.split('*')[0] + ('' if through else ' (blind)'),
                    dict(DRILL, **_drill_ends(through)),
                    _sel_faces('holeFaces', [h['face'] for h in sub])))

    # thread mills
    by_thread = {}
    for h, t in tapped:
        by_thread.setdefault(t, []).append(h)
    for t, all_hs in by_thread.items():
        for through in (True, False):
            hs = [h for h in all_hs if h['through'] == through]
            if hs:
                ops['main'].append(b.op(
                    setup, 'thread', THREADS[t][2], 'Thread ' + t + ('' if through else ' (blind)'),
                    _thread_params(t, through),
                    _sel_faces('circularFaces', [h['face'] for h in hs])))

    # deburr the outer face's edges
    if face is not None and plan['chamfer']:
        ops['main'].append(b.op(setup, 'chamfer2d', CHAMFER, 'Deburr', dict(CHAMFER_OP),
                                _sel_face_contours([face])))

    # bottom side: the tab cut, last and in its own NC program
    if not top:
        params = dict(CONTOUR, doMultipleDepths='true', maximumStepdown='0.2mm')
        params.update(_top_below_part_top(band_bot - a['zbot'], m))
        params.update(_bottom_below_part_top(band_top + tab['overlap'] - a['zbot'], m))
        ops['tabs'].append(b.op(setup, 'contour2d', (ENDMILL, 'rough'), 'Cut tabs', params,
                                _sel_layout_chains(lay['tabs'] if lay else [], False)))
    return ops


def _delete_previous(cam, design):
    for s in [s for s in cam.setups if s.name.endswith(SETUP_TAG)]:
        s.deleteMe()
    for n in [n for n in cam.ncPrograms if SETUP_TAG in n.name]:
        n.deleteMe()
    root = design.rootComponent
    for sk in [sk for sk in root.sketches if sk.name.startswith(LAYOUT_SKETCH)]:
        sk.deleteMe()
    for pl in [pl for pl in root.constructionPlanes if pl.name.startswith(LAYOUT_SKETCH)]:
        pl.deleteMe()
    for occ in [o for o in root.occurrences if o.component.name.startswith(VISE_PREFIX)]:
        occ.deleteMe()


def _nc_program(cam, name, ops, post):
    ni = cam.ncPrograms.createInput()
    ni.displayName = name + ' ' + SETUP_TAG
    ni.operations = ops
    _try_set(ni.parameters, 'nc_program_name', "'%s'" % name)
    nc = cam.ncPrograms.add(ni)
    if post is not None:
        nc.postConfiguration = post
    return nc


def build(args):
    """Create setups, operations and NC programs for the part.

    args:
      body:      body name (optional when the design has one visible body)
      stock:     {"x": mm, "y": mm, "z": mm, "y_mode": "back", "y_offset": 10,
                  "bottom": mm of stock below the part (default: centred)}
      taps:      {"1.6": "M2", ...} hole diameter (as analyze prints it) -> thread
      name:      program name prefix (default: document name)
      face:      face the top and bottom from stock (default true)
      chamfer:   deburr the outer faces (default true)
      tab_width: tab width along the outline in mm (default 5)
      tab_z:     model Z of the tab band's centre in mm (default: the mid-plane,
                 or the nearest height clear of pockets that open to the side)
      fixture:   model the vise and attach the setups to the machine table, for
                 collision checks (default true)
    """
    t0 = time.time()
    body = _find_body(args)
    a = analyze_body(body)
    if 'stock' not in args:
        raise RuntimeError('args["stock"] is required: {"x":..,"y":..,"z":..} in mm')
    stock = args['stock']
    thickness = a['ztop'] - a['zbot']
    if stock['z'] < thickness - 1e-3:
        raise RuntimeError('stock is %g mm thick but the part is %g mm' % (stock['z'], thickness))
    # stock left below the part; centred by default. Moving the part up or down
    # shifts machining time between the top and bottom setups, and so when the
    # operator has to come back to flip the stock.
    spare = max(0.0, stock['z'] - thickness)  # stock as thick as the part is fine
    bottom = stock.get('bottom', spare / 2)
    if not -1e-3 <= bottom <= spare + 1e-3:
        raise RuntimeError('stock["bottom"] must be between 0 and %g mm' % spare)
    bottom = min(max(bottom, 0.0), spare)
    stock = dict(stock, bottom=bottom)
    band = min(1.0, 0.4 * thickness)
    overlap = min(0.5, 0.2 * thickness)
    tab_width = args.get('tab_width', 5.0)
    if tab_width < ENDMILL_D + 0.5:
        raise RuntimeError('tab_width must exceed the %g mm end mill by 0.5 mm' % ENDMILL_D)
    tab_z, tab_clear, openings = _tab_height(a, band, overlap, args.get('tab_z'))
    log, warnings = [], []
    if not tab_clear:
        warnings.append('the tab band at z=%g mm overlaps a pocket that opens to the side '
                        '(open over z %s mm); pass a clear tab_z' % (tab_z, openings))
    elif openings and abs(tab_z - (a['ztop'] + a['zbot']) / 2) > 1e-6:
        warnings.append('tabs moved off the mid-plane to z=%g mm, clear of the pockets that '
                        'open to the side (open over z %s mm)' % (tab_z, openings))
    plan = {
        'margin_top': max(0.0, stock['z'] - thickness - bottom),
        'margin_bottom': bottom,
        'taps': {str(k): v for k, v in (args.get('taps') or {}).items()},
        'face': args.get('face', True),
        'chamfer': args.get('chamfer', True),
        'tab': {'band': band, 'overlap': overlap, 'width': tab_width, 'z': tab_z},
        'layout': None,
    }
    cam = _cam()
    design = _design()
    _delete_previous(cam, design)
    tools = Tools()
    # dry run to learn which tools the part needs, then number them in slot order
    dry = Builder(cam, tools, [], dry=True)
    for side in ('top', 'bottom'):
        _side_ops(dry, None, a, side, plan, [])
    for desc, key in _slot_order(dry.needed):
        tools.get(desc, key)
    plan['layout'] = _make_layout(design, a, plan, warnings)
    b = Builder(cam, tools, log)
    community = _fetch_community(warnings)
    machine = _machine(community, log, warnings)
    post = _post(community['post'])
    log.append('machine %s, post %s' % (machine.description, post.description))
    name = args.get('name') or _app().activeDocument.name
    vises = [None, None]
    if args.get('fixture', True):
        box = _stock_box(body, stock)
        opening = stock['y']
        if opening > FX_MAX_OPENING:
            warnings.append('the stock is %g mm across the vise jaws; the vise opens to about %g mm'
                            % (opening, FX_MAX_OPENING))
        fixture = _fixture_bodies()
        z0, ztop = box[0][2], a['ztop'] / 10
        vises = [_add_vise(design, VISE_PREFIX + 'top', fixture,
                           *_vise_matrices(box, z0, box[1][2], False)),
                 _add_vise(design, VISE_PREFIX + 'bottom', fixture,
                           *_vise_matrices(box, -ztop, -z0, True))]
        vises[1][0].isLightBulbOn = False  # it would hang upside down over the part
    top = _setup(cam, 'Top ' + SETUP_TAG, body, machine, stock, True, vises[0])
    top_ops = _side_ops(b, top, a, 'top', plan, warnings)
    bottom = _setup(cam, 'Bottom ' + SETUP_TAG, body, machine, stock, False, vises[1])
    bottom_ops = _side_ops(b, bottom, a, 'bottom', plan, warnings)
    _nc_program(cam, name + '_top', top_ops['main'], post)
    _nc_program(cam, name + '_bottom', bottom_ops['main'], post)
    _nc_program(cam, name + '_tabs', bottom_ops['tabs'], post)
    unreached = [h for h in a['holes'] if h['side'] == 'none']
    for h in unreached:
        warnings.append('hole %.2f mm at (%g, %g) is closed at both ends; skipped'
                        % (h['dia'], h['x'], h['y']))
    for param, pt in REJECTED:
        warnings.append('Fusion refused the %s point at %s mm; that cut has no pre-drilled entry'
                        % (param, pt))
    if len(tools.order) > 6:
        warnings.append('%d tools: the Carvera ATC holds 6, so T7+ need manual changes'
                        % len(tools.order))
    print(json.dumps({
        'tools': [{'T': n, 'tool': d, 'preset': p} for n, d, p in tools.order],
        'operations': log,
        'nc_programs': [name + s for s in ('_top', '_bottom', '_tabs')],
        'tabs': {'z_mm': tab_z, 'band_mm': band, 'width_mm': tab_width,
                 'count': len(plan['layout']['tabs'])},
        'pocket_entries': len(plan['layout']['pocket_entries']),
        'warnings': warnings,
        'seconds': round(time.time() - t0, 1),
    }, indent=1))


def _wait(fut, budget):
    t0 = time.time()
    while not fut.isGenerationCompleted and time.time() - t0 < budget:
        adsk.doEvents()
        time.sleep(0.2)
    return fut.isGenerationCompleted


def _fit_thread_passes(cam, adjusted):
    """Lower a thread mill's pass count until Fusion stops discarding passes.

    A safety net: with the pass counts and pitch diameter offsets in
    threads.toml Fusion should keep every pass. If it does discard some, fewer
    passes give the same cut without the warning; generate() lists each change.
    """
    for s in cam.setups:
        if not s.name.endswith(SETUP_TAG):
            continue
        for o in s.allOperations:
            if o.strategy != 'thread':
                continue
            p = o.parameters.itemByName('numberOfStepovers')
            while 'passes were discarded' in (getattr(o, 'warning', '') or '') and int(p.value.value) > 1:
                p.expression = str(int(p.value.value) - 1)
                _wait(cam.generateToolpath(o), 20)
                adjusted.append('%s: %s passes' % (o.name, p.expression))


def generate(args=None):
    """Generate toolpaths of this module's setups, within a time budget (args['budget'] s).

    args['limit'] caps how many operations one call starts, for stepping through
    a part whose generation might hang Fusion.
    """
    args = args or {}
    budget = args.get('budget', 50)
    cam = _cam()
    adjusted = []
    ops = adsk.core.ObjectCollection.create()
    for s in cam.setups:
        if s.name.endswith(SETUP_TAG):
            for o in s.allOperations:
                if not (o.hasToolpath and o.isToolpathValid) and ops.count < args.get('limit', 10**6):
                    ops.add(o)
    if ops.count:
        fut = cam.generateToolpath(ops)
        if not _wait(fut, budget):
            print(json.dumps({'status': 'running', 'completed': fut.numberOfCompleted,
                              'of': fut.numberOfOperations}))
            return
    _fit_thread_passes(cam, adjusted)
    report = []
    pending = []
    for s in cam.setups:
        if not s.name.endswith(SETUP_TAG):
            continue
        for o in s.allOperations:
            if not (o.hasToolpath or o.hasError or o.hasWarning):
                pending.append(o.name)
                continue
            entry ={'setup': s.name, 'op': o.name, 'toolpath': o.hasToolpath and o.isToolpathValid}
            for attr in ('hasWarning', 'hasError'):
                entry[attr] = getattr(o, attr, None)
            for attr in ('warning', 'error'):
                msg = getattr(o, attr, None)
                if msg:
                    entry[attr] = msg
            try:
                entry['machining_time_s'] = round(cam.getMachiningTime(o, 1, 1, 1).machiningTime, 1)
            except Exception:
                pass
            report.append(entry)
    total = sum(e.get('machining_time_s', 0) for e in report)
    print(json.dumps({'status': 'done' if not pending else 'partial', 'adjusted': adjusted,
                      'machining_time_min': round(total / 60, 1), 'pending': pending,
                      'operations': report}, indent=1))

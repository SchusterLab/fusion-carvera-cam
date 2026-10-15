"""Toolpaths for copper parts on SchusterLab's Makera Carvera, in Autodesk Fusion.

This module runs inside Fusion's Python interpreter (through the Fusion MCP
`script` feature or a Fusion script). It encodes the lab's CAM conventions,
learned from the parts in the project 2026-01-06_smpm-sample-package-6port:

- the Carvera 3-axis machine and the Carvera post, the latest versions in the
  Carvera Community's profiles repository on GitHub;
- the skill's tools, Makera's end mills, thread mills and chamfering bit and
  McMaster-Carr's drills, in assets/carvera-tools.json (tool_library.py builds
  it), each with the feed preset "Copper 101-H02" (aluminum runs the same
  program at 200% override);
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
import runpy
import time
import tomllib
import urllib.request

import adsk.cam
import adsk.core
import adsk.fusion

SKILL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
# The skill's tools, from Makera and McMaster-Carr; tool_library.py builds this file.
TOOLS_FILE = os.path.join(SKILL_DIR, 'assets', 'carvera-tools.json')
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

DRILL_3175 = '3.175*7.4mm Drill (1/8")'
ENDMILL = 'Spiral O Metal 3.175*12mm'
ENDMILL_D = 3.175
# Smaller end mills, largest first: (description, diameter, flute length).
SMALL_ENDMILLS = [
    ('Spiral O Metal 2.5*12mm', 2.5, 12.0),
    ('Spiral O Metal 2*8mm', 2.0, 8.0),
    ('Spiral O Metal 1.5*6mm', 1.5, 6.0),
    ('Spiral O Metal 1*3mm', 1.0, 3.0),
]
# matched by prefix: Fusion reads the inch mark (″) in this name differently
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
    try:
        p.expression = expression
    except RuntimeError as e:
        raise RuntimeError('%s = %s: %s' % (name, expression, e)) from None


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
        if occ.component.name.startswith(VISE_PREFIX) or \
                occ.component.name in (TABS_COMPONENT, FACES_COMPONENT):
            continue  # the vise, tabs or face patches an earlier build added
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


def _point_angle(hole, edge):
    """The included angle (degrees) of a drill point modelled at this closed end
    of the hole: a cone narrowing away from it. None if there is none."""
    other = _other_face(edge, hole)
    if other is None or not isinstance(other.geometry, adsk.core.Cone):
        return None
    bb = other.boundingBox
    if (bb.maxPoint.x - bb.minPoint.x) / 2 > hole.geometry.radius + EPS:
        return None  # a chamfer or countersink
    return math.degrees(2 * other.geometry.halfAngle)


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
    closed = bot_edges if top_open and not bot_open else top_edges if bot_open and not top_open else []
    return {
        'face': face,
        'dia': mm(2 * g.radius),
        'z0': mm(z0), 'z1': mm(z1),
        'depth': mm(z1 - z0),
        'side': side,
        'through': top_open and bot_open,
        'x': mm(g.origin.x), 'y': mm(g.origin.y),
        'thread': threads.get(face.tempId),
        'point_angle': _point_angle(face, closed[0]) if closed else None,
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
    """The skill's tools from TOOLS_FILE, numbered into Carvera ATC slots."""

    def __init__(self):
        with open(TOOLS_FILE, encoding='utf-8') as f:
            lib = adsk.cam.ToolLibrary.createFromJson(f.read())
        self.by_desc = {}
        for t in lib:
            self.by_desc.setdefault(t.parameters.itemByName('tool_description').value.value, t)
        self.slots = {}   # key -> (tool, preset)
        self.order = []

    def has(self, desc):
        return desc in self.by_desc

    def drills(self):
        """(description, diameter, flute length) of each drill, by diameter."""
        out = []
        for d, t in self.by_desc.items():
            p = t.parameters
            if p.itemByName('tool_type').value.value == 'drill':
                out.append((d, mm(p.itemByName('tool_diameter').value.value),
                            mm(p.itemByName('tool_fluteLength').value.value)))
        return sorted(out, key=lambda x: x[1])

    def tip_angle(self, desc):
        """A drill's point angle, included (degrees)."""
        x = self.by_desc[desc].parameters.itemByName('tool_tipAngle')
        angle = x.value.value if x is not None else 118.0
        return math.degrees(angle) if angle < 2 * math.pi + 1e-6 else angle

    def tip_length(self, desc):
        """How far a drill's point reaches below its full diameter (mm)."""
        p = self.by_desc[desc].parameters
        x = p.itemByName('tool_tipAngle')
        angle = x.value.value if x is not None else 118.0
        if angle < 2 * math.pi + 1e-6:  # radians
            angle = math.degrees(angle)
        return mm(p.itemByName('tool_diameter').value.value) / 2 / math.tan(math.radians(angle / 2))

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


# The lab's machine model (Community Version 1.17-slab) differs from upstream in
# its bed: no wasteboard or anchors, the bed lowered 18 mm for x < 342 mm, where
# the fixture plate sits, and a construction point Point1 at the corner of that
# recess, which the setups attach the fixture plate to. _slab_model makes the
# same changes to the newest upstream model. Machine coordinates, mm.
SLAB_RECESS = ((-20.0, -20.0), (342.0, 230.0))
SLAB_BED_TOP = -3.175
SLAB_RECESS_FLOOR = -21.175
SLAB_POINT = (-15.0, -15.0, SLAB_RECESS_FLOOR)
SLAB_POINT_NAME = 'Point1'
SLAB_EDIT = 1  # bump when _slab_model changes, so the model is uploaded again


def _slab_model(path):
    """Write the upstream machine model with the lab's bed changes; returns its path."""
    out = os.path.splitext(path)[0] + '-slab.f3d'
    app = _app()
    keep = app.activeDocument
    im = app.importManager
    doc = im.importToNewDocument(im.createFusionArchiveImportOptions(path))
    try:
        design = adsk.fusion.FusionDocument.cast(doc).design
        root = design.rootComponent
        bed = next(o for o in root.occurrences if o.component.name == 'Bed')
        for o in list(bed.childOccurrences):
            o.deleteMe()
        body = max(bed.bRepBodies, key=lambda b: b.boundingBox.maxPoint.x - b.boundingBox.minPoint.x)
        if abs(body.boundingBox.maxPoint.z * 10 - SLAB_BED_TOP) > 0.01:
            raise RuntimeError('the upstream machine bed is no longer at z = %g mm' % SLAB_BED_TOP)
        pi = root.constructionPlanes.createInput()
        pi.setByOffset(root.xYConstructionPlane, adsk.core.ValueInput.createByReal(SLAB_BED_TOP / 10))
        plane = root.constructionPlanes.add(pi)
        sk = root.sketches.add(plane)
        (x0, y0), (x1, y1) = SLAB_RECESS
        sk.sketchCurves.sketchLines.addTwoPointRectangle(
            sk.modelToSketchSpace(adsk.core.Point3D.create(x0 / 10, y0 / 10, SLAB_BED_TOP / 10)),
            sk.modelToSketchSpace(adsk.core.Point3D.create(x1 / 10, y1 / 10, SLAB_BED_TOP / 10)))
        ei = root.features.extrudeFeatures.createInput(
            sk.profiles.item(0), adsk.fusion.FeatureOperations.CutFeatureOperation)
        ei.setDistanceExtent(False, adsk.core.ValueInput.createByReal((SLAB_RECESS_FLOOR - SLAB_BED_TOP) / 10))
        ei.participantBodies = [body]
        root.features.extrudeFeatures.add(ei)
        if abs(body.boundingBox.maxPoint.z * 10 - SLAB_BED_TOP) > 0.01 or not any(
                abs(v.geometry.z * 10 - SLAB_RECESS_FLOOR) < 1e-3 for v in body.vertices):
            raise RuntimeError('cutting the fixture recess into the machine bed failed')
        sk2 = root.sketches.add(root.xYConstructionPlane)
        sp = sk2.sketchPoints.add(adsk.core.Point3D.create(*[v / 10 for v in SLAB_POINT]))
        ci = root.constructionPoints.createInput()
        ci.setByPoint(sp)
        root.constructionPoints.add(ci).name = SLAB_POINT_NAME
        sk.isVisible = sk2.isVisible = plane.isLightBulbOn = False
        opts = design.exportManager.createFusionArchiveExportOptions(out)
        if not design.exportManager.execute(opts):
            raise RuntimeError('could not write %s' % out)
    finally:
        doc.close(False)
        keep.activate()
    return out


def _model_urn(path, sha, log):
    """Version URN of the machine's 3D model, uploaded to the user's Fusion hub.

    The community .f3d links its 3D model to a file in its author's hub, which
    other users cannot open, and Fusion then refuses the machine. So the .f3d,
    with the lab's bed (_slab_model), is uploaded once per hub and per upstream
    version, to the project MODEL_PROJECT.
    """
    data = _app().data
    hub = data.activeHub.id
    models_path = os.path.join(CACHE_DIR, 'models.json')
    try:
        with open(models_path, encoding='utf-8') as f:
            models = json.load(f)
    except (OSError, ValueError):
        models = {}
    sha = '%s+slab%d' % (sha, SLAB_EDIT)
    known = models.get(hub)
    if known and known['sha'] == sha:
        try:
            if data.findFileById(known['urn']) is not None:
                return known['urn']
        except RuntimeError:
            pass
    projects = data.dataProjects
    project = next((p for p in projects if p.name == MODEL_PROJECT), None) or projects.add(MODEL_PROJECT)
    future = project.rootFolder.uploadFile(_slab_model(path))
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
            if not isinstance(v, list):
                _set(oi.parameters, k, v)
        for k, v in params.items():  # a list is a height's reference geometry
            if isinstance(v, list):
                oi.parameters.itemByName(k).value.value = v
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


def _sel_pocket_outline(face):
    """A pocket inside the face's outer loop, as a closed boundary."""
    def f(params):
        v = params.itemByName('pockets').value
        sels = v.getCurveSelections()
        s = sels.createNewFaceContourSelection()
        s.inputGeometry = [face]
        s.loopType = adsk.cam.LoopTypes.OnlyOutsideLoops
        s.sideType = adsk.cam.SideTypes.AlwaysInsideSideType
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
    # Inner loops of an upward face, cut from the top with the tool inside them.
    # The API ignores a chain's side and takes it from its first edge's direction:
    # with compensation left, the tool is inside a chain running counter-clockwise
    # seen from +Z. An inner loop's coedges run clockwise, so the chain runs
    # against the first coedge.
    def f(params):
        v = params.itemByName('contours').value
        sels = v.getCurveSelections()
        for loop in loops:
            s = sels.createNewChainSelection()
            s.inputGeometry = [c.edge for c in loop.coEdges]
            s.isOpen = False
            s.isReverted = not loop.coEdges.item(0).isOpposedToEdge
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


def _sel_floor_outlines(faces):
    # a chain selection ignores its side type, and the contour then runs
    # outside the floor's edge; the face's outer loop with the tool inside does not
    def f(params):
        v = params.itemByName('contours').value
        sels = v.getCurveSelections()
        for face in faces:
            s = sels.createNewFaceContourSelection()
            s.inputGeometry = [face]
            s.loopType = adsk.cam.LoopTypes.OnlyOutsideLoops
            s.sideType = adsk.cam.SideTypes.AlwaysInsideSideType
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
            if sel is not None:
                sel(params)
    return f


# ------------------------------------------------------------ parameters
# Values below are the lab's, read from its existing setups. Feeds and speeds
# come from the tool preset, not from here.

# Fusion rejects a pocket's entry hole unless the end mill has about 5 x the
# tolerance of room around it: 0.5 mm at the lab's 0.1 mm tolerance. A pocket
# with less room is cut by a contour along its wall instead (_make_layout).
POCKET_TOL = 0.01  # mm
POCKET = {
    'tolerance': '%gmm' % POCKET_TOL,
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
    'peckingDepth': '%gmm' % THREAD_COMMON['drill']['peck'],
    'accumulatedPeckingDepth': '%gmm' % THREAD_COMMON['drill']['accumulated_peck'],
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
# The rough face leaves this much on the outer face, and the finishing face
# takes it off after every other cut but the deburr: facing leaves fewer burrs
# than side milling and thread milling. Until then every operation that starts
# at the face starts this much higher.
FACE_ALLOWANCE = 0.2
# The facing pockets face this far past the part's outline: the outline cut is
# one end mill wide outside it, and starts on the faced stock (_add_face_patches).
# The main entry is the outline cut's, one radius outside the outline, and Fusion
# wants about 5 x the pocket tolerance of room round an entry hole.
FACE_MARGIN = ENDMILL_D + 5 * POCKET_TOL


def _drill_ends(through):
    """Peck and breakthrough of a drill op, from threads.toml (the lab drills every hole alike)."""
    d = THREAD_COMMON['drill']
    brk = d['through_breakthrough'] if through else d['blind_breakthrough']
    return {'cycleType': "'%s'" % d['cycle'], 'peckingDepth': '%gmm' % d['peck'],
            'accumulatedPeckingDepth': '%gmm' % d['accumulated_peck'],
            'chipBreakDistance': '%gmm' % d['chip_break'], 'breakThroughDepth': '%gmm' % brk,
            'drillTipThroughBottom': 'true' if through else 'false'}


def _thread_params(name, through, allow=0.0):
    """Thread-mill parameters for one thread size, from threads.toml; `allow` is
    the material still on the face above the hole top. The thread mill never
    goes deeper than the hole it threads: a through hole is drilled 0.5 mm past
    its bottom, but a blind hole ends at its bottom, so there the thread mill
    stops at the bottom, whatever threads.toml says, and enters along a flat
    lead, since a helical one starts below the bottom height."""
    r, c = THREAD_RECIPES[name], THREAD_COMMON['thread']
    r = dict(r, **r.get('through' if through else 'blind', {}))
    stepover = 'tool_stepover' if r['stepover'] == 'tool' else '%gmm' % r['stepover']
    bottom = c['through_bottom_offset'] if through else max(0.0, c['blind_bottom_offset'])
    return dict(THREAD, direction="'%s'" % c['direction'], threadPitch='%gmm' % r['pitch'],
                pitchDiameterOffset='%gmm' % r['pitch_diameter_offset'],
                numberOfStepovers=str(r['passes']), stepover=stepover,
                topHeight_offset='%gmm' % (c['top_offset'] + allow),
                bottomHeight_offset='%gmm' % bottom,
                useHelicalLeads='true' if through else 'false')
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


def _stage_plan(start, target, mill_end, reach):
    """Drill to `target` and mill to `mill_end` (depths, mm), drilling at most
    `reach` below the material the drill starts in: drill as deep as it
    reaches, mill down to there, drill on from the milled floor, and so on, as
    the lab does by hand. Returns ([('drill' or 'mill', top, bottom)], whether
    the drill reaches `target`)."""
    seq, d = [], start
    m = start
    while True:
        e = min(d + reach, target)
        seq.append(('drill', d, e))
        if e >= target - 1e-6:
            if mill_end > m + 1e-6:
                seq.append(('mill', m, mill_end))
            return seq, True
        me = min(e, mill_end)
        if me <= m + 1e-6:  # milled to the end, and the drill still falls short
            seq.pop()
            return seq, False
        seq.append(('mill', m, me))
        m = d = me


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
    Y through the stock's centre, as the setup's WCS turns it: the stock turns
    about the jaws' axis, so its back edge stays on the fixed jaw.
    """
    (x0, y0, _), (x1, y1, _) = box
    cx = (x0 + x1) / 2
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
        flip.setToRotation(math.pi, adsk.core.Vector3D.create(0, 1, 0),
                           adsk.core.Point3D.create(cx, 0, 0))
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


# The stock box corner each setup's origin sits on, as in the lab's setups:
# corner 2 is the stock top's corner at the setup's +X and -Y. Box corners and
# the fixed box's sides follow the setup's axes, whose X is the model's Y.
WCS_CORNER = {True: 'top 2', False: 'top 2'}
STOCK_X_MODE = {'back': 'right', 'front': 'left', 'center': 'center'}


def _machine_point(cam):
    """The machine model's table attach point (SLAB_POINT_NAME), in the CAM document."""
    croot = cam.designRootOccurrence.assemblyContext.sourceComponent
    found = []
    for occ in croot.occurrences:
        if not occ.name.startswith('Machines'):
            continue
        stack = list(occ.childOccurrences)
        while stack:
            o = stack.pop(0)
            for cp in o.component.constructionPoints:
                if cp.name == SLAB_POINT_NAME:
                    found.append(cp.createForAssemblyContext(o))
            stack += list(o.childOccurrences)
    return found[-1] if found else None


def _axis_line(design):
    """A sketch line along the model's +Y: the setups' X axis (see _setup)."""
    root = design.rootComponent
    sk = root.sketches.add(root.xYConstructionPlane)
    sk.name = LAYOUT_SKETCH + ' axes'
    ln = sk.sketchCurves.sketchLines.addByTwoPoints(adsk.core.Point3D.create(0, 0, 0),
                                                    adsk.core.Point3D.create(0, 1, 0))
    sk.isVisible = False
    return ln


def _setup(cam, name, body, machine, stock, top, x_axis, vise=None, warnings=None):
    """One setup. Its X axis is the model's +Y in both setups: the vise's
    jaws close along the Carvera's X axis and grip the stock's Y edges, so the
    part lies turned 90 degrees on the machine, as in the lab's setups."""
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
    _set(p, 'wcs_orientation_mode', "'axesZX'")
    p.itemByName('wcs_orientation_axisX').value.value = [x_axis]
    if vise is not None:
        # put the fixture plate's corner on the machine's table point, so
        # simulation checks the machine as well as the vise
        table = _machine_point(cam) if machine is not None and machine.hasSimulationModel else None
        if table is None:
            if warnings is not None and top:
                warnings.append('the machine model has no table point %r, so the setups are not '
                                'placed on the machine' % SLAB_POINT_NAME)
        else:
            _try_set(p, 'job_position', 'true')
            _try_set(p, 'job_positionReference_origin_mode', "'point'")
            p.itemByName('job_positionAttach').value.value = [table]
            p.itemByName('job_positionReference_origin_point').value.value = [vise[1]]
    if top:
        _set(p, 'job_stockMode', "'fixedbox'")
        # the box follows the setup's axes: its X is the model's Y
        _set(p, 'job_stockFixedX', '%gmm' % stock['y'])
        _set(p, 'job_stockFixedY', '%gmm' % stock['x'])
        _set(p, 'job_stockFixedZ', '%gmm' % stock['z'])
        _set(p, 'job_stockFixedXMode', "'%s'" % STOCK_X_MODE[stock.get('y_mode', 'back')])
        _set(p, 'job_stockFixedXOffset', '%gmm' % stock.get('y_offset', 10))
        _set(p, 'job_stockFixedYMode', "'center'")
        _set(p, 'job_stockFixedZMode', "'bottom'")
        _set(p, 'job_stockFixedZOffset', '%gmm' % stock['bottom'])
        _set(p, 'wcs_origin_mode', "'stockPoint'")
        _set(p, 'wcs_origin_boxPoint', "'%s'" % WCS_CORNER[True])
    else:
        _set(p, 'job_stockMode', "'previoussetup'")
        # turned about the setup's X axis (the model's Y), as the operator
        # turns the stock in the vise: Z flips, X stays
        _set(p, 'wcs_orientation_flipZ', 'true')
        _set(p, 'wcs_origin_mode', "'stockPoint'")
        _set(p, 'wcs_origin_boxPoint', "'%s'" % WCS_CORNER[False])
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
TABS_COMPONENT = LAYOUT_SKETCH + ' tabs'
FACES_COMPONENT = LAYOUT_SKETCH + ' faces'
TOOL_R = ENDMILL_D / 2 / 10  # cm
ENTRY_R = TOOL_R + 5 * POCKET_TOL / 10  # cm, the room a pocket's entry hole needs


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


def _add_tabs(design, lay, tab):
    """Model the tabs in their own component: one body per tab, the strip of
    stock the outline cut leaves under it, one end mill wide outward from the
    outline, from the tab band's bottom to its top. Its two ends are concave,
    where the cut between the tabs stops with the end mill's centre one radius
    out from the tab's edge. Return the first tab's top and bottom faces (as
    seen from the root), which the cuts around the tabs take their heights from;
    Fusion reads a height from them when it is set, not when the tabs change."""
    root = design.rootComponent
    occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = TABS_COMPONENT
    sk = comp.sketches.add(comp.xYConstructionPlane)
    sk.name = 'tab outlines'
    lines, arcs = sk.sketchCurves.sketchLines, sk.sketchCurves.sketchArcs
    w = 2 * TOOL_R
    for t in lay['tabs']:
        d0 = t['dir']
        for c in t['curves']:
            ev = c.geometry.evaluator
            ok, t0, t1 = ev.getParameterExtents()
            pts, outs, ends = [], [], []
            for u in (t0, (t0 + t1) / 2, t1):
                ok, q = ev.getPointAtParameter(u)
                ok, der = ev.getFirstDerivative(u)
                if der.x * d0.x + der.y * d0.y < 0:  # run along the tab, as the outline does
                    der = adsk.core.Vector3D.create(-der.x, -der.y, 0)
                n = math.hypot(der.x, der.y)
                # a clockwise loop has its outside on the left of the direction of travel
                pts.append(adsk.core.Point3D.create(q.x, q.y, 0))
                outs.append(adsk.core.Point3D.create(q.x - der.y / n * w, q.y + der.x / n * w, 0))
                # the end mill's centre where the cut beside the tab stops, and the
                # deepest point of the arc it leaves in the tab's end
                cx, cy = q.x - der.y / n * TOOL_R, q.y + der.x / n * TOOL_R
                k = TOOL_R if u == t0 else -TOOL_R
                ends.append(adsk.core.Point3D.create(cx + der.x / n * k, cy + der.y / n * k, 0))
            if isinstance(c, adsk.fusion.SketchLine):
                lines.addByTwoPoints(pts[0], pts[2])
                lines.addByTwoPoints(outs[0], outs[2])
            else:
                arcs.addByThreePoints(pts[0], pts[1], pts[2])
                arcs.addByThreePoints(outs[0], outs[1], outs[2])
            arcs.addByThreePoints(pts[0], ends[0], outs[0])
            arcs.addByThreePoints(pts[2], ends[2], outs[2])
    profiles = adsk.core.ObjectCollection.create()
    for pr in sk.profiles:
        profiles.add(pr)
    ext = comp.features.extrudeFeatures
    ei = ext.createInput(profiles, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    ei.startExtent = adsk.fusion.OffsetStartDefinition.create(
        adsk.core.ValueInput.createByReal((tab['z'] - tab['band'] / 2) / 10))
    ei.setDistanceExtent(False, adsk.core.ValueInput.createByReal(tab['band'] / 10))
    e = ext.add(ei)
    e.name = 'tabs'
    for k, body in enumerate(e.bodies):
        body.name = 'tab %d' % (k + 1)
    flat = [f for f in e.bodies.item(0).faces if isinstance(f.geometry, adsk.core.Plane)
            and abs(abs(f.geometry.normal.z) - 1) < 1e-6]
    return {'top': max(flat, key=lambda f: f.pointOnFace.z).createForAssemblyContext(occ),
            'bottom': min(flat, key=lambda f: f.pointOnFace.z).createForAssemblyContext(occ)}


def _add_face_patches(design, lay, a):
    """The faces the facing pockets select: in their own component, a planar
    patch filling the part's outline at its top and one at its bottom. The
    part's own outer faces have holes where the pockets, windows and holes
    open, and a pocket on them goes round those, leaving the stock above them
    uncut; the patches have none. The outline is the layout's, the larger of the
    top and bottom faces' outer loops, so a pocket open to the side is covered too.
    The patch reaches FACE_MARGIN past the outline, so that the face also clears
    the stock the outline cut comes down through, one end mill wide outside it.
    Return {'top': face, 'bottom': face} as seen from the root."""
    root = design.rootComponent
    occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = FACES_COMPONENT
    out = {}
    for side, z in (('top', a['ztop']), ('bottom', a['zbot'])):
        pi = comp.constructionPlanes.createInput()
        pi.setByOffset(comp.xYConstructionPlane, adsk.core.ValueInput.createByReal(z / 10))
        plane = comp.constructionPlanes.add(pi)
        plane.name = 'face ' + side
        sk = comp.sketches.add(plane)
        sk.name = 'face outline ' + side
        curves = adsk.core.ObjectCollection.create()
        for pc in lay['outline']:
            for c in _sketch_curve(sk, pc, 0, pc['len']):
                curves.add(c)
        bb = sk.boundingBox
        away = adsk.core.Point3D.create(bb.maxPoint.x + 10, bb.maxPoint.y + 10, 0)
        sk.offset(curves, away, FACE_MARGIN / 10)
        for c in curves:
            c.isConstruction = True  # the patch is the offset loop's inside
        prof = max(sk.profiles, key=lambda pr: pr.areaProperties().area)
        patches = comp.features.patchFeatures
        e = patches.add(patches.createInput(prof, adsk.fusion.FeatureOperations.NewBodyFeatureOperation))
        e.name = 'face ' + side
        body = e.bodies.item(0)
        body.name = 'face ' + side
        out[side] = body.faces.item(0).createForAssemblyContext(occ)
    return out


def _at_tab(which, face, offset=0.0):
    """A top or bottom height at a tab face, `offset` mm along the setup's tool axis,
    and the selector that sets the face. None without the tab bodies."""
    if face is None:
        return {}, None

    def f(params):
        params.itemByName(which + 'Height_ref').value.value = [face]
    return {which + 'Height_mode': "'from point'", which + 'Height_offset': '%gmm' % offset}, f


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


def _edge_points(e, reverse=False):
    """(x, y) points every 0.1 mm along an edge (strokes give a straight edge only its ends)."""
    ev = e.evaluator
    ok, t0, t1 = ev.getParameterExtents()
    n = max(2, int(e.length / 0.01))
    pts = []
    for k in range(n + 1):
        try:  # the end parameters can fall just outside the edge's range
            ok, q = ev.getPointAtParameter(min(max(t0 + (t1 - t0) * k / n, t0), t1))
        except RuntimeError:
            continue
        if ok:
            pts.append((q.x, q.y))
    return pts[::-1] if reverse else pts


def _entry_point(inside, edges, z, bbox):
    """The point inside farthest from the edges, its clearance (cm), and the
    nearest point on the edges. The search uses sampled edge points; the
    clearance is measured on the edges."""
    c, x, y = _most_open_point(inside, [q for e in edges for q in _edge_points(e)], bbox)
    q = None
    if c > 0:
        mm_ = adsk.core.Application.get().measureManager
        pt = adsk.core.Point3D.create(x, y, z)
        r = min((mm_.measureMinimumDistance(pt, e) for e in edges), key=lambda r: r.value)
        # the wall end of the measured distance, whichever of the two positions it is
        w = max((r.positionOne, r.positionTwo), key=lambda g: g.distanceTo(pt))
        c, q = r.value, (w.x, w.y)
    return c, x, y, q


def _on_contour(c, x, y, q):
    """A contour plunges on its own path, one tool radius from the wall, at the
    point nearest its entry position: move the entry from (x, y) toward the
    nearest wall point q until it is on that path. Every other wall stays at
    least a tool radius away, since it was at least c away."""
    k = (c - TOOL_R) / c
    return x + (q[0] - x) * k, y + (q[1] - y) * k


def _pocket_entry(face):
    """Point on a pocket floor farthest from its edges (see _entry_point)."""
    bb = face.boundingBox
    z = face.pointOnFace.z
    fev = face.evaluator

    def inside(x, y):
        ok, uv = fev.getParameterAtPoint(adsk.core.Point3D.create(x, y, z))
        return ok and fev.isParameterOnFace(uv)

    return _entry_point(inside, [e for lp in face.loops for e in lp.edges], z,
                        (bb.minPoint.x, bb.minPoint.y, bb.maxPoint.x, bb.maxPoint.y))


def _window_entry(loop):
    """Point inside a window's loop farthest from it (see _entry_point)."""
    bb = loop.boundingBox
    poly = [q for ce in loop.coEdges for q in _edge_points(ce.edge, ce.isOpposedToEdge)]

    def inside(x, y):  # ray casting
        hit = False
        for (u0, v0), (u1, v1) in zip(poly, poly[1:] + poly[:1]):
            if (v0 > y) != (v1 > y) and x < u0 + (y - v0) * (u1 - u0) / (v1 - v0):
                hit = not hit
        return hit

    return _entry_point(inside, [ce.edge for ce in loop.coEdges], bb.maxPoint.z,
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
    narrow = set()  # closed pockets the end mill fits, too narrow for a pocket toolpath's entry
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
        c, x, y, q = _pocket_entry(f['face'])
        if c >= ENTRY_R:
            pocket_entries[f['face'].tempId] = {
                'point': sk.sketchPoints.add(adsk.core.Point3D.create(x, y, 0)),
                'side': f['side'], 'depth': f['depth'], 'drill': True}
        elif c >= TOOL_R:
            # a pocket toolpath cannot enter there, but a contour along the wall can
            narrow.add(f['face'].tempId)
            x, y = _on_contour(c, x, y, q)
            pocket_entries[f['face'].tempId] = {
                'point': sk.sketchPoints.add(adsk.core.Point3D.create(x, y, 0)),
                'side': f['side'], 'depth': f['depth'], 'drill': True}
            warnings.append('pocket at z=%g mm leaves the end mill %.3f mm of room, too little '
                            'for a pocket toolpath to enter: a 2D contour cuts it, entering '
                            'through its entry hole' % (f['z'], (c - TOOL_R) * 10))
    # one entry per through window, drilled through the part
    window_entries = []
    for w in a['windows']:
        c, x, y, q = _window_entry(w['loop'])
        if c >= TOOL_R:
            x, y = _on_contour(c, x, y, q)
            window_entries.append(sk.sketchPoints.add(adsk.core.Point3D.create(x, y, 0)))
        else:
            window_entries.append(None)
            warnings.append('window at z=%g mm is narrower than the end mill; it gets no '
                            'entry hole' % w['z'])
    shelves = _shelf_regions(root, a, sk, pocket_entries)
    _merge_entries(pocket_entries)
    return {'sketch': sk, 'corner': corner, 'tabs': tab_chains, 'shelves': shelves,
            'entry': corner[0]['entry'], 'pocket_entries': pocket_entries, 'narrow': narrow,
            'outline': pieces,
            'window_entries': window_entries}


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
                      if c >= ENTRY_R else None)
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
    """The lab's slot order: T1 the 3.175 mm drill, T2/T3 the roughing and
    finishing end mill, then every other tool in the order the programs first
    use it (`needed` is in that order: the top setup's operations, then the
    bottom's)."""
    def rank(item):
        desc, key = item
        if desc == DRILL_3175 and key is None:
            return 0
        if desc == ENDMILL:
            return 1 if key == 'rough' else 2
        return 3
    return sorted(needed, key=rank)  # stable, so the rest keep their order of use


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
    facing = [f for f in floors if f['role'] == 'face'] if plan['face'] else []
    # the material the rough face leaves on the face, until the finishing face
    allow = FACE_ALLOWANCE if facing else 0.0
    # the stock's highest point in this setup, in mm above the part's face: the
    # stock top until the first face, then the top of what the latest face left.
    # face_op updates it; the operations are made in the order they run.
    patch = (plan.get('face_patches') or {}).get(side)
    high = [m if facing else 0.0]

    def top_at(depth):
        """The top height of an operation starting `depth` mm below the part's
        face. One starting at the face or above it starts at the stock's highest
        point: after a face, that face's patch plus what the face left on it."""
        if depth > 1e-6:  # a stage of a cut deeper than the drill reaches
            return _top_below_part_top(depth, m)
        if facing and high[0] >= m - 1e-6:  # not faced yet
            return {'topHeight_mode': "'from stock top'", 'topHeight_offset': '0mm'}
        ref = patch if patch is not None else face
        if ref is None:
            return _top_below_part_top(-high[0], m)
        return at('top', ref, high[0])

    def at(which, face, offset=0.0):
        """A top or bottom height at a face of the model, `offset` mm along the
        tool axis: heights come from geometry, and offsets only add the
        lab's fixed allowances (FACE_ALLOWANCE, the 0.5 mm breakthrough)."""
        return {which + 'Height_mode': "'from point'", which + 'Height_offset': '%gmm' % offset,
                which + 'Height_ref': [face]}
    # the part's face on the far side, where windows and through holes come out
    far_face = a['bottom_face'] if top else a['top_face']

    def through_part(which):
        if far_face is None:
            return (_bottom_below_part_top if which == 'bottom' else _top_below_part_top)(
                thickness + 0.5, m)
        return at(which, far_face, -0.5)

    def from_hole_top():
        return {'topHeight_offset': '%gmm' % max(0.0, high[0])}

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
            best = min(match, key=lambda d: abs(d[1] - h['dia']))  # the closest in diameter
            drilled.append((h, best[0]))
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

    # T1 drills at most its flute length less 0.5 mm into the material it starts
    # in; deeper holes go down in stages with the cut that uses them (_stage_plan)
    reach = next(d[2] for d in drills if d[0] == DRILL_3175) - 0.5

    def drill_for(depth):
        if depth > reach + 1e-6:
            warnings.append('a %.1f mm hole is deeper than the %s reaches (%.1f mm)'
                            % (depth, DRILL_3175, reach))
        return DRILL_3175

    lay = plan['layout']
    band_top, band_bot = tab['z'] + tab['band'] / 2, tab['z'] - tab['band'] / 2
    # the cuts around the tabs take their heights from the modelled tabs' faces
    tab_faces = plan.get('tab_faces') or {}
    tab_top, tab_bottom = tab_faces.get('top'), tab_faces.get('bottom')
    chain_bot = band_bot - tab['overlap']
    stock_z = thickness + plan['margin_top'] + plan['margin_bottom']
    points = lambda pts: _sel_faces('holePoints', pts)
    entry_ops = {'holeMode': "'selection-points'", 'topHeight_mode': "'from stock top'",
                 'topHeight_offset': '0mm'}

    # T1 drills only the main entry before the end mill cuts: the facing and the
    # outline enter there. Every other hole waits for the end mill to clear the
    # stock above it (later_drills), so the drill cuts only what it must.
    # The top setup drills the entry through the stock; where the stock is
    # deeper than the drill reaches, it drills and cuts the outline in stages,
    # and if the drill still cannot get through, the bottom setup drills the
    # entry from its own side, staged with its outline.
    outline_depth = {True: plan['margin_top'] + a['ztop'] - band_top,  # from the stock top
                     False: plan['margin_bottom'] + band_bot - a['zbot']}
    entry_seq, through = _stage_plan(0, stock_z, outline_depth[True], reach)
    if not top:
        if through:
            entry_seq = [('mill', 0, outline_depth[False])]
        else:
            entry_seq, ok = _stage_plan(0, outline_depth[False], outline_depth[False], reach)
            if not ok:
                warnings.append('the entry hole cannot be drilled to the outline depth')
    staged_entry = sum(k == 'mill' for k, _, _ in entry_seq) > 1

    def entry_drill(s, e):
        params = dict(DRILL, **entry_ops, drillTipThroughBottom='true')
        params.update(top_at(s - m))
        if top and e >= stock_z - 1e-6:
            params.update(bottomHeight_mode="'from stock bottom'", bottomHeight_offset='0mm',
                          breakThroughDepth='0.5mm')
        else:
            params.update(bottomHeight_mode="'from stock top'",
                          bottomHeight_offset='%.4fmm' % -e, breakThroughDepth='0mm')
            face = tab_top if top else tab_bottom
            if abs(e - outline_depth[top]) < 1e-6 and face is not None:  # to the tab band
                params.update(at('bottom', face))
        name = 'Entry hole %g to %g mm' % (round(s, 2), round(e, 2)) if staged_entry else 'Entry hole'
        return b.op(setup, 'drill', DRILL_3175, name, params, points([lay['entry']] if lay else []))

    if entry_seq[0][0] == 'drill':
        ops['main'].append(entry_drill(*entry_seq[0][1:]))
        if plan['face'] and m > entry_seq[0][2] + 1e-6:
            warnings.append('the facing (%.1f mm) is deeper than the first stage of the entry hole'
                            % m)
        entry_seq = entry_seq[1:]
    side_entries = {}
    if lay:
        side_entries = {k: e for k, e in lay['pocket_entries'].items() if e['side'] == side}
    else:  # dry run: the entries are not sketched yet
        side_entries = {f['face'].tempId: {'point': None, 'depth': f['depth'],
                                           'drill': not f['side_open']}
                        for f in floors if f['role'] == 'pocket'}

    # Pocket entries and pockets go down together in bands of the drill's
    # reach from the face: the entries are drilled as deep as the drill
    # reaches, the pockets cut down to there, and so on to the floors. A part
    # with no pocket deeper than the reach has one band.
    holes_in = []  # (entry point, floor depth), one per drilled hole
    for e in side_entries.values():
        if e['drill'] and (e['point'] is None or all(e['point'] is not q for q, _ in holes_in)):
            holes_in.append((e['point'], e['depth']))
    # each entry hole's drill tip goes through the pocket floor
    dimples = [(q, d) for q, d in holes_in if q is not None]
    if dimples:
        tip = b.tools.tip_length(DRILL_3175)
        where = ', '.join('(%.2f, %.2f)' % (mm(q.geometry.x), mm(q.geometry.y)) for q, _ in dimples)
        warnings.append('the %d pocket entry hole%s on the %s leave%s a cone-shaped dimple %g mm '
                        'across and %.2f mm deep in the pocket floor, so that the end mill '
                        'never plunges; at x, y = %s mm'
                        % (len(dimples), '' if len(dimples) == 1 else 's', side,
                           's' if len(dimples) == 1 else '', ENDMILL_D, tip, where))
        thin = [d for _, d in dimples if thickness - d < tip + 0.2]
        if thin:
            warnings.append('a pocket entry hole\'s dimple comes within 0.2 mm of the part\'s '
                            'other face, or through it, under a floor %g mm deep'
                            % max(thin))
    pocket_bands = []  # (top, bottom or None for the floors, [(point, drill bottom)])
    t = -allow
    while True:
        bot = t + reach
        last = not any(d > bot + 1e-6 for _, d in holes_in)
        pocket_bands.append((t, None if last else bot,
                             [(p, min(bot, d)) for p, d in holes_in if d > t + 1e-6]))
        if last:
            break
        t = bot
    staged_pockets = len(pocket_bands) > 1
    floor_at = {round(f['depth'], 3): f['face'] for f in floors if f['role'] == 'pocket'}

    def entry_drills(band):
        t, bot, holes = band
        by_depth = {}
        for p, d in holes:
            by_depth.setdefault(round(d, 3), []).append(p)
        out = []
        for depth, pts in sorted(by_depth.items()):
            # the tip goes through, so that the hole is full width down to the
            # floor and the end mill never plunges; it leaves a dimple there
            params = dict(DRILL, holeMode="'selection-points'", breakThroughDepth='0mm',
                          drillTipThroughBottom='true', **top_at(t))
            floor = floor_at.get(depth)
            # down to the floor, or a stage of the drill's reach above it
            params.update(at('bottom', floor) if floor is not None
                          else _bottom_below_part_top(depth, m))
            name = ('Pocket entry holes %g to %g mm' % (round(max(t, 0), 2), depth)
                    if staged_pockets else 'Pocket entry holes %g mm' % depth)
            out.append(b.op(setup, 'drill', drill_for(depth - t), name, params, points(pts)))
        return out

    # bores deeper than the drill reaches are pre-drilled in stages with their
    # rough bore, at the bore; the others are pre-drilled with the entries
    end_bored = [h for h, rough, _ in bored if rough[0] == ENDMILL]
    deep_bore = lambda h: h['depth'] > reach + 1e-6

    def later_drills():
        """T1 after the rough face and outline: tab entries from the tab band's
        top, which the outline has cut down to there, and pocket entries and
        bore pre-drills from the face, on the material left for its finish."""
        out = []
        if top and (lay is None or len(lay['corner']) > 1):
            depth = a['ztop'] - chain_bot
            params = dict(DRILL, holeMode="'selection-points'", breakThroughDepth='0mm',
                          drillTipThroughBottom='true')
            params.update(_top_below_part_top(a['ztop'] - band_top, m))
            params.update(_bottom_below_part_top(depth, m))
            hi, set_hi = _at_tab('top', tab_top)
            lo, set_lo = _at_tab('bottom', tab_bottom, -tab['overlap'])
            params.update(hi, **lo)
            out.append(b.op(setup, 'drill', drill_for(band_top - chain_bot), 'Tab entry holes',
                            params, _both(points([c['entry'] for c in lay['corner'][1:]]
                                                 if lay else []), set_hi, set_lo)))
        out += entry_drills(pocket_bands[0])
        # window entries, through the part, where the window contours enter
        pts = [p for p in (window_entry(w) for w in windows) if p is not None]
        if pts or (lay is None and windows):
            params = dict(DRILL, holeMode="'selection-points'", breakThroughDepth='0mm',
                          drillTipThroughBottom='true', **top_at(0))
            params.update(through_part('bottom'))
            out.append(b.op(setup, 'drill', drill_for(thickness + 0.5), 'Window entry holes',
                            params, points(pts)))
        # pre-drill everything the 3.175 end mill will bore
        pre = [h for h in end_bored + [h for h, _ in tap_bored] if not deep_bore(h)]
        for through in (True, False):
            hs = [h for h in pre if h['through'] == through]
            if hs:
                out.append(b.op(setup, 'drill', drill_for(max(h['depth'] for h in hs)),
                                'Pre-drill bores' + ('' if through else ' (blind)'),
                                dict(DRILL, **_drill_ends(through), **from_hole_top()),
                                _sel_faces('holeFaces', [h['face'] for h in hs])))
        return out

    pockets = sorted([f for f in floors if f['role'] == 'pocket'], key=lambda f: f['depth'])
    narrow = [f for f in pockets if lay and f['face'].tempId in lay['narrow']
              and f['face'].tempId not in lay['shelves']]
    windows = [w for w in a['windows']] if top else []

    def window_entry(w):
        return lay['window_entries'][a['windows'].index(w)] if lay else None
    entry = [lay['entry']] if lay else []

    def face_op(which, multi, suffix, leave):
        params = dict(POCKET, doMultipleDepths=multi, bottomHeight_mode="'from contour'",
                      rampType="'predrill'", **top_at(-m))
        if leave:
            params.update(useStockToLeave='true', simpleStockToLeave='false',
                          stockToLeave='0mm', verticalStockToLeave='%gmm' % leave)
        # the patch over the face, not the face: see _add_face_patches
        # the patch's edge, not the patch: nothing walls a selected face in, so
        # Fusion takes it for an open pocket and runs the tool past its edge
        sel = _sel_pocket_outline(patch) if patch else _sel_pocket([f['face'] for f in facing])
        op = b.op(setup, 'pocket2d', (ENDMILL, which), 'Face' + suffix, params,
                  _both(sel,
                        _sel_faces('predrillPositions', entry)))
        high[0] = leave  # the face is now the stock's highest point
        return op

    if facing:
        ops['main'].append(face_op('rough', 'true', '', allow))

    def staged_bore(hs, which, tool, params, bottom_offset, name):
        """A bore op for the holes `hs`; a rough bore deeper than the drill
        reaches is pre-drilled and bored in stages (_stage_plan)."""
        h = hs[0]
        t0 = depth_of(h['z1'] if top else h['z0'])  # the hole's top, below the part's
        faces = [x['face'] for x in hs]
        seq = [('mill', 0, h['depth'])]
        if which == 'rough' and deep_bore(h):
            seq, _ = _stage_plan(0, h['depth'], h['depth'], reach)
        for kind, s, e in seq:
            if t0 > 1e-6 and s <= 0:
                top_h = {'topHeight_mode': "'from hole top'", 'topHeight_offset': '0mm'}
            else:
                top_h = top_at(t0 + s)
            last = e >= h['depth'] - 1e-6
            stage = '' if len(seq) == 1 else ' %g to %g mm' % (round(s, 2), round(e, 2))
            if kind == 'drill':
                p = dict(DRILL, **top_h)
                if last:
                    p.update(_drill_ends(h['through']))
                else:
                    p.update(_bottom_below_part_top(t0 + e, m), breakThroughDepth='0mm',
                             drillTipThroughBottom='true')
                ops['main'].append(b.op(setup, 'drill', DRILL_3175,
                                        'Pre-drill ' + name[0].lower() + name[1:] + stage, p,
                                        _sel_faces('holeFaces', faces)))
                continue
            p = dict(params, **top_h)
            if last:
                p['bottomHeight_offset'] = '%gmm' % bottom_offset
            else:
                p.update(_bottom_below_part_top(t0 + e, m))
            ops['main'].append(b.op(setup, 'bore', tool, name + stage, p,
                                    _sel_faces('circularFaces', faces)))

    passes = (('rough', 'true', ''), ('finish', 'false', ' (finish)'))
    for which, multi, suffix in passes:
        tk = (ENDMILL, which)
        # outline: the top setup cuts down to the tab band, the bottom setup up to it
        bands = entry_seq if which == 'rough' else [('mill', 0, outline_depth[top])]
        for kind, s, e in bands:
            if kind == 'drill':
                ops['main'].append(entry_drill(s, e))
                continue
            params = dict(CONTOUR, doMultipleDepths=multi, bottomHeight_mode="'from stock top'",
                          bottomHeight_offset='%.4fmm' % -e, **top_at(s - m))
            name = ('Outline %g to %g mm' % (round(s, 2), round(e, 2))
                    if staged_entry and which == 'rough' else 'Outline' + suffix)
            set_lo = None
            if abs(e - outline_depth[top]) < 1e-6:  # down to the tab band: the tabs' face
                lo, set_lo = _at_tab('bottom', tab_top if top else tab_bottom)
                params.update(lo)
            ops['main'].append(b.op(setup, 'contour2d', tk, name, params, _both(
                _sel_silhouette(), _sel_faces('entryPositions', entry), set_lo)))
        if which == 'rough':
            ops['main'] += later_drills()
        if pockets:
            shelves = lay['shelves'] if lay else {}
            # what a pocket op selects: the plain floors, and one chain per shelf
            # region, though several shelves share it; each with its depth and entry
            units = []  # (floor face or None, chain or None, depth, entry point or None)
            for f in pockets:
                if f in narrow:
                    continue
                e = side_entries.get(f['face'].tempId)
                c = shelves.get(f['face'].tempId)
                if c is None or all(c is not u[1] for u in units):
                    units.append((f['face'] if c is None else None, c, f['depth'],
                                  e['point'] if e else None))
            has_entries = any(f['face'].tempId in side_entries for f in pockets
                              if f not in narrow)
            bands = pocket_bands if which == 'rough' else [(-allow, None, [])]
            for k, band in enumerate(bands):
                t, bot, _ = band
                if k:
                    ops['main'] += entry_drills(band)
                inside = [u for u in units if u[2] > t + 1e-6]
                # the pockets this band reaches the floor of, then the deeper ones
                for floor in (True, False):
                    us = [u for u in inside if (bot is None or u[2] <= bot + 1e-6) == floor]
                    if not us:
                        continue
                    unique = []
                    for u in us:
                        if u[3] is not None and all(u[3] is not q for q in unique):
                            unique.append(u[3])
                    params = dict(POCKET, doMultipleDepths=multi,
                                  rampType="'predrill'" if has_entries else "'plunge'",
                                  **top_at(t))
                    if floor:
                        params['bottomHeight_mode'] = "'from contour'"
                    else:
                        params.update(_bottom_below_part_top(bot, m))
                    name = 'Pockets' + suffix
                    if staged_pockets and which == 'rough':
                        name += (' %g mm to the floors' % round(max(t, 0), 2) if floor
                                 else ' %g to %g mm' % (round(max(t, 0), 2), round(bot, 2)))
                    ops['main'].append(b.op(setup, 'pocket2d', tk, name, params, _both(
                        _sel_pocket_regions([u[0] for u in us if u[0] is not None],
                                            [u[1] for u in us if u[1] is not None]),
                        _sel_faces('predrillPositions', unique))))
        for hs in _group(end_bored):
            h = hs[0]
            radial = max(0.1, (h['dia'] - ENDMILL_D) / 2)
            # the finishing bore is a single pass at the wall
            staged_bore(hs, which, tk, dict(BORE, stepover='0.3mm',
                                            numberOfStepovers=str(max(1, math.ceil(radial / 0.3))),
                                            doMultiplePasses=multi),
                        -0.5 if h['through'] else 0.0, 'Bore %.2f%s' % (h['dia'], suffix))
        # tap holes bored to size: one light pass, plus a finishing pass in blind holes
        for hs in _group([h for h, _ in tap_bored]):
            h = hs[0]
            t = [t for x, t in tap_bored if x is h][0]
            bore = THREAD_RECIPES[t]['bore']
            if which == 'finish' and (h['through'] or not bore.get('blind_finish_pass')):
                continue
            staged_bore(hs, which, (bore['end_mill'], which),
                        dict(BORE, stepover='%gmm' % bore['stepover'],
                             numberOfStepovers=str(bore['passes']), doMultiplePasses=multi),
                        bore['through_bottom_offset'] if h['through'] else 0.0,
                        'Bore %s %.3f%s' % (t, h['dia'], suffix))
        # pockets too narrow for a pocket toolpath to enter: a contour along the
        # wall, entering through the pocket's entry hole
        for f in narrow:
            params = dict(CONTOUR, doMultipleDepths=multi)
            params.update(top_at(0))
            params.update(at('bottom', f['face']))
            e = side_entries.get(f['face'].tempId)
            ops['main'].append(b.op(setup, 'contour2d', tk, 'Narrow pocket' + suffix, params, _both(
                _sel_floor_outlines([f['face']]),
                _sel_faces('entryPositions', [e['point']] if e and e['point'] else []))))
        if windows:
            for w in windows:
                params = dict(CONTOUR, doMultipleDepths=multi)
                params.update(top_at(0) if depth_of(w['z']) < 1e-6 else at('top', w['face']))
                params.update(through_part('bottom'))
                ops['main'].append(b.op(setup, 'contour2d', tk, 'Window' + suffix, params, _both(
                    _sel_chains([w['loop']]),
                    _sel_faces('entryPositions', [p for p in [window_entry(w)] if p]))))
        # top setup: the outline between the tabs, down through the tab band
        if top:
            params = dict(CONTOUR, doMultipleDepths=multi)
            params.update(_top_below_part_top(a['ztop'] - band_top, m))
            params.update(_bottom_below_part_top(a['ztop'] - chain_bot, m))
            hi, set_hi = _at_tab('top', tab_top)
            lo, set_lo = _at_tab('bottom', tab_bottom, -tab['overlap'])
            params.update(hi, **lo)
            ops['main'].append(b.op(setup, 'contour2d', tk, 'Outline between tabs' + suffix,
                                    params, _both(_sel_layout_chains(lay['corner'] if lay else [],
                                                                     True), set_hi, set_lo)))

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
            bottomHeight_mode="'from contour'", **top_at(0)),
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
            params = dict(BORE, stepover='0.2mm', numberOfStepovers='2', **from_hole_top())
            params.update({'bottomHeight_offset': '-0.5mm' if hs[0]['through'] else '0mm'})
            ops['main'].append(b.op(setup, 'bore', em, 'Bore %.2f' % hs[0]['dia'], params,
                                    _sel_faces('circularFaces', [x['face'] for x in hs])))

    def blind_tip_warning(desc, hs):
        if b.dry:
            return
        angle = b.tools.tip_angle(desc)
        # a hole whose modelled point matches the drill's is cut as modelled
        hs = [h for h in hs if h['point_angle'] is None or abs(h['point_angle'] - angle) > 0.5]
        if not hs:
            return
        tip = b.tools.tip_length(desc)
        name = desc.split('*')[0]
        model = 'Model the point in the design (in the Hole command, a %g° drill point) so '                 'the design shows what is cut' % round(angle, 1)
        plain = [h for h in hs if h['point_angle'] is None]
        if plain:
            where = ', '.join('(%g, %g)' % (h['x'], h['y']) for h in plain)
            warnings.append('the %d blind %s mm hole%s drilled by the %s mm drill %s no modelled '
                            "point: drilled full diameter to the bottom, the drill's %g° point "
                            "goes %.2f mm deeper than the hole's bottom and leaves a cone there. "
                            '%s; at x, y = %s mm'
                            % (len(plain), name, '' if len(plain) == 1 else 's', name,
                               'has' if len(plain) == 1 else 'have', round(angle, 1), tip,
                               model, where))
        for h in hs:
            if h['point_angle'] is None:
                continue
            # how far the modelled point reaches below the hole's full diameter
            modelled = h['dia'] / 2 / math.tan(math.radians(h['point_angle'] / 2))
            if modelled < tip:
                what = 'goes %.2f mm deeper than the modelled point' % (tip - modelled)
            else:
                what = 'stops %.2f mm short of the modelled tip and leaves material there' % (
                    modelled - tip)
            warnings.append("the blind %s mm hole at x, y = (%g, %g) mm has a %g° point modelled, "
                            "but the %s mm drill's point is %g°: it %s. %s"
                            % (name, h['x'], h['y'], round(h['point_angle'], 1), name,
                               round(angle, 1), what, model))
        thin = [h['depth'] for h in hs if thickness - h['depth'] < tip + 0.2]
        if thin:
            warnings.append("a blind %s mm hole's drill point comes within 0.2 mm of the part's "
                            'other face, or through it, under a hole %g mm deep' % (name, max(thin)))

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
            if not sub:
                continue
            params = dict(DRILL, **_drill_ends(through), **from_hole_top())
            if not through:
                # full diameter to the bottom: the drill's point goes below it
                params['drillTipThroughBottom'] = 'true'
                blind_tip_warning(desc, sub)
            ops['main'].append(b.op(
                setup, 'drill', desc,
                'Drill ' + desc.split('*')[0] + ('' if through else ' (blind)'),
                params, _sel_faces('holeFaces', [h['face'] for h in sub])))

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
                    _thread_params(t, through, max(0.0, high[0])),
                    _sel_faces('circularFaces', [h['face'] for h in hs])))

    # the finishing face, after every cut that leaves burrs on the face
    if facing:
        ops['main'].append(face_op('finish', 'false', ' (finish)', 0))

    # deburr the outer face's edges
    if face is not None and plan['chamfer']:
        ops['main'].append(b.op(setup, 'chamfer2d', CHAMFER, 'Deburr', dict(CHAMFER_OP),
                                _sel_face_contours([face])))

    # bottom side: the tab cut, last and in its own NC program
    if not top:
        params = dict(CONTOUR, doMultipleDepths='true', maximumStepdown='0.2mm')
        params.update(_top_below_part_top(band_bot - a['zbot'], m))
        params.update(_bottom_below_part_top(band_top + tab['overlap'] - a['zbot'], m))
        hi, set_hi = _at_tab('top', tab_bottom)
        lo, set_lo = _at_tab('bottom', tab_top, -tab['overlap'])
        params.update(hi, **lo)
        ops['tabs'].append(b.op(setup, 'contour2d', (ENDMILL, 'rough'), 'Cut tabs', params,
                                _both(_sel_layout_chains(lay['tabs'] if lay else [], False),
                                      set_hi, set_lo)))
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
    for occ in [o for o in root.occurrences if o.component.name.startswith(VISE_PREFIX)
                or o.component.name in (TABS_COMPONENT, FACES_COMPONENT)]:
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
    if 'stock' not in args:
        raise RuntimeError('args["stock"] is required: {"x":..,"y":..,"z":..} in mm')
    cam = _cam()
    design = _design()
    # before reading the body: deleting an earlier build's sketches and vises
    # recomputes any feature added after them, which would leave stale faces
    _delete_previous(cam, design)
    body = _find_body(args)
    a = analyze_body(body)
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
    plan['tab_faces'] = _add_tabs(design, plan['layout'], plan['tab'])
    plan['face_patches'] = _add_face_patches(design, plan['layout'], a)
    x_axis = _axis_line(design)
    top = _setup(cam, 'Top ' + SETUP_TAG, body, machine, stock, True, x_axis, vises[0], warnings)
    top_ops = _side_ops(b, top, a, 'top', plan, warnings)
    bottom = _setup(cam, 'Bottom ' + SETUP_TAG, body, machine, stock, False, x_axis, vises[1], warnings)
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

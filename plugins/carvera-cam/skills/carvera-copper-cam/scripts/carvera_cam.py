"""Toolpaths for copper parts on SchusterLab's Makera Carvera, in Autodesk Fusion.

This module runs inside Fusion's Python interpreter (through the Fusion MCP
`script` feature or a Fusion script). It encodes the lab's CAM conventions,
learned from the parts in the project 2026-01-06_smpm-sample-package-6port:

- the Carvera 3-axis machine and the Carvera post, the latest versions in the
  Carvera Community's profiles repository on GitHub;
- the skill's tools, Makera's end mills, thread mills and chamfering bit and
  McMaster-Carr's drills, in assets/carvera-tools.json (tool_library.py builds
  it), each with the feed preset "Copper" (aluminum runs the same
  program at 200% override);
- a top setup and a flipped bottom setup, the part held to the stock by four
  tabs that a separate "_tabs" program cuts last.

Entry points, each taking one dict of arguments:

    analyze(args)   recognize the part's features and print them as JSON
    build(args)     create the setups, operations and NC programs; with
                    {"recipes": true}, leave the features that have a recipe
                    (references/recipes/) to it and list them
    generate(args)  generate the toolpaths and report errors and warnings

and the primitives the recipes compose (see "recipe primitives" below):
recipes, walls, pass_path, coverage, tools, add_operation, add_contour and
renumber_tools.

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
PRESET_NAMES = ('Copper',)
SETUP_TAG = '(carvera-cam)'

DRILL_3175 = '3.175*7.4mm Drill (1/8")'
DRILL_D = 3.175
# a hole is drilled by a library drill from DRILL_UNDER mm under its diameter to
# DRILL_OVER mm over it: a 3.2 mm clearance hole takes T1, as the lab drills kiutra-beam's
DRILL_UNDER = 0.03
DRILL_OVER = 0.05
ENDMILL = 'Spiral O Metal 3.175*12mm'
ENDMILL_D = 3.175
# the end mill enters copper only down a drilled hole at least half its width,
# the drill's cone included (rule TH4)
ENTRY_CONE_D = ENDMILL_D / 2
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
    # each body with its path: its name in the root component, else '<occurrence path>/<name>'
    bodies = []
    for b in design.rootComponent.bRepBodies:
        bodies.append((b.name, b))
    for occ in design.rootComponent.allOccurrences:
        if occ.component.name.startswith(VISE_PREFIX) or \
                occ.component.name in (TABS_COMPONENT, FACES_COMPONENT, PART_COMPONENT):
            continue  # the vise, tabs, face patches or turned part an earlier build added
        for b in occ.bRepBodies:
            bodies.append((occ.fullPathName.replace('+', '/') + '/' + b.name, b))
    # a body an earlier build hid behind its turned copy still counts
    bodies = [(p, b) for p, b in bodies if b.isSolid and
              (b.isVisible or b.attributes.itemByName(*HIDDEN_ATTR) is not None)]
    name = args.get('body')
    if name:
        # by path first, so that a root body named like the bodies of inserted parts is found
        bodies = [(p, b) for p, b in bodies if p == name] or \
            [(p, b) for p, b in bodies if b.name == name]
    if len(bodies) != 1:
        raise RuntimeError('expected one visible solid body%s, found %s; pass args["body"]'
                           % (' named %r' % name if name else '', [p for p, _ in bodies]))
    return bodies[0][1]


# --------------------------------------------------------------- analysis

def _thread_faces(design):
    """Map face tempId -> thread name (e.g. 'M2') for tapped holes and thread features.

    The lab models tapped holes with the Hole command's Tapped type (not modeled),
    which leaves a plain hole at the minor diameter and stores the thread on the
    hole feature. A pattern or mirror of a tapped hole copies only its faces, so a
    cylinder made by a pattern takes the thread of a tapped hole of the same radius.
    """
    out = {}
    comps = [design.rootComponent] + [o.component for o in design.rootComponent.allOccurrences]
    for comp in comps:
        radii = []  # (radius in cm, thread) of this component's tapped holes
        for hf in comp.features.holeFeatures:
            try:
                if hf.holeTapType != adsk.fusion.HoleTapTypes.TappedHoleTapType:
                    continue
                name = hf.tappedHoleInfo.threadDesignation.split('x')[0]
                for f in hf.sideFaces:
                    out[f.tempId] = name
                    if f.geometry.surfaceType == adsk.core.SurfaceTypes.CylinderSurfaceType:
                        radii.append((f.geometry.radius, name))
            except Exception:
                pass
        if radii:
            fs = comp.features
            for coll in (fs.rectangularPatternFeatures, fs.circularPatternFeatures,
                         fs.pathPatternFeatures, fs.mirrorFeatures):
                for pf in coll:
                    try:
                        faces = list(pf.faces)
                    except Exception:
                        continue
                    for f in faces:
                        if f.geometry.surfaceType != adsk.core.SurfaceTypes.CylinderSurfaceType:
                            continue
                        for r, name in radii:
                            if abs(f.geometry.radius - r) < 1e-5:
                                out.setdefault(f.tempId, name)
                                break
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


def _hole_ends(h, top):
    """The hole's rim where the setup's tool enters it, as the loop of the flat
    face round it, and, for a blind hole, the face at its bottom."""
    face = h['face']
    z_in, z_far = (h['z1'], h['z0']) if top else (h['z0'], h['z1'])
    rim = bottom = None
    for e in face.edges:
        if not isinstance(e.geometry, adsk.core.Circle3D):
            continue
        other = _other_face(e, face)
        if other is None:
            continue
        z = mm(e.geometry.center.z)
        if abs(z - z_in) < 1e-3:
            rim = next((lp for lp in other.loops
                        if any(c.edge.tempId == e.tempId for c in lp.coEdges)), rim)
        elif abs(z - z_far) < 1e-3 and not h['through']:
            bottom = other
    return rim, bottom


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


def _tab_points(a, width):
    """Points (x, y in cm) along the tabs the layout will place, `width` cm wide."""
    faces = [f for f in (a['top_face'], a['bottom_face']) if f is not None]
    if not faces:
        return []
    pieces = _clockwise(_loop_pieces(_outline_loop(faces)[0]))
    tabs = _place_tabs(pieces, width) or {}
    return [_point_on(pieces[i], c + width * (k / 4 - 0.5))
            for i, cs in tabs.items() for c in cs for k in range(5)]


def _tab_height(a, band, overlap, requested=None, tab_points=None, lowest=False):
    """Model Z (mm) of the tab band's centre: the mid-plane unless a side opening
    is there, beside a tab. An opening counts when it comes within an end mill's
    width of a tab: the tab is the strip of stock that wide outside the outline.
    With tab_points None, every opening counts. With lowest (one side, where the
    tab cut comes from the top too), the band sits on the part's bottom, or the
    lowest height above it clear of the openings."""
    ztop, zbot = a['ztop'], a['zbot']
    reach = ENDMILL_D / 10 + 0.05
    openings = []
    for f in a['floors']:
        if not f.get('side_open'):
            continue
        if tab_points is not None:
            bb = [lp for lp in f['face'].loops if lp.isOuter][0].boundingBox
            if not any(bb.minPoint.x - reach <= p.x <= bb.maxPoint.x + reach and
                       bb.minPoint.y - reach <= p.y <= bb.maxPoint.y + reach
                       for p in tab_points):
                continue
        openings.append((f['z'], ztop) if f['side'] == 'top' else (zbot, f['z']))

    def clear(zc):
        lo, hi = zc - band / 2, zc + band / 2
        return all(hi <= o_lo + 1e-6 or lo >= o_hi - 1e-6 for o_lo, o_hi in openings)

    if requested is not None:
        return requested, clear(requested), openings
    if lowest:
        lo, hi = zbot + band / 2, ztop - band / 2 - overlap
        for k in range(int((hi - lo) / 0.05) + 2):
            zc = lo + k * 0.05
            if zc <= hi + 1e-6 and clear(zc):
                return round(zc, 3), True, openings
        return round(lo, 3), False, openings
    mid = (ztop + zbot) / 2
    lo, hi = zbot + band / 2 + overlap, ztop - band / 2 - overlap
    for k in range(int((hi - lo) / 0.05) + 2):
        for zc in (mid + k * 0.05, mid - k * 0.05):
            if lo - 1e-6 <= zc <= hi + 1e-6 and clear(zc):
                return round(zc, 3), True, openings
    return mid, False, openings


def analyze_body(body, design=None, threads=None):
    design = design or _design()
    if threads is None:
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
                                    'depth': mm(z - zbot), 'corner_radius': rmin,
                                    'walls': {w.tempId for w in walls}})

    # and windows seen from below: the inner loops of a floor facing down whose walls
    # run up to the top (a part turned over has these), cut from the top face down
    for f in [fl['face'] for fl in floors if fl['side'] == 'bottom']:
        for loop in f.loops:
            if loop.isOuter:
                continue
            walls = _walls_of_loop(loop, f)
            if any(w is None or w.tempId in hole_ids for w in walls):
                continue
            ids = {w.tempId for w in walls}
            if max(_zrange(w)[1] for w in walls) < ztop - EPS or \
                    any(ids & wd['walls'] for wd in windows):
                continue
            rmin = None
            for w in walls:
                if _is_vertical_cylinder(w) and _is_concave_cylinder(w):
                    r = mm(w.geometry.radius)
                    rmin = r if rmin is None else min(rmin, r)
            windows.append({'face': f, 'loop': loop, 'z': mm(ztop), 'depth': mm(ztop - zbot),
                            'corner_radius': rmin, 'walls': ids})

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
    return {k: v for k, v in feat.items() if k not in ('face', 'loop', 'body', 'opening', 'walls')}


def _feature_counts(a):
    """Features machined from each face: pocket floors and blind holes. Through
    holes and windows are cut from either side, so they count for neither."""
    n = {'top': 0, 'bottom': 0}
    for f in a['floors']:
        if f['role'] == 'pocket':
            n[f['side']] += 1
    for h in a['holes']:
        if not h['through'] and h['side'] in n:
            n[h['side']] += 1
    return n


def _flip_matrix(body):
    """Half a turn about the X axis through the body's centre: the part turned
    over in its own bounding box, so the bottom face becomes the top."""
    bb = body.boundingBox
    c = adsk.core.Point3D.create(*[(p + q) / 2 for p, q in
                                   zip(bb.minPoint.asArray(), bb.maxPoint.asArray())])
    m = adsk.core.Matrix3D.create()
    m.setToRotation(math.pi, adsk.core.Vector3D.create(1, 0, 0), c)
    return m


def _quarter_matrix(body):
    """A quarter turn about the Z axis through the body's centre: the part's X
    and Y swap, so that the side to grip lies across the vise's jaws."""
    bb = body.boundingBox
    c = adsk.core.Point3D.create(*[(p + q) / 2 for p, q in
                                   zip(bb.minPoint.asArray(), bb.maxPoint.asArray())])
    m = adsk.core.Matrix3D.create()
    m.setToRotation(math.pi / 2, adsk.core.Vector3D.create(0, 0, 1), c)
    return m


def _turned_threads(body, turned, m, threads):
    """The thread map of `turned` (body moved by m): each threaded face of body
    matched to the turned body's face on the same cylinder."""
    out = {}
    cyls = [f for f in turned.faces if isinstance(f.geometry, adsk.core.Cylinder)]
    for f in body.faces:
        name = threads.get(f.tempId)
        if name is None or not isinstance(f.geometry, adsk.core.Cylinder):
            continue
        g = f.geometry
        o = g.origin.copy()
        o.transformBy(m)
        ends = []
        for z in _zrange(f):
            p = adsk.core.Point3D.create(g.origin.x, g.origin.y, z)
            p.transformBy(m)
            ends.append(p.z)
        z0, z1 = min(ends), max(ends)
        for t in cyls:
            tz0, tz1 = _zrange(t)
            if abs(t.geometry.radius - g.radius) < EPS and                     math.hypot(t.geometry.origin.x - o.x, t.geometry.origin.y - o.y) < EPS and                     abs(tz0 - z0) < EPS and abs(tz1 - z1) < EPS:
                out[t.tempId] = name
    return out


def _machined_body(args, design, make, quarter=False):
    """The body the setups machine, its analysis, whether it is the design's body
    turned over, and the feature counts per face.

    The face with more features is machined first, as the top: when the design's
    bottom face has more (see _feature_counts), or args['flip'] is true, the part
    is turned over. With quarter, it is also given a quarter turn about Z (see
    _quarter_matrix). With make, the moved copy is added to the design in
    PART_COMPONENT and the design's body hidden behind it (the next build shows it
    again); otherwise the copy is a temporary body, for analyze.
    """
    body = _find_body(args)
    a = analyze_body(body, design)
    n = _feature_counts(a)
    flip = args.get('flip')
    if flip is None:
        flip = n['bottom'] > n['top']
    if not flip and not quarter:
        return body, a, False, n
    m = _flip_matrix(body) if flip else adsk.core.Matrix3D.create()
    if quarter:
        m.transformBy(_quarter_matrix(body))
    tb = adsk.fusion.TemporaryBRepManager.get()
    temp = tb.copy(body)
    tb.transform(temp, m)
    threads = _thread_faces(design)
    if not make:
        return temp, analyze_body(temp, design, _turned_threads(body, temp, m, threads)), flip, n
    occ = design.rootComponent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = PART_COMPONENT
    if design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
        bf = comp.features.baseFeatures.add()
        bf.startEdit()
        comp.bRepBodies.add(temp, bf)
        bf.finishEdit()
    else:
        comp.bRepBodies.add(temp)
    turned = occ.bRepBodies.item(0)  # in the root's context, as the setups need it
    turned.name = body.name + ' (%s)' % ' and '.join(
        w for w, on in (('turned over', flip), ('quarter-turned', quarter)) if on)
    body.attributes.add(HIDDEN_ATTR[0], HIDDEN_ATTR[1], '1')
    body.isLightBulbOn = False
    return turned, analyze_body(turned, design, _turned_threads(body, turned, m, threads)), flip, n


def analyze(args=None):
    """Print the recognized features of the part as JSON.

    When the part is to be turned over (see _machined_body), the features are
    those of the turned part: its top is the design's bottom face, and y and z
    are mirrored about the part's centre.
    """
    args = args or {}
    body, a, flipped, counts = _machined_body(args, _design(), False)
    groups = {}
    for h in a['holes']:
        key = (h['dia'], h['side'], h['through'], round(h['depth'], 2), h['thread'])
        g = groups.setdefault(key, dict(_strip(h), count=0, at=[]))
        g['count'] += 1
        g['at'].append([h['x'], h['y']])
        g['tap_candidate'] = h['thread'] or _tap_candidate(h['dia'])
    out = {
        'body': _find_body(args).name,
        'turned_over': flipped,
        'features_per_face': counts,
        'size_mm': a['size'],
        'ztop': a['ztop'], 'zbot': a['zbot'],
        'holes': list(groups.values()),
        'floors': [_strip(f) for f in a['floors']],
        'windows': [_strip(w) for w in a['windows']],
        'tab_z': _tab_height(a, min(1.0, 0.4 * a['size'][2]), min(0.5, 0.2 * a['size'][2]),
                             tab_points=_tab_points(a, 0.5))[0],
        # with one side: the tab band on the part's bottom (see _strategy)
        'tab_z_one_sided': _tab_height(a, min(1.0, 0.4 * a['size'][2]),
                                       min(0.5, 0.2 * a['size'][2]),
                                       tab_points=_tab_points(a, 0.5), lowest=True)[0],
        'has_top_face': a['top_face'] is not None,
        'has_bottom_face': a['bottom_face'] is not None,
    }
    print(json.dumps(out, indent=1))
    return out


# ------------------------------------------------------- recipe primitives
# Steps a recipe in references/recipes/ composes for a feature build leaves out.
# Each takes JSON-like args and prints its result as JSON. A floor is named by
# its index in analyze's "floors", which build's "recipes" use too.

PATH_ATTR = ('carvera-cam', 'path')
RECIPES_ATTR = ('carvera-cam', 'recipes')  # on the design: build's "recipes", as JSON  # on each curve of a sketched pass: '<path id>:<order>'


def _built(args):
    """The design, the body the setups machine (the turned copy, if build made
    one) and its analysis."""
    design = _design()
    occ = next((o for o in design.rootComponent.occurrences
                if o.component.name == PART_COMPONENT), None)
    body = occ.bRepBodies.item(0) if occ else _find_body(args)
    return design, body, analyze_body(body, design)


def _xy(p):
    return [round(mm(p.x), 3), round(mm(p.y), 3)]


def recipes(args=None):
    """The features the last build left to a recipe, as its output listed them."""
    attr = _design().attributes.itemByName(*RECIPES_ATTR)
    out = json.loads(attr.value) if attr is not None else []
    print(json.dumps(out, indent=1))
    return out


def walls(args):
    """The walls of a pocket open to the side, `args['floor']`: each run of
    walls rising from the floor between its openings, in order with the floor
    on the left, and how far the floor reaches from its walls."""
    design, body, a = _built(args)
    f = a['floors'][args['floor']]
    runs, reach = _open_walls(f['face'])
    out = {'floor': _strip(f), 'reach_mm': round(mm(reach), 3), 'runs': []}
    for run in runs:
        ws = []
        for ce in run:
            e = ce.edge
            ok, p0, p1 = e.evaluator.getEndPoints()
            if ce.isOpposedToEdge:
                p0, p1 = p1, p0
            ws.append({'type': type(e.geometry).__name__, 'from': _xy(p0), 'to': _xy(p1),
                       'length_mm': round(mm(e.length), 3)})
        out['runs'].append(ws)
    print(json.dumps(out, indent=1))
    return out


def pass_path(args):
    """Sketch one contour pass along a run of walls (`args['floor']`, `run`,
    default 0) as the tool centre's path, `offset_mm` off the walls, with a
    line at each end out to the outline cut's path. Returns the path's id for
    add_contour, or why there is none."""
    design, body, a = _built(args)
    f = a['floors'][args['floor']]
    runs, _ = _open_walls(f['face'])
    run = runs[args.get('run', 0)]
    sk = design.rootComponent.sketches.itemByName(LAYOUT_SKETCH)
    if sk is None:
        raise RuntimeError('no "%s" sketch: run build first' % LAYOUT_SKETCH)
    faces = [g for g in (a['top_face'], a['bottom_face']) if g is not None]
    ring = _loop_ring(_outline_loop(faces)[0])
    walls = _draw_walls(sk, run)
    path = _pass_path(sk, run, walls, args['offset_mm'] / 10, ring, f['face'])
    for c in walls:
        if c.isValid:
            c.deleteMe()
    if path is None:
        out = {'ok': False, 'why': "an end of the pass does not reach the outline cut's path, "
                                   "or the walls offset that far leave no path"}
    else:
        pid = '%d-%d' % (args['floor'], len(design.findAttributes(*PATH_ATTR)))
        for k, c in enumerate(path):
            c.attributes.add(PATH_ATTR[0], PATH_ATTR[1], '%s:%d' % (pid, k))
        first, last = path[0], path[-1]
        ends = [p for c in (first, last) for p in (c.startSketchPoint, c.endSketchPoint)]
        free = [p for p in ends if sum(1 for c in path for q in (c.startSketchPoint, c.endSketchPoint)
                                       if q.geometry.distanceTo(p.geometry) < 1e-5) == 1]
        out = {'ok': True, 'path': pid, 'ends': [_xy(p.geometry) for p in free],
               'length_mm': round(mm(sum(c.length for c in path)), 3)}
    print(json.dumps(out, indent=1))
    return out


def _path_curves(design, ids):
    parts = {}
    for at_ in design.findAttributes(*PATH_ATTR):
        pid, k = at_.value.rsplit(':', 1)
        parts.setdefault(pid, []).append((int(k), at_.parent))
    return [[c for _, c in sorted(parts[pid], key=lambda kc: kc[0])] for pid in ids]


def coverage(args):
    """How much of floor `args['floor']` the 3.175 mm end mill misses when it
    follows the sketched paths `paths` (pass_path ids; by default every pass
    sketched for that floor, by build or pass_path): the floor sampled every
    0.1 mm, each point cut when it lies within the tool's radius of a path.
    Returns the uncut area, split into what lies in inside corners (within a
    tool radius of the walls, which a corners pass cuts) and what lies
    elsewhere (a gap the passes leave), and for the gaps, or else the corners,
    their extent and the uncut point farthest from any path."""
    design, body, a = _built(args)
    f = a['floors'][args['floor']]
    rings = [_loop_ring(lp) for lp in f['face'].loops]
    outer = [r for r, lp in zip(rings, f['face'].loops) if lp.isOuter][0]
    inner = [r for r, lp in zip(rings, f['face'].loops) if not lp.isOuter]
    ids = args.get('paths')
    if not ids:  # every pass sketched for this floor, by build or pass_path
        ids = sorted({at_.value.rsplit(':', 1)[0] for at_ in design.findAttributes(*PATH_ATTR)
                      if at_.value.split(':')[0].replace('build-', '').split('-')[0]
                      == str(args['floor'])})
    segs = []
    for curves in _path_curves(design, ids):
        for c in curves:
            ev = c.geometry.evaluator
            ok, t0, t1 = ev.getParameterExtents()
            n = max(2, int(c.length / 0.01))
            q = [ev.getPointAtParameter(t0 + (t1 - t0) * k / n)[1] for k in range(n + 1)]
            segs += [(p0.x, p0.y, p1.x, p1.y) for p0, p1 in zip(q, q[1:])]
    if not segs:
        raise RuntimeError('coverage: no sketched paths for floor %d' % args['floor'])
    # the walls rising from the floor: an uncut point within a tool radius of
    # one lies in an inside corner the end mill's radius cannot reach
    walls = [q for e in _rising_edges(f['face']) for q in _edge_points(e)]
    xs, ys = [x for x, _ in outer], [y for _, y in outer]
    step, r = 0.01, TOOL_R + 0.002  # 0.02 mm for the sampling
    uncut, worst = [], (0.0, None)
    y = min(ys) + step / 2
    while y < max(ys):
        x = min(xs) + step / 2
        while x < max(xs):
            if _in_profile((outer, inner, None), x, y):
                d = min((math.hypot(x - ax - u * (bx - ax), y - ay - u * (by - ay))
                         for ax, ay, bx, by in segs
                         for u in [0.0 if (bx - ax) ** 2 + (by - ay) ** 2 < 1e-12 else
                                   max(0.0, min(1.0, ((x - ax) * (bx - ax) + (y - ay) * (by - ay))
                                                 / ((bx - ax) ** 2 + (by - ay) ** 2)))]),
                        default=1e9)
                if d > r:
                    uncut.append((x, y))
                    if d > worst[0]:
                        worst = (d, (x, y))
            x += step
        y += step
    corner = [(x, y) for x, y in uncut
              if min((math.hypot(x - u, y - v) for u, v in walls), default=1e9) < TOOL_R]
    in_corner = set(corner)
    gaps = [p for p in uncut if p not in in_corner]
    out = {'floor': args['floor'], 'uncut_area_mm2': round(len(uncut) * step * step * 100, 2),
           'in_corners_mm2': round(len(corner) * step * step * 100, 2),
           'elsewhere_mm2': round(len(gaps) * step * step * 100, 2)}
    uncut = gaps or uncut
    if uncut:
        out['uncut_extent_mm'] = [round(mm(min(x for x, _ in uncut)), 2), round(mm(min(y for _, y in uncut)), 2),
                                  round(mm(max(x for x, _ in uncut)), 2), round(mm(max(y for _, y in uncut)), 2)]
        out['farthest_uncut'] = {'at_mm': [round(mm(v), 2) for v in worst[1]],
                                 'from_path_mm': round(mm(worst[0]), 2)}
    print(json.dumps(out, indent=1))
    return out


def _setup_named(cam, name):
    return next(x for x in cam.setups if x.name == name)


def _all_ops(cam):
    return [o for st in cam.setups for o in st.allOperations]


def _tool_info(t):
    p = t.parameters
    v = lambda n: p.itemByName(n).value.value if p.itemByName(n) is not None else None
    return {'tool': v('tool_description'), 'type': v('tool_type'),
            'diameter_mm': round(mm(v('tool_diameter') or 0), 3),
            'flute_mm': round(mm(v('tool_fluteLength') or 0), 3)}


def tools(args=None):
    """The tools the part's operations use, by slot, and the skill's library."""
    cam = adsk.cam.CAM.cast(_app().activeDocument.products.itemByProductType('CAMProductType'))
    used = {}
    for o in _all_ops(cam):
        if o.tool is None:
            continue
        n = int(o.tool.parameters.itemByName('tool_number').value.value)
        used.setdefault(n, dict(_tool_info(o.tool), T=n, first_used_by=o.name))
    lib = Tools()
    out = {'in_use': [used[n] for n in sorted(used)],
           'library': [_tool_info(t) for t in lib.by_desc.values()]}
    print(json.dumps(out, indent=1))
    return out


def _tool_for(cam, desc, number=None):
    """(tool, preset) for the tool described `desc` (or the one tool whose
    description starts with it): the part's own copy when an operation uses it
    already, in slot `number` if given, else the library's, numbered `number`,
    or after the others (renumber_tools puts it in its slot)."""
    descr = lambda t: t.parameters.itemByName('tool_description').value.value
    num = lambda t: int(t.parameters.itemByName('tool_number').value.value)
    used = [o.tool for o in _all_ops(cam) if o.tool is not None]
    lib = None
    if not any(descr(t) == desc for t in used):
        lib = Tools()
        if desc not in lib.by_desc:
            hits = [d for d in lib.by_desc if d.startswith(desc)]
            if len(hits) != 1:
                raise RuntimeError('tool %r is not in the library (%s)' % (desc, TOOLS_FILE))
            desc = hits[0]
    for o in _all_ops(cam):
        if o.tool is not None and descr(o.tool) == desc and (number is None or num(o.tool) == number):
            return o.tool, o.toolPreset
    taken = {num(t): descr(t) for t in used}
    if number is not None and number in taken:
        raise RuntimeError('T%d already holds %s' % (number, taken[number]))
    src = (lib or Tools()).by_desc[desc]
    tool = adsk.cam.Tool.createFromJson(src.toJson())
    tool.parameters.itemByName('tool_number').expression = str(
        number if number is not None else max(list(taken) + [0]) + 1)
    preset = None
    for name in PRESET_NAMES:
        preset = next((tool.presets.item(i) for i in range(tool.presets.count)
                       if tool.presets.item(i).name == name), None)
        if preset:
            break
    return tool, preset


LAYOUT_ATTR = ('carvera-cam', 'layout')  # on the design: what build made, for the recipes


def _p3(p):
    return [p.x, p.y, p.z]


def _save_layout(design, a, plan, name, setups, threads):
    """What build made for the operations to use, kept in the design as entity
    tokens and as indices into the analysis (floors, holes, windows), so that
    features and add_operation find it again in later calls."""
    lay = plan['layout']
    tok = lambda e: e.entityToken
    floor_of = {f['face'].tempId: i for i, f in enumerate(a['floors'])}
    hole_of = {h['face'].tempId: i for i, h in enumerate(a['holes'])}

    def chain(ch):
        return {'curves': [tok(c) for c in ch['curves']], 'start': _p3(ch['start']),
                'dir': _p3(ch['dir'])}
    regions, index = [], {}
    for key, r in lay['shelves'].items():
        if id(r) not in index:
            index[id(r)] = len(regions)
            regions.append({'curves': [tok(c) for c in r['curves']], 'clockwise': r['clockwise'],
                            'side': r['side'], 'z_mm': r['z'], 'floors': []})
        regions[index[id(r)]]['floors'].append(floor_of[key])
    out = {
        'name': name, 'setups': setups,
        'strategy': {k: plan[k] for k in ('sides', 'outline', 'ends')},
        'margin_top_mm': plan['margin_top'], 'margin_bottom_mm': plan['margin_bottom'],
        'taps': plan['taps'], 'face': plan['face'], 'chamfer': plan['chamfer'],
        'tab': dict(plan['tab']) if plan['outline'] else None,
        'tab_faces': {k: tok(v) for k, v in (plan.get('tab_faces') or {}).items() if v is not None},
        'patches': {k: tok(v) for k, v in (plan.get('face_patches') or {}).items()},
        'entry': tok(lay['entry']),
        'corner': [dict(chain(c), entry=tok(c['entry'])) for c in lay['corner']],
        'tabs': [chain(c) for c in lay['tabs']],
        'ends': [chain(c) for c in lay['ends']],
        'pocket_entries': {str(floor_of[k]): {'point': tok(e['point']), 'drill': e['drill'],
                                              'depth_mm': e['depth']}
                           for k, e in lay['pocket_entries'].items() if k in floor_of},
        'narrow': sorted(floor_of[k] for k in lay['narrow'] if k in floor_of),
        'regions': regions,
        'window_entries': [None if e is None else {'point': tok(e['point']), 'pocket': e['pocket']}
                           for e in lay['window_entries']],
        'window_tops': [None if f is None else floor_of[f.tempId] for f in lay['window_tops']],
        'slots': [{'floor': floor_of[sl['floor']['face'].tempId], 'side': sl['side'],
                   'tool': sl['tool'][0], 'width_mm': sl['tool'][1], 'line': tok(sl['line']),
                   'end': tok(sl['end']), 'fed': [floor_of[g['face'].tempId] for g in sl['fed']]}
                  for sl in lay['slots']],
        'hole_centres': {str(hole_of[k]): tok(pt) for k, pt in lay['hole_centres'].items()
                         if k in hole_of},
        'threads': {str(i): t for i, t in threads.items()},
    }
    attr = design.attributes.itemByName(*LAYOUT_ATTR)
    if attr is not None:
        attr.deleteMe()
    design.attributes.add(LAYOUT_ATTR[0], LAYOUT_ATTR[1], json.dumps(out))
    return out


def _layout(design):
    attr = design.attributes.itemByName(*LAYOUT_ATTR)
    if attr is None:
        raise RuntimeError('no layout in the design: run build with "recipes": "all" first')
    return json.loads(attr.value)


def _entity(design, token):
    hits = design.findEntityByToken(token)
    if not hits:
        raise RuntimeError('something build made is gone from the design; build again')
    return hits[0]


def _hole_below(a, h):
    """The widest narrower hole that goes on down from blind hole h's floor, on its
    axis (the hole under a counterbore), or None."""
    if h['through']:
        return None
    top = h['side'] != 'bottom'
    floor = h['z0'] if top else h['z1']
    near = lambda u, v: abs(u - v) < 1e-3
    below = [g for g in a['holes'] if g['dia'] < h['dia'] - 1e-3
             and near(g['x'], h['x']) and near(g['y'], h['y'])
             and near(g['z1'] if top else g['z0'], floor)]
    return max(below, key=lambda g: g['dia']) if below else None


def features(args=None):
    """Everything a recipe needs to machine the part after build with "recipes":
    "all": the machining strategy, the setups and the stock round the part, the
    tab band, and every floor, hole and window with the entry points, chains and
    regions build sketched for them, each named as add_operation takes it."""
    args = args or {}
    design, body, a = _built(args)
    L = _layout(design)
    floor_of = {f['face'].tempId: i for i, f in enumerate(a['floors'])}
    faces = {getattr(a['top_face'], 'tempId', None): 'face', getattr(a['bottom_face'], 'tempId', None): 'face'}
    thickness = a['ztop'] - a['zbot']
    region_of = {i: k for k, r in enumerate(L['regions']) for i in r['floors']}
    slot_of = {sl['floor']: k for k, sl in enumerate(L['slots'])}
    floors = []
    for i, f in enumerate(a['floors']):
        e = L['pocket_entries'].get(str(i))
        floors.append(dict(_strip(f), floor=i, **{
            'entry': None if e is None else {'point': 'pocket:%d' % i, 'drilled': e['drill'],
                                             'hole_depth_mm': e['depth_mm']},
            'narrow': i in L['narrow'], 'region': region_of.get(i), 'slot': slot_of.get(i),
            'fed_by_slot': next((k for k, sl in enumerate(L['slots']) if i in sl['fed']), None)}))
    holes = []
    for i, h in enumerate(a['holes']):
        rim, bottom = _hole_ends(h, h['side'] != 'bottom')
        rf = rim.face.tempId if rim is not None else None
        g = _hole_below(a, h)
        holes.append({
            'hole': i, 'dia_mm': h['dia'], 'depth_mm': h['depth'], 'z0': h['z0'], 'z1': h['z1'],
            'side': h['side'], 'through': h['through'], 'x': h['x'], 'y': h['y'],
            'thread': L['threads'].get(str(i)) or L['taps'].get('%g' % h['dia']),
            'point_angle': h['point_angle'],
            # where its rim lies: the part's face, a pocket floor, or another face
            'opens_in': None if rf is None else faces.get(rf, floor_of.get(rf, 'other')),
            'has_bottom_face': bottom is not None,
            'hole_below': None if g is None else a['holes'].index(g),
            'centre': 'hole:%d' % i if str(i) in L['hole_centres'] else None})
    windows = []
    for j, w in enumerate(a['windows']):
        e = L['window_entries'][j]
        windows.append({'window': j, 'z': w['z'], 'depth_mm': w['depth'],
                        'corner_radius': w['corner_radius'],
                        'entry': None if e is None else {'point': 'window:%d' % j,
                                                         'pocket_fits': e['pocket']},
                        'starts_at_floor': L['window_tops'][j]})
    lib = Tools()
    out = {
        'name': L['name'], 'setups': L['setups'], 'strategy': L['strategy'],
        'part': {'size_mm': a['size'], 'ztop': a['ztop'], 'zbot': a['zbot'],
                 'thickness_mm': round(thickness, 4), 'has_top_face': a['top_face'] is not None,
                 'has_bottom_face': a['bottom_face'] is not None},
        'stock_above_mm': {'top': L['margin_top_mm'], 'bottom': L['margin_bottom_mm']},
        'face': L['face'], 'chamfer': L['chamfer'], 'taps': L['taps'],
        'tab': None if L['tab'] is None else dict(
            L['tab'], top_z=L['tab']['z'] + L['tab']['band'] / 2,
            bottom_z=L['tab']['z'] - L['tab']['band'] / 2, faces=sorted(L['tab_faces'])),
        'patches': sorted(L['patches']),
        'layout': {
            'entry': 'entry',
            'corner_chains': len(L['corner']), 'tab_chains': len(L['tabs']),
            'end_chains': len(L['ends']),
            'corner_entries': ['corner:%d' % k for k in range(len(L['corner']))],
            'regions': [{'region': k, 'side': r['side'], 'z': r['z_mm'], 'floors': r['floors']}
                        for k, r in enumerate(L['regions'])],
            'slots': [{'slot': k, 'floor': sl['floor'], 'side': sl['side'], 'tool': sl['tool'],
                       'width_mm': sl['width_mm'], 'fed': sl['fed'], 'end': 'slot_end:%d' % k}
                      for k, sl in enumerate(L['slots'])]},
        'floors': floors, 'holes': holes, 'windows': windows,
        'library': {
            'drills': [{'tool': d, 'dia_mm': round(dia, 4), 'flute_mm': round(fl, 3),
                        'point_mm': round(lib.tip_length(d), 4),
                        'point_angle': round(lib.tip_angle(d), 2)} for d, dia, fl in lib.drills()],
            'end_mill': ENDMILL,
            'small_end_mills': [{'tool': d, 'dia_mm': dd, 'flute_mm': fl}
                                for d, dd, fl in SMALL_ENDMILLS],
            'chamfer': CHAMFER,
            'thread_mills': {t: {'tool': v[2], 'dia_mm': round(mm(
                lib.by_desc[v[2]].parameters.itemByName('tool_diameter').value.value), 4)}
                for t, v in THREADS.items() if v[2] in lib.by_desc}},
    }
    print(json.dumps(out, indent=1))
    return out


def _chain(design, c):
    return {'curves': [_entity(design, t) for t in c['curves']],
            'start': adsk.core.Point3D.create(*c['start']),
            'dir': adsk.core.Vector3D.create(*c['dir'])}


def _ref(design, a, L, name):
    """The model face a height is measured from, by name: 'face:<top|bottom>',
    'floor:<i>', 'hole_bottom:<i>', 'window_top:<j>' (the face a window opens
    in), 'patch:<top|bottom>[ finish]' or 'tab:<top|bottom>'."""
    kind, _, rest = name.partition(':')
    if kind == 'face':
        f = a['top_face' if rest == 'top' else 'bottom_face']
        if f is None:
            raise RuntimeError('the part has no flat %s face' % rest)
        return f
    if kind == 'floor':
        return a['floors'][int(rest)]['face']
    if kind == 'hole_bottom':
        h = a['holes'][int(rest)]
        bottom = _hole_ends(h, h['side'] != 'bottom')[1]
        if bottom is None:
            raise RuntimeError('hole %s has no flat bottom' % rest)
        return bottom
    if kind == 'window_top':
        return a['windows'][int(rest)]['face']
    if kind == 'patch':
        return _entity(design, L['patches'][rest])
    if kind == 'tab':
        return _entity(design, L['tab_faces'][rest])
    raise RuntimeError('unknown reference %r' % name)


def _point(design, L, name):
    """A sketched point by name: 'entry', 'corner:<k>' (the entry beside corner
    chain k), 'pocket:<floor>', 'window:<j>', 'hole:<i>' (its centre) or
    'slot_end:<k>'."""
    kind, _, rest = name.partition(':')
    if kind == 'entry':
        return _entity(design, L['entry'])
    if kind == 'corner':
        return _entity(design, L['corner'][int(rest)]['entry'])
    if kind == 'pocket':
        return _entity(design, L['pocket_entries'][rest]['point'])
    if kind == 'window':
        return _entity(design, L['window_entries'][int(rest)]['point'])
    if kind == 'hole':
        return _entity(design, L['hole_centres'][rest])
    if kind == 'slot_end':
        return _entity(design, L['slots'][int(rest)]['end'])
    raise RuntimeError('unknown point %r' % name)


def _sel_items(design, a, L, param, items, top):
    """One curve-selection parameter from named items, in one apply (a second
    apply on a parameter drops the pocket selections the first made):
      floor:<i>          a pocket floor (pockets), or its loops (contours)
      floor_outline:<i>  the floor's outer loop, the tool inside it
      floor_inner:<i>    the floor's inner loops (the holes through it)
      hole_floor_inner:<i>  the inner loops of blind hole i's bottom face (the
                         hole that goes on down from a counterbore's floor)
      face:<top|bottom>  the part's face, its loops as Fusion picks them
      face_deburr:<top|bottom>  the face's edges a deburr takes: all of them, but
                         where the stock fits the part and the face is not faced,
                         not the edges on the stock's own sides, which no tool cuts
      patch:<name>       a face patch (pockets)
      patch_outline:<n>  a patch's outer loop, as a closed boundary (pockets)
      region:<k>         a shelf region's closed chain
      window:<j>         a window's loop, the tool inside it
      hole_rim:<i>       a hole's rim where the setup enters it, the tool inside it
      chains:<corner|tabs|ends>  the outline runs build sketched, the tool outside
      slot:<k>           a slot's centreline, open
      silhouette         the setup model's outline, the tool outside it
      path:<id>          a pass sketched by pass_path, followed exactly"""
    def f(params):
        v = params.itemByName(param).value
        sels = v.getCurveSelections()

        def loop_chain(loop):  # as _sel_chains
            s = sels.createNewChainSelection()
            s.inputGeometry = [c.edge for c in loop.coEdges]
            s.isOpen = False
            opposed = loop.coEdges.item(0).isOpposedToEdge
            s.isReverted = not opposed if _normal(loop.face).z > 0 else opposed

        def face_contour(face, loop=None, side=None):
            s = sels.createNewFaceContourSelection()
            s.inputGeometry = [face]
            if loop is not None:
                s.loopType = loop
            if side is not None:
                s.sideType = side
        for it in items:
            kind, _, rest = it.partition(':')
            if kind == 'floor':
                face = a['floors'][int(rest)]['face']
                if param == 'pockets':
                    sels.createNewPocketSelection().inputGeometry = [face]
                else:
                    face_contour(face)
            elif kind == 'floor_outline':
                face_contour(a['floors'][int(rest)]['face'], adsk.cam.LoopTypes.OnlyOutsideLoops,
                             adsk.cam.SideTypes.AlwaysInsideSideType)
            elif kind == 'floor_inner':
                face_contour(a['floors'][int(rest)]['face'], adsk.cam.LoopTypes.OnlyInsideLoops)
            elif kind == 'hole_floor_inner':
                face_contour(_ref(design, a, L, 'hole_bottom:' + rest),
                             adsk.cam.LoopTypes.OnlyInsideLoops)
            elif kind == 'face':
                face_contour(_ref(design, a, L, it))
            elif kind == 'face_deburr':
                st = L['strategy']
                faced = L['face'] and L['margin_%s_mm' % rest] > 1e-6
                axes = [] if st['outline'] or faced else ['y'] if st['ends'] else ['x', 'y']
                face = _ref(design, a, L, 'face:' + rest)
                _add_deburr(sels, face, _raw_edges(face, a['body'], axes) if axes else set())
            elif kind == 'patch':
                sels.createNewPocketSelection().inputGeometry = [_ref(design, a, L, it)]
            elif kind == 'patch_outline':
                face_contour(_ref(design, a, L, 'patch:' + rest),
                             adsk.cam.LoopTypes.OnlyOutsideLoops,
                             adsk.cam.SideTypes.AlwaysInsideSideType)
            elif kind == 'region':
                r = L['regions'][int(rest)]
                s = sels.createNewChainSelection()
                s.inputGeometry = [_entity(design, t) for t in r['curves']]
                s.isOpen = False
                s.isReverted = r['clockwise'] if top else not r['clockwise']
            elif kind == 'window':
                loop_chain(a['windows'][int(rest)]['loop'])
            elif kind == 'hole_rim':
                rim = _hole_ends(a['holes'][int(rest)], top)[0]
                if rim is None:
                    raise RuntimeError('hole %s has no flat face round its rim' % rest)
                loop_chain(rim)
            elif kind == 'chains':
                for ch in (_chain(design, c) for c in L[rest]):
                    s = sels.createNewChainSelection()
                    s.inputGeometry = list(ch['curves'])
                    s.isOpen = True
                    d = ch['dir'] if top else adsk.core.Vector3D.create(-ch['dir'].x, -ch['dir'].y, 0)
                    s.isReverted = _is_reverted(ch['curves'][0], ch['start'], d)
            elif kind == 'slot':
                s = sels.createNewChainSelection()
                s.inputGeometry = [_entity(design, L['slots'][int(rest)]['line'])]
                s.isOpen = True
                s.isReverted = False
            elif kind == 'silhouette':
                s = sels.createNewSilhouetteSelection()
                s.isSetupModelSelected = True
                s.loopType = adsk.cam.LoopTypes.OnlyOutsideLoops
                s.sideType = adsk.cam.SideTypes.AlwaysOutsideSideType
            elif kind == 'path':
                curves = _path_curves(design, [rest])[0]
                s = sels.createNewChainSelection()
                s.inputGeometry = list(curves)
                s.isOpen = True
                nxt = curves[1] if len(curves) > 1 else None
                s.isReverted = nxt is not None and any(
                    curves[0].startSketchPoint.geometry.distanceTo(q.geometry) < 1e-5
                    for q in (nxt.startSketchPoint, nxt.endSketchPoint))
            else:
                raise RuntimeError('unknown selection %r' % it)
        v.applyCurveSelections(sels)
    return f


def _program(cam, L, which, warnings):
    """The NC program '<name>_<which>', made with the post on first use."""
    name = '%s_%s' % (L['name'], which)
    nc = next((n for n in cam.ncPrograms if n.name == name + ' ' + SETUP_TAG), None)
    if nc is None:
        nc = _nc_program(cam, name, [], _post(_fetch_community(warnings)['post']))
    return nc


def add_operation(args):
    """Add one operation to a setup build made, after build.

    setup:     the setup's name, or "top" / "bottom"
    strategy:  pocket2d, contour2d, drill, bore, thread or chamfer2d (default contour2d)
    name:      the operation's name
    tool:      the tool's description, from the part's tools or the library; with
               T, that slot (T2 and T3 are both the 3.175 mm end mill); or
    tool_from: an operation whose tool and preset it copies
    params:    Fusion parameter expressions on top of the lab's settings for the
               strategy; a height's reference is {"ref": "<name>"} (see _ref) or
               {"entity_tokens": [...]}
    select:    what it cuts: {"pockets": [...], "contours": [...], "stock_contours":
               [...]} of named items (see _sel_items); {"holes": [hole, ...]} for a
               drill, bore or thread; {"points": [...]} for a drill, {"predrill":
               [...]} and {"entries": [...]}, of named points (see _point); or, as
               the first recipes named them, {"pocket_floors": [...]},
               {"window_loops": [...]}, {"paths": [...]}
    after:     the operation it goes right after, and into the NC program holding
               that; else it goes last in the setup
    program:   "top", "bottom" or "tabs": the NC program '<name>_<program>' it goes
               in, made on first use; after `after` if that is in it, else last
    bottom_at_floor: the floor its bottom height sits at
    """
    design, body, a = _built(args)
    cam = adsk.cam.CAM.cast(_app().activeDocument.products.itemByProductType('CAMProductType'))
    L = _layout(design) if design.attributes.itemByName(*LAYOUT_ATTR) is not None else None
    sname = args['setup']
    if L is not None and sname in L['setups']:
        sname = L['setups'][sname]
    setup = _setup_named(cam, sname)
    top = not sname.startswith('Bottom')
    by_name = {o.name: o for o in setup.allOperations}
    after = by_name[args['after']] if args.get('after') else None
    if args.get('tool_from'):
        like = by_name.get(args['tool_from']) or next(o for o in _all_ops(cam)
                                                      if o.name == args['tool_from'])
        tool, preset = like.tool, like.toolPreset
    else:
        tool, preset = _tool_for(cam, args['tool'], args.get('T'))
    strategy = args.get('strategy', 'contour2d')
    sel = args.get('select', {})
    base = dict({'pocket2d': POCKET, 'contour2d': CONTOUR, 'drill': DRILL, 'bore': BORE,
                 'thread': THREAD, 'chamfer2d': CHAMFER_OP}[strategy])
    if 'paths' in sel:
        base['compensationType'] = "'off'"
    oi = setup.operations.createInput(strategy)
    oi.tool = tool
    if preset is not None:
        oi.toolPreset = preset
    oi.displayName = args['name']
    modes = getattr(adsk.cam, 'AutomaticGenerationModes', None)
    skip = [getattr(modes, n) for n in dir(modes) if 'Skip' in n] if modes else []
    if skip:
        oi.generationMode = skip[0]
    _feed_height(oi.parameters)
    params = dict(base, **args.get('params', {}))
    for k, v in params.items():
        if not isinstance(v, dict):
            _set(oi.parameters, k, v)
    for k, v in params.items():  # a height's reference faces
        if isinstance(v, dict):
            ents = ([_ref(design, a, L, v['ref'])] if 'ref' in v else
                    [design.findEntityByToken(t)[0] for t in v['entity_tokens']])
            oi.parameters.itemByName(k).value.value = ents
    if args.get('bottom_at_floor') is not None:
        face = a['floors'][args['bottom_at_floor']]['face']
        _set(oi.parameters, 'bottomHeight_mode', "'from point'")
        _set(oi.parameters, 'bottomHeight_offset', '0mm')
        oi.parameters.itemByName('bottomHeight_ref').value.value = [face]
    picked = False
    for key, param in (('pockets', 'pockets'), ('contours', 'contours'),
                       ('stock_contours', 'stockContours')):
        if key in sel:
            _sel_items(design, a, L, param, sel[key], top)(oi.parameters)
            picked = True
    if 'holes' in sel:
        _sel_faces('holeFaces' if strategy == 'drill' else 'circularFaces',
                   [a['holes'][i]['face'] for i in sel['holes']])(oi.parameters)
        picked = True
    for key, param in (('points', 'holePoints'), ('predrill', 'predrillPositions'),
                       ('entries', 'entryPositions')):
        if sel.get(key):
            _sel_faces(param, [_point(design, L, n) for n in sel[key]])(oi.parameters)
            picked = picked or key == 'points'
    if 'paths' in sel:
        _sel_paths(_path_curves(design, sel['paths']))(oi.parameters)
        picked = True
    elif 'pocket_floors' in sel:
        _sel_pocket([a['floors'][i]['face'] for i in sel['pocket_floors']])(oi.parameters)
        picked = True
    elif 'window_loops' in sel:
        _sel_chains([a['windows'][j]['loop'] for j in sel['window_loops']], 'pockets')(oi.parameters)
        picked = True
    if not picked:
        raise RuntimeError('select: give what the operation cuts')
    op = setup.operations.add(oi)
    if after is not None:
        op.moveAfter(after)
    programs, warnings = [], []
    if args.get('program'):
        nc = _program(cam, L, args['program'], warnings)
        ops = list(nc.operations)
        ids = [o.operationId for o in ops]
        if after is not None and after.operationId in ids:
            ops.insert(ids.index(after.operationId) + 1, op)
        else:
            ops.append(op)
        nc.operations = ops
        programs.append(nc.name)
    elif after is not None:
        # into the NC program that holds the operation it follows, right after it
        for nc in cam.ncPrograms:
            ops = list(nc.operations)
            ids = [o.operationId for o in ops]
            if after.operationId in ids:
                ops.insert(ids.index(after.operationId) + 1, op)
                nc.operations = ops
                programs.append(nc.name)
    out = {'operation': op.name, 'setup': setup.name, 'after': after.name if after else None,
           'T': int(tool.parameters.itemByName('tool_number').value.value),
           'nc_programs': programs}
    if REJECTED:
        out['rejected'] = list(REJECTED)
    if warnings:
        out['warnings'] = warnings
    print(json.dumps(out, indent=1))
    return out


def add_contour(args):
    """add_operation for a contour along sketched paths: `paths` (pass_path
    ids), with the other arguments as add_operation takes them."""
    return add_operation(dict(args, strategy='contour2d', select={'paths': args['paths']}))


def renumber_tools(args=None):
    """Number the tools in the lab's slot order after operations were added: T1
    the 3.175 mm drill, T2 and T3 the roughing and finishing end mill as build
    numbered them, then every other tool in the order the programs first use
    it, the top setup's operations first."""
    cam = adsk.cam.CAM.cast(_app().activeDocument.products.itemByProductType('CAMProductType'))
    order, number = [], {}
    for o in _all_ops(cam):
        if o.tool is None:
            continue
        p = o.tool.parameters
        n = int(p.itemByName('tool_number').value.value)
        key = p.itemByName('tool_description').value.value if n > 3 else n
        if key not in number:
            number[key] = n if n <= 3 else 4 + len(order)
            if n > 3:
                order.append(key)
    changed = []
    for o in _all_ops(cam):
        if o.tool is None:
            continue
        t = o.tool
        p = t.parameters
        n = int(p.itemByName('tool_number').value.value)
        new = n if n <= 3 else number[p.itemByName('tool_description').value.value]
        if new != n:
            preset = o.toolPreset.name if o.toolPreset is not None else None
            p.itemByName('tool_number').expression = str(new)
            o.tool = t
            if preset is not None:
                o.toolPreset = next((t.presets.item(i) for i in range(t.presets.count)
                                     if t.presets.item(i).name == preset), None)
            changed.append({'operation': o.name, 'from': n, 'to': new})
    out = {'changed': changed, 'slots': [{'T': number[k], 'tool': k} for k in order]}
    print(json.dumps(out, indent=1))
    return out


# ------------------------------------------------------------- libraries

class Tools:
    """The skill's tools from TOOLS_FILE, numbered into Carvera ATC slots."""

    def __init__(self):
        with open(TOOLS_FILE, encoding='utf-8') as f:
            # escaped to ASCII: createFromJson garbles other characters, such as
            # the ″ in the chamfering bit's name
            lib = adsk.cam.ToolLibrary.createFromJson(json.dumps(json.load(f)))
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
        self.last = {}  # tool key -> the latest operation made with that tool

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
        _feed_height(oi.parameters)
        for k, v in params.items():
            if not isinstance(v, list):
                _set(oi.parameters, k, v)
        for k, v in params.items():  # a list is a height's reference geometry
            if isinstance(v, list):
                oi.parameters.itemByName(k).value.value = v
        if select:
            select(oi.parameters)
        op = setup.operations.add(oi)
        self.last[(desc, key)] = op
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


def _sel_chains(loops, param='contours'):
    # Inner loops of a floor, cut from the top with the tool inside them.
    # The API ignores a chain's side and takes it from its first edge's direction:
    # with compensation left, the tool is inside a chain running counter-clockwise
    # seen from +Z. An inner loop's coedges run clockwise seen from +Z on an
    # upward face, so the chain runs against the first coedge, and
    # counter-clockwise on a downward face (a window of a part turned over), so
    # the chain runs with it. A pocket on such a chain pockets inside it too.
    def f(params):
        v = params.itemByName(param).value
        sels = v.getCurveSelections()
        for loop in loops:
            s = sels.createNewChainSelection()
            s.inputGeometry = [c.edge for c in loop.coEdges]
            s.isOpen = False
            opposed = loop.coEdges.item(0).isOpposedToEdge
            s.isReverted = not opposed if _normal(loop.face).z > 0 else opposed
        v.applyCurveSelections(sels)
    return f


def _sel_open_lines(lines):
    """Open chains, one per sketch line, cut from its start to its end."""
    def f(params):
        v = params.itemByName('contours').value
        sels = v.getCurveSelections()
        for ln in lines:
            s = sels.createNewChainSelection()
            s.inputGeometry = [ln]
            s.isOpen = True
            s.isReverted = False
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


def _raw_edges(face, body, axes):
    """The edges of the face's outline that lie on the stock's own sides: straight
    edges at the body's extent along each of `axes` ('x', 'y'), where the stock
    fits the part. With the face not faced, no tool cuts them."""
    bb = body.boundingBox
    out = set()
    for loop in face.loops:
        if not loop.isOuter:
            continue
        for e in loop.edges:
            if not isinstance(e.geometry, adsk.core.Line3D):
                continue
            p, q = e.startVertex.geometry, e.endVertex.geometry
            for ax in axes:
                for v in (getattr(bb.minPoint, ax), getattr(bb.maxPoint, ax)):
                    if abs(getattr(p, ax) - v) < 1e-4 and abs(getattr(q, ax) - v) < 1e-4:
                        out.add(e.tempId)
    return out


def _add_deburr(sels, face, raw):
    """Add to `sels` the face's edges but those in `raw` (tempIds): the whole face
    when none are raw, else its inner loops and the runs of its outer loop between
    the raw edges, as open chains with the bit outside the part. The chain's side
    follows its first edge's direction seen along the tool axis: the outer loop's
    co-edges run counter-clockwise seen from the face's normal, which faces the
    tool, so the chain runs against them."""
    s = sels.createNewFaceContourSelection()
    s.inputGeometry = [face]
    if not raw:
        return
    s.loopType = adsk.cam.LoopTypes.OnlyInsideLoops
    outer = [lp for lp in face.loops if lp.isOuter][0]
    ces = list(outer.coEdges)
    k0 = next(k for k, ce in enumerate(ces) if ce.edge.tempId in raw)
    runs, run = [], []
    for ce in ces[k0:] + ces[:k0]:
        if ce.edge.tempId in raw:
            if run:
                runs.append(run)
            run = []
        else:
            run.append(ce)
    if run:
        runs.append(run)
    for run in runs:
        s = sels.createNewChainSelection()
        s.inputGeometry = [ce.edge for ce in run]
        s.isOpen = True
        s.isReverted = not run[0].isOpposedToEdge


def _sel_deburr(face, raw):
    def f(params):
        v = params.itemByName('contours').value
        sels = v.getCurveSelections()
        _add_deburr(sels, face, raw)
        v.applyCurveSelections(sels)
    return f


def _sel_inner_contours(faces):
    """The faces' inner loops: the edges of the holes through them."""
    def f(params):
        v = params.itemByName('contours').value
        sels = v.getCurveSelections()
        for face in faces:
            s = sels.createNewFaceContourSelection()
            s.inputGeometry = [face]
            s.loopType = adsk.cam.LoopTypes.OnlyInsideLoops
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


def _open_walls(face):
    """The walls of a pocket open to the side: each run of the floor's outer loop
    along walls rising from it, as co-edges in loop order (the floor on their
    left seen from the open side), and how far the floor reaches from its walls (cm)."""
    rising = {e.tempId for e in _rising_edges(face)}
    ces = list([lp for lp in face.loops if lp.isOuter][0].coEdges)
    k0 = next((k for k, ce in enumerate(ces) if ce.edge.tempId not in rising), None)
    if k0 is None:
        return [], 0.0
    runs, run = [], []
    for ce in ces[k0:] + ces[:k0]:
        if ce.edge.tempId in rising:
            run.append(ce)
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)
    walls = [q for e in face.edges if e.tempId in rising for q in _edge_points(e)]
    reach = max((min(math.hypot(x - u, y - v) for u, v in walls)
                 for e in face.edges if e.tempId not in rising for x, y in _edge_points(e)),
                default=0.0)
    return runs, reach


def _draw_walls(sk, run):
    """The walls of `run` (co-edges) drawn into the sketch flat, joined end to
    end, as construction curves. Drawn, not projected: projecting an edge the
    sketch already holds gives nothing."""
    out, prev = [], None
    P = lambda p: adsk.core.Point3D.create(p.x, p.y, 0)
    for ce in run:
        e = ce.edge
        ok, a_, b_ = e.evaluator.getEndPoints()
        if ce.isOpposedToEdge:
            a_, b_ = b_, a_
        ok, t0, t1 = e.evaluator.getParameterExtents()
        ok, m = e.evaluator.getPointAtParameter((t0 + t1) / 2)
        start = prev if prev is not None else P(a_)
        if isinstance(e.geometry, adsk.core.Line3D):
            c = sk.sketchCurves.sketchLines.addByTwoPoints(start, P(b_))
        elif isinstance(e.geometry, adsk.core.Arc3D):
            c = sk.sketchCurves.sketchArcs.addByThreePoints(start, P(m), P(b_))
        else:
            pts = adsk.core.ObjectCollection.create()
            for x, y in _edge_points(e, reverse=ce.isOpposedToEdge):
                pts.add(adsk.core.Point3D.create(x, y, 0))
            c = sk.sketchCurves.sketchFittedSplines.add(pts)
        c.isConstruction = True
        # the end the next wall starts from
        prev = c.endSketchPoint if c.endSketchPoint.geometry.distanceTo(P(b_)) < 1e-5             else c.startSketchPoint
        out.append(c)
    return out


def _pass_path(sk, run, walls, d, ring, face):
    """One contour pass along a pocket's open walls (`run`, co-edges, drawn as
    `walls`, see _draw_walls): the tool centre's path `d` cm off the walls,
    sketched, with a line at each end out to the outline cut's path, one tool
    radius outside the outline (`ring`); an end already beyond that path is cut
    back to it instead. The curves in the order the tool follows them, or None
    (leaving nothing sketched) when an end, run on 10 mm, does not meet that path."""
    q = face.pointOnFace
    coll = adsk.core.ObjectCollection.create()
    for c in walls:
        coll.add(c)
    try:
        curves = list(sk.offset(coll, adsk.core.Point3D.create(q.x, q.y, 0), d))
    except RuntimeError:
        return None
    for c in curves:
        c.isConstruction = False
    # chain the offset curves, from the end beside the run's start
    ok, s0, e0 = run[0].edge.evaluator.getEndPoints()
    if run[0].isOpposedToEdge:
        s0 = e0
    pts = lambda c: (c.startSketchPoint, c.endSketchPoint)
    near = lambda p, r: p.geometry.distanceTo(r.geometry) < 1e-5
    ends = [p for c in curves for p in pts(c)
            if sum(1 for c2 in curves for p2 in pts(c2) if near(p, p2)) == 1]
    if len(ends) != 2:
        for c in curves:
            c.deleteMe()
        return None
    start = min(ends, key=lambda p: math.hypot(p.geometry.x - s0.x, p.geometry.y - s0.y))
    order, node, left = [], start, list(curves)
    while left:
        c = next((c for c in left if any(near(p, node) for p in pts(c))), None)
        if c is None:
            for c in curves:
                c.deleteMe()
            return None
        left.remove(c)
        order.append(c)
        a_, b_ = pts(c)
        node = b_ if near(a_, node) else a_
    stop = node

    def outside(x, y):  # distance outside the outline, negative inside
        dd = _ring_clearance((ring, [], None), x, y)
        return -dd if _inside(ring, x, y) else dd

    def run_on(p, c):
        # from the end p of curve c, on along c's direction there to the cut's
        # path: the point, False when p is on it already or beyond it, None
        # when the line misses it within 10 mm
        ev = c.geometry.evaluator
        ok, t = ev.getParameterAtPoint(p.geometry)
        ok, v = ev.getFirstDerivative(t)
        n = math.hypot(v.x, v.y)
        ok, t0, t1 = ev.getParameterExtents()
        ok, m = ev.getPointAtParameter((t0 + t1) / 2)
        sg = 1 if (p.geometry.x - m.x) * v.x + (p.geometry.y - m.y) * v.y > 0 else -1
        ux, uy = sg * v.x / n, sg * v.y / n
        x0, y0 = p.geometry.x, p.geometry.y
        f = lambda L: outside(x0 + L * ux, y0 + L * uy) - TOOL_R
        if f(0) > -1e-4:
            return False
        lo, hi, L = 0.0, None, 0.0
        while L < 1.0:
            L += 0.002
            if f(L) >= 0:
                hi = L
                break
            lo = L
        if hi is None:
            return None
        for _ in range(30):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if f(mid) < 0 else (lo, mid)
        return adsk.core.Point3D.create(x0 + hi * ux, y0 + hi * uy, 0)

    def trim_back(end, chain):
        # an end beyond the cut's path: cut the chain back to where it crosses
        # the path, from that end; chain runs from that end inward. The new end
        # sketch point, or None when the chain never comes inside the path
        while chain:
            c = chain[0]
            ev = c.geometry.evaluator
            ok, t0, t1 = ev.getParameterExtents()
            ok, te = ev.getParameterAtPoint(end.geometry)
            tf = t1 if abs(te - t0) < abs(te - t1) else t0  # the far end's parameter
            g = lambda tt: (lambda q: outside(q.x, q.y) - TOOL_R)(ev.getPointAtParameter(tt)[1])
            ts = [te + (tf - te) * k / 200 for k in range(201)]
            k = next((k for k, tt in enumerate(ts) if g(tt) <= 0), None)
            if k is None:  # all of this curve lies beyond the path
                a_, b_ = pts(c)
                end = b_ if near(a_, end) else a_
                chain.pop(0)
                c.deleteMe()
                continue
            lo, hi = ts[k - 1], ts[k]
            for _ in range(40):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if g(mid) > 0 else (lo, mid)
            ok, q = ev.getPointAtParameter(hi)
            # redraw the curve from its far end to q (SketchCurve.split leaves
            # some curves whole, without an error)
            a_, b_ = pts(c)
            far = b_ if near(a_, end) else a_
            if isinstance(c, adsk.fusion.SketchLine):
                new = sk.sketchCurves.sketchLines.addByTwoPoints(far, q)
            elif isinstance(c, adsk.fusion.SketchArc):
                ok, m = ev.getPointAtParameter((hi + tf) / 2)
                new = sk.sketchCurves.sketchArcs.addByThreePoints(far, m, q)
            else:
                return None
            c.deleteMe()
            chain[0] = new
            a_, b_ = pts(new)
            return a_ if a_.geometry.distanceTo(q) < 1e-5 else b_
        return None

    made = []

    def fail():
        for c in list(order) + made:
            if c.isValid:
                c.deleteMe()
        return None

    ends_out = []
    for which in (0, 1):
        chain = order if which == 0 else order[::-1]
        end = start if which == 0 else stop
        q = run_on(end, chain[0])
        if q is None:
            return fail()
        if q is False and outside(end.geometry.x, end.geometry.y) - TOOL_R > 1e-4:
            end = trim_back(end, chain)
            if end is None:
                return fail()
            if which == 0:
                order[:] = chain
            else:
                order[:] = chain[::-1]
        ends_out.append((end, q))
    (start, qa), (stop, qb) = ends_out
    path = list(order)
    if qa:
        ln = sk.sketchCurves.sketchLines.addByTwoPoints(qa, start)
        made.append(ln)
        path.insert(0, ln)
    if qb:
        ln = sk.sketchCurves.sketchLines.addByTwoPoints(stop, qb)
        made.append(ln)
        path.append(ln)
    return path


def _sel_paths(paths):
    """Open chains of sketch curves, each cut from its first curve on."""
    def f(params):
        v = params.itemByName('contours').value
        sels = v.getCurveSelections()
        for curves in paths:
            s = sels.createNewChainSelection()
            s.inputGeometry = list(curves)
            s.isOpen = True
            # the chain runs along its first curve's own direction: reverted when
            # that curve starts where it meets the next one
            first = curves[0]
            nxt = curves[1] if len(curves) > 1 else None
            s.isReverted = nxt is not None and any(
                first.startSketchPoint.geometry.distanceTo(q.geometry) < 1e-5
                for q in (nxt.startSketchPoint, nxt.endSketchPoint))
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


def _sel_pocket_regions(faces, regions, top):
    # Floors and closed chains go in one applyCurveSelections: a second apply on
    # the same parameter drops the pocket selections made by the first.
    # The API takes a chain's side from its first curve's direction seen along the
    # setup's tool axis: a clockwise chain pockets everything outside it, so
    # clockwise chains are reverted. A chain clockwise from +Z runs
    # counter-clockwise seen by the bottom setup.
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
            s.isReverted = r['clockwise'] if top else not r['clockwise']
        v.applyCurveSelections(sels)
    return f


def _sel_stock_contours(regions, inner, faces, top):
    """Stock contours: the closed chains of `regions`, less the closed chains
    of `inner` and the outer loops of floor `faces` inside them. A pocket then
    cuts only where stock is left, and runs the tool over the rest only as far
    as its radius."""
    def f(params):
        v = params.itemByName('stockContours').value
        sels = v.getCurveSelections()
        for r in list(regions) + list(inner):
            s = sels.createNewChainSelection()
            s.inputGeometry = list(r['curves'])
            s.isOpen = False
            s.isReverted = r['clockwise'] if top else not r['clockwise']
        for face in faces:
            s = sels.createNewChainSelection()
            s.inputGeometry = [e for lp in face.loops if lp.isOuter for e in lp.edges]
            s.isOpen = False
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
    # the preset's stepdown, as the lab's own pockets take it; a new 2D pocket
    # otherwise keeps a 0.2 mm default, use_tool_stepdown notwithstanding
    'maximumStepdown': 'tool_stepdown',
    'useStockToLeave': 'false',
    'doRamp': 'true',
    # copper is always entered at a pre-drilled hole, never by a helix or ramp
    'rampType': "'predrill'",
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
# The most a rough bore takes off the wall: two 0.3 mm passes, as the lab bores
# its 4.2 mm holes. A wider hole is pocketed, as the lab cuts its 7 mm holes
# (hanger-for-chris, 2026-10-18): a 7 mm bore takes seven passes and four times
# as long.
BORE_MAX_RADIAL = 0.6
BORE = {
    'tolerance': '0.01mm',
    'direction': "'climb'",
    'doMultiplePasses': 'true',
}
THREAD = {
    'doMultiplePasses': 'true',
    # a helical lead-in starts below the bottom height and climbs to it; a flat
    # one stays at it
    'useHelicalLeads': 'false',
}
# The rough face leaves this much on the outer face, and the finishing face
# takes it off after every other cut but the deburr: facing leaves fewer burrs
# than side milling and thread milling. Until then every operation that starts
# at the face starts this much higher.
FACE_ALLOWANCE = 0.2
# The rough pocket leaves this much on its floors, as the rough face does; the
# finishing pocket takes it off in one pass.
POCKET_FLOOR_ALLOWANCE = 0.2
# The facing pockets face this far past the part's outline: the outline cut is
# one end mill wide outside it, and starts on the faced stock (_add_face_patches).
# The main entry is the outline cut's, one radius outside the outline, and Fusion
# wants about 5 x the pocket tolerance of room round an entry hole.
FACE_MARGIN = ENDMILL_D + 5 * POCKET_TOL
# Every operation rapids down to this far above its top height and feeds from
# there; the top height sits at the stock the operation starts on. Fusion's
# default, 5 mm, fed each entry and each drilled hole through 5 mm of air at the
# plunge or drill feed (user, 2026-10-17).
FEED_HEIGHT = 1.0  # mm


def _feed_height(params):
    if params.itemByName('feedHeight_offset') is not None:
        _set(params, 'feedHeight_mode', "'from top'")
        _set(params, 'feedHeight_offset', '%gmm' % FEED_HEIGHT)
    # Fusion raises the feed height to the ramp clearance height plus the
    # vertical lead-in radius (0.1 x the tool diameter) when they reach above it
    if params.itemByName('rampClearanceHeight') is not None:
        _set(params, 'rampClearanceHeight', '%gmm' % (FEED_HEIGHT / 2))


def _drill_ends(through):
    """Peck and breakthrough of a drill op, from threads.toml (the lab drills every hole alike)."""
    d = THREAD_COMMON['drill']
    brk = d['through_breakthrough'] if through else d['blind_breakthrough']
    return {'cycleType': "'%s'" % d['cycle'], 'peckingDepth': '%gmm' % d['peck'],
            'accumulatedPeckingDepth': '%gmm' % d['accumulated_peck'],
            'chipBreakDistance': '%gmm' % d['chip_break'], 'breakThroughDepth': '%gmm' % brk,
            'drillTipThroughBottom': 'true' if through else 'false'}


def _thread_passes(name):
    """The most radial passes the thread mill makes at threads.toml's stepover.

    The last pass runs the tool's centre on a helix of radius (modelled hole +
    pitch diameter offset - thread mill diameter) / 2, and each earlier pass one
    stepover further in; Fusion discards a pass whose radius would fall below zero.
    """
    r = THREAD_RECIPES[name]
    with open(TOOLS_FILE, encoding='utf-8') as f:
        tool = [t for t in json.load(f)['data'] if t.get('description') == r['thread_mill']][0]
    helix = (r['modelled_hole'] + r['pitch_diameter_offset'] - tool['geometry']['DC']) / 2
    return max(1, math.floor(helix / THREAD_COMMON['thread']['stepover'] + 1e-9) + 1)


def _thread_params(name, through, allow=0.0):
    """Thread-mill parameters for one thread size, from threads.toml; `allow` is
    the material still on the face above the hole top."""
    r, c = THREAD_RECIPES[name], THREAD_COMMON['thread']
    return dict(THREAD, direction="'%s'" % c['direction'], threadPitch='%gmm' % r['pitch'],
                pitchDiameterOffset='%gmm' % r['pitch_diameter_offset'],
                numberOfStepovers=str(_thread_passes(name)), stepover='%gmm' % c['stepover'],
                topHeight_offset='%gmm' % (c['top_offset'] + allow),
                bottomHeight_offset='%gmm' % (c['through_bottom_offset'] if through
                                              else c['blind_bottom_offset']))
CHAMFER_TIP_MAX = 0.5   # the chamfering bit's point below the chamfer, in mm, where there is room
CHAMFER_TIP_MIN = 0.1   # and at least this, so the point doesn't cut the edge
CHAMFER_CLEAR = 0.1     # the point kept this far above floors and the tabs
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
FX_JAW_WIDTH = 220.14 - 124.64  # the insert bars' length, across the vise
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


def _drill_tip(d):
    """Length (mm) of a 130 degree drill point of diameter d mm."""
    return d / 2 / math.tan(math.radians(65))


def _planned_cuts(a, plan, box):
    """Where each setup's cuts reach, for the check against the vise: per side,
    (what, (x0, y0, x1, y1), z) in model cm, the footprint of the tool's cutting
    end and the deepest Z it reaches: the lowest for the top setup, the highest
    for the bottom setup, whose tool comes from below. The footprints are
    boxes round the features, which is exact for the jaws, straight strips
    along the stock's X edges."""
    lay = plan['layout']
    bb = a['body'].boundingBox
    lo, hi = bb.minPoint, bb.maxPoint
    tab = plan['tab']
    band_top, band_bot = (tab['z'] + tab['band'] / 2) / 10, (tab['z'] - tab['band'] / 2) / 10
    ztop, zbot = a['ztop'] / 10, a['zbot'] / 10
    D = ENDMILL_D / 10
    deburr = (float(CHAMFER_OP['chamferWidth'][:-2]) + CHAMFER_TIP_MAX) / 10

    def ring(m):  # the part's footprint grown by m cm
        return (lo.x - m, lo.y - m, hi.x + m, hi.y + m)

    def circle(x, y, r):
        return (x - r, y - r, x + r, y + r)
    cuts = {'top': [], 'bottom': []}
    for side in ('top', 'bottom')[:plan['sides']]:
        top = side == 'top'
        margin = plan['margin_top'] if top else plan['margin_bottom']
        face = ztop if top else zbot
        if plan['face'] and margin > 1e-6:
            cuts[side].append(('the facing', ring(FACE_MARGIN / 10), face))
        if plan['chamfer']:
            cuts[side].append(('the deburr', ring(TOOL_R), face - deburr if top else face + deburr))
        if plan['outline']:
            cuts[side].append(('the outline cut', ring(D), band_top if top else band_bot))
    for e in (lay['ends'] if plan.get('ends') else []):
        x0, y0, x1, y1 = e['box']
        cuts['top'].append(("the cut at the part's end", (x0 - D, y0 - TOOL_R, x1 + D, y1 + TOOL_R),
                            box[0][2] - 0.05))
    if plan['outline']:
        chain_bot = band_bot - tab['overlap'] / 10
        cuts['top'].append(('the outline between the tabs', ring(D), chain_bot))
        p = lay['entry'].geometry
        cuts['top'].append(('the entry hole', circle(p.x, p.y, DRILL_D / 20),
                            box[0][2] - (0.5 + _drill_tip(DRILL_D)) / 10))
    for w in a['windows']:
        wb = w['loop'].boundingBox
        cuts['top'].append(('a window', (wb.minPoint.x, wb.minPoint.y, wb.maxPoint.x,
                                         wb.maxPoint.y), zbot - 0.05))
    for h in a['holes']:
        if h['through'] and h['side'] == 'top':
            cuts['top'].append(('the %.2f mm hole at (%g, %g) mm' % (h['dia'], h['x'], h['y']),
                                circle(h['x'] / 10, h['y'] / 10, h['dia'] / 20),
                                zbot - (0.5 + _drill_tip(min(h['dia'], DRILL_D))) / 10))
    return cuts


def _vise_conflicts(cuts, vises, box):
    """The planned cuts (see _planned_cuts) whose tool would enter the vise:
    the jaws beside the stock, or their ledges under its Y edges, where a cut
    through the stock's bottom goes on into the jaw. Each cut is tested as a
    box from its footprint and deepest Z out past the stock's far side."""
    tb = adsk.fusion.TemporaryBRepManager.get()
    out = []
    reach = 5.0  # cm past the stock, for the tool's shank above the cut
    for side, items in cuts.items():
        occ = vises.get(side)
        if occ is None:
            continue
        bodies = list(occ.bRepBodies)
        for what, (x0, y0, x1, y1), z in items:
            z0, z1 = (z, box[1][2] + reach) if side == 'top' else (box[0][2] - reach, z)
            obb = adsk.core.OrientedBoundingBox3D.create(
                adsk.core.Point3D.create((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2),
                adsk.core.Vector3D.create(1, 0, 0), adsk.core.Vector3D.create(0, 1, 0),
                x1 - x0, y1 - y0, z1 - z0)
            for b in bodies:
                bb = b.boundingBox
                if bb.maxPoint.x < x0 or bb.minPoint.x > x1 or bb.maxPoint.y < y0 or \
                        bb.minPoint.y > y1 or bb.maxPoint.z < z0 or bb.minPoint.z > z1:
                    continue
                c = tb.copy(b)
                tb.booleanOperation(c, tb.createBox(obb), adsk.fusion.BooleanTypes.IntersectionBooleanType)
                if c.volume > 1e-6:
                    q = c.boundingBox
                    out.append('in the %s setup, %s would cut into the vise at model x %.1f..%.1f, '
                               'y %.1f..%.1f, z %.2f..%.2f mm'
                               % (side, what, q.minPoint.x * 10, q.maxPoint.x * 10,
                                  q.minPoint.y * 10, q.maxPoint.y * 10, q.minPoint.z * 10,
                                  q.maxPoint.z * 10))
                    break
    return out


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
    try:  # the machine stays in the simulation, out of the way elsewhere
        setup.visibilityManager.machineVisible = False
        setup.visibilityManager.machineBaseVisible = False
    except AttributeError:
        pass
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
PART_COMPONENT = LAYOUT_SKETCH + ' part'  # the part turned over, when its bottom is machined first
HIDDEN_ATTR = ('carvera-cam', 'hidden')  # on the design's body while the turned copy stands in
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
    sk.isVisible = False
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
    The finishing face runs after the outline cut has cleared that, so its
    patch is the outline itself.
    Return {'top': face, 'bottom': face, 'top finish': face, 'bottom finish':
    face} as seen from the root. The component is hidden."""
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
        sk.isVisible = False
        curves = adsk.core.ObjectCollection.create()
        for pc in lay['outline']:
            for c in _sketch_curve(sk, pc, 0, pc['len']):
                curves.add(c)
        bb = sk.boundingBox
        away = adsk.core.Point3D.create(bb.maxPoint.x + 10, bb.maxPoint.y + 10, 0)
        sk.offset(curves, away, FACE_MARGIN / 10)
        for c in curves:
            c.isConstruction = True  # the patch is the offset loop's inside
        out[side] = _patch(comp, occ, sk, 'face ' + side)
        sk = comp.sketches.add(plane)
        sk.name = 'face outline ' + side + ' (finish)'
        sk.isVisible = False
        for pc in lay['outline']:
            _sketch_curve(sk, pc, 0, pc['len'])
        out[side + ' finish'] = _patch(comp, occ, sk, 'face ' + side + ' (finish)')
    occ.isLightBulbOn = False  # out of the way; the facing pockets still select the patches
    return out


def _patch(comp, occ, sk, name):
    """A planar patch body filling the sketch's largest profile; its face as
    seen from the root."""
    prof = max(sk.profiles, key=lambda pr: pr.areaProperties().area)
    patches = comp.features.patchFeatures
    e = patches.add(patches.createInput(prof, adsk.fusion.FeatureOperations.NewBodyFeatureOperation))
    e.name = name
    body = e.bodies.item(0)
    body.name = name
    return body.faces.item(0).createForAssemblyContext(occ)


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


def _centroid(ring):
    """Area centroid of a closed XY polygon."""
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        w = x0 * y1 - x1 * y0
        a, cx, cy = a + w, cx + (x0 + x1) * w, cy + (y0 + y1) * w
    if abs(a) < 1e-12:
        return None
    return cx / (3 * a), cy / (3 * a)


def _rising_edges(face):
    """The floor's edges along walls that rise from it toward the open side: the
    pocket's sides and its islands. Where the floor steps down into a deeper
    pocket, a hole or a window, the tool has room."""
    dz = 0.002 if _normal(face).z > 0 else -0.002
    out = []
    for e in face.edges:
        w = _other_face(e, face)
        if w is None:
            out.append(e)
            continue
        # a wall may rise from one stretch of floor and drop from another, so
        # look at it just above this edge's middle
        ev = e.evaluator
        ok, t0, t1 = ev.getParameterExtents()
        ok, m = ev.getPointAtParameter((t0 + t1) / 2)
        p = adsk.core.Point3D.create(m.x, m.y, m.z + dz)
        wev = w.evaluator
        ok, uv = wev.getParameterAtPoint(p)
        if ok and wev.isParameterOnFace(uv):
            ok, q = wev.getPointAtParameter(uv)
            if ok and q.distanceTo(p) < EPS:
                out.append(e)
    return out


def _pocket_entry(face):
    """The pocket's entry: its centre (the centre of the floor's outer loop, or
    else its centroid) where the end mill fits there clear of the walls rising
    from the floor, a deeper pocket, hole or window in the floor counting as
    open; else the point on the floor farthest from its edges (see _entry_point)."""
    bb = face.boundingBox
    z = face.pointOnFace.z
    fev = face.evaluator
    up = _normal(face).z > 0
    rising = _rising_edges(face)
    rising_ids = {e.tempId for e in rising}
    outer = [lp for lp in face.loops if lp.isOuter]
    if outer and rising:
        ring = _loop_ring(outer[0])
        drops = [_loop_ring(lp) for lp in face.loops if not lp.isOuter
                 and not any(e.tempId in rising_ids for e in lp.edges)]
        lb = outer[0].boundingBox
        mm_ = adsk.core.Application.get().measureManager
        for ctr in ((lb.minPoint.x + lb.maxPoint.x) / 2, (lb.minPoint.y + lb.maxPoint.y) / 2), \
                _centroid(ring):
            if ctr is None:
                continue
            x, y = ctr
            above = adsk.core.Point3D.create(x, y, z + (0.001 if up else -0.001))
            if face.body.pointContainment(above) != \
                    adsk.fusion.PointContainment.PointOutsidePointContainment:
                continue  # in the material: an island, or outside the pocket
            pt = adsk.core.Point3D.create(x, y, z)
            r = min((mm_.measureMinimumDistance(pt, e) for e in rising), key=lambda r: r.value)
            if r.value < ENTRY_R:
                continue
            # over this pocket: inside the floor's outline, over a hole or window in
            # it, or over a deeper pocket next to the floor
            if _inside(ring, x, y) or any(_inside(d, x, y) for d in drops) or \
                    min(mm_.measureMinimumDistance(pt, e).value for e in face.edges) < ENTRY_R:
                w = max((r.positionOne, r.positionTwo), key=lambda g: g.distanceTo(pt))
                return r.value, x, y, (w.x, w.y)

    def inside(x, y):
        ok, uv = fev.getParameterAtPoint(adsk.core.Point3D.create(x, y, z))
        return ok and fev.isParameterOnFace(uv)

    return _entry_point(inside, [e for lp in face.loops for e in lp.edges], z,
                        (bb.minPoint.x, bb.minPoint.y, bb.maxPoint.x, bb.maxPoint.y))


def _window_entry(loop):
    """The window's centre (of its bounding box, else its centroid) where the end
    mill fits there, else the point inside the loop farthest from it (see
    _entry_point)."""
    bb = loop.boundingBox
    poly = [q for ce in loop.coEdges for q in _edge_points(ce.edge, ce.isOpposedToEdge)]

    def inside(x, y):  # ray casting
        hit = False
        for (u0, v0), (u1, v1) in zip(poly, poly[1:] + poly[:1]):
            if (v0 > y) != (v1 > y) and x < u0 + (y - v0) * (u1 - u0) / (v1 - v0):
                hit = not hit
        return hit

    mm_ = adsk.core.Application.get().measureManager
    edges = [ce.edge for ce in loop.coEdges]
    for ctr in ((bb.minPoint.x + bb.maxPoint.x) / 2, (bb.minPoint.y + bb.maxPoint.y) / 2), \
            _centroid(poly):
        if ctr is None or not inside(*ctr):
            continue
        pt = adsk.core.Point3D.create(ctr[0], ctr[1], bb.maxPoint.z)
        r = min((mm_.measureMinimumDistance(pt, e) for e in edges), key=lambda r: r.value)
        if r.value >= ENTRY_R:
            w = max((r.positionOne, r.positionTwo), key=lambda g: g.distanceTo(pt))
            return r.value, ctr[0], ctr[1], (w.x, w.y)

    return _entry_point(inside, [ce.edge for ce in loop.coEdges], bb.maxPoint.z,
                        (bb.minPoint.x, bb.minPoint.y, bb.maxPoint.x, bb.maxPoint.y))


def _slot(face):
    """A straight slot floor too narrow for the 3.175 end mill, open at one end
    (the floor drops away there, into a deeper cut) and closed at the other:
    its small end mill (as wide as the slot), the centreline from just past the
    open end to the centre of the closed end's round, and those ends as (x, y)
    in cm. None for any other floor."""
    lines = sorted((e for e in face.edges if isinstance(e.geometry, adsk.core.Line3D)),
                   key=lambda e: -e.length)
    if len(lines) < 2:
        return None
    a0, a1 = lines[0].geometry.startPoint, lines[0].geometry.endPoint
    b0, b1 = lines[1].geometry.startPoint, lines[1].geometry.endPoint
    ux, uy = a1.x - a0.x, a1.y - a0.y
    n = math.hypot(ux, uy)
    vx, vy = b1.x - b0.x, b1.y - b0.y
    if n < EPS or abs(ux * vy - uy * vx) > 1e-3 * n * math.hypot(vx, vy):
        return None  # the longest two edges are not parallel
    ux, uy = ux / n, uy / n
    nx, ny = -uy, ux
    sa, sb = a0.x * nx + a0.y * ny, b0.x * nx + b0.y * ny
    width = abs(sa - sb)
    mid = (sa + sb) / 2
    tool = None
    for desc, d, flute in SMALL_ENDMILLS:
        if width - 0.002 <= d / 10 <= width + 1e-4:
            tool = (desc, d, flute)
    if tool is None:
        return None
    # the two ends along the slot: open where no wall rises from the floor
    rising = {e.tempId for e in _rising_edges(face)}
    ts, end_rising = [], {}
    for e in face.edges:
        pts = _edge_points(e)
        t = [x * ux + y * uy for x, y in pts]
        ts += t
        if isinstance(e.geometry, adsk.core.Line3D):
            g = e.geometry
            dx, dy = g.endPoint.x - g.startPoint.x, g.endPoint.y - g.startPoint.y
            if abs(dx * uy - dy * ux) < 1e-3 * math.hypot(dx, dy):
                continue  # a side of the slot
        end_rising.setdefault(sum(t) / len(t), []).append(e.tempId in rising)
    t0, t1 = min(ts), max(ts)
    closed = {k: any(v) for k, v in end_rising.items()}
    lo = any(c for t, c in closed.items() if t - t0 < (t1 - t0) / 2)
    hi = any(c for t, c in closed.items() if t - t0 >= (t1 - t0) / 2)
    if lo == hi:
        return None  # closed at both ends, or open at both
    r = tool[1] / 20
    t_open, t_end = (t0 - r - 0.03, t1 - r) if hi else (t1 + r + 0.03, t0 + r)
    at = lambda t: (mid * nx + t * ux, mid * ny + t * uy)
    return {'tool': tool, 'start': at(t_open), 'end': at(t_end)}


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
    if plan['outline']:
        tabs = _place_tabs(pieces, width)
        if tabs is None:
            raise RuntimeError('no straight or arc edge of the outline is long enough for a '
                               '%g mm tab' % plan['tab']['width'])
    else:
        tabs = {i: [] for i in range(len(pieces))}

    sk = root.sketches.add(root.xYConstructionPlane)
    sk.name = LAYOUT_SKETCH
    sk.isVisible = False
    segs = []  # (curves, is_tab, start point, direction)
    for i, pc in enumerate(pieces if plan['outline'] else []):
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
    chains, run = [], []
    if segs:
        first_tab = [k for k, sg in enumerate(segs) if sg[1]][0]
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
    if corner:
        entry = corner[0]['entry']
    else:
        # no outline cut: the facing enters beside the part, where the stock
        # ends, one radius out from the middle of the longest edge running along
        # Y, away from the jaws, which grip the edges running along X
        def along_y(pc):
            d = _direction(pc, pc['len'] / 2, pc['len'])
            return abs(d.y) > 0.9, pc['len']
        pc = max(pieces, key=along_y)
        entry = sk.sketchPoints.add(outside(_flat(_point_on(pc, pc['len'] / 2)),
                                            _direction(pc, pc['len'] / 2, pc['len'])))
    ends = _end_chains(sk, a, pieces) if plan.get('ends') else []

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
    # one entry per through window, drilled through the part from the top: at its
    # centre, where a pocket mills the window; or, with too little room there for a
    # pocket to enter, on the path of a contour that cuts it
    window_entries = []  # {'point', 'pocket': True for a pocket, False for a contour} or None
    for w in a['windows']:
        c, x, y, q = _window_entry(w['loop'])
        if c >= ENTRY_R:
            window_entries.append({'point': sk.sketchPoints.add(adsk.core.Point3D.create(x, y, 0)),
                                   'pocket': True})
        elif c >= TOOL_R:
            x, y = _on_contour(c, x, y, q)
            window_entries.append({'point': sk.sketchPoints.add(adsk.core.Point3D.create(x, y, 0)),
                                   'pocket': False})
        else:
            window_entries.append(None)
            warnings.append('window at z=%g mm is narrower than the end mill; it gets no '
                            'entry hole' % w['z'])
    shelves = _shelf_regions(root, a, sk, pocket_entries)
    _merge_entries(pocket_entries)
    # a pocket entry on a window's through hole, on either side, enters there undrilled
    for we in window_entries:
        if we is None:
            continue
        wp = we['point'].geometry
        for e in pocket_entries.values():
            p = e['point'].geometry
            if e['drill'] and math.hypot(p.x - wp.x, p.y - wp.y) < 0.05:
                e['point'], e['drill'] = we['point'], False
    # a window under a shelf region is cleared down to the region's floor by the
    # region's pocket; the window starts there
    window_tops = []
    for w, we in zip(a['windows'], window_entries):
        best = None
        if we is not None:
            wp = we['point'].geometry
            for r in {id(r): r for r in shelves.values()}.values():
                if r['side'] == 'top' and _in_profile(r['rings'], wp.x, wp.y) and \
                        r['z'] < w['z'] - 1e-6 and (best is None or r['z'] < best['z']):
                    best = r
        window_tops.append(best['face'] if best else None)
    # slots the 3.175 end mill can't enter: a small end mill as wide as the slot
    # cuts along it from the open end, whose stock a deeper cut has cleared, and
    # then mills each shallower pocket the slot's closed end reaches into, entering
    # at that end
    slots = []
    for f in a['floors']:
        if f['role'] != 'pocket' or f['side_open']:
            continue
        sl = _slot(f['face'])
        if sl is None or sl['tool'][2] < f['depth'] + 0.5:
            continue
        (sx, sy), (ex, ey) = sl['start'], sl['end']
        line = sk.sketchCurves.sketchLines.addByTwoPoints(
            adsk.core.Point3D.create(sx, sy, 0), adsk.core.Point3D.create(ex, ey, 0))
        fed = []
        r = sl['tool'][1] / 20
        for g in a['floors']:
            if g is f or g['role'] != 'pocket' or g['side'] != f['side'] or \
                    g['depth'] >= f['depth'] - 1e-6:
                continue
            gb = [lp for lp in g['face'].loops if lp.isOuter][0].boundingBox
            if not (gb.minPoint.x < ex < gb.maxPoint.x and gb.minPoint.y < ey < gb.maxPoint.y):
                continue
            pt = adsk.core.Point3D.create(ex, ey, g['face'].pointOnFace.z)
            mm_ = adsk.core.Application.get().measureManager
            if min(mm_.measureMinimumDistance(pt, e).value for e in g['face'].edges) <= r + 0.001:
                fed.append(g)
        slots.append({'floor': f, 'side': f['side'], 'tool': sl['tool'], 'line': line,
                      'end': line.endSketchPoint, 'fed': fed})
    # pockets open to the side, cut by contour passes along their walls, 0.6 x
    # the end mill's diameter apart, as many as cover the floor. Each pass runs
    # on at both ends to the outline cut's path, where the outline cut has
    # cleared the stock for its plunge.
    ring = _loop_ring(loop)
    open_passes = {}
    if plan.get('recipes'):
        a_floors = []  # left to the recipe
    else:
        a_floors = a['floors']
    step = 0.6 * ENDMILL_D / 10
    for f in a_floors:
        if f['role'] != 'pocket' or not f['side_open']:
            continue
        runs, reach = _open_walls(f['face'])
        n = max(1, math.ceil((reach + 0.01 - 2 * TOOL_R) / step) + 1)
        paths = []
        for run in runs:
            walls = _draw_walls(sk, run)
            for k in range(n):
                pth = _pass_path(sk, run, walls, TOOL_R + k * step, ring, f['face'])
                if pth is None:
                    warnings.append('a pass along the walls of the pocket open to the side '
                                    'at z=%g mm does not end on the outline cut; it is left '
                                    'out, so check that pocket' % f['z'])
                else:
                    paths.append(pth)
                    pid = 'build-%d-%d' % (a['floors'].index(f), len(paths) - 1)
                    for j, c in enumerate(pth):  # so coverage can read the passes
                        c.attributes.add(PATH_ATTR[0], PATH_ATTR[1], '%s:%d' % (pid, j))
            for c in walls:
                if c.isValid:
                    c.deleteMe()
        open_passes[f['face'].tempId] = paths
    # the holes' centres, where a pocket that cuts a hole enters (hole_pocket)
    hole_centres = {h['face'].tempId: sk.sketchPoints.add(adsk.core.Point3D.create(
                        h['x'] / 10, h['y'] / 10, 0)) for h in a['holes']}
    return {'sketch': sk, 'corner': corner, 'tabs': tab_chains, 'shelves': shelves,
            'entry': entry, 'hole_centres': hole_centres, 'ends': ends, 'pocket_entries': pocket_entries, 'narrow': narrow,
            'outline': pieces,
            'window_entries': window_entries, 'window_tops': window_tops, 'slots': slots,
            'open_passes': open_passes}


def _end_chains(sk, a, pieces):
    """The cuts that free the ends of a part the vise holds by its sides (the
    sides the stock fits, along the design's X; see _strategy): the runs of the
    outline between those sides, sketched, each run on straight past the
    stock's sides far enough that the end mill starts and ends clear of it. The
    outline must lie on the gripped sides wherever the jaws reach."""
    bb = a['body'].boundingBox
    ylo, yhi = bb.minPoint.y, bb.maxPoint.y
    ends = lambda pc: (_flat(_point_on(pc, 0)), _flat(_point_on(pc, pc['len'])))

    def side_of(pc):  # the gripped side a straight piece lies on, or None
        if not isinstance(pc['edge'].geometry, adsk.core.Line3D):
            return None
        p, q = ends(pc)
        return next((y for y in (ylo, yhi) if abs(p.y - y) < 1e-4 and abs(q.y - y) < 1e-4), None)
    sides = [side_of(pc) for pc in pieces]
    cx = (bb.minPoint.x + bb.maxPoint.x) / 2
    lo, hi = cx - FX_JAW_WIDTH / 20, cx + FX_JAW_WIDTH / 20
    for y in (ylo, yhi):
        spans = sorted(tuple(sorted((p.x, q.x))) for pc, sy in zip(pieces, sides) if sy == y
                       for p, q in [ends(pc)])
        reach = lo
        for x0, x1 in spans:
            if x0 <= reach + 1e-4:
                reach = max(reach, x1)
        if reach < hi - 1e-4:
            raise RuntimeError("the part's side at y = %g mm is not straight over the jaws' "
                               '%.1f mm width, so the vise cannot hold it there; give stock '
                               'larger than the part on every side' % (mm(y), FX_JAW_WIDTH))
    k0 = next(k for k, sy in enumerate(sides) if sy is not None)
    runs, run = [], []
    for i in list(range(k0, len(pieces))) + list(range(k0)):
        if sides[i] is not None:
            if run:
                runs.append(run)
            run = []
        else:
            run.append(i)
    if run:
        runs.append(run)
    off = 2 * TOOL_R + 0.1  # cm: the tool's edge 1 mm clear of the stock
    out = []
    for run in runs:
        curves = [c for i in run for c in _sketch_curve(sk, pieces[i], 0, pieces[i]['len'])]
        p0, p1 = ends(pieces[run[0]])[0], ends(pieces[run[-1]])[1]

        def past(p):
            s = 1 if abs(p.y - yhi) < abs(p.y - ylo) else -1
            return adsk.core.Point3D.create(p.x, p.y + s * off, 0)
        a0, a1 = past(p0), past(p1)
        lines = sk.sketchCurves.sketchLines
        first, last = lines.addByTwoPoints(a0, p0), lines.addByTwoPoints(p1, a1)
        d = adsk.core.Vector3D.create(p0.x - a0.x, p0.y - a0.y, 0)
        d.normalize()
        xs = [a0.x, a1.x] + [q.x for i in run for q in ends(pieces[i])]
        out.append({'curves': [first] + curves + [last], 'start': a0, 'dir': d,
                    'box': (min(xs), min(a0.y, a1.y), max(xs), max(a0.y, a1.y))})
    return out


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


def _ring_clearance(rings, x, y):
    """Distance from (x, y) to the nearest side of a profile's rings."""
    best = None
    for r in [rings[0]] + rings[1]:
        for (x0, y0), (x1, y1) in zip(r, r[1:] + r[:1]):
            dx, dy = x1 - x0, y1 - y0
            t = 0.0 if dx == dy == 0 else \
                max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy)))
            d = math.hypot(x - x0 - t * dx, y - y0 - t * dy)
            best = d if best is None else min(best, d)
    return best


def _shelf_regions(root, a, sk, pocket_entries):
    """For each shelf floor, the pocket opening just above it, sketched as a closed chain.

    A shelf is pocketed with the deeper pocket it steps down into, as one region
    bounded by the walls above the shelf, the way the lab selects such pockets by
    hand. The region is entered at its centre, or its most open point if the end
    mill does not fit there; that lands in the deeper pocket and is drilled to the
    shelf's depth. A shelf the region reaches only through a gap narrower than the
    end mill gets no hole of its own: the small end mill's rest machining comes in
    through the gap. Each region's 'keys' name its entries in pocket_entries.
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
        sec.isVisible = False
        sec.intersectWithSketchPlane([a['body']])
        # read every profile before adding to the layout sketch: a change to the
        # design invalidates the section's profile objects
        profiles = [(p, _profile_rings(p), p.areaProperties().area) for p in sec.profiles]
        regions = {}  # profile index -> (region, entry (x, y) or None)
        placed = []
        for f in floors:
            p0 = f['face'].pointOnFace
            hit = [(k, r) for k, (p, r, _) in enumerate(profiles)
                   if r[0] and _in_profile(r, p0.x, p0.y)]
            if not hit:
                continue
            k, rings = min(hit, key=lambda kr: profiles[kr[0]][2])
            if k not in regions:
                curves = [pc.sketchEntity for pc in rings[2].profileCurves]
                ctr = _centroid(rings[0])
                if ctr is not None and _in_profile(rings, *ctr) and \
                        _ring_clearance(rings, *ctr) >= ENTRY_R:
                    pt = ctr
                else:
                    xs, ys = [q[0] for q in rings[0]], [q[1] for q in rings[0]]
                    c, x, y = _most_open_point(lambda x, y: _in_profile(rings, x, y),
                                               rings[0] + [q for r in rings[1] for q in r],
                                               (min(xs), min(ys), max(xs), max(ys)))
                    pt = (x, y) if c >= ENTRY_R else None
                # the ring starts along the first profile curve's own direction;
                # the chain starts along its sketch curve's
                r0 = rings[0]
                ccw = sum(r0[i - 1][0] * r0[i][1] - r0[i][0] * r0[i - 1][1]
                          for i in range(len(r0))) > 0
                pc0 = rings[2].profileCurves.item(0)
                s0 = pc0.sketchEntity.startSketchPoint.geometry
                if math.hypot(s0.x - r0[0][0], s0.y - r0[0][1]) > 1e-4:
                    ccw = not ccw
                regions[k] = ({'curves': curves, 'clockwise': not ccw, 'keys': [],
                               'rings': (rings[0], rings[1], None), 'side': side, 'z': z,
                               'face': f['face']}, pt)
            region, pt = regions[k]
            out[f['face'].tempId] = region
            if pt is not None:
                placed.append((f, pt))
        points = {}
        for f, xy in placed:
            if xy not in points:
                points[xy] = sk.sketchPoints.add(adsk.core.Point3D.create(xy[0], xy[1], 0))
            pocket_entries[f['face'].tempId] = {
                'point': points[xy], 'side': side, 'depth': f['depth'], 'drill': True}
            out[f['face'].tempId]['keys'].append(f['face'].tempId)
    return out


def _enclosing(u, units, top, bottom):
    """The deepest shelf region among `units` that holds pocket unit `u` and
    whose floor lies between `top` and `bottom` (None: any depth below `top`),
    or None. A unit is (floor face or None, region or None, depth, ...)."""
    face = u[0] if u[0] is not None else u[1]['face']
    p = face.pointOnFace
    best = None
    for w in units:
        if w is u or w[1] is None or not top + 1e-6 < w[2] < u[2] - 1e-6:
            continue
        if bottom is not None and w[2] > bottom + 1e-6:
            continue
        if _inside(w[1]['rings'][0], p.x, p.y) and (best is None or w[2] > best[2]):
            best = w
    return best


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
    # faced only where the stock stands above the part's face
    facing = [f for f in floors if f['role'] == 'face'] if plan['face'] and m > 1e-6 else []
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

    def hole_below(h):
        """The widest narrower hole that goes on down from a blind hole's floor, on
        its axis (the hole under a counterbore), or None."""
        if h['through']:
            return None
        floor = h['z0'] if top else h['z1']
        near = lambda u, v: abs(u - v) < 1e-3
        below = [g for g in a['holes'] if g['dia'] < h['dia'] - 1e-3
                 and near(g['x'], h['x']) and near(g['y'], h['y'])
                 and near(g['z1'] if top else g['z0'], floor)]
        return max(below, key=lambda g: g['dia']) if below else None

    def pre_drill_ends(h):
        """A bore pre-drill's ends. A blind hole stops at its bottom, but where a
        narrower hole goes on below it (a counterbore), the drill's point goes on
        down until its cone meets the floor at that hole's edge, or the hole's
        own bottom if that comes first and it is blind: as wide as it can be without marking the
        floor. A hole below as wide as the drill takes the tip through."""
        ends = _drill_ends(h['through'])
        g = hole_below(h)
        if g is None:
            return ends
        if g['dia'] >= ENDMILL_D - 1e-3:
            return dict(ends, drillTipThroughBottom='true')
        angle = 130.0 if b.dry else b.tools.tip_angle(DRILL_3175)
        point = g['dia'] / 2 / math.tan(math.radians(angle / 2))
        if not g['through']:
            point = min(point, g['depth'])
        return dict(ends, bottomHeight_offset='%gmm' % -round(point, 3))

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
        match = [d for d in drills if h['dia'] - DRILL_UNDER - 1e-6 <= d[1] <= h['dia'] + DRILL_OVER
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
    # Stock of the part's own size has no outline cut: the facing enters beside
    # the part, where there is no stock, so there is no entry hole to drill.
    outline_depth = {True: plan['margin_top'] + a['ztop'] - band_top,  # from the stock top
                     False: plan['margin_bottom'] + band_bot - a['zbot']}
    entry_seq, through = _stage_plan(0, stock_z, outline_depth[True], reach)
    if not plan['outline']:
        entry_seq = []
    elif not top:
        if through:
            entry_seq = [('mill', 0, outline_depth[False])]
        else:
            entry_seq, ok = _stage_plan(0, outline_depth[False], outline_depth[False], reach)
            if not ok:
                warnings.append('the entry hole cannot be drilled to the outline depth')
    elif not through and plan['sides'] == 1:
        warnings.append('the entry hole cannot be drilled through the stock')
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

    if entry_seq and entry_seq[0][0] == 'drill':
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
    # each entry hole's drill point goes below the pocket floor only until its
    # cone is half the end mill wide at the floor, as the end mill needs to
    # enter there (rule TH4); the rest of the cone is a dimple in the floor
    cone = b.tools.tip_length(DRILL_3175) * ENTRY_CONE_D / DRILL_D
    dimples = [(q, d) for q, d in holes_in if q is not None]
    if dimples:
        where = ', '.join('(%.2f, %.2f)' % (mm(q.geometry.x), mm(q.geometry.y)) for q, _ in dimples)
        warnings.append('the %d pocket entry hole%s on the %s leave%s a cone-shaped dimple %g mm '
                        'across and %.2f mm deep in the pocket floor, so that the end mill '
                        'enters a hole at least half its width; at x, y = %s mm'
                        % (len(dimples), '' if len(dimples) == 1 else 's', side,
                           's' if len(dimples) == 1 else '', ENTRY_CONE_D, cone, where))
        thin = [d for _, d in dimples if thickness - d < cone + 0.2]
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
            # at a floor, the point goes only `cone` below it (see above); a stage
            # of the drill's reach above the floor takes the tip through
            params = dict(DRILL, holeMode="'selection-points'", breakThroughDepth='0mm',
                          drillTipThroughBottom='true', **top_at(t))
            floor = floor_at.get(depth)
            if floor is not None:
                params.update(at('bottom', floor, -round(cone, 3)),
                              drillTipThroughBottom='false')
            else:
                params.update(_bottom_below_part_top(depth, m))
            name = ('Pocket entry holes %g to %g mm' % (round(max(t, 0), 2), depth)
                    if staged_pockets else 'Pocket entry holes %g mm' % depth)
            out.append(b.op(setup, 'drill', drill_for(depth - t), name, params, points(pts)))
        return out

    # bores deeper than the drill reaches are pre-drilled in stages with their
    # rough bore, at the bore; the others are pre-drilled with the entries
    end_bored = [h for h, rough, _ in bored if rough[0] == ENDMILL]
    small_bored = [h for h, rough, _ in bored if rough[0] != ENDMILL]

    def small_pre_drill(h):
        """The widest library drill no wider than the hole less DRILL_UNDER that
        reaches its depth, or None."""
        fits = [d for d in drills if d[1] <= h['dia'] - DRILL_UNDER + 1e-6 and d[2] >= h['depth'] + 0.5]
        return max(fits, key=lambda d: d[1])[0] if fits else None
    deep_bore = lambda h: h['depth'] > reach + 1e-6
    # holes whose rough bore would need more passes than the lab's are pocketed
    pocketed = lambda h: not deep_bore(h) and (h['dia'] - ENDMILL_D) / 2 > BORE_MAX_RADIAL + 1e-6

    def later_drills():
        """T1 after the rough face and outline: tab entries from the tab band's
        top, which the outline has cut down to there, and pocket entries and
        bore pre-drills from the face, on the material left for its finish."""
        out = []
        if top and plan['outline'] and (lay is None or len(lay['corner']) > 1):
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
        pts = [e['point'] for e in (window_entry(w) for w in windows) if e is not None]
        if pts or (lay is None and windows):
            params = dict(DRILL, holeMode="'selection-points'", breakThroughDepth='0mm',
                          drillTipThroughBottom='true', **top_at(0))
            params.update(through_part('bottom'))
            out.append(b.op(setup, 'drill', drill_for(thickness + 0.5), 'Window entry holes',
                            params, points(pts)))
        # pre-drill every bore: T1 for those the 3.175 end mill bores, and for those
        # a small end mill bores, the widest drill that fits the hole, so that no
        # end mill enters solid copper (rule TH4)
        pre = [(h, DRILL_3175) for h in end_bored + [h for h, _ in tap_bored] if not deep_bore(h)]
        for h in small_bored:
            d = small_pre_drill(h)
            if d is None:
                warnings.append('no drill fits the %.2f mm hole at (%g, %g) and reaches its depth: '
                                'its bore enters solid copper' % (h['dia'], h['x'], h['y']))
            else:
                pre.append((h, d))
        groups = {}
        for h, d in pre:
            ends = pre_drill_ends(h)
            groups.setdefault((d, h['through'], tuple(sorted(ends.items()))), (ends, []))[1].append(h)
        for (d, through, _), (ends, hs) in sorted(groups.items(),
                                                  key=lambda kv: (kv[0][0] != DRILL_3175,
                                                                  kv[0][0], not kv[0][1])):
            name = 'Pre-drill bores' if d == DRILL_3175 else 'Pre-drill bores %s' % d.split('*')[0]
            out.append(b.op(setup, 'drill',
                            drill_for(max(h['depth'] for h in hs)) if d == DRILL_3175 else d,
                            name + ('' if through else ' (blind)'),
                            dict(DRILL, **ends, **from_hole_top()),
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
                      **top_at(-m))
        if leave:
            params.update(useStockToLeave='true', simpleStockToLeave='false',
                          stockToLeave='0mm', verticalStockToLeave='%gmm' % leave)
        # The patch over the face, not the face: see _add_face_patches. The rough
        # face selects the patch's edge, not the patch: nothing walls a selected
        # face in, so Fusion takes it for an open pocket and runs the tool past
        # its edge. The finishing face selects its patch, which stops at the
        # outline: the outline cut has cleared the stock past it, so the tool
        # may run past the edge, and it reaches the entry hole outside it.
        if which == 'finish' and plan.get('face_patches'):
            sel = _sel_pocket([plan['face_patches'][side + ' finish']])
        elif patch:
            sel = _sel_pocket_outline(patch)
        else:
            sel = _sel_pocket([f['face'] for f in facing])
        op = b.op(setup, 'pocket2d', (ENDMILL, which), 'Face' + suffix, params,
                  _both(sel, _sel_faces('predrillPositions', entry)))
        high[0] = leave  # the face is now the stock's highest point
        return op

    def end_cut(which, multi, suffix):
        """The cut that frees the part's ends, when the vise holds it by its
        sides: through the whole stock from its top, starting and ending in the
        air past the stock's sides (see _end_chains), first of all, so that the
        facing enters at the end of the part through its groove."""
        params = dict(CONTOUR, doMultipleDepths=multi, topHeight_mode="'from stock top'",
                      topHeight_offset='0mm', bottomHeight_mode="'from stock bottom'",
                      bottomHeight_offset='-0.5mm')
        return b.op(setup, 'contour2d', (ENDMILL, which), 'Ends' + suffix, params,
                    _sel_layout_chains(lay['ends'] if lay else [], True))

    if plan.get('ends') and top:
        ops['main'].append(end_cut('rough', 'true', ''))
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
                # a hole in a floor: the rough pockets and bores leave 0.2 mm on
                # the floor, which the rough bore's entries would plunge through
                top_h = {'topHeight_mode': "'from hole top'", 'topHeight_offset': '%gmm' % (
                    POCKET_FLOOR_ALLOWANCE if which == 'rough' else 0.0)}
            else:
                top_h = top_at(t0 + s)
            last = e >= h['depth'] - 1e-6
            stage = '' if len(seq) == 1 else ' %g to %g mm' % (round(s, 2), round(e, 2))
            if kind == 'drill':
                p = dict(DRILL, **top_h)
                if last:
                    p.update(pre_drill_ends(h))
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

    # floors the small end mills cut through slots (see below)
    slot_floors = {g['face'].tempId for sl in (lay['slots'] if lay else [])
                   if sl['side'] == side for g in [sl['floor']] + sl['fed']}
    passes = (('rough', 'true', ''), ('finish', 'false', ' (finish)'))

    def hole_pocket(hs, multi, suffix):
        """The rough cut of holes too wide for the lab's bore: a pocket entered
        down the hole T1 pre-drilled at its centre, as the lab cuts its 7 mm
        holes; the finishing bore then cleans the wall."""
        h = hs[0]
        t0 = depth_of(h['z1'] if top else h['z0'])
        rims, bottoms = zip(*(_hole_ends(x, top) for x in hs))
        centres = [(lay or {}).get('hole_centres', {}).get(x['face'].tempId) for x in hs]
        if None in rims or None in centres or (not h['through'] and None in bottoms):
            return None  # no flat face round the rim or at the bottom: bored instead
        params = dict(POCKET, doMultipleDepths=multi)
        # a hole in a floor starts at that floor, where the rough pocket left the allowance
        params.update(top_at(0) if t0 < 1e-6 else at('top', rims[0].face, POCKET_FLOOR_ALLOWANCE))
        if h['through']:
            params.update(through_part('bottom'))
        else:
            # the finishing bore takes the allowance off the floor
            params.update(at('bottom', bottoms[0], POCKET_FLOOR_ALLOWANCE))
        return b.op(setup, 'pocket2d', (ENDMILL, 'rough'), 'Hole pocket %.2f%s' % (h['dia'], suffix),
                    params, _both(_sel_chains(list(rims), 'pockets'),
                                  _sel_faces('predrillPositions', centres)))

    def end_bores(which, multi, suffix):
        for hs in _group(end_bored):
            h = hs[0]
            radial = max(0.1, (h['dia'] - ENDMILL_D) / 2)
            if which == 'rough' and pocketed(h):
                op = hole_pocket(hs, multi, suffix)
                if op is not None or b.dry:
                    ops['main'].append(op)
                    continue
            # the finishing bore is a single pass at the wall; in a blind hole the
            # rough bore stops the floor allowance above the floor for it to take
            # off (user, 2026-10-17); a bore has no axial stock to leave
            params = dict(BORE, stepover='0.3mm',
                          numberOfStepovers=str(max(1, math.ceil(radial / 0.3))),
                          doMultiplePasses=multi)
            bottom = -0.5 if h['through'] else (
                POCKET_FLOOR_ALLOWANCE if which == 'rough' else 0.0)
            staged_bore(hs, which, (ENDMILL, which), params,
                        bottom, 'Bore %.2f%s' % (h['dia'], suffix))

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

    pocket_floors = {f['face'].tempId for f in pockets}

    def in_floor(h):
        rim = _hole_ends(h, top)[0]
        return rim is not None and rim.face.tempId in pocket_floors

    def drill_ops(floor):
        """The drills (plain holes, then tap drills), grouped by tool, of the
        holes that open in a pocket floor (floor true) or of the others. Those in
        a floor are drilled before the finishing pockets, so that the finishing
        pass cleans up the burrs the drill leaves on the floor."""
        out = []
        by_drill = {}
        for h, desc in [(h, d) for h, d in drilled if in_floor(h) == floor]:
            by_drill.setdefault(desc, []).append(h)
        for h, t in [(h, t) for h, t in tapped if in_floor(h) == floor]:
            if THREADS[t][1]:
                by_drill.setdefault(THREADS[t][1], []).append(h)
        for desc, hs in by_drill.items():
            for through in (True, False):
                sub = [h for h in hs if h['through'] == through]
                if not sub:
                    continue
                params = dict(DRILL, **_drill_ends(through), **from_hole_top())
                if floor:
                    # the floor still carries the rough pocket's allowance
                    params['topHeight_offset'] = '%gmm' % POCKET_FLOOR_ALLOWANCE
                if not through:
                    # full diameter to the bottom: the drill's point goes below it
                    params['drillTipThroughBottom'] = 'true'
                    blind_tip_warning(desc, sub)
                out.append(b.op(
                    setup, 'drill', desc,
                    'Drill ' + desc.split('*')[0] + ('' if through else ' (blind)'),
                    params, _sel_faces('holeFaces', [h['face'] for h in sub])))
        return out

    for which, multi, suffix in passes:
        tk = (ENDMILL, which)
        # outline: the top setup cuts down to the tab band, the bottom setup up to it
        bands = entry_seq if which == 'rough' else [('mill', 0, outline_depth[top])]
        if plan.get('ends') and top and which == 'finish':
            ops['main'].append(end_cut(which, multi, suffix))
        for kind, s, e in bands if plan['outline'] else []:
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
        if which == 'finish':
            ops['main'] += drill_ops(True)
        if pockets:
            shelves = lay['shelves'] if lay else {}
            # what a pocket op selects: the plain floors, and one chain per shelf
            # region, though several shelves share it; each with its depth and entry
            units = []  # (floor face or None, chain or None, depth, [entry points], side-open)
            for f in pockets:
                if f in narrow:
                    continue
                e = side_entries.get(f['face'].tempId)
                c = shelves.get(f['face'].tempId)
                if c is None and f['face'].tempId in slot_floors:
                    continue  # the 3.175 can't get in; a small end mill cuts it through its slot
                if c is None:
                    units.append((f['face'], None, f['depth'],
                                  [e['point']] if e and e['point'] else [], f['side_open']))
                elif all(c is not u[1] for u in units):
                    units.append((None, c, f['depth'],
                                  [side_entries[k]['point'] for k in c['keys']
                                   if k in side_entries and side_entries[k]['point']], False))
            has_open = any(u[4] for u in units) and not all(u[4] for u in units)
            has_entries = any(f['face'].tempId in side_entries for f in pockets
                              if f not in narrow)
            bands = pocket_bands if which == 'rough' else [(-allow, None, [])]
            for k, band in enumerate(bands):
                t, bot, _ = band
                if k:
                    ops['main'] += entry_drills(band)
                inside = [u for u in units if u[2] > t + 1e-6]
                # the pockets this band reaches the floor of, then the deeper ones;
                # the closed ones, entered at their holes, then those open to the side,
                # which the end mill enters from the cut beside the opening
                for floor, side_open in ((True, False), (True, True), (False, False), (False, True)):
                    us = [u for u in inside if (bot is None or u[2] <= bot + 1e-6) == floor
                          and u[4] == side_open]
                    if not us:
                        continue
                    params = dict(POCKET, doMultipleDepths=multi,
                                  rampType="'predrill'" if has_entries else "'plunge'",
                                  **top_at(t))
                    if floor:
                        params['bottomHeight_mode'] = "'from contour'"
                        if which == 'rough':
                            # the finishing pocket takes the last of the floor
                            params.update(useStockToLeave='true', simpleStockToLeave='false',
                                          stockToLeave='0mm',
                                          verticalStockToLeave='%gmm' % POCKET_FLOOR_ALLOWANCE)
                    else:
                        params.update(_bottom_below_part_top(bot, m))
                    name = ('Open pockets' if side_open and has_open else 'Pockets') + suffix
                    if staged_pockets and which == 'rough':
                        name += (' %g mm to the floors' % round(max(t, 0), 2) if floor
                                 else ' %g to %g mm' % (round(max(t, 0), 2), round(bot, 2)))
                    if side_open and not plan.get('recipes'):
                        # Fusion picks where a pocket enters an open pocket, and it
                        # plunges with the tool's edge in uncut stock: contour passes
                        # along the walls instead, on the tool-centre paths sketched
                        # for them (see _make_layout), one operation per floor depth
                        by_depth = {}
                        for u in us:
                            by_depth.setdefault(round(u[2], 3), []).append(u)
                        for dep, grp in sorted(by_depth.items()):
                            paths = [pth for u in grp
                                     for pth in (lay['open_passes'].get(u[0].tempId, [])
                                                 if lay else [])]
                            if not paths and lay:
                                continue
                            cparams = dict(CONTOUR, doMultipleDepths=multi,
                                           compensationType="'off'", **top_at(t))
                            cparams.update({k: v for k, v in params.items() if k.startswith((
                                'useStockToLeave', 'simpleStockToLeave', 'stockToLeave',
                                'verticalStockToLeave'))})
                            if floor:
                                cparams.update(at('bottom', grp[0][0]))
                            else:
                                cparams.update({k: v for k, v in params.items()
                                                if k.startswith('bottomHeight')})
                            nm = name if len(by_depth) == 1 else name + ' %g mm' % dep
                            ops['main'].append(b.op(setup, 'contour2d', tk, nm, cparams,
                                                    _sel_paths(paths)))
                        continue
                    if side_open:
                        # cut after build by the open-pocket recipe
                        # (references/recipes/open-pocket.md): record where its
                        # operations go and the heights and allowance they take
                        if not b.dry:
                            anchor = next((o for o in reversed(ops['main']) if o is not None), None)
                            rp = dict(doMultipleDepths=multi, **top_at(t))
                            rp.update({k: v for k, v in params.items() if k.startswith((
                                'useStockToLeave', 'simpleStockToLeave', 'stockToLeave',
                                'verticalStockToLeave'))})
                            if not floor:
                                rp.update({k: v for k, v in params.items()
                                           if k.startswith('bottomHeight')})
                            # a height's reference faces, as entity tokens
                            rp = {k: ({'entity_tokens': [x.entityToken for x in v]}
                                      if isinstance(v, list) else v) for k, v in rp.items()}
                            # one operation per floor depth
                            by_depth = {}
                            for u in us:
                                i = next(k for k, g in enumerate(a['floors']) if g['face'] is u[0])
                                by_depth.setdefault(round(u[2], 3), []).append(i)
                            for dep, idx in sorted(by_depth.items()):
                                plan.setdefault('recipe_entries', []).append({
                                    'recipe': 'open-pocket', 'floors': idx, 'depth_mm': dep,
                                    'pass': which, 'setup': setup.name,
                                    'name': name if len(by_depth) == 1 else name + ' %g mm' % dep,
                                    'after': anchor.name if anchor else None,
                                    'tool_from': b.last[tk].name if tk in b.last else None,
                                    'bottom_at_floor': idx[0] if floor else None, 'params': rp})
                        continue
                    # a region inside a shallower one (a shelf's opening holds the
                    # deeper pockets) is roughed from that one's rough floor, since
                    # the end mill has cleared it down to there; from the top it
                    # would mill air. The finishing pass cuts a shelf region that
                    # holds deeper ones on its own, with stock contours leaving
                    # them out: they are roughed below its floor by then, so
                    # sweeping them at that floor would mill air.
                    groups = [(None, us, [])]  # (enclosing unit, units, units left out)
                    if which == 'rough' and floor:
                        groups = []
                        for u in us:
                            w = _enclosing(u, inside, t, bot)
                            g = next((g for g in groups if g[0] is w), None)
                            if g is None:
                                groups.append((w, [u], []))
                            else:
                                g[1].append(u)
                        groups.sort(key=lambda g: -1 if g[0] is None else g[0][2])
                    elif floor:
                        held = [(u, [w for w in inside if _enclosing(w, inside, t, bot) is u])
                                for u in us if u[1] is not None]
                        held = [(u, ws) for u, ws in held if ws]
                        rest = [u for u in us if all(u is not h for h, _ in held)]
                        groups = [(u, [u], ws) for u, ws in sorted(held, key=lambda h: h[0][2])]
                        if rest:
                            groups.append((None, rest, []))
                    for parent, gu, out in groups:
                        gp, nm, sel = params, name, None
                        if out:
                            gp = dict(params, useStockContours='true')
                            nm = name + ' at %g mm' % round(parent[2], 2)
                            sel = _sel_stock_contours([parent[1]], [w[1] for w in out if w[1]],
                                                      [w[0] for w in out if not w[1]], top)
                        elif parent is not None:
                            gp = dict(params, **at('top', parent[1]['face'], POCKET_FLOOR_ALLOWANCE))
                            nm = name + ' from %g mm' % round(parent[2], 2)
                        pts = []
                        for u in gu:
                            for p in u[3]:
                                if all(p is not q for q in pts):
                                    pts.append(p)
                        ops['main'].append(b.op(setup, 'pocket2d', tk, nm, gp, _both(
                            _sel_pocket_regions([u[0] for u in gu if u[0] is not None],
                                                [u[1] for u in gu if u[1] is not None], top),
                            _sel_faces('predrillPositions', pts), sel)))
        if which == 'rough':
            end_bores(which, multi, suffix)
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
                e = window_entry(w)
                below = lay['window_tops'][a['windows'].index(w)] if lay else None
                start = (at('top', below) if below is not None else
                         top_at(0) if depth_of(w['z']) < 1e-6 else at('top', w['face']))
                if which == 'rough' and (e is None or e['pocket']):
                    # a pocket from the through hole at the window's centre; the
                    # finishing pass, with the middle cleared, a contour
                    params = dict(POCKET, doMultipleDepths=multi)
                    params.update(start)
                    params.update(through_part('bottom'))
                    ops['main'].append(b.op(setup, 'pocket2d', tk, 'Window' + suffix, params, _both(
                        _sel_chains([w['loop']], 'pockets'),
                        _sel_faces('predrillPositions', [e['point']] if e else []))))
                else:
                    params = dict(CONTOUR, doMultipleDepths=multi)
                    params.update(start)
                    params.update(through_part('bottom'))
                    ops['main'].append(b.op(setup, 'contour2d', tk, 'Window' + suffix, params, _both(
                        _sel_chains([w['loop']]),
                        _sel_faces('entryPositions', [e['point']]) if e else None)))
        # top setup: the outline between the tabs, down through the tab band
        if top and plan['outline']:
            params = dict(CONTOUR, doMultipleDepths=multi)
            params.update(_top_below_part_top(a['ztop'] - band_top, m))
            params.update(_bottom_below_part_top(a['ztop'] - chain_bot, m))
            hi, set_hi = _at_tab('top', tab_top)
            lo, set_lo = _at_tab('bottom', tab_bottom, -tab['overlap'])
            params.update(hi, **lo)
            ops['main'].append(b.op(setup, 'contour2d', tk, 'Outline between tabs' + suffix,
                                    params, _both(_sel_layout_chains(lay['corner'] if lay else [],
                                                                     True), set_hi, set_lo)))

    # slots the 3.175 can't enter, cut along their centreline by an end mill as wide
    # as the slot from the open end, and the pockets they reach, entered at the
    # slot's closed end; one operation of each per tool
    slots = [sl for sl in (lay['slots'] if lay else []) if sl['side'] == side]
    by_tool = {}
    for sl in slots:
        by_tool.setdefault(sl['tool'], []).append(sl)
    slotted = set()
    for (em, d, _), sls in by_tool.items():
        params = dict(CONTOUR, doMultipleDepths='true', compensationType="'off'", **top_at(0))
        params.update(at('bottom', sls[0]['floor']['face']))
        if any(abs(sl['floor']['depth'] - sls[0]['floor']['depth']) > 1e-6 for sl in sls):
            warnings.append('slots of one width at different depths; check the slot cut')
        ops['main'].append(b.op(setup, 'contour2d', em, 'Slots %g' % d, params,
                                _sel_open_lines([sl['line'] for sl in sls])))
        fed = [g for sl in sls for g in sl['fed']]
        if fed:
            ops['main'].append(b.op(setup, 'pocket2d', em, 'Pockets through slots %g' % d, dict(
                POCKET, doMultipleDepths='true', bottomHeight_mode="'from contour'", **top_at(0)),
                _both(_sel_pocket([g['face'] for g in fed]),
                      _sel_faces('predrillPositions', [sl['end'] for sl in sls if sl['fed']]))))
        slotted |= {sl['floor']['face'].tempId for sl in sls}
        slotted |= {g['face'].tempId for g in fed}

    # corners the 3.175 can't reach, in pockets and windows, with small end mills:
    # by build itself, or after build by the corners recipe
    # (references/recipes/corners.md), which build records what it needs for.
    # A floor inside a shelf region has walls only up to that region's floor:
    # above it the region's pocket has cleared everything, its corners too, so
    # its corner cut starts there
    shelves = lay['shelves'] if lay else {}
    regions = []
    for f in pockets:
        c = shelves.get(f['face'].tempId)
        if c is not None and all(c is not w[1] for w in regions):
            regions.append((None, c, f['depth']))

    def corner_top(f):
        """The enclosing shelf region (or None) and the corner cut's top height."""
        w = _enclosing((f['face'], None, f['depth']), regions, 0.0, None)
        return w, (at('top', w[1]['face']) if w else top_at(0))

    if not plan.get('recipes'):
        # small end mills: rest machining in pockets whose corners the 3.175 can't reach,
        # one operation per tool
        def corner_tool(r, depth):
            # a small end mill already chosen for corners, on either side, where one
            # fits, so the part needs fewer tools; else the largest that fits
            used = plan.setdefault('corner_endmills', set())
            for desc, d, flute in SMALL_ENDMILLS:
                if desc in used and d / 2 <= r + 1e-3 and flute >= depth + 0.5:
                    return desc, d
            em, d = _small_endmill(r, depth)
            if em:
                used.add(em)
            return em, d

        corners = {}
        for f in pockets:
            if f['face'].tempId in slotted:
                continue
            r = f['corner_radius']
            if r is None or r >= ENDMILL_D / 2 - 1e-3:
                continue
            smallest = SMALL_ENDMILLS[-1][1] / 2
            if r < smallest:
                warnings.append('pocket at z=%g mm has sharp or R%.2f inside corners; the %g mm '
                                'end mill leaves R%g' % (f['z'], r, 2 * smallest, smallest))
            em, d = corner_tool(max(r, smallest), f['depth'])
            if not em:
                warnings.append('pocket at z=%g mm: corner radius %.2f mm needs a smaller tool '
                                'than the library has, or is deeper than its flute' % (f['z'], r))
                continue
            w, top_h = corner_top(f)
            corners.setdefault((em, d, w[2] if w else 0.0), (w, top_h, []))[2].append(f['face'])
        for (em, d, z), (w, top_h, faces) in sorted(corners.items(), key=lambda kv: kv[0][2]):
            name = 'Pocket corners %g' % d
            if w is not None:
                name += ' from %g mm' % round(z, 2)
            ops['main'].append(b.op(setup, 'pocket2d', em, name, dict(
                POCKET, doMultipleDepths='true', rampType="'plunge'", useRestMachining='true',
                bottomHeight_mode="'from contour'", **top_h),
                _sel_pocket(faces)))
        # windows' corners likewise, from the part's face (the shelf pockets above a
        # window leave the same corners) through the part
        for w in windows:
            r = w['corner_radius']
            if r is None or r >= ENDMILL_D / 2 - 1e-3:
                continue
            smallest = SMALL_ENDMILLS[-1][1] / 2
            if r < smallest:
                warnings.append('window has sharp or R%.2f inside corners; the %g mm end mill '
                                'leaves R%g' % (r, 2 * smallest, smallest))
            em, d = corner_tool(max(r, smallest), thickness + 0.5)
            if not em:
                warnings.append('window: corner radius %.2f mm needs a smaller tool than the '
                                'library has, or is deeper than its flute; the 3.175 end mill '
                                'leaves material in its corners' % r)
                continue
            params = dict(POCKET, doMultipleDepths='true', rampType="'plunge'",
                          useRestMachining='true', **top_at(0))
            params.update(through_part('bottom'))
            ops['main'].append(b.op(setup, 'pocket2d', em, 'Window corners %g' % d, params,
                                    _sel_chains([w['loop']], 'pockets')))
    elif not b.dry:
        def tokens(params):
            return {k: ({'entity_tokens': [x.entityToken for x in v]} if isinstance(v, list) else v)
                    for k, v in params.items()}
        feats = []
        for f in pockets:
            r = f['corner_radius']
            if f['face'].tempId in slotted or r is None or r >= ENDMILL_D / 2 - 1e-3:
                continue
            w, top_h = corner_top(f)
            feats.append({'kind': 'pocket', 'floor': next(k for k, g in enumerate(a['floors'])
                                                          if g is f),
                          'corner_radius_mm': r, 'depth_mm': f['depth'],
                          'top_mm': round(w[2], 3) if w else 0.0,
                          'params': tokens(dict(bottomHeight_mode="'from contour'", **top_h))})
        for w in windows:
            r = w['corner_radius']
            if r is None or r >= ENDMILL_D / 2 - 1e-3:
                continue
            feats.append({'kind': 'window', 'window': a['windows'].index(w),
                          'corner_radius_mm': r, 'depth_mm': thickness,
                          'params': tokens(dict(top_at(0), **through_part('bottom')))})
        if feats:
            anchor = next((o for o in reversed(ops['main']) if o is not None), None)
            plan.setdefault('recipe_entries', []).append({
                'recipe': 'corners', 'setup': setup.name,
                'after': anchor.name if anchor else None, 'features': feats})
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

    # drills (plain holes, then tap drills) of the holes not in a pocket floor
    ops['main'] += drill_ops(False)

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

    # the finishing bores after the drills and thread mills, to clean up the
    # burrs they leave on the bores' floors and edges (user, 2026-10-17); tap holes
    # bored to size keep theirs before the thread mill, which needs the final size
    end_bores('finish', 'false', ' (finish)')

    # the finishing face, after every cut that leaves burrs on the face
    if facing:
        ops['main'].append(face_op('finish', 'false', ' (finish)', 0))

    # deburr the outer face's edges, the bit's point kept off the shallowest floor
    # and blind hole bottom below the face, and off the tabs, which still stand in
    # the outline cut; Fusion leaves out any edge where the point would go deeper
    if face is not None and plan['chamfer']:
        width = float(CHAMFER_OP['chamferWidth'][:-2])
        room = [f['depth'] for f in floors if f['role'] == 'pocket']
        room += [h['depth'] for h in holes if not h['through']]
        if plan['outline']:
            room.append(a['ztop'] - band_top if top else band_bot - a['zbot'])
        room.append(CHAMFER_TIP_MAX + width + CHAMFER_CLEAR)
        tip = max(CHAMFER_TIP_MIN, min(CHAMFER_TIP_MAX, min(room) - width - CHAMFER_CLEAR))
        if width + tip > min(room) - 0.02:
            warnings.append('a floor or the tabs lie %.2f mm below the %s face, too close for '
                            'a %g mm chamfer: the Deburr leaves those edges out or cuts the tabs'
                            % (min(room), side, width))
        # the stock's own sides, where it fits the part, are left as they are when
        # the face is not faced: no tool cuts them, so they have no burrs
        axes = [] if plan['outline'] or facing else ['y'] if plan['ends'] else ['x', 'y']
        raw = _raw_edges(face, body, axes) if axes else set()
        ops['main'].append(b.op(setup, 'chamfer2d', CHAMFER, 'Deburr', dict(
            CHAMFER_OP, chamferTipOffset='%gmm' % round(tip, 3)), _sel_deburr(face, raw)))

    # the edges of the holes under counterbores, at the counterbore's floor, which
    # the face's deburr does not reach; the point goes down the hole below
    if plan['chamfer']:
        width = float(CHAMFER_OP['chamferWidth'][:-2])
        by_tip = {}
        for h in holes:
            g = hole_below(h)
            if g is None:
                continue
            fz = (h['z0'] if top else h['z1']) / 10  # cm
            floor = next((f for e in h['face'].edges for f in e.faces
                          if f != h['face'] and isinstance(f.geometry, adsk.core.Plane)
                          and abs(f.pointOnFace.z - fz) < 1e-4), None)
            if floor is None:
                continue
            room = CHAMFER_TIP_MAX + width + CHAMFER_CLEAR if g['through'] else g['depth']
            tip = max(CHAMFER_TIP_MIN, min(CHAMFER_TIP_MAX, room - width - CHAMFER_CLEAR))
            by_tip.setdefault(round(tip, 3), []).append(floor)
        for tip, fl in sorted(by_tip.items()):
            ops['main'].append(b.op(setup, 'chamfer2d', CHAMFER, 'Deburr counterbores', dict(
                CHAMFER_OP, chamferTipOffset='%gmm' % tip), _sel_inner_contours(fl)))

    # the last setup: the tab cut, last and in its own NC program; from the
    # bottom with two sides, from the top with one
    if plan['outline'] and top == (plan['sides'] == 1):
        params = dict(CONTOUR, doMultipleDepths='true', maximumStepdown='0.2mm')
        if top:
            params.update(_top_below_part_top(a['ztop'] - band_top, m))
            params.update(_bottom_below_part_top(a['ztop'] - chain_bot, m))
        else:
            params.update(_top_below_part_top(band_bot - a['zbot'], m))
            params.update(_bottom_below_part_top(band_top + tab['overlap'] - a['zbot'], m))
        hi, set_hi = _at_tab('top', tab_top if top else tab_bottom)
        lo, set_lo = _at_tab('bottom', tab_bottom if top else tab_top, -tab['overlap'])
        params.update(hi, **lo)
        ops['tabs'].append(b.op(setup, 'contour2d', (ENDMILL, 'rough'), 'Cut tabs', params,
                                _both(_sel_layout_chains(lay['tabs'] if lay else [], top),
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
                or o.component.name in (TABS_COMPONENT, FACES_COMPONENT, PART_COMPONENT)]:
        occ.deleteMe()
    attr = design.attributes.itemByName(*LAYOUT_ATTR)
    if attr is not None:
        attr.deleteMe()
    for attr in design.findAttributes(*HIDDEN_ATTR):
        body = adsk.fusion.BRepBody.cast(attr.parent)
        attr.deleteMe()
        if body is not None:
            body.isLightBulbOn = True


def _nc_program(cam, name, ops, post):
    ni = cam.ncPrograms.createInput()
    ni.displayName = name + ' ' + SETUP_TAG
    ni.operations = ops
    _try_set(ni.parameters, 'nc_program_name', "'%s'" % name)
    nc = cam.ncPrograms.add(ni)
    if post is not None:
        nc.postConfiguration = post
    return nc


STOCK_FIT = 0.01  # mm: stock this close to the part's size is the part's size
MIN_FRAME = 3.0   # mm of stock left outside the outline cut in window machining


def _bottom_work(a):
    """What only a bottom setup can cut: pocket floors facing down, and holes open
    only at the bottom. Through holes and windows are cut from the top."""
    return ([f for f in a['floors'] if f['side'] == 'bottom' and f['role'] == 'pocket'],
            [h for h in a['holes'] if h['side'] == 'bottom'])


def _strategy(a, stock, sides, warnings):
    """How the stock is machined into the part.

    outline: the stock is larger than the part in both X and Y, by the outline cut
      and MIN_FRAME on every side, so the part is cut out of it (window
      machining): an outline cut round the part, which leaves four tabs holding
      it, and a last program that cuts the tabs. Stock of the part's own X and Y
      size needs neither; its outline must then be the rectangle the stock is.
      Anything between is refused.
    sides: 2, a top and a flipped bottom setup, when the bottom has features to
      cut or stock to face (stock["bottom"] above 0); else 1, the top setup
      alone, with the part on the stock's bottom. `sides` (1 or 2) overrides.
    """
    size = {'x': a['size'][0], 'y': a['size'][1]}
    for k, s in size.items():
        if stock[k] < s - STOCK_FIT:
            raise RuntimeError('the stock is %g mm along %s but the part is %g mm'
                               % (stock[k], k.upper(), s))
    fit = [k for k, s in size.items() if abs(stock[k] - s) <= STOCK_FIT]
    outline = not fit
    ends = fit == ['y']  # see _quarter_turn: a fit along X is turned to Y first
    if fit == ['x']:
        raise RuntimeError('the stock fits the part along X only; give the stock as the design '
                           'has it, so that build can turn the part')
    if ends:
        # held by its own sides, which the jaws grip; the cuts that free its ends
        # run outside the jaws only if the part is longer than they are wide
        if size['x'] <= FX_JAW_WIDTH + 1e-3:
            raise RuntimeError(
                'the stock fits the part along one side only, so the vise must hold the part '
                'by that side and its ends are cut free; the part is %g mm long that way, which '
                'must exceed the width of the jaws, %.1f mm, so that the end cuts run clear of them. '
                'Give stock larger than the part on every side, or of its own X and Y size'
                % (size['x'], FX_JAW_WIDTH))
    elif len(fit) == 2:
        faces = [f for f in (a['top_face'], a['bottom_face']) if f is not None]
        ring = _outline_loop(faces)[1] if faces else None
        if ring is None or _ring_area(ring) * 100 < size['x'] * size['y'] * (1 - 1e-3):
            raise RuntimeError("the stock is the part's size, but the part's outline is not "
                               'the rectangle the stock is, so it still needs an outline cut: '
                               'give stock larger than the part, which is then cut out of it')
    else:
        # window machining: the outline cut, one end mill wide, must leave a frame of
        # stock round it on every side, to hold the part through its tabs and to
        # take the vise's grip
        y_off = stock.get('y_offset', 10)
        spare_y = stock['y'] - size['y']
        mode = stock.get('y_mode', 'back')
        margins = {'-X': (stock['x'] - size['x']) / 2, '+X': (stock['x'] - size['x']) / 2,
                   '+Y': y_off if mode == 'back' else spare_y - y_off if mode == 'front'
                   else spare_y / 2,
                   '-Y': spare_y - y_off if mode == 'back' else y_off if mode == 'front'
                   else spare_y / 2}
        need = ENDMILL_D + MIN_FRAME
        short = ['%s %.2f mm' % (k, v) for k, v in margins.items() if v < need - 1e-3]
        if short:
            raise RuntimeError(
                'the stock must reach at least %.2f mm past the part on every side (the %g mm '
                'outline cut plus a %g mm frame of stock that holds the part through its tabs), '
                'or be of the part\'s own X and Y size; it reaches %s past it'
                % (need, ENDMILL_D, MIN_FRAME, ', '.join(short)))
    floors, holes = _bottom_work(a)
    if sides is None:
        sides = 2 if floors or holes or stock.get('bottom', 0) > 1e-3 else 1
    elif sides == 1 and (floors or holes):
        what = [('%d pocket floor%s' % (len(floors), 's' if len(floors) > 1 else '')) if floors
                else '', ('%d hole%s' % (len(holes), 's' if len(holes) > 1 else '')) if holes
                else '']
        raise RuntimeError('one side cannot cut the %s that open%s at the bottom; pass "sides": 2'
                           % (' and '.join(w for w in what if w),
                              's' if len(floors) + len(holes) == 1 else ''))
    elif sides not in (1, 2):
        raise RuntimeError('"sides" must be 1 or 2')
    return {'sides': sides, 'outline': outline, 'ends': ends}


def _quarter_turn(body, stock):
    """Must the part be turned a quarter about Z? When the stock fits the part
    along the design's X only, the vise must grip that side, and the jaws close
    along the setups' X axis, the design's Y (see _setup)."""
    bb = body.boundingBox
    fx = abs(stock['x'] - mm(bb.maxPoint.x - bb.minPoint.x)) <= STOCK_FIT
    fy = abs(stock['y'] - mm(bb.maxPoint.y - bb.minPoint.y)) <= STOCK_FIT
    return fx and not fy


def build(args):
    """Create setups, operations and NC programs for the part.

    args:
      body:      body name (optional when the design has one visible body)
      flip:      machine the design's bottom face first, as the top (default: when
                 it has more features than the top face; see _machined_body)
      stock:     {"x": mm, "y": mm, "z": mm, "y_mode": "back", "y_offset": 10,
                  "bottom": mm of stock below the part (default: centred with two
                  sides, 0 with one)}, in the design's axes. Stock of the part's
                  X and Y size, or of its size along one side for a part longer
                  than the jaws are wide, needs no outline cut or tabs (see
                  _strategy); build turns the part a quarter about Z when the
                  stock fits along the design's X only (see _quarter_turn)
      sides:     1 or 2 setups (default: 2 when the bottom has features to cut or
                 stock to face, else 1)
      taps:      {"1.6": "M2", ...} hole diameter (as analyze prints it) -> thread
      name:      program name prefix (default: document name)
      face:      face the top and bottom from stock (default true)
      chamfer:   deburr the outer faces (default true)
      recipes:   leave the features that have a recipe (references/recipes/) to it,
                 and list them under "recipes" (default false: build cuts them);
                 "all" leaves every operation to the recipes: build makes the
                 setups, the layout, the tabs, the face patches and the vise, and
                 no operations or NC programs (see features and add_operation)
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
    # a Part design holds one component, but the vise, tabs, face patches and
    # turned-over copy are components of their own; Fusion turns a Part with
    # bodies into a Hybrid design, which keeps its modelling
    was_part = False
    try:
        if design.designIntent == adsk.fusion.DesignIntentTypes.PartDesignIntentType:
            design.designIntent = adsk.fusion.DesignIntentTypes.HybridDesignIntentType
            was_part = True
    except AttributeError:  # Fusion before design intents
        pass
    if 'stock' in args and _quarter_turn(_find_body(args), args['stock']):
        quarter = True
        args = dict(args, stock=dict(args['stock'], x=args['stock']['y'], y=args['stock']['x']))
    else:
        quarter = False
    body, a, flipped, counts = _machined_body(args, design, True, quarter)
    if flipped and args.get('tab_z') is not None:  # given in the design's Z
        args = dict(args, tab_z=a['ztop'] + a['zbot'] - args['tab_z'])
    stock = args['stock']
    thickness = a['ztop'] - a['zbot']
    if stock['z'] < thickness - 1e-3:
        raise RuntimeError('stock is %g mm thick but the part is %g mm' % (stock['z'], thickness))
    log, warnings = [], []
    strategy = _strategy(a, stock, args.get('sides'), warnings)
    sides, outline = strategy['sides'], strategy['outline']
    ends = strategy['ends']
    if not outline:  # the stock fits the part across the jaws, so it sits where the part is
        stock = dict(stock, y_mode='back', y_offset=0)
    # stock left below the part; centred by default. Moving the part up or down
    # shifts machining time between the top and bottom setups, and so when the
    # operator has to come back to flip the stock. One side: on the stock's bottom.
    spare = max(0.0, stock['z'] - thickness)  # stock as thick as the part is fine
    bottom = stock.get('bottom', spare / 2 if sides == 2 else 0.0)
    if not -1e-3 <= bottom <= spare + 1e-3:
        raise RuntimeError('stock["bottom"] must be between 0 and %g mm' % spare)
    if sides == 1 and bottom > 1e-3:
        raise RuntimeError('one side leaves the stock below the part uncut: stock["bottom"] '
                           'must be 0, or pass "sides": 2')
    bottom = min(max(bottom, 0.0), spare)
    stock = dict(stock, bottom=bottom)
    band = min(1.0, 0.4 * thickness)
    overlap = min(0.5, 0.2 * thickness)
    tab_width = args.get('tab_width', 5.0)
    if tab_width < ENDMILL_D + 0.5:
        raise RuntimeError('tab_width must exceed the %g mm end mill by 0.5 mm' % ENDMILL_D)
    tab_z, tab_clear, openings = _tab_height(a, band, overlap, args.get('tab_z'),
                                             _tab_points(a, tab_width / 10) if outline else [],
                                             lowest=sides == 1)
    if was_part:
        warnings.append('the design was a Part design, which holds only one component; it is '
                        'now a Hybrid design, so that the vise, tabs and face patches can be '
                        'components. Saving the document keeps the change')
    if outline:
        warnings.append('before running the _tabs program, hot-glue the part to the stock '
                        'round it, so that the part stays put when the tab cut frees it')
    if outline and not tab_clear:
        warnings.append('the tab band at z=%g mm overlaps a pocket that opens to the side '
                        '(open over z %s mm); pass a clear tab_z' % (tab_z, openings))
    elif outline and openings and abs(tab_z - _tab_height(a, band, overlap, lowest=sides == 1,
                                                          tab_points=[])[0]) > 1e-6:
        warnings.append('tabs moved off the %s to z=%g mm, clear of the pockets that open to '
                        'the side (open over z %s mm)'
                        % ('mid-plane' if sides == 2 else "part's bottom", tab_z, openings))
    plan = {
        'margin_top': max(0.0, stock['z'] - thickness - bottom),
        'margin_bottom': bottom,
        'taps': {str(k): v for k, v in (args.get('taps') or {}).items()},
        'face': args.get('face', True),
        'chamfer': args.get('chamfer', True),
        'recipes': bool(args.get('recipes', False)),
        'all': args.get('recipes') == 'all',
        'sides': sides,
        'outline': outline,
        'ends': ends,
        'tab': {'band': band, 'overlap': overlap, 'width': tab_width, 'z': tab_z},
        'layout': None,
    }
    side_names = ('top', 'bottom')[:sides]
    tools = Tools()
    # the layout first, so that the dry run sees the slots and makes the same
    # choices as the real one
    plan['layout'] = _make_layout(design, a, plan, warnings)
    # dry run to learn which tools the part needs, then number them in slot order
    if not plan['all']:
        dry = Builder(cam, tools, [], dry=True)
        for side in side_names:
            _side_ops(dry, None, a, side, plan, [])
        for desc, key in _slot_order(dry.needed):
            tools.get(desc, key)
    plan.pop('corner_endmills', None)
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
                           *_vise_matrices(box, z0, box[1][2], False))]
        if sides == 2:
            vises.append(_add_vise(design, VISE_PREFIX + 'bottom', fixture,
                                   *_vise_matrices(box, -ztop, -z0, True)))
        for occ, _ in vises:  # out of the way; the setups still carry them as fixtures
            occ.isLightBulbOn = False
        vises += [None]
    plan['tab_faces'] = _add_tabs(design, plan['layout'], plan['tab']) if outline else {}
    plan['face_patches'] = _add_face_patches(design, plan['layout'], a)
    if args.get('fixture', True):
        conflicts = _vise_conflicts(_planned_cuts(a, plan, box),
                                    {'top': vises[0][0], 'bottom': vises[1] and vises[1][0]}, box)
        if conflicts:
            raise RuntimeError(
                '; '.join(conflicts) + '. Move the part away from the jaws in larger stock '
                '(stock "y_offset", or wider stock), or, for a cut through the stock\'s bottom '
                'over a jaw, leave stock below the part ("sides": 2 and stock "bottom") so the '
                'cut ends in it')
    x_axis = _axis_line(design)
    top = _setup(cam, 'Top ' + SETUP_TAG, body, machine, stock, True, x_axis, vises[0], warnings)
    if plan['all']:
        # every operation is left to the recipes (references/recipes/part.md)
        setups = {'top': top.name}
        if sides == 2:
            setups['bottom'] = _setup(cam, 'Bottom ' + SETUP_TAG, body, machine, stock, False,
                                      x_axis, vises[1], warnings).name
        _save_layout(design, a, plan, name, setups,
                     {i: h['thread'] for i, h in enumerate(a['holes']) if h['thread']})
        print(json.dumps({
            'turned_over': flipped, 'quarter_turned': quarter, 'strategy': strategy,
            'setups': setups, 'operations': log, 'recipes': 'all: see features()',
            'warnings': warnings, 'seconds': round(time.time() - t0, 1)}, indent=1))
        return
    top_ops = _side_ops(b, top, a, 'top', plan, warnings)
    programs = [(name + '_top', top_ops['main'])]
    tab_ops = top_ops['tabs']
    if sides == 2:
        bottom = _setup(cam, 'Bottom ' + SETUP_TAG, body, machine, stock, False, x_axis,
                        vises[1], warnings)
        bottom_ops = _side_ops(b, bottom, a, 'bottom', plan, warnings)
        programs.append((name + '_bottom', bottom_ops['main']))
        tab_ops = bottom_ops['tabs']
    if outline:
        programs.append((name + '_tabs', tab_ops))
    for prog, ops in programs:
        _nc_program(cam, prog, ops, post)
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
    if sides == 1:
        log.insert(0, "one side: the part's bottom face is the stock's bottom, not faced, "
                      'and its edges and the holes and windows coming out there are not '
                      'deburred')
    if ends:
        log.insert(0, "the stock fits the part along its sides, which the vise grips: no tabs, "
                      "and the part's ends are cut free first, through the stock")
    elif not outline:
        log.insert(0, "the stock is the part's size: no outline cut and no tabs")
    if quarter:
        log.insert(0, "the part is turned a quarter about Z, so that the vise grips the sides "
                      "the stock fits: the design's X runs across the jaws; the design's body "
                      'is hidden behind the turned copy in "%s"' % PART_COMPONENT)
    if flipped:
        log.insert(0, 'the part is turned over: the design\'s bottom face (%d features) is '
                      'machined first, as the top (%d features); the design\'s body is hidden '
                      'behind the turned copy in "%s"' % (counts['bottom'], counts['top'],
                                                          PART_COMPONENT))
    # kept in the document too, for the recipe primitives' later calls
    attr = design.attributes.itemByName(*RECIPES_ATTR)
    if attr is not None:
        attr.deleteMe()
    design.attributes.add(RECIPES_ATTR[0], RECIPES_ATTR[1],
                          json.dumps(plan.get('recipe_entries', [])))
    print(json.dumps({
        'turned_over': flipped,
        'quarter_turned': quarter,
        'strategy': strategy,
        'tools': [{'T': n, 'tool': d, 'preset': p} for n, d, p in tools.order],
        'operations': log,
        'nc_programs': [prog for prog, _ in programs],
        'tabs': {'z_mm': tab_z, 'band_mm': band, 'width_mm': tab_width,
                 'count': len(plan['layout']['tabs'])} if outline else None,
        'pocket_entries': sum(1 for e in plan['layout']['pocket_entries'].values() if e['drill']),
        # features build leaves to a recipe in references/recipes/, in run order
        'recipes': plan.get('recipe_entries', []),
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

    A safety net: _thread_passes counts the passes Fusion should keep, so
    it should keep every one. If it does discard some, fewer
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

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
- a machining strategy chosen from the stock (see _strategy): the part cut out
  of larger stock and held by four tabs that a separate "_tabs" program cuts
  last, held by its own sides in the vise, or stock of its own size; a top
  setup, and a flipped bottom setup when the bottom has work.

Entry points, each taking one dict of arguments:

    analyze(args)   recognize the part's features and print them as JSON
    build(args)     create the setups, operations and NC programs
    generate(args)  generate the toolpaths and report errors and warnings

Lengths in args are millimetres. Fusion's internal unit is the centimetre.
"""

import itertools
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
    ids = getattr(face, 'ids', None) or {face.tempId}  # a _Joined floor: any of its faces
    for f in edge.faces:
        if f.tempId not in ids:
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
    """Whether the cylinder goes all the way round at one of its ends: a circular
    edge there, or arcs at one height that close the circle, where several faces
    meet that end (a hole opening into sloped walls, with a slot into its side)."""
    if any(isinstance(e.geometry, adsk.core.Circle3D) for e in face.edges):
        return True
    full = 2 * math.pi * face.geometry.radius
    for z in {round(z, 5) for z in _zrange(face)}:
        if abs(sum(e.length for e in _circle_edges(face, z)) - full) < 1e-4:
            return True
    return False


def _circle_edges(face, z):
    """The cylinder's circular edges and arcs at height z (cm)."""
    return [e for e in face.edges
            if isinstance(e.geometry, (adsk.core.Circle3D, adsk.core.Arc3D))
            and abs(e.geometry.center.z - z) < EPS]


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


def _cam(create=True):
    """The document's CAM product; with create false, None when it has none yet."""
    doc = _app().activeDocument
    try:
        cam = doc.products.itemByProductType('CAMProductType')
    except RuntimeError:
        cam = None
    if cam is None and not create:
        return None
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


def _loop_clear(body, loop, z0, z1):
    """Whether the space over a loop's inside, from z0 to z1 (cm), is empty of the
    part: its walls can all run the whole height while something juts into the
    opening from them partway up, such as a tongue over a pocket cut from the
    other side. Samples a grid of points inside the loop at several heights."""
    ring = _loop_ring(loop)
    xs, ys = [q[0] for q in ring], [q[1] for q in ring]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    step = max(0.02, math.sqrt((x1 - x0) * (y1 - y0) / 1500))
    nz = max(2, min(8, int((z1 - z0) / 0.025)))
    zs = [z0 + (z1 - z0) * (k + 0.5) / nz for k in range(nz)]
    inside = adsk.fusion.PointContainment.PointInsidePointContainment  # on a wall is clear
    y = y0 + step / 2
    while y < y1:
        x = x0 + step / 2
        while x < x1:
            if _inside(ring, x, y):
                for z in zs:
                    if body.pointContainment(adsk.core.Point3D.create(x, y, z)) == inside:
                        return False
            x += step
        y += step
    return True


def _is_groove(face):
    """Whether a concave full cylinder is the outer wall of a groove, not a hole:
    the floor at its closed end, facing into it, has something standing up in it,
    a wall rising from one of its inner loops (a counterbore's floor has the
    smaller hole falling away from it instead). The floor is then a pocket's,
    with that island in it."""
    z0, z1 = _zrange(face)
    zm = (z0 + z1) / 2
    for e in face.edges:
        if not isinstance(e.geometry, adsk.core.Circle3D):
            continue
        w = _other_face(e, face)
        if w is None or not isinstance(w.geometry, adsk.core.Plane) or abs(_normal(w).z) < 0.99:
            continue
        zc = e.geometry.center.z
        if (zm - zc) * _normal(w).z <= 0:
            continue  # the face round the hole's open end
        rising = {r.tempId for r in _rising_edges(w)}
        if any(not lp.isOuter and any(x.tempId in rising for x in lp.edges) for lp in w.loops):
            return True
    return False


def _classify_hole(face, ztop, zbot, threads):
    g = face.geometry
    z0, z1 = _zrange(face)
    top_edges, bot_edges = _circle_edges(face, z1), _circle_edges(face, z0)
    top_open = bool(top_edges) and _end_open(face, top_edges[0], True)
    bot_open = bool(bot_edges) and _end_open(face, bot_edges[0], False)
    side = 'top' if top_open else ('bottom' if bot_open else 'none')
    closed = bot_edges if top_open and not bot_open else top_edges if bot_open and not top_open else []
    # a hole that opens into a countersink or chamfer, a cone widening away from
    # it, starts where that cone does: at the face it is cut from, not in a floor
    # (Fusion's drills and bores still take the hole's top at the cylinder's end,
    # so their heights rise by 'rise' from it)
    rise = {'top': 0.0, 'bottom': 0.0}
    for edges, at_top in ((top_edges, True), (bot_edges, False)):
        other = _other_face(edges[0], face) if edges else None
        if other is not None and isinstance(other.geometry, adsk.core.Cone) and \
                _end_open(face, edges[0], at_top):
            c0, c1 = _zrange(other)
            if at_top:
                rise['top'], z1 = mm(max(0.0, c1 - z1)), max(z1, c1)
            else:
                rise['bottom'], z0 = mm(max(0.0, z0 - c0)), min(z0, c0)
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
        'rise': rise,
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
    """The part's outline: the outer loop of the top or bottom face enclosing more area,
    or of any other level face of the body that encloses more.

    A pocket that opens to the side notches one face's loop; the other face's
    loop is then the outline at the tabs' height. A part whose top and bottom
    faces are small pads standing proud of a plate has the plate's floor as
    its outline.
    """
    best = None
    faces = list(faces)
    if faces:
        ids = {f.tempId for f in faces}
        faces += [f for f in faces[0].body.faces if f.tempId not in ids and
                  isinstance(f.geometry, adsk.core.Plane) and abs(_normal(f).z) > 0.99]
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
    With tab_points None, every opening counts. With tab_points, a height clear
    of every opening still comes first: a pocket open to the side is entered
    from the outline cut beside it, which goes only down to the tab band, so a
    band clear of the openings only beside the tabs leaves the others' plunges
    in uncut stock (the can lid's corner notches, once its tabs were spread
    round it). With lowest (one side, where the tab cut comes from the top
    too), the band sits on the part's bottom, or the lowest height above it
    clear of the openings."""
    if tab_points is not None and requested is None:
        every = _tab_height(a, band, overlap, None, None, lowest)
        if every[1]:
            return every
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


def _join_floors(floors):
    """Pocket floors at one height that an opening the 3.175 end mill cannot
    enter separates, such as a groove a small end mill cuts: the end mill
    cuts them as one floor, running over the opening ("patch" it, user,
    2026-10-24). A closed floor joined so to one floor open to the side is
    cut with that floor's passes, which start on the outline cut's path, so it
    needs no entry hole of its own; its loops along the opening, and the open
    floor's, are left out of the floor the passes cut (see _Joined). Sets
    'group' on the open floor ({'members': the closed floors, 'patched':
    (face tempId, loop index) of the loops left out}) and 'joined' (the open
    floor) on each closed one."""
    pockets = [f for f in floors if f['role'] == 'pocket']
    root = {id(f): f for f in pockets}

    def find(f):
        while root[id(f)] is not f:
            f = root[id(f)]
        return f
    patched = set()
    for g in pockets:
        if g['side_open']:
            continue
        walls = {w.tempId for e in g['face'].edges for w in [_other_face(e, g['face'])]
                 if w is not None}
        # the floors with a loop all of whose walls go down into g
        touch = {}
        for f in pockets:
            if f is g or f['side'] != g['side'] or f['depth'] >= g['depth'] - 1e-6:
                continue
            for i, lp in enumerate(f['face'].loops):
                ws = [_other_face(e, f['face']) for e in lp.edges]
                if ws and all(w is not None and w.tempId in walls for w in ws):
                    touch.setdefault(round(f['depth'], 4), []).append((f, i))
        touch = [fl for fl in touch.values() if len({id(f) for f, _ in fl}) > 1]
        if not touch or _pocket_entry(g['face'])[0] >= TOOL_R:
            continue  # nothing to join, or the end mill cuts the opening itself
        for fl in touch:
            for f, i in fl:
                patched.add((f['face'].tempId, i))
                root[id(find(f))] = find(fl[0][0])
    groups = {}
    for f in pockets:
        groups.setdefault(id(find(f)), []).append(f)
    for grp in groups.values():
        opens = [f for f in grp if f['side_open']]
        if len(grp) < 2 or len(opens) != 1:
            continue
        members = [f for f in grp if f is not opens[0]]
        if not all((f['face'].tempId, i) in patched
                   for f in members for i, lp in enumerate(f['face'].loops) if lp.isOuter):
            continue
        ids = {f['face'].tempId for f in grp}
        opens[0]['group'] = {'members': members, 'patched': {k for k in patched if k[0] in ids}}
        for f in members:
            f['joined'] = opens[0]


class _Joined:
    """An open floor and the closed floors joined to it (see _join_floors), as
    one floor face to the functions that find a floor's walls and islands and
    sketch its passes: the faces' loops but those along the openings, the
    open floor's outer loop the floor's outer loop."""

    def __init__(self, f):
        faces = [f['face']] + [g['face'] for g in f['group']['members']]
        patched = f['group']['patched']
        self.face = faces[0]
        self.ids = {g.tempId for g in faces}
        self.key = tuple(sorted(self.ids))
        self.loops = [lp for g in faces for i, lp in enumerate(g.loops)
                      if (g.tempId, i) not in patched]
        self.edges = [e for lp in self.loops for e in lp.edges]
        self.body = faces[0].body
        self.pointOnFace = faces[0].pointOnFace
        self.evaluator = faces[0].evaluator
        self.geometry = faces[0].geometry
        self.tempId = faces[0].tempId
        self.boundingBox = faces[0].boundingBox
        self.area = sum(g.area for g in faces)


def _floor_face(f):
    """What the floor's passes cut: its face, or with closed floors joined to
    it, a _Joined of them."""
    return _Joined(f) if f.get('group') else f['face']


def analyze_body(body, design=None, threads=None):
    design = design or _design()
    if threads is None:
        threads = _thread_faces(design)
    bb = body.boundingBox
    ztop, zbot = bb.maxPoint.z, bb.minPoint.z
    holes, floors, windows = [], [], []
    partial = []  # (x, y) mm of openings that go through the part only in part
    hole_ids = set()

    for f in body.faces:
        if _is_vertical_cylinder(f) and _is_full_circle_face(f) and _is_concave_cylinder(f):
            if _is_groove(f):
                continue
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
        # through windows: inner loops whose walls all run down to the bottom of
        # the part. A loop only some of whose walls go through, round pockets
        # that stop at a floor, is no window: cut through along the whole loop,
        # the window's contour would cut those floors away
        if up:
            for loop in f.loops:
                if loop.isOuter:
                    continue
                walls = _walls_of_loop(loop, f)
                if any(w is None or w.tempId in hole_ids for w in walls):
                    continue
                through = [_zrange(w)[0] <= zbot + EPS for w in walls]
                if all(through) and not _loop_clear(body, loop, zbot, z):
                    through = through[:-1] + [False]  # something stands in the opening
                if any(through) and not all(through):
                    lb = loop.boundingBox
                    partial.append([mm((lb.minPoint.x + lb.maxPoint.x) / 2),
                                    mm((lb.minPoint.y + lb.maxPoint.y) / 2)])
                if all(through):
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
            if any(ids & wd['walls'] for wd in windows):
                continue
            through = [_zrange(w)[1] >= ztop - EPS for w in walls]
            if all(through) and not _loop_clear(body, loop, f.pointOnFace.z, ztop):
                through = through[:-1] + [False]  # something stands in the opening
            if any(through) and not all(through):
                lb = loop.boundingBox
                partial.append([mm((lb.minPoint.x + lb.maxPoint.x) / 2),
                                mm((lb.minPoint.y + lb.maxPoint.y) / 2)])
            if not all(through):
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

    chamfers, sloped = _slopes(body)
    _join_floors(floors)
    return {
        'body': body,
        'size': [mm(bb.maxPoint.x - bb.minPoint.x), mm(bb.maxPoint.y - bb.minPoint.y),
                 mm(ztop - zbot)],
        'ztop': mm(ztop), 'zbot': mm(zbot),
        'holes': holes, 'floors': floors, 'windows': windows,
        'side_holes': _side_holes(body),
        'partial_windows': [list(q) for q in sorted({(round(x, 2), round(y, 2))
                                                     for x, y in partial})],
        'chamfers': chamfers, 'sloped_faces': sloped,
        'top_face': top_face, 'bottom_face': bot_face,
    }


def _side_holes(body):
    """Holes whose axis does not run along Z, such as holes into the part's sides:
    no tool reaches them from the top or the bottom. One entry per hole, at the
    middle of its axis, with the axis's direction."""
    out = {}
    for f in body.faces:
        g = f.geometry
        if not isinstance(g, adsk.core.Cylinder) or abs(g.axis.z) > 0.99 or                 not _is_concave_cylinder(f):
            continue
        a = g.axis.copy()
        a.normalize()
        # the face's extent along its axis, from points on its edges
        ts, o = [], g.origin
        for e in f.edges:
            for q in (e.startVertex, e.endVertex):
                if q is not None:
                    ts.append(o.vectorTo(q.geometry).dotProduct(a))
        if not ts:
            continue
        t = (min(ts) + max(ts)) / 2
        c = [o.x + a.x * t, o.y + a.y * t, o.z + a.z * t]
        key = (round(g.radius * 200), round(c[0] * 100), round(c[1] * 100), round(c[2] * 100))
        if key not in out:
            out[key] = {'dia': mm(2 * g.radius), 'at': [mm(v) for v in c],
                        'axis': [round(v, 3) for v in (a.x, a.y, a.z)]}
    return list(out.values())


def _slope_of(face):
    """A face's slope from level in degrees, for a flat face or a cone about Z
    neither level nor upright; else None."""
    g = face.geometry
    if isinstance(g, adsk.core.Plane):
        n = g.normal.copy()
        n.normalize()
        if 0.01 <= abs(n.z) <= 0.99:
            return math.degrees(math.acos(min(1.0, abs(n.z))))
    elif isinstance(g, adsk.core.Cone):
        ax = g.axis.copy()
        ax.normalize()
        if abs(ax.z) > 0.999 and 0.5 < math.degrees(g.halfAngle) < 89.5:
            return 90.0 - math.degrees(g.halfAngle)
    return None


def _is_level(face):
    g = face.geometry
    return isinstance(g, adsk.core.Plane) and abs(g.normal.z) > 0.99


def _is_upright(face, at):
    ok, n = face.evaluator.getNormalAtPoint(at)
    return ok and abs(n.z) < 0.01


def _slopes(body):
    """The sloped faces (see _slope_of) of the part, as (chamfers, others).

    A chamfer is a 45-degree face, flat or a cone about Z such as a countersink,
    that a level face meets along its edge on the side it faces (up for 'top',
    down for 'bottom') and upright walls along its far edge, no wider than the
    chamfering bit reaches (CHAMFER_REACH less its least tip offset): the
    chamfering bit cuts it. Each chamfer has its face, side, width (mm, as deep
    as it is wide) and its edges' tempIds. The others, which nothing cuts, have a
    point on them, their area and slope. A cone at a drill point meets no level
    face, so it is neither."""
    chamfers, others = [], []
    for f in body.faces:
        slope = _slope_of(f)
        if slope is None:
            continue
        g = f.geometry
        z0, z1 = _zrange(f)
        ok, n = f.evaluator.getNormalAtPoint(f.pointOnFace)
        side = 'top' if n.z > 0 else 'bottom'
        near, far = (z1, z0) if side == 'top' else (z0, z1)
        meets_level, far_upright, far_edges = False, True, 0
        for e in f.edges:
            other = [h for h in e.faces if h != f]
            if not other:
                continue
            ez0, ez1 = _zrange(e)
            if ez1 - ez0 > 1e-4:
                continue  # an edge down the slope, such as a corner between two chamfers
            if abs(ez0 - near) < 1e-4 and _is_level(other[0]):
                meets_level = True
            elif abs(ez0 - far) < 1e-4:
                far_edges += 1
                ok, t0, t1 = e.evaluator.getParameterExtents()
                ok, pt = e.evaluator.getPointAtParameter((t0 + t1) / 2)
                far_upright = far_upright and _is_upright(other[0], pt)
        if isinstance(g, adsk.core.Cone) and not meets_level:
            continue  # a drill point, cut by its drill
        width = mm(z1 - z0)
        if abs(slope - 45.0) <= CHAMFER_ANGLE_TOL and meets_level and far_edges and                 far_upright and width <= CHAMFER_REACH - CHAMFER_TIP_MIN + 1e-6:
            chamfers.append({'face': f, 'side': side, 'width': width,
                             'z0': mm(z0), 'z1': mm(z1),
                             'edges': {e.tempId for e in f.edges}})
            continue
        c = f.pointOnFace
        others.append({'at': [mm(c.x), mm(c.y), mm(c.z)], 'area_mm2': round(f.area * 100, 2),
                       'slope_deg': round(slope, 1), 'width_mm': width})
    return chamfers, others


def _tap_candidate(dia):
    for name, (d, _, _, _) in THREADS.items():
        if abs(dia - d) < 0.03:
            return name
    return None


def _strip(feat):
    return {k: v for k, v in feat.items()
            if k not in ('face', 'loop', 'body', 'opening', 'walls', 'group', 'joined')}


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


# The part's face to machine first, as the top: the design's axis whose positive or
# negative direction the part is turned to face up, as (rotation axis, angle) about
# the body's centre. '-Z' turns the part over about X; '+X' and '-X' turn it a
# quarter about Y, '+Y' and '-Y' a quarter about X.
TOPS = {'+Z': None, '-Z': ((1, 0, 0), math.pi),
        '+X': ((0, 1, 0), -math.pi / 2), '-X': ((0, 1, 0), math.pi / 2),
        '+Y': ((1, 0, 0), math.pi / 2), '-Y': ((1, 0, 0), -math.pi / 2)}


def _top_matrix(body, top):
    """The turn about the body's centre that brings the design's face `top` (a key
    of TOPS) up: for '-Z', half a turn about X, so the bottom face becomes the top."""
    m = adsk.core.Matrix3D.create()
    if TOPS[top] is None:
        return m
    axis, angle = TOPS[top]
    bb = body.boundingBox
    c = adsk.core.Point3D.create(*[(p + q) / 2 for p, q in
                                   zip(bb.minPoint.asArray(), bb.maxPoint.asArray())])
    m.setToRotation(angle, adsk.core.Vector3D.create(*axis), c)
    return m


def _turned_axes(top):
    """For each axis of the part turned so that `top` faces up, the design's axis it
    runs along: 'x', 'y' or 'z' (the sign is dropped)."""
    return {'+Z': 'xyz', '-Z': 'xyz', '+X': 'zyx', '-X': 'zyx',
            '+Y': 'xzy', '-Y': 'xzy'}[top]


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
        a = g.axis.copy()
        a.normalize()
        # the face's ends along its axis, from points on its edges, moved by m
        ts = [g.origin.vectorTo(q.geometry).dotProduct(a)
              for e in f.edges for q in (e.startVertex, e.endVertex) if q is not None]
        if not ts:
            continue
        ends = []
        for t in (min(ts), max(ts)):
            p = adsk.core.Point3D.create(g.origin.x + a.x * t, g.origin.y + a.y * t,
                                         g.origin.z + a.z * t)
            p.transformBy(m)
            ends.append(p)
        (x0, y0, z0), (x1, y1, z1) = [q.asArray() for q in ends]
        if math.hypot(x1 - x0, y1 - y0) > EPS:
            continue  # turned off Z: a hole into the side, which nothing cuts
        z0, z1 = min(z0, z1), max(z0, z1)
        for t in cyls:
            tz0, tz1 = _zrange(t)
            if abs(t.geometry.radius - g.radius) < EPS and                     math.hypot(t.geometry.origin.x - x0, t.geometry.origin.y - y0) < EPS and                     abs(tz0 - z0) < EPS and abs(tz1 - z1) < EPS:
                out[t.tempId] = name
    return out


def _top_arg(args):
    """args['top'], a key of TOPS, or None to choose."""
    if 'flip' in args:
        raise RuntimeError('"flip" is gone: pass "top": "-Z" to machine the bottom face first, '
                           'or "+Z" for the top')
    top = args.get('top')
    if top is not None:
        top = str(top).upper()
        if top in ('X', 'Y', 'Z'):
            top = '+' + top
        if top not in TOPS:
            raise RuntimeError('"top" must be one of %s' % ', '.join(TOPS))
    return top


def _machined_body(args, design, make, quarter=False):
    """The body the setups machine, its analysis, the design's face machined first
    as the top (a key of TOPS), and the feature counts per face.

    args['top'] names that face. Without it, the face with more features is
    machined first: when the design's bottom face has more (see _feature_counts),
    the part is turned over. With quarter, it is also
    given a quarter turn about Z (see _quarter_matrix). With make, the moved copy
    is added to the design in PART_COMPONENT and the design's body hidden behind it
    (the next build shows it again); otherwise the copy is a temporary body, for
    analyze. The counts are those of the turned part's top and bottom faces.
    """
    body = _find_body(args)
    top = _top_arg(args)
    if top is None:
        a = analyze_body(body, design)
        n = _feature_counts(a)
        top = '-Z' if n['bottom'] > n['top'] else '+Z'
        if top == '-Z':
            n = {'top': n['bottom'], 'bottom': n['top']}
    else:
        a = n = None
    if top == '+Z' and not quarter:
        if a is None:
            a = analyze_body(body, design)
            n = _feature_counts(a)
        return body, a, top, n
    m = _top_matrix(body, top)
    if quarter:
        m.transformBy(_quarter_matrix(body))
    tb = adsk.fusion.TemporaryBRepManager.get()
    temp = tb.copy(body)
    tb.transform(temp, m)
    threads = _thread_faces(design)
    if make:
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
            w for w, on in (('turned over', top == '-Z'),
                            ('%s face up' % top, top not in ('+Z', '-Z')),
                            ('quarter-turned', quarter)) if on)
        body.attributes.add(HIDDEN_ATTR[0], HIDDEN_ATTR[1], '1')
        body.isLightBulbOn = False
    else:
        turned = temp
    ta = analyze_body(turned, design, _turned_threads(body, turned, m, threads))
    return turned, ta, top, _feature_counts(ta)


def analyze(args=None):
    """Print the recognized features of the part as JSON.

    When the part is to be turned over (see _machined_body), the features are
    those of the turned part: its top is the design's bottom face, and y and z
    are mirrored about the part's centre.
    """
    args = args or {}
    body, a, top, counts = _machined_body(args, _design(), False)
    groups = {}
    for h in a['holes']:
        key = (h['dia'], h['side'], h['through'], round(h['depth'], 2), h['thread'])
        g = groups.setdefault(key, dict(_strip(h), count=0, at=[]))
        g['count'] += 1
        g['at'].append([h['x'], h['y']])
        g['tap_candidate'] = h['thread'] or _tap_candidate(h['dia'])
    out = {
        'body': _find_body(args).name,
        'top': top,
        'turned_over': top == '-Z',
        'features_per_face': counts,
        'size_mm': a['size'],
        'ztop': a['ztop'], 'zbot': a['zbot'],
        'holes': list(groups.values()),
        'floors': [_strip(f) for f in a['floors']],
        'windows': [_strip(w) for w in a['windows']],
        'side_holes': a['side_holes'],
        'partial_windows': a['partial_windows'],
        'chamfers': [{k: v for k, v in _strip(c).items() if k != 'edges'} for c in a['chamfers']],
        'sloped_faces': a['sloped_faces'],
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

class _Pending:
    """An operation _side_ops asked for, made later, in the order _run_order
    picks: what Builder.op takes, and where it cuts (see _footprint)."""

    def __init__(self, setup, strategy, tool_key, name, params, select, barrier):
        self.setup, self.strategy, self.tool_key = setup, strategy, tool_key
        self.name, self.params, self.select = name, params, select
        self.barrier = barrier or strategy == 'chamfer2d'
        self.area = None if self.barrier else _footprint(select, params)


class Builder:
    """Creates operations; with dry=True it only records which tools they need.
    With defer, op returns a _Pending, which make() creates."""

    def __init__(self, cam, tools, log, dry=False, outline=None):
        self.cam = cam
        self.tools = tools
        self.log = log
        self.dry = dry
        self.defer = False
        self.needed = []
        _FOOT['outline'] = outline

    def op(self, setup, strategy, tool_key, name, params, select=None, barrier=False):
        """barrier: the operation keeps its place among all the others, as a
        face, which sets the height the next ones start at, does."""
        desc, key = tool_key if isinstance(tool_key, tuple) else (tool_key, None)
        if self.dry:
            if (desc, key) not in self.needed:
                self.needed.append((desc, key))
            return None
        if self.defer:
            return _Pending(setup, strategy, (desc, key), name, params, select, barrier)
        return self.make(_Pending(setup, strategy, (desc, key), name, params, select, True))

    def make(self, p):
        """Create the operation p (a _Pending), last in its setup."""
        setup, strategy, (desc, key), name, params, select = (
            p.setup, p.strategy, p.tool_key, p.name, p.params, p.select)
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
        # The geometry, the selections and the heights taken from geometry (a
        # list is a height's reference: its mode and offset go with it), is set
        # on the operation once it is made: set on its input, Fusion 2706.1.14
        # crashed with an access violation at a height's reference or at an open
        # chain of sketch curves (2026-10-25)
        later = {k[:-len('ref')] for k, v in params.items() if isinstance(v, list)}
        for k, v in params.items():
            if not any(k.startswith(h) for h in later):
                _set(oi.parameters, k, v)
        op = setup.operations.add(oi)
        for k, v in params.items():
            if any(k.startswith(h) for h in later) and not isinstance(v, list):
                _set(op.parameters, k, v)
        for k, v in params.items():
            if isinstance(v, list):
                op.parameters.itemByName(k).value.value = v
        if select:
            select(op.parameters)
        self.log.append('%s / %s [%s] T%s %s' % (setup.name, name, strategy,
                        tool.parameters.itemByName('tool_number').expression, desc))
        return op


# ------------------------------------------------------------ operation order
# Each operation's footprint: the cells of a grid in XY (FOOT_H cm rows, spans
# of x in each) where it cuts. Two operations whose footprints overlap keep the
# order _side_ops made them in, as the conventions set it (a pocket's entry hole
# before the pocket, the drills in a floor before its finishing pass, a rest
# pass after the cuts it follows); the others may change places, so that each
# tool cuts as much as it can before the next tool change (user, 2026-10-24).

FOOT_H = 0.025  # cm
_FOOT = {'outline': None}  # the part's outline (cm), for the outline cut's footprint


class _Rec:
    """Stands in for an operation's parameters, to record what a selector
    (see _sel_faces and the other _sel_ functions) selects: (parameter,
    entities) for each value set, and (parameter, kind, selection) for each
    curve selection made."""

    def __init__(self):
        self.values, self.curves = [], []

    def itemByName(self, name):
        rec = self

        class Sel:
            def __init__(self, kind):
                self.kind, self.inputGeometry = kind, []
                self.isOpen = False
                self.isSetupModelSelected = False

        class Sels:
            def __getattr__(self, attr):
                if attr.startswith('createNew'):
                    def make():
                        s = Sel(attr[len('createNew'):-len('Selection')])
                        rec.curves.append((name, s))
                        return s
                    return make
                raise AttributeError(attr)

        class Value:
            def getCurveSelections(self):
                return Sels()

            def applyCurveSelections(self, sels):
                pass

            def __setattr__(self, attr, v):
                if attr == 'value':
                    rec.values.append((name, list(v)))

        class Param:
            value = Value()
        return Param()


class _Area:
    """A footprint: {row: [(x0, x1), ...]}, rows FOOT_H cm apart."""

    def __init__(self):
        self.rows = {}

    def span(self, j, x0, x1):
        if x1 > x0:
            self.rows.setdefault(j, []).append((x0, x1))

    def disk(self, x, y, r):
        for j in range(int(math.floor((y - r) / FOOT_H)), int(math.ceil((y + r) / FOOT_H)) + 1):
            dy = j * FOOT_H - y
            if abs(dy) <= r:
                w = math.sqrt(r * r - dy * dy)
                self.span(j, x - w, x + w)

    def band(self, pts, r):
        """Within r of the polyline through pts."""
        for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[-1:]):
            n = max(1, int(math.hypot(x1 - x0, y1 - y0) / (2 * FOOT_H)))
            for k in range(n):
                self.disk(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n, r)

    def fill(self, rings):
        """Inside the rings, by the even-odd rule."""
        ys = [y for rg in rings for _, y in rg]
        if not ys:
            return
        for j in range(int(math.floor(min(ys) / FOOT_H)), int(math.ceil(max(ys) / FOOT_H)) + 1):
            y = j * FOOT_H
            xs = sorted(x1 + (y - y1) * (x2 - x1) / (y2 - y1)
                        for rg in rings for (x1, y1), (x2, y2) in zip(rg, rg[1:] + rg[:1])
                        if (y1 <= y < y2) or (y2 <= y < y1))
            for a_, b_ in zip(xs[::2], xs[1::2]):
                self.span(j, a_, b_)

    def merged(self):
        for j, sp in self.rows.items():
            sp.sort()
            out = [list(sp[0])]
            for a_, b_ in sp[1:]:
                if a_ <= out[-1][1]:
                    out[-1][1] = max(out[-1][1], b_)
                else:
                    out.append([a_, b_])
            self.rows[j] = [tuple(s) for s in out]
        return self

    def minus(self, other):
        for j, sp in list(self.rows.items()):
            for c0, c1 in other.rows.get(j, []):
                cut = []
                for a_, b_ in sp:
                    if b_ <= c0 or a_ >= c1:
                        cut.append((a_, b_))
                        continue
                    if a_ < c0:
                        cut.append((a_, c0))
                    if b_ > c1:
                        cut.append((c1, b_))
                sp = cut
            self.rows[j] = sp
        return self

    def meets(self, other):
        for j, sp in self.rows.items():
            for a_, b_ in other.rows.get(j, ()):
                for c0, c1 in sp:
                    if a_ < c1 - 1e-6 and c0 < b_ - 1e-6:
                        return True
        return False


def _curve_points(c):
    """(x, y) points (cm) along a sketch curve or B-rep edge, from its start."""
    if isinstance(c, adsk.fusion.BRepEdge):
        return _edge_points(c)
    ev = c.worldGeometry.evaluator
    ok, t0, t1 = ev.getParameterExtents()
    n = max(2, int(c.length / 0.02))
    return [(q.x, q.y) for q in (ev.getPointAtParameter(t0 + (t1 - t0) * k / n)[1]
                                 for k in range(n + 1))]


def _chain_rings(curves):
    """The closed rings (cm) the curves make, joined end to end; None where
    they do not close."""
    left = [_curve_points(c) for c in curves]
    left = [p for p in left if len(p) > 1]
    rings = []
    near = lambda p, q: math.hypot(p[0] - q[0], p[1] - q[1]) < 2e-4
    while left:
        ring = left.pop(0)
        while not near(ring[0], ring[-1]):
            k = next((k for k, p in enumerate(left) if near(p[0], ring[-1]) or near(p[-1], ring[-1])),
                     None)
            if k is None:
                return None
            p = left.pop(k)
            ring += p if near(p[0], ring[-1]) else p[::-1]
        rings.append(ring)
    return rings


def _footprint(select, params):
    """Where the operation cuts, as an _Area, from what its selector selects;
    None (it keeps its place among all the others) when that cannot be told."""
    rec = _Rec()
    try:
        if select is not None:
            select(rec)
    except Exception:
        return None
    area = _Area()
    off = params.get('compensationType') == "'off'"
    try:
        for name, ents in rec.values:
            if name.endswith('Height_ref'):
                continue  # where a height is measured, not what is cut
            for e in ents:
                if isinstance(e, adsk.fusion.SketchPoint):
                    g = e.worldGeometry
                    area.disk(g.x, g.y, 0.9 * TOOL_R)
                elif isinstance(e, adsk.fusion.BRepFace):
                    bb = e.boundingBox  # a hole's wall: the hole
                    r = max(bb.maxPoint.x - bb.minPoint.x, bb.maxPoint.y - bb.minPoint.y) / 2
                    area.disk((bb.minPoint.x + bb.maxPoint.x) / 2,
                              (bb.minPoint.y + bb.maxPoint.y) / 2, r)
                else:
                    return None
        for name, s in rec.curves:
            if name == 'stockContours':
                continue  # bounds the stock the pocket cuts, inside its pockets
            ents = list(s.inputGeometry)
            if s.kind == 'Silhouette':
                # the outline cut: the tool outside the outline
                ring = _FOOT['outline']
                if ring is None:
                    return None
                band = _Area()
                band.band(ring + ring[:1], 2 * TOOL_R)
                inner = _Area()
                inner.fill([ring])
                for j, sp in band.merged().minus(inner.merged()).rows.items():
                    for x0, x1 in sp:
                        area.span(j, x0, x1)
                continue
            if s.kind == 'Pocket' or (s.kind == 'FaceContour' and name == 'pockets'):
                # a floor: inside its outer loop, holes and islands too, so that a
                # drill in the floor counts as cutting where the floor's pocket does
                for f in ents:
                    area.fill([_loop_ring(lp) for lp in f.loops if lp.isOuter])
                continue
            if s.kind == 'FaceContour':
                for f in ents:
                    for lp in f.loops:
                        area.band(_loop_ring(lp), 2 * TOOL_R)
                continue
            if s.kind == 'Chain' and name == 'pockets' and not s.isOpen:
                rings = _chain_rings(ents)
                if rings is None:
                    return None
                area.fill(rings)
                continue
            if s.kind == 'Chain':
                # the tool centre on the chain, or a radius to one side of it
                for c in ents:
                    area.band(_curve_points(c), TOOL_R if off else 2 * TOOL_R)
                continue
            return None
    except Exception:
        return None
    return area.merged() if area.rows else None


def _run_order(ops):
    """The _Pending ops (in the order the conventions give) in the order they
    run: each after every op before it whose footprint overlaps its own, and
    as few tool changes as that allows. While an op for the tool in the spindle
    can run, the next one does (that never costs a change); which tool comes
    next is searched for every way (_fewest_changes), ties going to the order
    that keeps ops nearest the conventions' order; a setup with too many ways
    to search falls back to a look-ahead of one tool."""
    n = len(ops)
    before = [0] * n  # bitmask of the ops each waits for
    for j in range(n):
        for i in range(j):
            a_, b_ = ops[i], ops[j]
            if a_.area is None or b_.area is None or a_.area.meets(b_.area):
                before[j] |= 1 << i
    order = _fewest_changes(before, [o.tool_key for o in ops])
    if order is not None:
        return [ops[j] for j in order]
    return _lookahead_order(ops)


def _fewest_changes(before, tool, budget=200000):
    """The order of ops 0..n-1 (op j after the ops in bitmask before[j]) with
    the fewest changes of `tool`, or None past `budget` states searched. Each
    step runs one tool for as long as it has ops that can run."""
    n = len(tool)
    full = (1 << n) - 1
    memo, count = {}, [0]

    def ready(done):
        return [j for j in range(n) if not done >> j & 1 and before[j] & ~done == 0]

    def run_tool(done, t):
        seq = []
        while True:
            js = [j for j in ready(done) if tool[j] == t]
            if not js:
                return done, seq
            done |= 1 << js[0]
            seq.append(js[0])

    def best(done, cur):
        if done == full:
            return 0, ()
        key = (done, cur)
        if key in memo:
            return memo[key]
        count[0] += 1
        if count[0] > budget:
            raise OverflowError
        out = None
        for t in dict.fromkeys(tool[j] for j in ready(done)):
            nd, seq = run_tool(done, t)
            c, rest = best(nd, t)
            cand = (c + (t != cur), tuple(seq) + rest)
            if out is None or cand < out:
                out = cand
        memo[key] = out
        return out
    try:
        return list(best(0, None)[1])
    except (OverflowError, RecursionError):
        return None


def _lookahead_order(ops):
    """_run_order's fallback: the next tool is the one that, run as far as it
    goes and then the rest ordered greedily, leaves the fewest changes."""
    n = len(ops)
    after = [[] for _ in range(n)]  # the ops that wait for each
    waits = [0] * n  # how many ops each still waits for
    for j in range(n):
        for i in range(j):
            a_, b_ = ops[i], ops[j]
            if a_.area is None or b_.area is None or a_.area.meets(b_.area):
                after[i].append(j)
                waits[j] += 1
    tool = [o.tool_key for o in ops]

    def run(state, j):
        ready, waits_ = state
        ready.remove(j)
        for k in after[j]:
            waits_[k] -= 1
            if not waits_[k]:
                ready.append(k)
        ready.sort()

    def pick(ready, cur, first=None, waiting=None):
        same = [j for j in ready if tool[j] == cur]
        if same:
            return same[0]
        if first is not None:
            return next(j for j in ready if tool[j] == first)
        # a tool all of whose ops left can run now, so that it is done with
        # for good, before one that will be back
        done_with = [j for j in ready if not any(tool[k] == tool[j] and waiting[k]
                                                 for k in range(n))]
        return (done_with or ready)[0]

    def rollout(state, cur, first):
        """The tool changes left, running `first` next and then greedily."""
        state = (list(state[0]), list(state[1]))
        k = 0
        while state[0]:
            j = pick(state[0], cur, first, state[1])
            first = None
            k += tool[j] != cur
            cur = tool[j]
            run(state, j)
        return k
    state = ([j for j in range(n) if not waits[j]], list(waits))
    cur, order = None, []
    while state[0]:
        ready = state[0]
        if any(tool[j] == cur for j in ready):
            j = pick(ready, cur)
        else:
            best = None
            for t in dict.fromkeys(tool[j] for j in ready):
                score = (rollout(state, cur, t), min(j for j in ready if tool[j] == t))
                if best is None or score < best[0]:
                    best = (score, t)
            j = pick(ready, cur, best[1])
        order.append(j)
        cur = tool[j]
        run(state, j)
    return [ops[j] for j in order]


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


def _sel_chains(loops, param='contours', bottom=False):
    # Inner loops of a floor, cut from the top with the tool inside them; with
    # bottom, cut by the bottom setup, which looks up from below, so that each
    # chain's sense, and so its side, is the other way round.
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
            s.isReverted = (not opposed if _normal(loop.face).z > 0 else opposed) != bottom
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
    when none are raw, else the loops and runs of loops between raw edges (see
    _add_edge_runs)."""
    if not raw:
        s = sels.createNewFaceContourSelection()
        s.inputGeometry = [face]
        return
    _add_edge_runs(sels, face, lambda ce: ce.edge.tempId not in raw)


def _into_face(face, pt, v):
    """The direction from pt, on an edge of the face, into the face: v (the face
    lies left of its co-edges, seen from its normal), or -v where a point just
    along v is off the face, as on a part turned over, whose faces' co-edges
    can run the other way round."""
    q = adsk.core.Point3D.create(pt.x + v.x * 0.001, pt.y + v.y * 0.001, pt.z + v.z * 0.001)
    ok, uv = face.evaluator.getParameterAtPoint(q)
    if ok and not face.evaluator.isParameterOnFace(uv):
        v = v.copy()
        v.scaleBy(-1)
    return v


def _falls_away(face, ce):
    """Does the wall at co-edge `ce` of the up-facing `face` fall away below it,
    making a convex edge with a burr to take off? A wall that rises from the
    face makes an inside corner, and a face in the same plane no edge at all."""
    e = ce.edge
    other = [g for g in e.faces if g != face]
    if not other:
        return False
    ev = e.evaluator
    ok, t0, t1 = ev.getParameterExtents()
    ok, pt = ev.getPointAtParameter((t0 + t1) / 2)
    ok, tan = ev.getFirstDerivative((t0 + t1) / 2)
    if ce.isOpposedToEdge:
        tan.scaleBy(-1)
    ok, n = face.evaluator.getNormalAtPoint(pt)
    into = _into_face(face, pt, n.crossProduct(tan))
    ok, nw = other[0].evaluator.getNormalAtPoint(pt)
    if not ok or abs(nw.dotProduct(n)) > 0.999:
        return False
    return nw.dotProduct(into) < -1e-6


# Where the chamfering bit's point goes, worked out from the part round each
# edge: a ray cast from points along the edge, at heights above and below the
# floor and in RAY_DIRS directions, finds the nearest copper. The bit's axis
# lies the point's offset plus its tip's radius out from the edge, over the drop
# (the cone meets the edge at the chamfer's foot), and the bit is a cone from its
# 0.1 mm point to its 3.175 mm body CHAMFER_REACH above it. The deeper the point,
# the farther out the axis and the lower the cone's top: the bit clears walls
# rising beside the edge better, and comes nearer the far side of a narrow drop.
# Each cut starts and ends with a lead (CHAMFER_OP's, perpendicular and a tenth
# of the bit's diameter long), which takes the axis that much farther out.
TIP_STEP = 0.02                                     # mm between the offsets tried
CHAMFER_LEAD = 3.175 * 0.1                          # mm
RAY_DIRS = 12
RAY_UP = (0.05, 0.3, 0.6, 0.9, 1.2, 1.5, 2.0, 3.0)  # mm above the floor
RAY_DOWN = (0.15, 0.4, 0.7, 1.0, 1.3, 1.6)          # mm below it
RAY_REACH = 3.5                                     # mm: copper farther off is clear


def _deburr_tip(faces, up, depth, hi, width, skip=frozenset()):
    """The tip offset, in mm, for deburring the falling edges of `faces`, a pocket
    floor `depth` mm below the face, seen along `up` (the tool axis, away from
    the part): of the offsets from CHAMFER_TIP_MIN to `hi` (the most the floors,
    holes and tabs below allow), the deepest at which the bit keeps
    CHAMFER_CLEAR off the part at the most sample points, three along each edge;
    and how many of them it clears there, of how many."""
    root = adsk.fusion.Design.cast(adsk.core.Application.get().activeDocument.products.itemByProductType(
        'DesignProductType')).rootComponent
    tips = [hi - TIP_STEP * k for k in range(int((hi - CHAMFER_TIP_MIN) / TIP_STEP + 1e-9) + 1)]
    score = [0] * len(tips)
    n = 0
    ux, uy, uz = up.x, up.y, up.z
    for face in faces:
        for lp in face.loops:
            for ce in lp.coEdges:
                e = ce.edge
                if e.tempId in skip or not _falls_away(face, ce):
                    continue
                own = {g.tempId for g in e.faces}
                ev = e.evaluator
                ok, p0, p1 = ev.getParameterExtents()
                for k in (1, 2, 3):
                    ok, pt = ev.getPointAtParameter(p0 + (p1 - p0) * k / 4)
                    ok, tan = ev.getFirstDerivative(p0 + (p1 - p0) * k / 4)
                    out = _into_face(face, pt, up.crossProduct(tan))
                    out.scaleBy(-1)
                    out.normalize()
                    side = up.crossProduct(out)
                    # the copper round the edge: (along out, along side, height) in mm
                    near = []
                    for h in [z for z in RAY_UP if z <= depth] + [-z for z in RAY_DOWN]:
                        o = adsk.core.Point3D.create(pt.x + out.x * 0.001 + ux * h / 10,
                                                     pt.y + out.y * 0.001 + uy * h / 10,
                                                     pt.z + out.z * 0.001 + uz * h / 10)
                        for j in range(RAY_DIRS):
                            a = 2 * math.pi * j / RAY_DIRS
                            d = adsk.core.Vector3D.create(
                                out.x * math.cos(a) + side.x * math.sin(a),
                                out.y * math.cos(a) + side.y * math.sin(a),
                                out.z * math.cos(a) + side.z * math.sin(a))
                            hits = adsk.core.ObjectCollection.create()
                            found = root.findBRepUsingRay(
                                o, d, adsk.fusion.BRepEntityTypes.BRepFaceEntityType, -1, True, hits)
                            if not found.count or found.item(0).tempId in own:
                                continue  # nothing, or the edge's own wall, which the cone clears
                            q = hits.item(0)
                            r = q.distanceTo(o) * 10
                            if r < RAY_REACH:
                                near.append((r * math.cos(a), r * math.sin(a), h))
                    n += 1
                    for i, t in enumerate(tips):
                        # the axis, along out from the edge, in the cut and its leads
                        axes = [CHAMFER_TIP_R + t + CHAMFER_LEAD * f for f in (0, 0.5, 1)]
                        clear = True
                        for x, y, h in near:
                            z = h + t + width  # above the point
                            if z > 0 and any(math.hypot(x - ax, y) < min(CHAMFER_TIP_R + z, CHAMFER_R)
                                             + CHAMFER_CLEAR for ax in axes):
                                clear = False
                                break
                        score[i] += clear
    if not n:
        return hi, 0, 0
    best = max(score)
    return tips[score.index(best)], best, n


def _add_falling_edges(sels, face, skip=frozenset()):
    """Add to `sels` the edges where walls fall away from `face` (see
    _falls_away), but those in `skip` (tempIds), as _add_edge_runs adds them."""
    _add_edge_runs(sels, face, lambda ce: ce.edge.tempId not in skip and _falls_away(face, ce))


def _add_edge_runs(sels, face, keep):
    """Add to `sels` the co-edges of `face` that `keep` passes: a loop whose edges
    all pass, as the face's contour (Fusion selects a face's outer loop, its inner
    loops or all of them), and each run of passing edges in a loop where others
    fail, as an open chain with the bit outside the part. The chain's side follows
    its first edge's direction seen along the tool axis: the outer loop's co-edges
    run counter-clockwise seen from the face's normal, which faces the tool, so the
    chain runs against them."""
    loops = [(lp, [bool(keep(ce)) for ce in lp.coEdges]) for lp in face.loops]
    outer = [fl for lp, fl in loops if lp.isOuter][0]
    inner = [fl for lp, fl in loops if not lp.isOuter]
    whole_outer = all(outer)
    whole_inner = bool(inner) and all(all(fl) for fl in inner)
    if whole_outer or whole_inner:
        s = sels.createNewFaceContourSelection()
        s.inputGeometry = [face]
        if whole_outer and (whole_inner or not inner):
            pass  # every loop
        elif whole_outer:
            s.loopType = adsk.cam.LoopTypes.OnlyOutsideLoops
        else:
            s.loopType = adsk.cam.LoopTypes.OnlyInsideLoops
    for lp, fl in loops:
        if all(fl) and (whole_inner if not lp.isOuter else True):
            continue  # in the face's contour
        ces = list(lp.coEdges)
        if all(fl):
            runs = [ces]  # a whole inner loop beside inner loops that rise: one open chain
        else:
            k0 = fl.index(False)
            runs, run = [], []
            for ce, ok in zip(ces[k0:] + ces[:k0], fl[k0:] + fl[:k0]):
                if ok:
                    run.append(ce)
                elif run:
                    runs.append(run)
                    run = []
            if run:
                runs.append(run)
        for run in runs:
            s = sels.createNewChainSelection()
            s.inputGeometry = [ce.edge for ce in run]
            s.isOpen = True
            s.isReverted = not run[0].isOpposedToEdge


def _has_falling_edges(face, skip=frozenset()):
    return any(ce.edge.tempId not in skip and _falls_away(face, ce)
               for lp in face.loops for ce in lp.coEdges)


def _sel_deburr_faces(faces, main=None, raw=(), skip=frozenset()):
    """The deburr's contours: the main face's edges but the raw ones (see
    _add_deburr) and those in `skip`, the edges of modelled chamfers, which have
    no burr, and the falling edges of each of `faces` but those in `skip`."""
    def f(params):
        v = params.itemByName('contours').value
        sels = v.getCurveSelections()
        if main is not None:
            _add_deburr(sels, main, set(raw) | set(skip))
        for face in faces:
            _add_falling_edges(sels, face, skip)
        v.applyCurveSelections(sels)
    return f


def _chamfer_edges(a, side=None):
    """The tempIds of the edges of the part's modelled chamfers (on `side`, or all)."""
    return set().union(*[c['edges'] for c in a['chamfers'] if side in (None, c['side'])])


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
    if not walls:
        # a floor every edge of which falls away, such as a ring outside a
        # groove: the passes run out from its inner loops (see _islands)
        walls = [q for ces in _islands(face) for ce in ces for q in _edge_points(ce.edge)]
    if not walls:
        return [], 0.0
    # the farthest any point of the floor lies from its walls: along its open
    # edges, and inside it, sampled every 0.5 mm (coarser on a large floor),
    # where a floor that widens away from its opening is widest
    pts = [q for e in face.edges if e.tempId not in rising for q in _edge_points(e)]
    rings = [(_loop_ring(lp), lp.isOuter) for lp in face.loops]
    outer = [r for r, o in rings if o][0]
    x0, x1 = min(x for x, _ in outer), max(x for x, _ in outer)
    y0, y1 = min(y for _, y in outer), max(y for _, y in outer)
    h = max(0.05, math.sqrt((x1 - x0) * (y1 - y0) / 4000))
    for i in range(int((x1 - x0) / h) + 1):
        for j in range(int((y1 - y0) / h) + 1):
            x, y = x0 + i * h, y0 + j * h
            if _inside(outer, x, y) and not any(_inside(r, x, y) for r, o in rings if not o):
                pts.append((x, y))
    few = walls[::5]  # every 0.5 mm: off the nearest wall by under 0.01 mm
    reach = max((min(math.hypot(x - u, y - v) for u, v in few) for x, y in pts), default=0.0)
    return runs, reach


def _islands(face):
    """The islands in a pocket's floor: each inner loop with a wall rising from
    the floor along any of its edges, as its co-edges in loop order, such as a
    boss. The passes round an island keep the tool off the whole loop. Where
    some of the loop's walls fall away instead, to a slot, a hole or a deeper
    pocket, as round a pad a slot cuts through, the island is the loop with
    each opening bridged by a line (see _patched_island): the passes run on
    over the openings, as if they were patched (user, 2026-10-24). A
    floor with no wall rising from it anywhere, such as a ring between a
    groove and the outline, has every inner loop as an island, so that passes
    run out from them."""
    rising = {e.tempId for e in _rising_edges(face)}
    inner = [lp for lp in face.loops if not lp.isOuter]
    if not rising:
        return [list(lp.coEdges) for lp in inner]
    out = []
    for lp in inner:
        up = [e.tempId in rising for e in lp.edges]
        if all(up):
            out.append(list(lp.coEdges))
        elif any(up):
            out.append(_patched_island(lp, rising) or list(lp.coEdges))
    return out


class _WireCoEdge:
    """A line across an opening in an island's loop (see _patched_island), as a
    co-edge to _draw_walls and _open_walls."""
    isOpposedToEdge = False

    def __init__(self, edge):
        self.edge = edge


def _patched_island(lp, rising):
    """An island loop lp, some of whose walls fall away, patched: its co-edges
    along walls rising from the floor (`rising`, tempIds), in loop order, with
    each run of the others between them, along the openings, replaced by a
    straight line across it, so that the island is the copper standing on the
    floor and the passes round it run on over the openings. None where such
    a line would leave the loop, over the floor."""
    ces = list(lp.coEdges)
    up = [ce.edge.tempId in rising for ce in ces]
    k0 = next(k for k in range(len(ces)) if up[k] and not up[k - 1])
    ces, up = ces[k0:] + ces[:k0], up[k0:] + up[:k0]

    def ends(ce):
        a_, b_ = ce.edge.startVertex.geometry, ce.edge.endVertex.geometry
        return (b_, a_) if ce.isOpposedToEdge else (a_, b_)
    ring = _loop_ring(lp)
    tbm = adsk.fusion.TemporaryBRepManager.get()
    out, i = [], 0
    while i < len(ces):
        if up[i]:
            out.append(ces[i])
            i += 1
            continue
        j = i
        while j < len(ces) and not up[j]:
            j += 1
        p, q = ends(ces[i - 1])[1], ends(ces[j % len(ces)])[0]
        if p.distanceTo(q) < 1e-6:
            i = j
            continue
        if not all(_inside(ring, p.x + (q.x - p.x) * t, p.y + (q.y - p.y) * t)
                   for t in (0.02 * k for k in range(1, 50))):
            return None
        wire, _ = tbm.createWireFromCurves([adsk.core.Line3D.create(p, q)])
        out.append(_WireCoEdge(wire.edges.item(0)))
        i = j
    return out


def _draw_walls(sk, run, closed=False):
    """The walls of `run` (co-edges) drawn into the sketch flat, joined end to
    end, as construction curves; with `closed`, the last joined to the first.
    Drawn, not projected: projecting an edge the sketch already holds gives
    nothing."""
    out, prev = [], None
    P = lambda p: adsk.core.Point3D.create(p.x, p.y, 0)

    def ends(ce):
        a_, b_ = ce.edge.startVertex.geometry, ce.edge.endVertex.geometry
        return (b_, a_) if ce.isOpposedToEdge else (a_, b_)
    for i, ce in enumerate(run):
        e = ce.edge
        a_, b_ = ends(ce)
        # the evaluator's direction need not be the edge's, so go by where the
        # neighbours meet it: the end it shares with the next wall is its far end
        if len(run) > 1:
            nb = run[(i + 1) % len(run)] if closed or i + 1 < len(run) else None
            if prev is not None:
                if P(b_).distanceTo(prev.geometry) < P(a_).distanceTo(prev.geometry):
                    a_, b_ = b_, a_
            elif nb is not None:
                n0, n1 = ends(nb)
                near = lambda p: min(p.distanceTo(n0), p.distanceTo(n1))
                if near(a_) < near(b_):
                    a_, b_ = b_, a_
        ok, t0, t1 = e.evaluator.getParameterExtents()
        ok, m = e.evaluator.getPointAtParameter((t0 + t1) / 2)
        start = prev if prev is not None else P(a_)
        end = P(b_)
        if closed and i == len(run) - 1 and out:
            first = out[0]
            end = min((first.startSketchPoint, first.endSketchPoint),
                      key=lambda q: q.geometry.distanceTo(P(b_)))
        if isinstance(e.geometry, adsk.core.Line3D):
            c = sk.sketchCurves.sketchLines.addByTwoPoints(start, end)
        elif isinstance(e.geometry, adsk.core.Arc3D):
            c = sk.sketchCurves.sketchArcs.addByThreePoints(start, P(m), end)
        elif isinstance(e.geometry, adsk.core.Circle3D):  # an island's whole loop
            c = sk.sketchCurves.sketchCircles.addByCenterRadius(
                P(e.geometry.center), e.geometry.radius)
            c.isConstruction = True
            out.append(c)
            continue
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


def _outside_ring(ring, x, y):
    """Distance (cm) from (x, y) outside the outline `ring`, negative inside."""
    dd = _ring_clearance((ring, [], None), x, y)
    return -dd if _inside(ring, x, y) else dd


ISLAND_GAP = 0.005  # cm left open where a pass round an island closes on itself


WHY = [None]  # why _island_passes last failed


class _IntoPart(Exception):
    """A pass round islands would run into the part (see _island_passes)."""


def _island_passes(sk, walls, d, ring, face):
    """One contour pass round the islands of a pocket open on every side (each
    island's walls drawn closed, see _draw_walls; `walls` a list of them): each
    island's walls offset `d` cm, sketched, and cut back to the outline cut's
    path, one tool radius outside the outline (`ring`), and where it goes
    inside another island's loop. There the two loops cross, and they join
    into one chain, so that no two passes cross: Fusion splits a contour
    where selected curves cross, and enters each piece anew.
    A chain that reaches the outline cut's path starts and ends there. One that
    closes on itself stops ISLAND_GAP short of its start, where an entry hole
    is drilled for it, since a line in from the outline would cross the passes
    farther out.

    Returns [(curves in the order the tool follows them, entry point (cm) or
    None)], or None (leaving nothing sketched) when an offset fails or makes
    something other than lines and arcs, or the loops do not join up."""
    q = face.pointOnFace
    made = []

    def fail(why):
        WHY[0] = why
        for c in made:
            if c.isValid:
                c.deleteMe()
        return None
    pts = lambda c: (c.startSketchPoint, c.endSketchPoint)
    near = lambda p, r: p.geometry.distanceTo(r.geometry) < 1e-5

    def point(c, t):
        return c.geometry.evaluator.getPointAtParameter(t)[1]
    loops = []  # per island: [(curve, the end the tool enters it at)]
    pinched = set()  # loops that pinched off, away from their island
    owner = []  # each loop's island
    for w_i, ws in enumerate(walls):
        coll = adsk.core.ObjectCollection.create()
        for c in ws:
            coll.add(c)
        try:
            curves = list(sk.offset(coll, adsk.core.Point3D.create(q.x, q.y, 0), d))
        except RuntimeError:
            return fail('the offset fails')
        made += curves
        # a whole circle becomes two arcs, so that the loop has ends to walk between
        if len(curves) == 1 and isinstance(curves[0], adsk.fusion.SketchCircle):
            g0 = curves[0].geometry
            cx, cy, r = g0.center.x, g0.center.y, g0.radius
            Q = lambda t: adsk.core.Point3D.create(cx + r * math.cos(t), cy + r * math.sin(t), 0)
            arcs = sk.sketchCurves.sketchArcs
            a1 = arcs.addByThreePoints(Q(0), Q(math.pi / 2), Q(math.pi))
            at = lambda c, p: min(pts(c), key=lambda s: s.geometry.distanceTo(p))
            a2 = arcs.addByThreePoints(at(a1, Q(math.pi)), Q(3 * math.pi / 2), at(a1, Q(0)))
            curves[0].deleteMe()
            curves = [a1, a2]
            made += curves
        if not all(isinstance(c, (adsk.fusion.SketchLine, adsk.fusion.SketchArc))
                   for c in curves):
            return fail('the offset is not lines and arcs')
        # one loop, or several where the offset pinches off, as round a run of
        # walls whose far side is a narrow opening
        left = list(curves)
        while left:
            loop, node = [], left[0].startSketchPoint
            while True:
                c = next((c for c in left if any(near(p, node) for p in pts(c))), None)
                if c is None:
                    break
                left.remove(c)
                a_, b_ = pts(c)
                loop.append((c, a_ if near(a_, node) else b_))
                node = b_ if near(a_, node) else a_
            if not loop or not near(node, loop[0][1]):
                return fail('the offset loop does not close')
            # walk clockwise round the island, so that loops meeting at a junction
            # run on one into the other, along the edge of the stock left
            ring_ = [p for c, a_ in loop for p in _walk_points(c, a_)]
            if sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring_, ring_[1:] + ring_[:1])) > 0:
                loop = [(c, pts(c)[1] if near(pts(c)[0], a_) else pts(c)[0])
                        for c, a_ in reversed(loop)]
            m0 = ws[0].geometry.evaluator
            ok, t0, t1 = m0.getParameterExtents()
            src = m0.getPointAtParameter((t0 + t1) / 2)[1]
            if not _inside(ring_, src.x, src.y):
                pinched.add(len(loops))
            loops.append(loop)
            owner.append(w_i)
    # each loop as a polygon: another loop is cut back where it goes inside,
    # so that two loops are cut at the points where they cross. (Fusion's
    # offset keeps the islands' corners sharp, so the loops are not the lines
    # at d from the walls.)
    polys = [[p for c, a_ in loop for p in _walk_points(c, a_)] for loop in loops]
    boxes = [(min(x for x, _ in pg), min(y for _, y in pg), max(x for x, _ in pg),
              max(y for _, y in pg)) for pg in polys]

    def in_other(i, x, y):  # a pinched-off loop bounds the stock from outside: not it
        return any(owner[j] != owner[i] and j not in pinched and bx[0] <= x <= bx[2] and
                   bx[1] <= y <= bx[3] and _inside(pg, x, y)
                   for j, (pg, bx) in enumerate(zip(polys, boxes)))
    body = face.body
    zf = q.z + (0.002 if _normal(face).z > 0 else -0.002)

    def in_part(c, t0, t1):
        # inside the part: the span lies within it, as a run's far side does,
        # and is dropped; a span partly inside would cut into the part
        ins = []
        ok, length = c.geometry.evaluator.getLengthAtParameter(min(t0, t1), max(t0, t1))
        n = max(2, int(length / 0.02))
        for j in range(n + 1):
            p = point(c, t0 + (t1 - t0) * j / n)
            ins.append(body.pointContainment(adsk.core.Point3D.create(p.x, p.y, zf)) ==
                       adsk.fusion.PointContainment.PointInsidePointContainment)
        if any(ins[1:-1]) and not all(ins[1:-1]):
            raise _IntoPart()
        return ins[n // 2]

    # each loop's stretches inside the outline cut's path and clear of the
    # other islands: (curve, entry point, exit point, t0, t1, what bounds the
    # start, what bounds the end), each bound 'curve' at the curve's own end,
    # else 'ring' or 'join'
    spans = []
    for i, loop in enumerate(loops):
        def bound(x, y):
            return 'ring' if _outside_ring(ring, x, y) - TOOL_R > -1e-4 else 'join'

        def ok_at(c, t):
            p = point(c, t)
            return _outside_ring(ring, p.x, p.y) - TOOL_R <= 0 and not in_other(i, p.x, p.y)

        def cross(c, t0, t1):
            s0 = ok_at(c, t0)
            for _ in range(40):
                tm = (t0 + t1) / 2
                if ok_at(c, tm) == s0:
                    t0 = tm
                else:
                    t1 = tm
            return (t0 + t1) / 2
        flat = []
        for c, a_ in loop:
            b_ = pts(c)[1] if near(pts(c)[0], a_) else pts(c)[0]
            ev = c.geometry.evaluator
            ta = ev.getParameterAtPoint(a_.geometry)[1]
            tb = ev.getParameterAtPoint(b_.geometry)[1]
            n = max(4, int(c.length / 0.01))
            ts = [ta + (tb - ta) * k / n for k in range(n + 1)]
            ins = [ok_at(c, t) for t in ts]
            k = 0
            while k <= n:
                if not ins[k]:
                    k += 1
                    continue
                j = k
                while j < n and ins[j + 1]:
                    j += 1
                t0 = ts[k] if k == 0 else cross(c, ts[k - 1], ts[k])
                t1 = ts[j] if j == n else cross(c, ts[j], ts[j + 1])
                try:
                    within = in_part(c, t0, t1)
                except _IntoPart:
                    return fail('the pass would run into the part')
                if not within:
                    p0, p1 = point(c, t0), point(c, t1)
                    flat.append((c, a_, b_, t0, t1,
                                 'curve' if k == 0 else bound(p0.x, p0.y),
                                 'curve' if j == n else bound(p1.x, p1.y)))
                k = j + 1
        spans.append(flat)
    # the pieces of each loop: runs of spans joined at the curves' ends
    pieces = []  # [spans], each piece's first span bounded at its start, last at its end
    closed = []  # loops wholly kept
    for i, flat in enumerate(spans):
        if len(flat) == len(loops[i]) and all(f[5] == f[6] == 'curve' for f in flat):
            closed.append(i)
            continue
        if not flat:
            continue
        k0 = next((k for k, f in enumerate(flat) if f[5] != 'curve'), None)
        if k0 is None:
            return fail('a loop is cut back but no piece starts')
        flat = flat[k0:] + flat[:k0]
        piece = []
        for f in flat:
            if piece and f[5] != 'curve':
                pieces.append(piece)
                piece = []
            piece.append(f)
            if f[6] != 'curve':
                pieces.append(piece)
                piece = []
        if piece:
            pieces.append(piece)
    # chain the pieces: one ending at a junction goes on into the piece of
    # another loop that starts there
    pos = lambda f, end: point(f[0], f[4] if end else f[3])
    starts = {k: pos(p[0], False) for k, p in enumerate(pieces)}
    chains, used = [], set()

    def follow(k):
        chain = [k]
        used.add(k)
        while pieces[chain[-1]][-1][6] == 'join':
            e = pos(pieces[chain[-1]][-1], True)
            nxt = min((j for j in starts if pieces[j][0][5] == 'join'),
                      key=lambda j: starts[j].distanceTo(e), default=None)
            if nxt is None or starts[nxt].distanceTo(e) > 1e-3:
                return None
            if nxt in used:
                return chain if nxt == chain[0] else None
            chain.append(nxt)
            used.add(nxt)
        return chain
    for k, p in enumerate(pieces):
        if k not in used and p[0][5] == 'ring':
            ch = follow(k)
            if ch is None:
                return fail('a junction finds no loop going on from it')
            chains.append((ch, False))
    for k in range(len(pieces)):
        if k not in used:
            ch = follow(k)
            if ch is None or pieces[ch[-1]][-1][6] != 'join':
                return fail('a chain through junctions does not close')
            chains.append((ch, True))  # closes on itself, through junctions only

    def redraw(c, t0, t1, pa, pb):
        A = pa if pa is not None else point(c, t0)
        B = pb if pb is not None else point(c, t1)
        ga = A.geometry if hasattr(A, 'geometry') else A
        gb = B.geometry if hasattr(B, 'geometry') else B
        if ga.distanceTo(gb) < 1e-5:  # a span too short to draw: the tool goes on from A
            return None
        m = point(c, (t0 + t1) / 2)
        # a short span of an arc is all but straight, and Fusion refuses an arc through it
        sag = abs((gb.x - ga.x) * (m.y - ga.y) - (gb.y - ga.y) * (m.x - ga.x)) / ga.distanceTo(gb)
        if isinstance(c, adsk.fusion.SketchLine) or sag < 1e-6:
            new = sk.sketchCurves.sketchLines.addByTwoPoints(A, B)
        else:
            new = sk.sketchCurves.sketchArcs.addByThreePoints(A, m, B)
        made.append(new)
        return new

    def gap_back(c, t0, t1):  # the parameter ISLAND_GAP before t1, from t0
        ok, length = c.geometry.evaluator.getLengthAtParameter(min(t0, t1), max(t0, t1))
        return t1 - (t1 - t0) * min(0.5, ISLAND_GAP / max(length, 1e-9))
    keep, out = set(), []
    for ch, loops_back in chains:
        spans_ = [f for k in ch for f in pieces[k]]
        path, prev = [], None
        for n_, (c, a_, b_, t0, t1, s0, s1) in enumerate(spans_):
            last = n_ == len(spans_) - 1
            whole = s0 == s1 == 'curve' and not (loops_back and last)
            if whole and prev is None or whole and near(prev, a_):
                path.append(c)
                keep.add(c.entityToken)
                prev = b_
                continue
            if loops_back and last:
                t1 = gap_back(c, t0, t1)
            pa = prev if prev is not None else (a_ if s0 == 'curve' else None)
            pb = b_ if s1 == 'curve' and not (loops_back and last) else None
            new = redraw(c, t0, t1, pa, pb)
            if new is None:
                continue
            path.append(new)
            prev = new.endSketchPoint if new.endSketchPoint.geometry.distanceTo(
                point(c, t1)) < 1e-5 else new.startSketchPoint
        out.append((path, point(spans_[0][0], spans_[0][3]) if loops_back else None))
    for i in closed:
        # wholly inside: enter at its vertex nearest the outline
        loop = loops[i]
        k0 = max(range(len(loop)), key=lambda k: _outside_ring(
            ring, loop[k][1].geometry.x, loop[k][1].geometry.y))
        order = loop[k0:] + loop[:k0]
        path = [c for c, _ in order[:-1]]
        keep.update(c.entityToken for c in path)
        c, a_ = order[-1]
        b_ = pts(c)[1] if near(pts(c)[0], a_) else pts(c)[0]
        ev = c.geometry.evaluator
        ta, tb = ev.getParameterAtPoint(a_.geometry)[1], ev.getParameterAtPoint(b_.geometry)[1]
        new = redraw(c, ta, gap_back(c, ta, tb), a_, None)
        if new is not None:
            path.append(new)
        out.append((path, order[0][1].geometry))
    for loop in loops:
        for c, _ in loop:
            if c.isValid and c.entityToken not in keep:
                c.deleteMe()
    out = [(p, e) for p, e in out if sum(c.length for c in p) > 1e-3]
    return out or fail('nothing is left')


def _path_ends(path, e=None):
    """The sketch points where the tool starts and ends a sketched path, its
    curves in the order the tool follows them; a one-curve path starts at the
    end nearest e, if given."""
    same = lambda p, q: p.geometry.distanceTo(q.geometry) < 1e-5
    ps = lambda c: (c.startSketchPoint, c.endSketchPoint)
    if len(path) == 1:
        a_, b_ = ps(path[0])
        if e is not None and b_.geometry.distanceTo(e) < a_.geometry.distanceTo(e):
            a_, b_ = b_, a_
        return a_, b_
    first = [p for p in ps(path[0]) if not any(same(p, q) for q in ps(path[1]))]
    last = [p for p in ps(path[-1]) if not any(same(p, q) for q in ps(path[-2]))]
    return (first or [ps(path[0])[0]])[0], (last or [ps(path[-1])[1]])[0]


def _split_path(sk, path, q):
    """Split an open sketched path at the point on it nearest q (cm): the
    curves before that point and after it, in the order the tool follows
    them, and the sketch point where they meet. The curve the point falls on
    is redrawn as two."""
    at = _path_ends(path)[0]
    best = None
    for i, c in enumerate(path):
        a_, b_ = c.startSketchPoint, c.endSketchPoint
        fwd = a_.geometry.distanceTo(at.geometry) < 1e-5
        ev = c.geometry.evaluator
        ok, t0, t1 = ev.getParameterExtents()
        n = max(4, int(c.length / 0.005))
        for k in range(n + 1):
            t = t0 + (t1 - t0) * k / n
            d = ev.getPointAtParameter(t)[1].distanceTo(q)
            if best is None or d < best[0]:
                best = (d, i, t, fwd)
        at = b_ if fwd else a_
    _, i, t, fwd = best
    c = path[i]
    ev = c.geometry.evaluator
    ok, t0, t1 = ev.getParameterExtents()
    a_, b_ = c.startSketchPoint, c.endSketchPoint
    P = lambda u: ev.getPointAtParameter(u)[1]
    first, last = (a_, b_) if fwd else (b_, a_)
    if P(t).distanceTo(first.geometry) < 1e-4:
        return path[:i], path[i:], first
    if P(t).distanceTo(last.geometry) < 1e-4:
        return path[:i + 1], path[i + 1:], last
    if isinstance(c, adsk.fusion.SketchLine):
        c1 = sk.sketchCurves.sketchLines.addByTwoPoints(a_, P(t))
        qp = c1.endSketchPoint
        c2 = sk.sketchCurves.sketchLines.addByTwoPoints(qp, b_)
    else:
        arcs = sk.sketchCurves.sketchArcs
        c1 = arcs.addByThreePoints(a_, P((t0 + t) / 2), P(t))
        qp = min((c1.startSketchPoint, c1.endSketchPoint),
                 key=lambda sp: sp.geometry.distanceTo(P(t)))
        c2 = arcs.addByThreePoints(qp, P((t + t1) / 2), b_)
    c.deleteMe()
    before, after = (c1, c2) if fwd else (c2, c1)
    return path[:i] + [before], [after] + path[i + 1:], qp


class _Seg:
    """A straight line between two points (cm), standing in for a sketch
    line in _too_near."""
    def __init__(self, a_, b_):
        self.geometry = adsk.core.Line3D.create(a_, b_)
        self.length = a_.distanceTo(b_)


def _samples(path, h=0.03):
    """(x, y) points (cm) every `h` cm or so along a sketched path."""
    out = []
    for c in path:
        ev = c.geometry.evaluator
        ok, t0, t1 = ev.getParameterExtents()
        n = max(2, int(c.length / h))
        out += [(q.x, q.y) for q in (ev.getPointAtParameter(t0 + (t1 - t0) * j / n)[1]
                                     for j in range(n + 1))]
    return out


def _reopen(sk, path, e, q):
    """Closed pass `path` (open ISLAND_GAP short of its start, entered near e)
    opened instead at its point nearest q (cm): from there all the way round
    to ISLAND_GAP short of that point, the old gap bridged. Returns the new
    path, its curves in the order followed."""
    s0, s1 = _path_ends(path, e)
    before, after, qp = _split_path(sk, path, q)
    if not before or not after:  # q is at the path's start or end
        return path
    new = after + [sk.sketchCurves.sketchLines.addByTwoPoints(s1, s0)] + before
    c = new[-1]
    ev = c.geometry.evaluator
    ok, t0, t1 = ev.getParameterExtents()
    if qp.geometry.distanceTo(ev.getPointAtParameter(t0)[1]) < 1e-5:
        t0, t1 = t1, t0
    ok, length = ev.getLengthAtParameter(min(t0, t1), max(t0, t1))
    if length <= ISLAND_GAP:
        c.deleteMe()
        return new[:-1]
    back = ev.getPointAtParameter(t1 - (t1 - t0) * ISLAND_GAP / length)[1]
    keep, drop, _ = _split_path(sk, new, back)
    for c in drop:
        c.deleteMe()
    return keep


def _crosses(a_, b_, polys):
    """Does the segment a_-b_ ((x, y), cm) cross any of the polylines? Not
    its last 0.003 cm at each end, where it meets the passes it joins: their
    polylines, sampled from arcs, sag that far off them."""
    L = math.hypot(b_[0] - a_[0], b_[1] - a_[1])
    if L < 0.007:
        return False
    ux, uy = (b_[0] - a_[0]) / L, (b_[1] - a_[1]) / L
    p0 = (a_[0] + ux * 0.003, a_[1] + uy * 0.003)
    p1 = (b_[0] - ux * 0.003, b_[1] - uy * 0.003)
    lo = (min(p0[0], p1[0]), min(p0[1], p1[1]))
    hi = (max(p0[0], p1[0]), max(p0[1], p1[1]))
    cr = lambda o, p, q: (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    for pl in polys:
        for q0, q1 in zip(pl, pl[1:]):
            if max(q0[0], q1[0]) < lo[0] or min(q0[0], q1[0]) > hi[0] or \
                    max(q0[1], q1[1]) < lo[1] or min(q0[1], q1[1]) > hi[1]:
                continue
            if cr(p0, p1, q0) * cr(p0, p1, q1) < 0 and cr(q0, q1, p0) * cr(q0, q1, p1) < 0:
                return True
    return False


def _join_closed(sk, levels, step, rising, ring):
    """Join each closed pass round islands (see _island_passes) into a chain
    that starts on the outline cut's path, so that the end mill enters there
    rather than down a hole drilled in the floor (user, 2026-10-21): the tool
    cuts a pass farther out, then follows a line from it into the closed
    pass, which is opened where the line meets it, and goes round. Fusion
    orders the chains of a contour as it likes, so the pass farther out must
    be in the same chain. An open pass is split where it comes nearest the
    closed pass: the tool cuts it up to there, leads in and goes round, and
    the rest of it, reversed, is a chain of its own from the outline cut's
    path. The end of a closed pass leads on into the next closed pass in.
    A lead is at most two steps long, keeps a tool radius off the walls and
    crosses no pass: Fusion splits a contour where any of its curves cross.
    Only an open pass with both ends on the outline cut's path is split, as
    the rest of it starts at its far end. An open pass of the closed one's own
    offset, or one nearer the islands, is tried last, as for an island beside
    a run of walls, or a pass pinched off inside the walls.
    A closed pass that no chain reaches starts a chain of its own, entered
    down a hole drilled at its start. `levels` holds, for each pass offset k
    cut, k and the passes _island_passes made.

    Returns [(chain, entry)]: each chain's curves in the order followed, and
    for a chain that starts in the floor, its entry point (cm), else None."""
    on_ring = lambda sp: _outside_ring(ring, sp.geometry.x, sp.geometry.y) >= TOOL_R - 1e-3
    chains = [{'curves': p, 'tail': None, 'k': k,
               'open': all(on_ring(sp) for sp in _path_ends(p))}
              for k, got in levels for p, e in got if e is None]
    closed = sorted(((k, p, e) for k, got in levels for p, e in got if e is not None),
                    key=lambda kpe: -kpe[0])
    # join, each time, the closed pass with the shortest lead from a chain:
    # first from a pass farther out, or the end of a closed pass farther out;
    # last from one nearer the islands or of the same offset, as for an
    # island beside a run of walls, or a pass pinched off inside the walls
    while closed:
        # each curve on its own: a chain's curves run either way, and their
        # samples strung together would jump from one curve's end to the next's
        polys = [_samples([c], 0.02) for ch in chains for c in ch['curves']] + \
            [_samples([c], 0.02) for _, q, _ in closed for c in q]
        options = []

        def nearest(cands):
            # the shortest leads, ends at least 1 mm apart on the closed pass, so
            # that where the shortest crosses a pass, as where the leads into
            # passes round one round island all lie on one line, one beside it
            # may not
            out = []
            for c in sorted(cands):
                if c[0] > 2 * step + 1e-3 or len(out) == 6:
                    break
                if all(math.hypot(c[2][0] - o[2][0], c[2][1] - o[2][1]) > 0.1 for o in out):
                    out.append(c)
            return out
        for i_, (k, p, e) in enumerate(closed):
            pts = _samples(p)
            for ch in chains:
                if ch['tail'] is not None:
                    t = ch['tail'][0].geometry
                    for d, _, (x, y) in nearest((math.hypot(x - t.x, y - t.y), (t.x, t.y), (x, y))
                                                for x, y in pts):
                        options.append((ch['tail'][1] <= k, d, i_, ch, t,
                                        adsk.core.Point3D.create(x, y, 0)))
                elif ch['open'] and k - 2 <= ch['k'] <= k + 2:
                    near_ = _samples(ch['curves'], 0.05)
                    for d, a_, b_ in nearest(min((math.hypot(x - u, y - v), (u, v), (x, y))
                                                 for u, v in near_) for x, y in pts):
                        options.append((ch['k'] <= k, d, i_, ch, adsk.core.Point3D.create(*a_, 0),
                                        adsk.core.Point3D.create(*b_, 0)))
        joined = None
        for _, d, i_, ch, a_, b_ in sorted(options, key=lambda o: o[:2]):
            if d > 2 * step + 1e-3:
                continue
            if _too_near([[_Seg(a_, b_)]], rising, TOOL_R - 0.002) or                     _crosses((a_.x, a_.y), (b_.x, b_.y), polys):
                continue
            k, p, e = closed.pop(i_)
            q = _reopen(sk, p, e, b_)
            s0, s1 = _path_ends(q)
            if ch['tail'] is not None:
                link = sk.sketchCurves.sketchLines.addByTwoPoints(ch['tail'][0], s0)
                ch['curves'] = ch['curves'] + [link] + q
            else:
                before, after, qp = _split_path(sk, ch['curves'], a_)
                link = sk.sketchCurves.sketchLines.addByTwoPoints(qp, s0)
                ch['curves'] = before + [link] + q
                ch['open'] = False
                if after:
                    chains.append({'curves': list(reversed(after)), 'tail': None,
                                   'open': not before, 'k': ch['k']})
            ch['tail'] = (s1, k)
            joined = ch
            break
        if joined is None:  # none reached: the one farthest out starts a chain of its own
            k, p, e = closed.pop(0)
            chains.append({'curves': p, 'tail': (_path_ends(p, e)[1], k), 'open': False,
                           'k': k, 'entry': e})
    return [(ch['curves'], ch.get('entry')) for ch in chains]


def _run_island(sk, run, ring, face, far):
    """The walls of `run` drawn as an island: closed round the outside of the
    part, `far` cm beyond its outline, the way that leaves the floor outside.
    The passes round it then run along the walls and out to the outline cut's
    path, and its far side lies beyond that path, where they are cut back."""
    ws = _draw_walls(sk, run)
    a_, b_ = _path_ends(ws)
    X0, X1 = min(x for x, _ in ring) - far, max(x for x, _ in ring) + far
    Y0, Y1 = min(y for _, y in ring) - far, max(y for _, y in ring) + far
    corners = [(X0, Y0), (X1, Y0), (X1, Y1), (X0, Y1)]  # side k runs from corner k on

    rings = [(_loop_ring(lp), lp.isOuter) for lp in face.loops]
    outer = [r for r, o in rings if o][0]

    def on_floor(x, y):
        return _inside(outer, x, y) and not any(_inside(r, x, y) for r, o in rings if not o)

    def to_box(p):
        # straight out to the nearest side of the box that the line reaches
        # without crossing the floor: that point, and the side
        x, y = p.geometry.x, p.geometry.y
        for k in sorted(range(4), key=lambda k: (y - Y0, X1 - x, Y1 - y, x - X0)[k]):
            q = [(x, Y0), (X1, y), (x, Y1), (X0, y)][k]
            L = math.hypot(q[0] - x, q[1] - y)
            n = max(2, int(L / 0.02))
            # from 0.1 mm out: the run's end lies on the floor's edge
            if not any(on_floor(x + (q[0] - x) * j / n, y + (q[1] - y) * j / n)
                       for j in range(1, n + 1) if L * j / n > 0.01):
                return q, k
        return None, None
    (pa, ka), (pb, kb) = to_box(a_), to_box(b_)
    if pa is None or pb is None:
        for c in ws:
            c.deleteMe()
        return None

    def way(step_):  # the corners from b_'s side round to a_'s, one way or the other
        out, k = [], kb
        if ka == kb:  # the same side: straight along it, or all the way round
            (cx, cy), (dx, dy) = corners[kb], corners[(kb + 1) % 4]
            t = lambda p: (p[0] - cx) * (dx - cx) + (p[1] - cy) * (dy - cy)
            if (t(pa) < t(pb)) != (step_ == 1):
                return []
            return [corners[(kb + step_ * j + (1 if step_ == 1 else 0)) % 4] for j in range(4)]
        while k != ka:
            k = (k + step_) % 4
            out.append(corners[k] if step_ == 1 else corners[(k + 1) % 4])
        return out
    q = face.pointOnFace
    walls_pts, node = [], a_  # the walls as a polyline from a_ to b_
    for c in ws:
        walls_pts += _walk_points(c, node)
        sp, ep = c.startSketchPoint, c.endSketchPoint
        node = ep if sp.geometry.distanceTo(node.geometry) < 1e-5 else sp
    best = None
    for step_ in (1, -1):
        route = [pb] + way(step_) + [pa]
        if not _inside(walls_pts + route, q.x, q.y):
            best = route
            break
    if best is None:
        for c in ws:
            c.deleteMe()
        return None
    lines, node = [], b_
    for x, y in best:
        ln = sk.sketchCurves.sketchLines.addByTwoPoints(node, adsk.core.Point3D.create(x, y, 0))
        ln.isConstruction = True
        lines.append(ln)
        node = ln.endSketchPoint
    ln = sk.sketchCurves.sketchLines.addByTwoPoints(node, a_)
    ln.isConstruction = True
    return ws + lines + [ln]


def _island_chains(sk, face, n, ring, left_out):
    """The passes round the islands of a floor open to the side: n of them,
    the first a tool radius off the islands, each further one 0.6 x the end
    mill's diameter farther out (see _island_passes), joined into chains
    (see _join_closed). A floor with runs of walls along its sides as well
    has each run drawn as one more island, closed round the outside of the
    part (see _run_island), so that its passes and the islands' meet and join
    rather than cross. Each pass left out goes on `left_out` as (offset cm,
    why). Returns _join_closed's [(chain, entry)]."""
    step = 0.6 * ENDMILL_D / 10
    rising = [q for e in _rising_edges(face) for q in _edge_points(e)]
    over = _walls_over(face)
    walls = [_draw_walls(sk, run, True) for run in _islands(face)]
    far = TOOL_R + n * step + 0.2
    for run in _open_walls(face)[0]:
        w = _run_island(sk, run, ring, face, far)
        if w is None:
            left_out.append((TOOL_R, 'a run of walls cannot be closed round the part'))
        else:
            walls.append(w)
    levels = []
    for k in range(n):
        d = TOOL_R + k * step
        got = _island_passes(sk, walls, d, ring, face) if walls else None
        if got and _too_near([p for p, _ in got], over, TOOL_R - 0.002):
            for c in [c for p, _ in got for c in p]:
                if c.isValid:
                    c.deleteMe()
            got = False
        if not got:
            left_out.append((d, 'it would cut into a wall' if got is False else
                             WHY[0] if walls else 'the floor has no islands'))
            continue
        levels.append((k, got))
    # where a pass vanishes, the floor too narrow for it, the pass before it
    # can leave a strip uncut, up to 0.6 x the end mill's diameter less its
    # radius wide: a pass one tool radius beyond that one reaches it
    for k in _gap_levels(face, levels, rising, step):
        d = TOOL_R + k * step + TOOL_R - 0.001
        got = _island_passes(sk, walls, d, ring, face)
        if got and _too_near([p for p, _ in got], over, TOOL_R - 0.002):
            for c in [c for p, _ in got for c in p]:
                if c.isValid:
                    c.deleteMe()
            got = None
        if got:
            levels.append((k + 0.5, got))
    # where every pass closes round the islands short of the outline cut's
    # path, passes farther out until one reaches it, so that the chain enters
    # from there, not down a hole drilled in the floor
    k = n
    while levels and k < n + 4 and all(e is not None for _, got in levels for _, e in got):
        got = _island_passes(sk, walls, TOOL_R + k * step, ring, face)
        if got and _too_near([p for p, _ in got], over, TOOL_R - 0.002):
            for c in [c for p, _ in got for c in p]:
                if c.isValid:
                    c.deleteMe()
            got = None
        if got:
            levels.append((k, got))
        k += 1
    levels.sort(key=lambda kg: kg[0])
    for c in [c for ws in walls for c in ws]:
        if c.isValid:
            c.deleteMe()
    return _join_closed(sk, levels, step, over, ring)


def _gap_levels(face, levels, rising, step):
    """The offsets k after which the passes `levels` ([(k, [(path, entry)])])
    leave the floor uncut: the floor sampled every 0.2 mm (coarser on a large
    floor), a point uncut when it lies over a tool radius from every pass and
    from every wall (what lies nearer a wall is an inside corner, which a
    corners pass cuts); its distance from the walls gives k."""
    if not rising or not levels:
        return []
    rings = [(_loop_ring(lp), lp.isOuter) for lp in face.loops]
    outer = [r for r, o in rings if o][0]
    x0, x1 = min(x for x, _ in outer), max(x for x, _ in outer)
    y0, y1 = min(y for _, y in outer), max(y for _, y in outer)
    h = max(0.02, math.sqrt((x1 - x0) * (y1 - y0) / 20000))
    cut = _near_points([q for _, got in levels for p, _ in got for q in _samples(p, 0.01)],
                       TOOL_R)
    corner = _near_points(rising, TOOL_R + 0.002)
    few = rising[::5]
    ks = set()
    for i in range(int((x1 - x0) / h) + 1):
        for j in range(int((y1 - y0) / h) + 1):
            x, y = x0 + i * h, y0 + j * h
            if not _inside(outer, x, y) or any(_inside(r, x, y) for r, o in rings if not o)                     or corner(x, y) or cut(x, y):
                continue
            rho = min(math.hypot(x - u, y - v) for u, v in few)
            ks.add(max(0, int((rho - 2 * TOOL_R) // step)))
    return sorted(k for k in ks if any(k == k_ for k_, _ in levels))


def _walk_points(c, a_):
    """(x, y) points every 0.1 mm along sketch curve c, from its end a_ on, but for its last."""
    ev = c.geometry.evaluator
    ok, t0, t1 = ev.getParameterExtents()
    if c.startSketchPoint.geometry.distanceTo(a_.geometry) > 1e-5:
        t0, t1 = t1, t0
    n = max(2, int(c.length / 0.01))
    return [(p.x, p.y) for p in (ev.getPointAtParameter(t0 + (t1 - t0) * k / n)[1]
                                 for k in range(n))]


def _near_points(points, clear):
    """A test of whether (x, y) lies within `clear` cm of any of the points."""
    cell = max(clear, 0.01)
    grid = {}
    for x, y in points:
        grid.setdefault((math.floor(x / cell), math.floor(y / cell)), []).append((x, y))

    def near(x, y):
        i, j = math.floor(x / cell), math.floor(y / cell)
        return any(math.hypot(x - u, y - v) < clear for di in (-1, 0, 1) for dj in (-1, 0, 1)
                   for u, v in grid.get((i + di, j + dj), ()))
    return near


def _too_near(paths, walls, clear):
    """Does any of the paths (lists of sketch curves) come within `clear` cm of
    the wall points (x, y)?"""
    near = _near_points(walls, clear)
    for path in paths:
        for c in path:
            ev = c.geometry.evaluator
            ok, t0, t1 = ev.getParameterExtents()
            n = max(2, int(c.length / 0.01))
            for k in range(n + 1):
                q = ev.getPointAtParameter(t0 + (t1 - t0) * k / n)[1]
                if near(q.x, q.y):
                    return True
    return False


def _same_path(p, q):
    """True when two sketched paths run over the same points, either way round."""
    pt = lambda sp: (round(sp.geometry.x, 4), round(sp.geometry.y, 4))
    ends = lambda pth: sorted(tuple(sorted((pt(c.startSketchPoint), pt(c.endSketchPoint))))
                              for c in pth)
    return ends(p) == ends(q)


def _simplify(pts, tol):
    """The polyline `pts` ((x, y), cm) with the points it can do without
    dropped, keeping it within `tol` cm of them (Douglas-Peucker)."""
    if len(pts) < 3:
        return list(pts)
    (x0, y0), (x1, y1) = pts[0], pts[-1]
    L = math.hypot(x1 - x0, y1 - y0)
    dev = [abs((x1 - x0) * (y - y0) - (y1 - y0) * (x - x0)) / L if L > 1e-12
           else math.hypot(x - x0, y - y0) for x, y in pts[1:-1]]
    k = max(range(len(dev)), key=lambda i: dev[i])
    if dev[k] <= tol:
        return [pts[0], pts[-1]]
    return _simplify(pts[:k + 2], tol)[:-1] + _simplify(pts[k + 1:], tol)


def _pass_path(sk, run, walls, d, ring, face, windows=()):
    """One contour pass along a pocket's open walls (`run`, co-edges, drawn as
    `walls`, see _draw_walls): the tool centre's path `d` cm off the walls,
    sketched, with a line at each end out to the outline cut's path, one tool
    radius outside the outline (`ring`); an end already beyond that path is cut
    back to it instead. One end may instead run on into a through window
    (`windows`, their rings), as a ledge open to the outside and to a window
    has it: until the tool is a radius inside the window, clear of the floor
    and of the window's walls. The tool then starts at the other end, where
    the outline cut has cleared the stock for its plunge. The path keeps a
    tool radius off every wall above the floor (see _walls_over): near an end
    it is cut back from that end instead, and where it strays past the
    outline cut's path between its ends it follows that path. The curves in
    the order the tool follows them, or None (leaving nothing sketched) when
    an end, run on 10 mm, meets neither."""
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
    # the run's start, by the edge's vertices: its evaluator can run the other way
    s0 = (run[0].edge.endVertex if run[0].isOpposedToEdge else run[0].edge.startVertex).geometry
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

    clear = _near_points(_walls_over(face), TOOL_R - 0.002)

    def seg_clear(a_, b_):  # the tool kept a radius off the walls from a_ to b_
        n = max(2, int(math.hypot(b_[0] - a_[0], b_[1] - a_[1]) / 0.005))
        return not any(clear(a_[0] + (b_[0] - a_[0]) * k / n, a_[1] + (b_[1] - a_[1]) * k / n)
                       for k in range(n + 1))

    def nearest(x, y):  # the outline's point nearest (x, y): (distance, x, y)
        best = None
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            L2 = (x2 - x1) ** 2 + (y2 - y1) ** 2
            t = 0 if L2 < 1e-12 else max(0, min(1, ((x - x1) * (x2 - x1) + (y - y1) * (y2 - y1)) / L2))
            nx, ny = x1 + t * (x2 - x1), y1 + t * (y2 - y1)
            dd = math.hypot(x - nx, y - ny)
            if best is None or dd < best[0]:
                best = (dd, nx, ny)
        return best

    def straight_out(x, y):
        # from (x, y) straight out of the part, square to the outline nearest,
        # to the cut's path: the point, or None
        dd, nx, ny = nearest(x, y)
        if dd < 1e-6:
            return None
        sg = 1 if _inside(ring, x, y) else -1
        vx, vy = sg * (nx - x) / dd, sg * (ny - y) / dd
        L = sg * dd + TOOL_R  # to the cut's path, if the outline runs straight there
        return (x + L * vx, y + L * vy)

    def run_on(p, c):
        # from the end p of curve c, on along c's direction there to the cut's
        # path: the points the tool goes through, and whether it ends in a
        # window; False when p is on the path already or beyond it, None when
        # the line misses it within 10 mm. A wall that meets the outline at a
        # shallow angle, with a sliver of the part outside it, takes the line
        # too far: then it runs on only until the tool can turn straight out
        # of the part, a radius clear of every wall.
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

        def in_window(L):  # how far the tool is inside a window, less a radius
            x, y = x0 + L * ux, y0 + L * uy
            return max((_ring_clearance((w, [], None), x, y) - TOOL_R - 0.001
                        for w in windows if _inside(w, x, y)), default=-1.0)
        for g in (f, in_window):
            lo, hi, L = 0.0, None, 0.0
            while L < 1.0:
                L += 0.002
                if g(L) >= 0:
                    hi = L
                    break
                lo = L
            if hi is None:
                continue
            for _ in range(30):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if g(mid) < 0 else (lo, mid)
            q = adsk.core.Point3D.create(x0 + hi * ux, y0 + hi * uy, 0)
            # an end into a window must not first cross the outline cut's path
            if g is in_window and any(f(hi * k / 50) >= 0 for k in range(51)):
                return None
            if g is f and not seg_clear((x0, y0), (q.x, q.y)):
                break  # it would cut into a wall: turn out sooner
            return ([q], g is in_window)
        L = 0.0
        while L < 1.0:
            a_ = (x0 + L * ux, y0 + L * uy)
            if not seg_clear((x0, y0), a_):
                return None
            b_ = straight_out(*a_)
            if b_ is not None and abs(outside(*b_) - TOOL_R) < 1e-3 and seg_clear(a_, b_):
                P = lambda xy: adsk.core.Point3D.create(xy[0], xy[1], 0)
                return ([P(b_)] if L == 0 else [P(a_), P(b_)], False)
            L += 0.01
        return None

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

    # the walls offset can leave the floor: round the end of a wall standing
    # over a deeper pocket beside the floor, where the tool has room above
    # the floor but must keep off that pocket's own walls, and round the
    # outside of a corner where two walls meet, past the outline cut's path.
    # Where the path comes within a radius of the part near an end, it is cut
    # back from that end; where it strays past the outline cut's path between
    # its ends, it follows that path instead, which the outline cut has cleared
    walk, node = [], start  # (curve, parameter from, parameter to), in order
    for c in order:
        ev = c.geometry.evaluator
        ok, t0, t1 = ev.getParameterExtents()
        ok, tn = ev.getParameterAtPoint(node.geometry)
        walk.append((c, t0, t1) if abs(tn - t0) < abs(tn - t1) else (c, t1, t0))
        a_, b_ = pts(c)
        node = b_ if near(a_, node) else a_
    at = lambda i, t: walk[i][0].geometry.evaluator.getPointAtParameter(t)[1]
    smp = []  # (curve index, parameter, x, y) every 0.05 mm or so
    for i, (c, ta, tb) in enumerate(walk):
        n = max(4, int(c.length / 0.005))
        for k in range(0 if i == 0 else 1, n + 1):
            q = at(i, ta + (tb - ta) * k / n)
            smp.append((i, ta + (tb - ta) * k / n, q.x, q.y))
    bad = [clear(x, y) for _, _, x, y in smp]
    lo, hi, k = 0, len(smp) - 1, 0
    while k < len(smp):
        if not bad[k]:
            k += 1
            continue
        j = k
        while j + 1 < len(smp) and bad[j + 1]:
            j += 1
        if k <= len(smp) - 1 - j:  # nearer the start: cut back from there
            lo = max(lo, j + 1)
        else:
            hi = min(hi, k - 1)
        k = j + 1
    if hi - lo < 2:
        return fail()
    out_ = [outside(x, y) - TOOL_R > 1e-4 for _, _, x, y in smp]
    strays, k = [], lo
    while k <= hi and out_[k]:  # an end beyond the path: trim_back's
        k += 1
    while k <= hi:
        if not out_[k]:
            k += 1
            continue
        j = k
        while j + 1 <= hi and out_[j + 1]:
            j += 1
        if j == hi:
            break
        strays.append((k, j))
        k = j + 1
    if lo > 0 or hi < len(smp) - 1 or strays:
        def crossing(a, b):
            # where the path crosses the outline cut's path between samples
            # a and b = a + 1, one inside it and one beyond: (curve index,
            # parameter). Sample a may end the curve before b's
            i, tb = smp[b][0], smp[b][1]
            ta = smp[a][1] if smp[a][0] == i else walk[i][1]
            g = lambda t: (lambda q: outside(q.x, q.y) - TOOL_R)(at(i, t)) > 1e-4
            ga = g(ta)
            for _ in range(40):
                mid = (ta + tb) / 2
                ta, tb = (mid, tb) if g(mid) == ga else (ta, mid)
            return i, (ta + tb) / 2

        def pull(x, y):  # (x, y) beyond the path, moved square onto it
            dd, nx, ny = nearest(x, y)
            return nx + (x - nx) * TOOL_R / dd, ny + (y - ny) * TOOL_R / dd
        pieces, pos = [], (smp[lo][0], smp[lo][1])
        for k, j in strays:
            e_in, e_out = crossing(k - 1, k), crossing(j, j + 1)
            pieces.append(('along', pos, e_in))
            qa, qb = at(*e_in), at(*e_out)
            route = _simplify([(qa.x, qa.y)] + [pull(x, y) for _, _, x, y in smp[k:j + 1]]
                              + [(qb.x, qb.y)], 0.0002)[1:-1]
            pieces.append(('route', qa, [adsk.core.Point3D.create(x, y, 0) for x, y in route] + [qb]))
            pos = e_out
        pieces.append(('along', pos, (smp[hi][0], smp[hi][1])))
        new, node = [], None
        for p in pieces:
            if p[0] == 'route':
                if node is None:
                    node = sk.sketchPoints.add(p[1])
                for q in p[2]:
                    if node.geometry.distanceTo(q) < 1e-5:
                        continue
                    ln = sk.sketchCurves.sketchLines.addByTwoPoints(node, q)
                    new.append(ln)
                    node = ln.endSketchPoint
                continue
            (i0, f0), (i1, f1) = p[1], p[2]
            for i in range(i0, i1 + 1):
                c, ta, tb = walk[i]
                fa, fb = (f0 if i == i0 else ta), (f1 if i == i1 else tb)
                A, B = at(i, fa), at(i, fb)
                if A.distanceTo(B) < 1e-5:
                    continue
                if isinstance(c, adsk.fusion.SketchLine):
                    nc = sk.sketchCurves.sketchLines.addByTwoPoints(node or A, B)
                elif isinstance(c, adsk.fusion.SketchArc):
                    nc = sk.sketchCurves.sketchArcs.addByThreePoints(node or A, at(i, (fa + fb) / 2), B)
                else:
                    for c_ in new:
                        c_.deleteMe()
                    return fail()
                new.append(nc)
                node = nc.endSketchPoint if nc.endSketchPoint.geometry.distanceTo(B) < 1e-5 \
                    else nc.startSketchPoint
        first = at(smp[lo][0], smp[lo][1])
        for c in order:
            c.deleteMe()
        order[:] = new
        if not new:
            return fail()
        a_, b_ = pts(new[0])
        start = min((a_, b_), key=lambda p: p.geometry.distanceTo(first))
        stop = node

    ends_out = []
    for which in (0, 1):
        chain = order if which == 0 else order[::-1]
        end = start if which == 0 else stop
        q = run_on(end, chain[0])
        if q is None:
            return fail()
        into_window = bool(q) and q[1]
        q = q and q[0]  # the points out from the end, the last on the path
        if q is False and outside(end.geometry.x, end.geometry.y) - TOOL_R > 1e-4:
            end = trim_back(end, chain)
            if end is None:
                return fail()
            if which == 0:
                order[:] = chain
            else:
                order[:] = chain[::-1]
        ends_out.append((end, q, into_window))
    (start, qa, wa), (stop, qb, wb) = ends_out
    if wa and wb:  # both ends in windows: nowhere cleared to plunge
        return fail()
    # a pass that still strays past the outline cut's path between its ends
    # would cut the stock round the part beyond what the outline cut clears
    if any(outside(x, y) - TOOL_R > 0.01 for x, y in _samples(order, 0.02)):
        return fail()
    path = list(order)
    for end, route, first in ((start, qa, True), (stop, qb, False)):
        node = end
        for q in route or []:
            ln = sk.sketchCurves.sketchLines.addByTwoPoints(node, q)
            made.append(ln)
            if first:
                path.insert(0, ln)
            else:
                path.append(ln)
            node = ln.endSketchPoint
    if wa:
        path.reverse()
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
# tolerance of room around it: 0.05 mm at the lab's 0.01 mm tolerance. A pocket
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
# A modelled chamfer: the chamfering bit's 45-degree cone (3.175 mm across, its
# point 0.1 mm across) reaches 1.5375 mm out from its point's edge, which must go
# at least CHAMFER_TIP_MIN below the chamfer, so the widest chamfer it cuts is
# 1.4375 mm; a slope within CHAMFER_ANGLE_TOL degrees of 45 counts.
CHAMFER_REACH = (3.175 - 0.1) / 2
CHAMFER_R, CHAMFER_TIP_R = 3.175 / 2, 0.1 / 2  # the bit's radius and its point's
CHAMFER_ANGLE_TOL = 0.5
# A modelled chamfer is cut this much deeper at each pass, the last pass the full
# chamfer: one pass the full width loads the bit's whole cone at once.
CHAMFER_STEPDOWN = 0.2
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


def _small_fits(radius, depth):
    """The small end mills no wider than 2 x radius (mm) that reach the depth."""
    return [t for t in SMALL_ENDMILLS if t[1] / 2 <= radius + 1e-3 and t[2] >= depth + 0.5]


def _small_choice(plan, fits):
    """The small end mill (desc, d) for a cut any of `fits` can make, or (None,
    None). The part uses as few small end mills as it can (user, 2026-10-21):
    build's first dry run collects every cut's `fits` in plan['small_needs'],
    _small_set picks the fewest end mills that serve them all, and each cut
    then takes the largest of those that fits it."""
    if not fits:
        return None, None
    needs = plan.get('small_needs')
    if needs is not None:
        needs.append(frozenset(t[0] for t in fits))
    chosen = plan.get('small_set')
    pool = [t for t in fits if chosen is None or t[0] in chosen] or fits
    t = max(pool, key=lambda t: t[1])
    return t[0], t[1]


def _small_set(needs):
    """The fewest small end mills such that each need (a set of end mills that
    would do) has one, the largest where there is a choice. Each need is a run
    of neighbours in SMALL_ENDMILLS (those narrow enough that reach deep
    enough), so taking, for the need whose widest end mill is narrowest, that
    end mill, and repeating, is the fewest."""
    order = {t[0]: t[1] for t in SMALL_ENDMILLS}
    chosen = set()
    for need in sorted(needs, key=lambda n: max(order[x] for x in n)):
        if not need & chosen:
            chosen.add(max(need, key=lambda x: order[x]))
    return chosen


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
        # the passes of a pocket open to the side run out to one tool radius
        # past the outline, and its deburr goes below its floor
        for f in a['floors']:
            if f['role'] == 'pocket' and f['side_open'] and f['side'] == side:
                ob = [lp for lp in f['face'].loops if lp.isOuter][0].boundingBox
                fz = f['z'] / 10
                if plan['chamfer']:
                    fz = fz - deburr if top else fz + deburr
                cuts[side].append(('the pocket open to the side at z=%g mm' % f['z'],
                                   (ob.minPoint.x - D, ob.minPoint.y - D,
                                    ob.maxPoint.x + D, ob.maxPoint.y + D), fz))
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
PART_COMPONENT = LAYOUT_SKETCH + ' part'  # the part's turned copy: turned over, a quarter turned, or both
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
        # the n pieces spread most evenly round the loop, the longest of those:
        # the n longest alone can all lie on one side, as on an octagon whose
        # sides are about the same length
        start, s = [], 0.0
        for pc in pieces:
            start.append(s)
            s += pc['len']
        total = s
        mid = {i: start[i] + pieces[i]['len'] / 2 for i in cand}

        def score(sel):
            ps = sorted(mid[i] for i in sel)
            gap = min((ps[(k + 1) % n] - ps[k]) % total or total for k in range(n))
            # spacing within a tenth of the even spacing counts as even
            return round(gap / (total / n), 1), sum(pieces[i]['len'] for i in sel)
        pool = sorted(cand, key=lambda i: -pieces[i]['len'])[:20]
        for i in max(itertools.combinations(pool, n), key=score):
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
    if isinstance(g, adsk.core.Circle3D) and a < 1e-6 and b > pc['len'] - 1e-6:
        return [sk.sketchCurves.sketchCircles.addByCenterRadius(_flat(g.center), g.radius)]
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
                backward = der.x * d0.x + der.y * d0.y < 0
                if backward:  # run along the tab, as the outline does
                    der = adsk.core.Vector3D.create(-der.x, -der.y, 0)
                n = math.hypot(der.x, der.y)
                # a clockwise loop has its outside on the left of the direction of travel
                pts.append(adsk.core.Point3D.create(q.x, q.y, 0))
                outs.append(adsk.core.Point3D.create(q.x - der.y / n * w, q.y + der.x / n * w, 0))
                # the end mill's centre where the cut beside the tab stops, and the
                # deepest point of the arc it leaves in the tab's end
                cx, cy = q.x - der.y / n * TOOL_R, q.y + der.x / n * TOOL_R
                # the arc bulges into the tab: forward at the end the tab starts from
                k = TOOL_R if (u == t0) != backward else -TOOL_R
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
    and None, where a selector once set the face: Builder.make sets it, as a
    list, once the operation is made. ({}, None) without the tab bodies."""
    if face is None:
        return {}, None
    return {which + 'Height_mode': "'from point'", which + 'Height_offset': '%gmm' % offset,
            which + 'Height_ref': [face]}, None


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
    # in the edge's own direction, from its start vertex: its evaluator, such as
    # an arc's, which runs counter-clockwise, can run the other way
    v0, v1 = e.startVertex.geometry, e.endVertex.geometry
    d = lambda v: math.hypot(pts[0][0] - v.x, pts[0][1] - v.y)
    if pts and v0.distanceTo(v1) > 1e-6 and d(v1) < d(v0):
        pts.reverse()
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


_WALLS_OVER = {}


def _walls_over(face):
    """(x, y) points (cm) every 0.1 mm along the walls a tool cutting the floor
    must keep off: those rising from it, and every edge of the part above it
    (below, for a floor facing down). A pass that runs on past the floor, such
    as over a deeper pocket beside it, meets walls that do not stand on the
    floor itself, such as that pocket's walls where they rise past the floor."""
    key = getattr(face, 'key', face.tempId)
    if key in _WALLS_OVER:
        return _WALLS_OVER[key]
    up = _normal(face).z > 0
    zf = face.pointOnFace.z
    beyond = (lambda z: z > zf + 0.002) if up else (lambda z: z < zf - 0.002)
    pts = [q for e in _rising_edges(face) for q in _edge_points(e)]
    for e in face.body.edges:
        bb = e.boundingBox
        if not beyond(bb.maxPoint.z if up else bb.minPoint.z):
            continue
        if bb.maxPoint.x - bb.minPoint.x < 1e-4 and bb.maxPoint.y - bb.minPoint.y < 1e-4:
            pts.append((bb.minPoint.x, bb.minPoint.y))  # an edge along Z
            continue
        ev = e.evaluator
        ok, t0, t1 = ev.getParameterExtents()
        n = max(2, int(e.length / 0.01))
        for k in range(n + 1):
            try:
                ok, q = ev.getPointAtParameter(min(max(t0 + (t1 - t0) * k / n, t0), t1))
            except RuntimeError:
                continue
            if ok and beyond(q.z):
                pts.append((q.x, q.y))
    _WALLS_OVER[key] = pts
    return pts


def _column_clear(body, x, y, z, up, r):
    """Whether a tool of radius r (cm) standing at (x, y) clears the part all the
    way from z out to the part's face it comes in from: points round its edge,
    every 0.25 mm up, all outside the body. A floor's own walls are not the only
    ones a pocket's entry hole passes: walls of the pockets above the floor come
    nearer the hole in narrow parts."""
    bb = body.boundingBox
    z1 = bb.maxPoint.z if up else bb.minPoint.z
    n = max(1, int(abs(z1 - z) / 0.025))
    out = adsk.fusion.PointContainment.PointOutsidePointContainment
    for k in range(n + 1):
        zk = z + (z1 - z) * k / n + (0.001 if up else -0.001) * (k == 0)
        for j in range(16):
            t = 2 * math.pi * j / 16
            p = adsk.core.Point3D.create(x + r * math.cos(t), y + r * math.sin(t), zk)
            if body.pointContainment(p) != out:
                return False
    return True


def _pocket_entry(face):
    """The pocket's entry: its centre (the centre of the floor's outer loop, or
    else its centroid) where the end mill fits there clear of the walls rising
    from the floor, a deeper pocket, hole or window in the floor counting as
    open; else the point on the floor farthest from its edges (see _entry_point).
    Either must leave the end mill room all the way up (see _column_clear);
    where it does not, the room returned is 0."""
    c, x, y, q = _pocket_entry_point(face)
    if c >= TOOL_R and not _column_clear(face.body, x, y, face.pointOnFace.z,
                                         _normal(face).z > 0, TOOL_R - 0.0005):
        return 0.0, x, y, q
    return c, x, y, q


def _pocket_entry_point(face):
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


def _chamfer_edge_sketches(design, a):
    """For each of the part's modelled chamfers (a['chamfers']), its sharp edge: the
    chamfer's far edges, where it meets its walls, raised (or lowered, on the bottom)
    to the level face it meets, sketched in a sketch at that height. A 2D chamfer
    along that edge at width w cuts the chamfer's first w mm, so passes at growing
    widths cut it a layer at a time. Each curve comes with the chain direction that
    puts the bit on the open side: Fusion's 2D chamfer of a chain keeps the bit on
    the left of the chain's direction seen along the tool axis, and leaves out a
    pass that would cut the part."""
    root = design.rootComponent
    sketches, out = {}, []
    for c in a['chamfers']:
        f = c['face']
        near, far = (c['z1'], c['z0']) if c['side'] == 'top' else (c['z0'], c['z1'])
        key = round(near, 4)
        if key not in sketches:
            pi = root.constructionPlanes.createInput()
            pi.setByOffset(root.xYConstructionPlane, adsk.core.ValueInput.createByReal(near / 10))
            plane = root.constructionPlanes.add(pi)
            plane.name = LAYOUT_SKETCH + ' chamfer edges %g' % key
            plane.isLightBulbOn = False
            sk = root.sketches.add(plane)
            sk.name = LAYOUT_SKETCH + ' chamfer edges %g' % key
            sk.isVisible = False
            sketches[key] = sk
        sk = sketches[key]
        dz = (near - far) / 10

        def up(q):
            return sk.modelToSketchSpace(adsk.core.Point3D.create(q.x, q.y, q.z + dz))
        curves, reverted = [], []
        for e in f.edges:
            ez0, ez1 = _zrange(e)
            if ez1 - ez0 > 1e-4 or abs(ez0 * 10 - far) > 1e-3:
                continue
            g = e.geometry
            ok, t0, t1 = e.evaluator.getParameterExtents()
            if isinstance(g, adsk.core.Circle3D):
                cv = sk.sketchCurves.sketchCircles.addByCenterRadius(up(g.center), g.radius)
            elif isinstance(g, adsk.core.Arc3D):
                ok, mid = e.evaluator.getPointAtParameter((t0 + t1) / 2)
                cv = sk.sketchCurves.sketchArcs.addByThreePoints(
                    up(e.startVertex.geometry), up(mid), up(e.endVertex.geometry))
            else:
                cv = sk.sketchCurves.sketchLines.addByTwoPoints(
                    up(e.startVertex.geometry), up(e.endVertex.geometry))
            # the open side: the chamfer's normal, level, at the curve's start
            ev = cv.worldGeometry.evaluator  # in the model's axes, as the normal is
            ok, u0, u1 = ev.getParameterExtents()
            ok, start = ev.getPointAtParameter(u0)
            ok, der = ev.getFirstDerivative(u0)
            q = adsk.core.Point3D.create(start.x, start.y, far / 10)
            ok, q = f.evaluator.getPointAtParameter(f.evaluator.getParameterAtPoint(q)[1])
            ok, n = f.evaluator.getNormalAtPoint(q)
            left = der.x * n.y - der.y * n.x  # the open side seen from above
            # seen from below, as the bottom setup sees it, left and right swap
            reverted.append(left < 0 if c['side'] == 'top' else left > 0)
            curves.append(cv)
        out.append({'curves': curves, 'reverted': reverted})
    return out


def _sel_chamfer_edges(edges):
    """Each sketched chamfer edge (see _chamfer_edge_sketches) as an open chain."""
    def f(params):
        v = params.itemByName('contours').value
        sels = v.getCurveSelections()
        for e in edges:
            for cv, rev in zip(e['curves'], e['reverted']):
                s = sels.createNewChainSelection()
                s.inputGeometry = [cv]
                s.isOpen = True
                s.isReverted = rev
        v.applyCurveSelections(sels)
    return f


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
        warnings.append('the part is wider than the outer loop of any of its level faces; '
                        'the tab chains follow the largest such loop, so check them')
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
    angle = Tools().tip_angle(DRILL_3175)
    for f in a['floors']:
        if f['role'] != 'pocket' or f.get('joined'):
            continue  # a floor joined to one open to the side is cut from that one's passes
        he = None if f['opening'] else _hole_entry(f, a, angle)
        if he is not None:
            # at a hole in the floor, T1's point going on down the hole until its
            # cone meets the hole's edge: no dimple (user, 2026-10-24)
            (x, y), i, below = he
            pocket_entries[f['face'].tempId] = {
                'point': sk.sketchPoints.add(adsk.core.Point3D.create(x, y, 0)),
                'side': f['side'], 'depth': f['depth'], 'drill': True, 'hole': i,
                'below': below}
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
            # a pocket the 3.175 enters by itself, or one open to the side, which
            # its passes cut, is not one the slot has to reach
            if g['side_open'] or g.get('joined') or g['face'].tempId in pocket_entries:
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
    # closed pockets with no room for the 3.175's entry hole and no slot into
    # them: a small end mill pockets each, entering down a hole drilled as wide
    # as itself at the point farthest from the walls (user, 2026-10-21); which
    # of the end mills that fit is chosen with the part's other small cuts (see
    # _small_choice)
    by_slot = {g['face'].tempId for sl in slots for g in [sl['floor']] + sl['fed']}
    small_pockets = {}
    for f in a['floors']:
        k = f['face'].tempId
        if f['role'] != 'pocket' or f['opening'] or k in pocket_entries or k in narrow \
                or k in by_slot or k in shelves or f.get('joined'):
            continue
        c, x, y, q = _pocket_entry(f['face'])
        up = _normal(f['face']).z > 0
        tools = [t for t in SMALL_ENDMILLS
                 if c >= t[1] / 20 + 5 * POCKET_TOL / 10 and t[2] >= f['depth'] + 0.5
                 and _column_clear(f['face'].body, x, y, f['face'].pointOnFace.z, up,
                                   t[1] / 20 - 0.0005)]
        if tools:
            small_pockets[k] = {'point': sk.sketchPoints.add(adsk.core.Point3D.create(x, y, 0)),
                                'side': f['side'], 'depth': f['depth'], 'tools': tools}
    # pockets open to the side, cut by contour passes along their walls, 0.6 x
    # the end mill's diameter apart, as many as cover the floor. Each pass runs
    # on at both ends to the outline cut's path, where the outline cut has
    # cleared the stock for its plunge, or at one end into a through window.
    # A pass never ends anywhere else: it would plunge into uncut copper; one
    # it cannot make is left out with a warning. A pocket open on two sides
    # that are not next to each other, such as a channel across the part, has
    # two runs of walls, each with passes of its own. Rule E4 cannot see a floor
    # left uncut, so _gap_levels adds a pass where the passes leave a strip.
    # The shapes tested: walls meeting the outline steeply or at a shallow
    # angle, islands with or without walls beside them, ledges open to a
    # window, and ledges beside a deeper pocket; look at the toolpaths of
    # others.
    ring = _loop_ring(loop)
    window_rings = [_loop_ring(w['loop']) for w in a['windows']]
    open_passes = {}
    a_floors = a['floors']
    step = 0.6 * ENDMILL_D / 10
    for f in a_floors:
        if f['role'] != 'pocket' or not f['side_open']:
            continue
        fc = _floor_face(f)
        runs, reach = _open_walls(fc)
        n = max(1, math.ceil((reach + 0.01 - 2 * TOOL_R) / step) + 1)
        # a pass must keep a tool radius off every wall above the floor, not
        # only its own (see _walls_over). A floor with islands is cut by passes round the
        # islands (see _island_chains), and round its runs of walls too, if
        # it has any, each pass that closes on itself entered from one
        # farther out. The passes run from the walls out.
        over = _walls_over(fc)
        islands = _islands(fc)
        passes = []  # (k, path)
        # a floor whose passes along its walls cannot all be made, such as
        # where a wall meets the outline at a shallow angle with a sliver of
        # the part outside it, has its runs drawn as islands instead, closed
        # round the part (see _run_island): an offset of an open run keeps
        # clear of the walls only square to them, not round the run's ends
        missed = []
        if runs and not islands:
            for run in runs:
                walls = _draw_walls(sk, run)
                for k in range(n):
                    d = TOOL_R + k * step
                    pth = _pass_path(sk, run, walls, d, ring, fc, window_rings)
                    if pth is not None and _too_near([pth], over, TOOL_R - 0.002):
                        for c in pth:
                            if c.isValid:
                                c.deleteMe()
                        missed.append('a pass %g mm off the walls of the pocket open to the '
                                      'side at z=%g mm would cut into another of its walls; '
                                      'it is left out, so check that pocket' % (mm(d), f['z']))
                    elif pth is None:
                        missed.append('a pass along the walls of the pocket open to the side '
                                      'at z=%g mm ends neither on the outline cut nor in a '
                                      'window; it is left out, so check that pocket' % f['z'])
                    elif any(_same_path(pth, q) for _, q in passes):
                        # a slot as wide as the end mill: the passes along its two
                        # walls are one line, which Fusion would chain into a closed
                        # contour of no area
                        for c in pth:
                            if c.isValid:
                                c.deleteMe()
                    else:
                        passes.append((k, pth))
                for c in walls:
                    if c.isValid:
                        c.deleteMe()
            if missed:
                for c in [c for _, pth in passes for c in pth]:
                    if c.isValid:
                        c.deleteMe()
                passes = []
        if islands or missed:
            left_out = []
            joined = _island_chains(sk, fc, n, ring, left_out)
            for d, why in left_out:
                warnings.append('the pass %g mm round the islands of the pocket open to the '
                                'side at z=%g mm is left out (%s), so check that pocket'
                                % (mm(d), f['z'], why))
            passes += [(0, pth) for pth, _ in joined]
            for j, e in enumerate(e for _, e in joined if e is not None):
                pocket_entries[(f['face'].tempId, 'island', j)] = {
                    'point': sk.sketchPoints.add(adsk.core.Point3D.create(e.x, e.y, 0)),
                    'side': f['side'], 'depth': f['depth'], 'drill': True}
        paths = [pth for k, pth in sorted(passes, key=lambda kp: kp[0])]
        if not paths:
            warnings.append('the pocket open to the side at z=%g mm has no walls to run '
                            'passes along, so nothing cuts it' % f['z'])
        open_passes[f['face'].tempId] = paths
    # the holes' centres, where a pocket that cuts a hole enters (hole_pocket)
    hole_centres = {h['face'].tempId: sk.sketchPoints.add(adsk.core.Point3D.create(
                        h['x'] / 10, h['y'] / 10, 0)) for h in a['holes']}
    return {'sketch': sk, 'corner': corner, 'tabs': tab_chains, 'shelves': shelves,
            'entry': entry, 'hole_centres': hole_centres, 'ends': ends, 'pocket_entries': pocket_entries, 'narrow': narrow,
            'outline': pieces,
            'window_entries': window_entries, 'window_tops': window_tops, 'slots': slots,
            'small_pockets': small_pockets, 'open_passes': open_passes}


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
        if f.get('shelf') and not f.get('joined'):
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
                if pt is not None and not _column_clear(a['body'], pt[0], pt[1], z / 10,
                                                        side == 'top', TOOL_R - 0.0005):
                    pt = None  # the walls above the shelf close in on the hole
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


def _hole_entry(f, a, angle):
    """A hole in closed pocket floor f to drill the pocket's entry hole at, so that
    it leaves no dimple: T1, as wide as the end mill, drilled at the hole's centre
    from the face to the floor and on down the hole until its cone, `angle`
    degrees, meets the hole's edge (or to a blind hole's bottom, if that comes
    first; all the way down a hole as wide as T1). The hole must be at least
    half the end mill wide (rule TH4: there the cone is the hole's width), with
    room for the pocket's entry round its centre (ENTRY_R from the walls rising
    from the floor) all the way up; of several, the one with the most room.
    Its centre (x, y, cm), its index in a['holes'] and how far below the floor
    T1's point goes (mm, or None to take the tip through the hole), or None."""
    top = f['side'] == 'top'
    face = f['face']
    walls = _rising_edges(face)
    mm_ = adsk.core.Application.get().measureManager
    best = None
    for i, h in enumerate(a['holes']):
        if h['side'] != f['side'] or h['dia'] < ENTRY_CONE_D - 1e-6:
            continue
        rim = _hole_ends(h, top)[0]
        if rim is None or rim.face.tempId != face.tempId:
            continue
        x, y = h['x'] / 10, h['y'] / 10
        p = adsk.core.Point3D.create(x, y, face.pointOnFace.z)
        room = min((mm_.measureMinimumDistance(p, e).value for e in walls), default=9.0)
        if room < ENTRY_R or not _column_clear(face.body, x, y, face.pointOnFace.z, top,
                                               TOOL_R - 0.0005):
            continue
        if h['dia'] >= DRILL_D - 1e-3:
            below = None if h['through'] else h['depth']
        else:
            below = h['dia'] / 2 / math.tan(math.radians(angle / 2))
            if not h['through']:
                below = min(below, h['depth'])
        if best is None or room > best[0]:
            best = (room, (x, y), i, below)
    return None if best is None else best[1:]


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


class _Side:
    """Roughing, finishing, small-tool, hole, thread and chamfer ops for one side,
    in the order the lab's conventions give (references/conventions.md,
    "Operation order"); build then runs them in the order with the fewest tool
    changes (_run_order). The bottom setup makes the same operations for its
    side's features, but no Ends, Tab entry holes or Outline between tabs; no
    main entry hole when the top one goes through the stock; an outline down to
    the tab band's bottom; and no windows or through holes, which the top setup
    cut through the part. The tab cut goes in the last setup, in its own
    program.

    Every height comes from the part's geometry (see at()), and every offset is
    one of the lab's fixed numbers: the 0.2 mm the rough face and the rough
    pockets leave (FACE_ALLOWANCE, POCKET_FLOOR_ALLOWANCE), the 0.5 mm a cut
    breaks through past a face, and threads.toml's thread offsets. The one
    exception is a cut deeper than T1 reaches, which goes down in stages
    measured from the stock top (_stage_plan): each stage is drilled as deep as
    T1 reaches and milled down to there, and its name gives its depths
    ("Outline 0 to 6.9 mm"). The lab's parts are mostly thinner than that.

    The setup's operations are made stage by stage, each a method, in the
    order they run; the state they share is in attributes."""

    def __init__(self, b, setup, a, side, plan, warnings):
        self.b, self.setup, self.a, self.side, self.plan, self.warnings = b, setup, a, side, plan, warnings
        self.body = self.a['body']
        self.top = self.side == 'top'
        is_side = lambda f: f['side'] == self.side
        self.holes = [h for h in self.a['holes'] if is_side(h)]
        self.floors = [f for f in self.a['floors'] if is_side(f)]
        self.face = self.a['top_face'] if self.top else self.a['bottom_face']
        self.thickness = self.a['ztop'] - self.a['zbot']
        self.taps = self.plan['taps']
        self.tab = self.plan['tab']
        # stock above the part's face this setup machines: the top margin, or the bottom one
        self.m = self.plan['margin_top'] if self.top else self.plan['margin_bottom']
        self.ops = {'main': [], 'tabs': []}
        # faced only where the stock stands above the part's face
        self.facing = [f for f in self.floors if f['role'] == 'face'] if self.plan['face'] and self.m > 1e-6 else []
        # the material the rough face leaves on the face, until the finishing face
        self.allow = FACE_ALLOWANCE if self.facing else 0.0
        # the stock's highest point in this setup, in mm above the part's face: the
        # stock top until the first face, then the top of what the latest face left.
        # face_op updates it; the operations are made in the order they run.
        self.patch = (self.plan.get('face_patches') or {}).get(self.side)
        self.high = [self.m if self.facing else 0.0]
        # the part's face on the far side, where windows and through holes come out
        far_face = self.a['bottom_face'] if self.top else self.a['top_face']
        def through_part(which):
            if far_face is None:
                return (_bottom_below_part_top if which == 'bottom' else _top_below_part_top)(
                    self.thickness + 0.5, self.m)
            return self.at(which, far_face, -0.5)
        self.through_part = through_part

    def _hole_plan(self):
        """Which tool cuts each hole: drill, bore, pocket or thread mill."""
        # classify holes:
        # - tapped: a thread from the Hole command, or taps[diameter]; drilled and
        #   thread-milled as threads.toml gives (M4, with no matching drill, is
        #   drilled by T1 and bored to size);
        # - drilled: a library drill within -DRILL_UNDER to +DRILL_OVER of the hole
        #   (-0.03 to +0.05 mm, so T1 drills a 3.2 mm clearance hole, as the lab does
        #   kiutra-beam's) whose flute is 0.5 mm longer than the hole; the closest;
        # - bored by the 3.175 mm end mill: wider than 3.275 mm, pocketed instead
        #   (hole_pocket) when its rough bore would take more than two 0.3 mm passes;
        # - bored by a small end mill: one at most the hole's radius less 0.05 mm
        #   wide whose flute is 0.5 mm longer than the hole (_small_choice); with
        #   none, the hole is skipped, with a warning
        self.drilled, self.bored, self.tapped = [], [], []
        self.drills = self.b.tools.drills()
        for h in self.holes:
            t = h['thread'] or self.taps.get('%g' % h['dia'])
            if t:
                if t not in THREADS:
                    self.warnings.append('unknown thread %r on %.2f mm hole; skipped' % (t, h['dia']))
                    continue
                self.tapped.append((h, t))
                continue
            match = [d for d in self.drills if h['dia'] - DRILL_UNDER - 1e-6 <= d[1] <= h['dia'] + DRILL_OVER
                     and d[2] >= h['depth'] + 0.5]
            if match:
                best = min(match, key=lambda d: abs(d[1] - h['dia']))  # the closest in diameter
                self.drilled.append((h, best[0]))
            elif h['dia'] > ENDMILL_D + 0.1:
                self.bored.append((h, (ENDMILL, 'rough'), (ENDMILL, 'finish')))
            else:
                em, d = _small_choice(self.plan, _small_fits(h['dia'] / 2 - 0.05, h['depth']))
                if em:
                    self.bored.append((h, (em, None), None))
                else:
                    self.warnings.append('no tool for %.2f mm hole at (%g, %g); skipped'
                                    % (h['dia'], h['x'], h['y']))
        # taps with no matching drill (M4) are drilled, then bored to the modelled
        # diameter, as threads.toml says
        self.tap_bored = [(h, t) for h, t in self.tapped if THREADS[t][1] is None]
        # the holes the 3.175 mm end mill bores or pockets, which T1 pre-drills
        self.by_endmill = lambda h: any(h is x for x, rough, _ in self.bored if rough[0] == ENDMILL) or \
            any(h is x for x, _ in self.tap_bored)
        self.blind_cone = lambda: (self.b.tools.tip_length(DRILL_3175) * ENTRY_CONE_D / DRILL_D)
        sunk = [h for h in self.holes if not h['through'] and self.by_endmill(h) and self.hole_below(h) is None]
        if sunk and not self.b.dry:
            self.warnings.append('the %d blind hole%s the 3.175 mm end mill bores or pockets %s a cone-shaped '
                            "dimple %g mm across and %.2f mm deep in %s, where T1's point goes "
                            'below the floor so that the end mill plunges to the floor down a hole at least '
                            'half its width; at x, y = %s mm'
                            % (len(sunk), '' if len(sunk) == 1 else 's',
                               'leaves' if len(sunk) == 1 else 'leave', ENTRY_CONE_D, self.blind_cone(),
                               'its floor' if len(sunk) == 1 else 'their floors',
                               ', '.join('(%g, %g)' % (h['x'], h['y']) for h in sunk)))
            thin = [h['depth'] for h in sunk if self.thickness - h['depth'] < self.blind_cone() + 0.2]
            if thin:
                self.warnings.append("a blind hole's dimple comes within 0.2 mm of the part's other face, "
                                'or through it, under a hole %g mm deep' % max(thin))
        # T1 drills at most its flute length less 0.5 mm into the material it starts
        # in; deeper holes go down in stages with the cut that uses them (_stage_plan)
        self.reach = next(d[2] for d in self.drills if d[0] == DRILL_3175) - 0.5

    def _entries(self):
        """The entry holes, the tab band's heights and the staged cuts."""
        self.lay = self.plan['layout']
        self.band_top, self.band_bot = self.tab['z'] + self.tab['band'] / 2, self.tab['z'] - self.tab['band'] / 2
        # the cuts around the tabs take their heights from the modelled tabs' faces
        tab_faces = self.plan.get('tab_faces') or {}
        self.tab_top, self.tab_bottom = tab_faces.get('top'), tab_faces.get('bottom')
        self.chain_bot = self.band_bot - self.tab['overlap']
        stock_z = self.thickness + self.plan['margin_top'] + self.plan['margin_bottom']
        self.points = lambda pts: _sel_faces('holePoints', pts)
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
        self.outline_depth = {True: self.plan['margin_top'] + self.a['ztop'] - self.band_top,  # from the stock top
                         False: self.plan['margin_bottom'] + self.band_bot - self.a['zbot']}
        self.entry_seq, through = _stage_plan(0, stock_z, self.outline_depth[True], self.reach)
        if not self.plan['outline']:
            self.entry_seq = []
        elif not self.top:
            if through:
                self.entry_seq = [('mill', 0, self.outline_depth[False])]
            else:
                self.entry_seq, ok = _stage_plan(0, self.outline_depth[False], self.outline_depth[False], self.reach)
                if not ok:
                    self.warnings.append('the entry hole cannot be drilled to the outline depth')
        elif not through and self.plan['sides'] == 1:
            self.warnings.append('the entry hole cannot be drilled through the stock')
        self.staged_entry = sum(k == 'mill' for k, _, _ in self.entry_seq) > 1
        def entry_drill(s, e):
            params = dict(DRILL, **entry_ops, drillTipThroughBottom='true')
            params.update(self.top_at(s - self.m))
            if self.top and e >= stock_z - 1e-6:
                params.update(bottomHeight_mode="'from stock bottom'", bottomHeight_offset='0mm',
                              breakThroughDepth='0.5mm')
            else:
                params.update(bottomHeight_mode="'from stock top'",
                              bottomHeight_offset='%.4fmm' % -e, breakThroughDepth='0mm')
                face = self.tab_top if self.top else self.tab_bottom
                if abs(e - self.outline_depth[self.top]) < 1e-6 and face is not None:  # to the tab band
                    params.update(self.at('bottom', face))
            name = 'Entry hole %g to %g mm' % (round(s, 2), round(e, 2)) if self.staged_entry else 'Entry hole'
            return self.b.op(self.setup, 'drill', DRILL_3175, name, params, self.points([self.lay['entry']] if self.lay else []))
        self.entry_drill = entry_drill
        if self.entry_seq and self.entry_seq[0][0] == 'drill':
            self.ops['main'].append(self.entry_drill(*self.entry_seq[0][1:]))
            if self.plan['face'] and self.m > self.entry_seq[0][2] + 1e-6:
                self.warnings.append('the facing (%.1f mm) is deeper than the first stage of the entry hole'
                                % self.m)
            self.entry_seq = self.entry_seq[1:]
        self.side_entries = {}
        if self.lay:
            self.side_entries = {k: e for k, e in self.lay['pocket_entries'].items() if e['side'] == self.side}
        else:  # dry run: the entries are not sketched yet
            self.side_entries = {f['face'].tempId: {'point': None, 'depth': f['depth'],
                                               'drill': not f['side_open']}
                            for f in self.floors if f['role'] == 'pocket' and not f.get('joined')}
        # Pocket entries and pockets go down together in bands of the drill's
        # reach from the face: the entries are drilled as deep as the drill
        # reaches, the pockets cut down to there, and so on to the floors. A part
        # with no pocket deeper than the reach has one band.
        holes_in = []  # (entry point, floor depth), one per drilled hole
        for e in self.side_entries.values():
            if e['drill'] and (e['point'] is None or all(e['point'] is not q for q, _ in holes_in)):
                holes_in.append((e['point'], e['depth']))
        # entries at a hole in the floor (see _hole_entry): how far below the floor
        # T1's point goes there, None to take the tip through
        below_at = {id(e['point']): e['below'] for e in self.side_entries.values()
                    if e['drill'] and e.get('hole') is not None}
        # each entry hole's drill point goes below the pocket floor only until its
        # cone is half the end mill wide at the floor, as the end mill needs to
        # enter there (rule TH4); the rest of the cone is a dimple in the floor
        cone = self.b.tools.tip_length(DRILL_3175) * ENTRY_CONE_D / DRILL_D
        dimples = [(q, d) for q, d in holes_in if q is not None and id(q) not in below_at]
        if dimples:
            where = ', '.join('(%.2f, %.2f)' % (mm(q.geometry.x), mm(q.geometry.y)) for q, _ in dimples)
            self.warnings.append('the %d pocket entry hole%s on the %s leave%s a cone-shaped dimple %g mm '
                            'across and %.2f mm deep in the pocket floor, so that the end mill '
                            'enters a hole at least half its width; at x, y = %s mm'
                            % (len(dimples), '' if len(dimples) == 1 else 's', self.side,
                               's' if len(dimples) == 1 else '', ENTRY_CONE_D, cone, where))
            thin = [d for _, d in dimples if self.thickness - d < cone + 0.2]
            if thin:
                self.warnings.append('a pocket entry hole\'s dimple comes within 0.2 mm of the part\'s '
                                'other face, or through it, under a floor %g mm deep'
                                % max(thin))
        self.pocket_bands = []  # (top, bottom or None for the floors, [(point, drill bottom)])
        t = -self.allow
        while True:
            bot = t + self.reach
            last = not any(d > bot + 1e-6 for _, d in holes_in)
            self.pocket_bands.append((t, None if last else bot,
                                 [(p, min(bot, d)) for p, d in holes_in if d > t + 1e-6]))
            if last:
                break
            t = bot
        self.staged_pockets = len(self.pocket_bands) > 1
        floor_at = {round(f['depth'], 3): f['face'] for f in self.floors if f['role'] == 'pocket'}
        def entry_drills(band):
            t, bot, holes = band
            by_depth = {}
            for p, d in holes:
                below = below_at.get(id(p), cone) if bot is None or d <= bot + 1e-6 else cone
                by_depth.setdefault((round(d, 3), below), []).append(p)
            out = []
            for (depth, below), pts in sorted(by_depth.items(), key=lambda kv: (
                    kv[0][0], -1 if kv[0][1] is None else kv[0][1])):
                # at a floor, the point goes only `cone` below it (see above), or at a
                # hole in the floor as far as the hole lets it; a stage of the drill's
                # reach above the floor takes the tip through
                params = dict(DRILL, holeMode="'selection-points'", breakThroughDepth='0mm',
                              drillTipThroughBottom='true', **self.top_at(t))
                floor = floor_at.get(depth)
                if floor is not None and below is None:
                    params.update(self.at('bottom', floor))
                elif floor is not None:
                    params.update(self.at('bottom', floor, -round(below, 3)),
                                  drillTipThroughBottom='false')
                else:
                    params.update(_bottom_below_part_top(depth, self.m))
                name = ('Pocket entry holes %g to %g mm' % (round(max(t, 0), 2), depth)
                        if self.staged_pockets else 'Pocket entry holes %g mm' % depth)
                out.append(self.b.op(self.setup, 'drill', self.drill_for(depth - t), name, params, self.points(pts)))
            return out
        self.entry_drills = entry_drills
        # bores deeper than the drill reaches are pre-drilled in stages with their
        # rough bore, at the bore; the others are pre-drilled with the entries
        self.end_bored = [h for h, rough, _ in self.bored if rough[0] == ENDMILL]
        self.small_bored = [h for h, rough, _ in self.bored if rough[0] != ENDMILL]
        self.deep_bore = lambda h: h['depth'] > self.reach + 1e-6

    def _plan_cuts(self):
        """The pockets and windows to cut, and the end cut and rough face, which come first."""
        # holes whose rough bore would need more passes than the lab's are pocketed
        pocketed = lambda h: (h['dia'] - ENDMILL_D) / 2 > BORE_MAX_RADIAL + 1e-6
        self.floor_pre = []  # (hole, drill) pre-drilled after the rough pockets
        self.pockets = sorted([f for f in self.floors if f['role'] == 'pocket'], key=lambda f: f['depth'])
        self.narrow = [f for f in self.pockets if self.lay and f['face'].tempId in self.lay['narrow']
                  and f['face'].tempId not in self.lay['shelves']]
        self.windows = [w for w in self.a['windows']] if self.top else []
        self.entry = [self.lay['entry']] if self.lay else []
        if self.plan.get('ends') and self.top:
            self.ops['main'].append(self.end_cut('rough', 'true', ''))
        if self.facing:
            self.ops['main'].append(self.face_op('rough', 'true', '', self.allow))
        # floors the small end mills cut through slots (see below)
        self.slot_floors = {g['face'].tempId for sl in (self.lay['slots'] if self.lay else [])
                       if sl['side'] == self.side for g in [sl['floor']] + sl['fed']}
        self.passes = (('rough', 'true', ''), ('finish', 'false', ' (finish)'))
        def end_bores(which, multi, suffix):
            # the shallowest first, each rough cut just after its hole's pre-drill
            # when the hole opens in a floor: a hole under another one starts at that
            # one's floor, so that one is cut, and its floor made, before the drill
            # rapids down to it
            for hs in sorted(_group(self.end_bored),
                             key=lambda hs: self.depth_of(hs[0]['z1'] if self.top else hs[0]['z0'])):
                h = hs[0]
                if which == 'rough':
                    self.ops['main'] += self.pre_drills([(x, d) for x, d in self.floor_pre
                                               if any(x is y for y in hs)], ' in floors')
                radial = max(0.1, (h['dia'] - ENDMILL_D) / 2)
                if pocketed(h):
                    cuts = self.hole_pocket(hs, multi, suffix, which)
                    if cuts is not None or self.b.dry:
                        self.ops['main'].extend(cuts or [])
                        continue
                # the finishing bore is a single pass at the wall; in a blind hole the
                # rough bore stops the floor allowance above the floor for it to take
                # off (user, 2026-10-17); a bore has no axial stock to leave
                params = dict(BORE, stepover='0.3mm',
                              numberOfStepovers=str(max(1, math.ceil(radial / 0.3))),
                              doMultiplePasses=multi)
                bottom = -0.5 if h['through'] else (
                    POCKET_FLOOR_ALLOWANCE if which == 'rough' else 0.0)
                self.staged_bore(hs, which, (ENDMILL, which), params,
                            bottom, 'Bore %.2f%s' % (h['dia'], suffix))
        self.end_bores = end_bores
        pocket_floors = {f['face'].tempId for f in self.pockets}
        def in_floor(h):
            rim = _hole_ends(h, self.top)[0]
            return rim is not None and rim.face.tempId in pocket_floors
        def drill_ops(floor):
            """The drills (plain holes, then tap drills), grouped by tool, of the
        holes that open in a pocket floor (floor true) or of the others. Those in
        a floor are drilled before the finishing pockets, so that the finishing
        pass cleans up the burrs the drill leaves on the floor."""
            out = []
            by_drill = {}
            for h, desc in [(h, d) for h, d in self.drilled if in_floor(h) == floor]:
                by_drill.setdefault(desc, []).append(h)
            for h, t in [(h, t) for h, t in self.tapped if in_floor(h) == floor]:
                if THREADS[t][1]:
                    by_drill.setdefault(THREADS[t][1], []).append(h)
            for desc, hs in by_drill.items():
                for through in (True, False):
                    sub = [h for h in hs if h['through'] == through]
                    if not sub:
                        continue
                    params = dict(DRILL, **_drill_ends(through), **self.from_hole_top(sub))
                    if floor:
                        # the floor still carries the rough pocket's allowance
                        params['topHeight_offset'] = '%gmm' % POCKET_FLOOR_ALLOWANCE
                    if not through:
                        # full diameter to the bottom: the drill's point goes below it
                        params['drillTipThroughBottom'] = 'true'
                        self.blind_tip_warning(desc, sub)
                    out.append(self.b.op(
                        self.setup, 'drill', desc,
                        'Drill ' + desc.split('*')[0] + ('' if through else ' (blind)'),
                        params, _sel_faces('holeFaces', [h['face'] for h in sub])))
            return out
        self.drill_ops = drill_ops
        # closed pockets with no room for the 3.175's entry hole: a small end mill
        # pockets each, in one operation with no finishing pass, since only the
        # 3.175 end mill takes a finishing pass (user, 2026-10-24); after the drills
        # in the floors, which leave burrs, and before the finishing pockets. It
        # enters down a hole drilled as wide as itself, with the rough passes, whose
        # point goes below the floor only until its cone is half the end mill wide
        # there (rule TH4)
        small = {}  # end mill: [(floor, entry point)]
        self.small_tool = {}  # floor's tempId: (end mill, diameter)
        for f in self.pockets:
            sp = (self.lay or {}).get('small_pockets', {}).get(f['face'].tempId)
            if sp is None or sp['side'] != self.side:
                continue
            em, d = _small_choice(self.plan, sp['tools'])
            small.setdefault((em, d), []).append((f, sp['point']))
            self.small_tool[f['face'].tempId] = (em, d)
        used_drills = {desc for _, desc in self.drilled}
        def small_entry_drill(d, depth):
            """The drill for a small end mill's entry hole: one the part drills its
        holes with already, if one is up to 0.1 mm narrower than the end mill,
        else the library drill closest to the end mill's width, not wider."""
            fits = [x for x in self.drills if d - 0.1 - 1e-6 <= x[1] <= d + 1e-6 and x[2] >= depth + 0.5]
            return min(fits, key=lambda x: (x[0] not in used_drills, d - x[1])) if fits else None
        def small_pockets(which):
            """The entry holes (which 'rough') or the pockets ('finish')."""
            out = []
            for (em, d), group in sorted(small.items(), key=lambda kv: -kv[0][1]):
                faces = [f['face'] for f, _ in group]
                pts = [p for _, p in group]
                if which == 'rough':
                    by_drill = {}
                    for f, p in group:
                        dr = small_entry_drill(d, f['depth'])
                        if dr is None:
                            self.warnings.append('no drill as wide as the %g mm end mill reaches the pocket '
                                            'floor at z=%g mm, so its entry hole is not drilled'
                                            % (d, f['z']))
                            continue
                        by_drill.setdefault((dr[0], dr[1], f['face'].tempId), (f, []))[1].append(p)
                    for (dr, dd, _), (f, ps) in by_drill.items():
                        cone = self.b.tools.tip_length(dr) * d / 2 / dd
                        params = dict(DRILL, holeMode="'selection-points'", breakThroughDepth='0mm',
                                      drillTipThroughBottom='false', **self.top_at(-self.allow))
                        params.update(self.at('bottom', f['face'], -round(cone, 3)))
                        out.append(self.b.op(self.setup, 'drill', dr, 'Small pocket entry holes %g' % dd, params,
                                        self.points(ps)))
                    if not self.b.dry:
                        where = ', '.join('(%.2f, %.2f)' % (mm(p.geometry.x), mm(p.geometry.y)) for p in pts)
                        self.warnings.append('the %g mm end mill pockets %d pocket%s too narrow for the '
                                        "3.175 mm end mill's entry hole, entering down holes drilled "
                                        'as wide as itself, which leave a dimple %g mm across in the '
                                        'floor; at x, y = %s mm'
                                        % (d, len(group), '' if len(group) == 1 else 's', d / 2, where))
                    continue
                params = dict(POCKET, doMultipleDepths='true', bottomHeight_mode="'from contour'",
                              **self.top_at(-self.allow))
                out.append(self.b.op(self.setup, 'pocket2d', em, 'Small pockets %g' % d, params,
                                _both(_sel_pocket(faces), _sel_faces('predrillPositions', pts))))
            return out
        self.small_pockets = small_pockets

    def _passes(self):
        """The rough and finishing passes: the outline, pockets, windows and bores, and the drills between them."""
        for which, multi, suffix in self.passes:
            tk = (ENDMILL, which)
            # outline: the top setup cuts down to the tab band, the bottom setup up to it
            bands = self.entry_seq if which == 'rough' else [('mill', 0, self.outline_depth[self.top])]
            if self.plan.get('ends') and self.top and which == 'finish':
                self.ops['main'].append(self.end_cut(which, multi, suffix))
            for kind, s, e in bands if self.plan['outline'] else []:
                if kind == 'drill':
                    self.ops['main'].append(self.entry_drill(s, e))
                    continue
                params = dict(CONTOUR, doMultipleDepths=multi, bottomHeight_mode="'from stock top'",
                              bottomHeight_offset='%.4fmm' % -e, **self.top_at(s - self.m))
                name = ('Outline %g to %g mm' % (round(s, 2), round(e, 2))
                        if self.staged_entry and which == 'rough' else 'Outline' + suffix)
                set_lo = None
                if abs(e - self.outline_depth[self.top]) < 1e-6:  # down to the tab band: the tabs' face
                    lo, set_lo = _at_tab('bottom', self.tab_top if self.top else self.tab_bottom)
                    params.update(lo)
                self.ops['main'].append(self.b.op(self.setup, 'contour2d', tk, name, params, _both(
                    _sel_silhouette(), _sel_faces('entryPositions', self.entry), set_lo)))
            if which == 'rough':
                self.ops['main'] += self.later_drills()
            if which == 'finish':
                self.ops['main'] += self.drill_ops(True)
                self.ops['main'] += self.small_pockets('finish')
            if self.pockets:
                shelves = self.lay['shelves'] if self.lay else {}
                # what a pocket op selects: the plain floors, and one chain per shelf
                # region, though several shelves share it; each with its depth and entry
                units = []  # (floor face or None, chain or None, depth, [entry points], side-open)
                for f in self.pockets:
                    if f in self.narrow or f.get('joined'):
                        continue  # a joined floor: the open floor's passes cut it
                    e = self.side_entries.get(f['face'].tempId)
                    c = shelves.get(f['face'].tempId)
                    if c is None and f['face'].tempId in self.slot_floors:
                        continue  # the 3.175 can't get in; a small end mill cuts it through its slot
                    if c is None:
                        units.append((f['face'], None, f['depth'],
                                      [e['point']] if e and e['point'] else [], f['side_open']))
                    elif all(c is not u[1] for u in units):
                        units.append((None, c, f['depth'],
                                      [self.side_entries[k]['point'] for k in c['keys']
                                       if k in self.side_entries and self.side_entries[k]['point']], False))
                has_entries = any(f['face'].tempId in self.side_entries for f in self.pockets
                                  if f not in self.narrow)
                if has_entries and self.lay:
                    # a closed pocket with no room anywhere for the end mill's entry
                    # hole: a predrill pocket refuses it, and it could not cut it anyway
                    for u in [u for u in units if not u[4] and not u[3]]:
                        units.remove(u)
                        if which == 'rough' and (u[0] is None or u[0].tempId not in self.small_tool):
                            self.warnings.append('the pocket at z=%g mm has no room for the %g mm end '
                                            "mill's entry hole, nor for a small end mill's entry "
                                            'hole and pocket (the %g mm end mill needs %g mm of '
                                            'room round its entry), so no pocket cuts it; the small '
                                            "end mills' corner passes cut what they reach"
                                            % (mm(u[0].pointOnFace.z) if u[0] is not None else
                                               u[1]['z'], ENDMILL_D, SMALL_ENDMILLS[-1][1],
                                               SMALL_ENDMILLS[-1][1] / 2 + 5 * POCKET_TOL))
                has_open = any(u[4] for u in units) and not all(u[4] for u in units)
                bands = self.pocket_bands if which == 'rough' else [(-self.allow, None, [])]
                for k, band in enumerate(bands):
                    t, bot, _ = band
                    if k:
                        self.ops['main'] += self.entry_drills(band)
                    inside = [u for u in units if u[2] > t + 1e-6]
                    # the pockets this band reaches the floor of, then the deeper ones;
                    # the closed ones, entered at their holes, then those open to the side,
                    # which the end mill enters from the cut beside the opening
                    for floor, side_open in ((True, False), (True, True), (False, False), (False, True)):
                        us = [u for u in inside if (bot is None or u[2] <= bot + 1e-6) == floor
                              and u[4] == side_open]
                        if which == 'rough' and floor:
                            # a floor no deeper than the rough pass leaves on it: the
                            # rough pass would mill air
                            us = [u for u in us if u[2] > POCKET_FLOOR_ALLOWANCE + 1e-6]
                        if not us:
                            continue
                        params = dict(POCKET, doMultipleDepths=multi,
                                      rampType="'predrill'" if has_entries else "'plunge'",
                                      **self.top_at(t))
                        if floor:
                            params['bottomHeight_mode'] = "'from contour'"
                            if which == 'rough':
                                # the finishing pocket takes the last of the floor
                                params.update(useStockToLeave='true', simpleStockToLeave='false',
                                              stockToLeave='0mm',
                                              verticalStockToLeave='%gmm' % POCKET_FLOOR_ALLOWANCE)
                        else:
                            params.update(_bottom_below_part_top(bot, self.m))
                        name = ('Open pockets' if side_open and has_open else 'Pockets') + suffix
                        if self.staged_pockets and which == 'rough':
                            name += (' %g mm to the floors' % round(max(t, 0), 2) if floor
                                     else ' %g to %g mm' % (round(max(t, 0), 2), round(bot, 2)))
                        if side_open:
                            # Fusion picks where a pocket enters an open pocket, and it
                            # plunges with the tool's edge in uncut stock: contour passes
                            # along the walls instead, on the tool-centre paths sketched
                            # for them (see _make_layout), one operation per floor depth
                            by_depth = {}
                            for u in us:
                                by_depth.setdefault(round(u[2], 3), []).append(u)
                            for dep, grp in sorted(by_depth.items()):
                                if which == 'rough' and floor and dep <= POCKET_FLOOR_ALLOWANCE + 1e-6:
                                    continue  # the rough pass, above the floor's allowance, mills air
                                paths = [pth for u in grp
                                         for pth in (self.lay['open_passes'].get(u[0].tempId, [])
                                                     if self.lay else [])]
                                if not paths and self.lay:
                                    continue
                                cparams = dict(CONTOUR, doMultipleDepths=multi,
                                               compensationType="'off'", **self.top_at(t))
                                cparams.update({k: v for k, v in params.items() if k.startswith((
                                    'useStockToLeave', 'simpleStockToLeave', 'stockToLeave',
                                    'verticalStockToLeave'))})
                                if floor:
                                    cparams.update(self.at('bottom', grp[0][0]))
                                else:
                                    cparams.update({k: v for k, v in params.items()
                                                    if k.startswith('bottomHeight')})
                                nm = name if len(by_depth) == 1 else name + ' %g mm' % dep
                                self.ops['main'].append(self.b.op(self.setup, 'contour2d', tk, nm, cparams,
                                                        _sel_paths(paths)))
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
                                                          [w[0] for w in out if not w[1]], self.top)
                            elif parent is not None:
                                gp = dict(params, **self.at('top', parent[1]['face'], POCKET_FLOOR_ALLOWANCE))
                                nm = name + ' from %g mm' % round(parent[2], 2)
                            pts = []
                            for u in gu:
                                for p in u[3]:
                                    if all(p is not q for q in pts):
                                        pts.append(p)
                            self.ops['main'].append(self.b.op(self.setup, 'pocket2d', tk, nm, gp, _both(
                                _sel_pocket_regions([u[0] for u in gu if u[0] is not None],
                                                    [u[1] for u in gu if u[1] is not None], self.top),
                                _sel_faces('predrillPositions', pts), sel)))
            if which == 'rough':
                self.ops['main'] += self.pre_drills([(h, d) for h, d in self.floor_pre
                                           if all(h is not x for x in self.end_bored)], ' in floors')
                self.end_bores(which, multi, suffix)
            # tap holes bored to size: one light pass, plus a finishing pass in blind holes
            for hs in _group([h for h, _ in self.tap_bored]):
                h = hs[0]
                t = [t for x, t in self.tap_bored if x is h][0]
                bore = THREAD_RECIPES[t]['bore']
                if which == 'finish' and (h['through'] or not bore.get('blind_finish_pass')):
                    continue
                self.staged_bore(hs, which, (bore['end_mill'], which),
                            dict(BORE, stepover='%gmm' % bore['stepover'],
                                 numberOfStepovers=str(bore['passes']), doMultiplePasses=multi),
                            bore['through_bottom_offset'] if h['through'] else 0.0,
                            'Bore %s %.3f%s' % (t, h['dia'], suffix))
            # pockets too narrow for a pocket toolpath to enter: a contour along the
            # wall, entering through the pocket's entry hole
            for f in self.narrow:
                params = dict(CONTOUR, doMultipleDepths=multi)
                params.update(self.top_at(0))
                params.update(self.at('bottom', f['face']))
                e = self.side_entries.get(f['face'].tempId)
                self.ops['main'].append(self.b.op(self.setup, 'contour2d', tk, 'Narrow pocket' + suffix, params, _both(
                    _sel_floor_outlines([f['face']]),
                    _sel_faces('entryPositions', [e['point']] if e and e['point'] else []))))
            if which == 'rough':
                self.ops['main'] += self.small_pockets('rough')
            if self.windows:
                for w in self.windows:
                    e = self.window_entry(w)
                    below = self.lay['window_tops'][self.a['windows'].index(w)] if self.lay else None
                    start = (self.at('top', below) if below is not None else
                             self.top_at(0) if self.depth_of(w['z']) < 1e-6 else self.at('top', w['face']))
                    if which == 'rough' and (e is None or e['pocket']):
                        # a pocket from the through hole at the window's centre; the
                        # finishing pass, with the middle cleared, a contour
                        params = dict(POCKET, doMultipleDepths=multi)
                        params.update(start)
                        params.update(self.through_part('bottom'))
                        self.ops['main'].append(self.b.op(self.setup, 'pocket2d', tk, 'Window' + suffix, params, _both(
                            _sel_chains([w['loop']], 'pockets'),
                            _sel_faces('predrillPositions', [e['point']] if e else []))))
                    else:
                        params = dict(CONTOUR, doMultipleDepths=multi)
                        params.update(start)
                        params.update(self.through_part('bottom'))
                        self.ops['main'].append(self.b.op(self.setup, 'contour2d', tk, 'Window' + suffix, params, _both(
                            _sel_chains([w['loop']]),
                            _sel_faces('entryPositions', [e['point']]) if e else None)))
            # top setup: the outline between the tabs, down through the tab band
            if self.top and self.plan['outline']:
                params = dict(CONTOUR, doMultipleDepths=multi)
                params.update(_top_below_part_top(self.a['ztop'] - self.band_top, self.m))
                params.update(_bottom_below_part_top(self.a['ztop'] - self.chain_bot, self.m))
                hi, set_hi = _at_tab('top', self.tab_top)
                lo, set_lo = _at_tab('bottom', self.tab_bottom, -self.tab['overlap'])
                params.update(hi, **lo)
                self.ops['main'].append(self.b.op(self.setup, 'contour2d', tk, 'Outline between tabs' + suffix,
                                        params, _both(_sel_layout_chains(self.lay['corner'] if self.lay else [],
                                                                         True), set_hi, set_lo)))

    def _small_tool_ops(self):
        """The small end mills' slots, pocket and window corners, and bores."""
        # slots the 3.175 can't enter, cut along their centreline by an end mill as wide
        # as the slot from the open end, and the pockets they reach, entered at the
        # slot's closed end; one operation of each per tool
        slots = [sl for sl in (self.lay['slots'] if self.lay else []) if sl['side'] == self.side]
        by_tool = {}
        for sl in slots:
            by_tool.setdefault(sl['tool'], []).append(sl)
        slotted = set()
        for (em, d, _), sls in by_tool.items():
            params = dict(CONTOUR, doMultipleDepths='true', compensationType="'off'", **self.top_at(0))
            params.update(self.at('bottom', sls[0]['floor']['face']))
            if any(abs(sl['floor']['depth'] - sls[0]['floor']['depth']) > 1e-6 for sl in sls):
                self.warnings.append('slots of one width at different depths; check the slot cut')
            self.ops['main'].append(self.b.op(self.setup, 'contour2d', em, 'Slots %g' % d, params,
                                    _sel_open_lines([sl['line'] for sl in sls])))
            fed = [g for sl in sls for g in sl['fed']]
            if fed:
                self.ops['main'].append(self.b.op(self.setup, 'pocket2d', em, 'Pockets through slots %g' % d, dict(
                    POCKET, doMultipleDepths='true', bottomHeight_mode="'from contour'", **self.top_at(0)),
                    _both(_sel_pocket([g['face'] for g in fed]),
                          _sel_faces('predrillPositions', [sl['end'] for sl in sls if sl['fed']]))))
            slotted |= {sl['floor']['face'].tempId for sl in sls}
            slotted |= {g['face'].tempId for g in fed}
        # corners the 3.175 can't reach, in pockets and windows, with small end mills:
        # rest machining, a pocket that cuts only the slivers the 3.175 left in
        # corners tighter than its radius, entering by plunging into them. An end mill
        # fits when its radius is no larger than the corner's and its flute 0.5 mm
        # longer than the cut (a window's: the part's thickness and 0.5 mm); a corner
        # tighter than the smallest end mill keeps that one's radius, with a
        # warning; with no flute long enough, the corner keeps the 3.175's radius.
        # One operation per end mill and start height, those starting highest
        # first, then one per window.
        # A floor inside a shelf region has walls only up to that region's floor:
        # above it the region's pocket has cleared everything, its corners too, so
        # its corner cut starts there
        shelves = self.lay['shelves'] if self.lay else {}
        regions = []
        for f in self.pockets:
            c = shelves.get(f['face'].tempId)
            if c is not None and all(c is not w[1] for w in regions):
                regions.append((None, c, f['depth']))
        def corner_top(f):
            """The enclosing shelf region (or None) and the corner cut's top height."""
            w = _enclosing((f['face'], None, f['depth']), regions, 0.0, None)
            return w, (self.at('top', w[1]['face']) if w else self.top_at(0))
        corners = {}
        for f in self.pockets:
            if f['face'].tempId in slotted:
                continue
            r = f['corner_radius']
            if r is None or r >= ENDMILL_D / 2 - 1e-3:
                continue
            st = self.small_tool.get(f['face'].tempId)
            if st and st[1] / 2 <= r + 1e-3:
                continue  # its own small end mill's pocket reaches the corners
            smallest = SMALL_ENDMILLS[-1][1] / 2
            if r < smallest:
                self.warnings.append('pocket at z=%g mm has sharp or R%.2f inside corners; the %g mm '
                                'end mill leaves R%g' % (f['z'], r, 2 * smallest, smallest))
            em, d = self.corner_tool(max(r, smallest), f['depth'])
            if not em:
                self.warnings.append('pocket at z=%g mm: corner radius %.2f mm needs a smaller tool '
                                'than the library has, or is deeper than its flute' % (f['z'], r))
                continue
            w, top_h = corner_top(f)
            corners.setdefault((em, d, w[2] if w else 0.0), (w, top_h, []))[2].append(f['face'])
        for (em, d, z), (w, top_h, faces) in sorted(corners.items(), key=lambda kv: kv[0][2]):
            name = 'Pocket corners %g' % d
            if w is not None:
                name += ' from %g mm' % round(z, 2)
            self.ops['main'].append(self.b.op(self.setup, 'pocket2d', em, name, dict(
                POCKET, doMultipleDepths='true', rampType="'plunge'", useRestMachining='true',
                bottomHeight_mode="'from contour'", **top_h),
                _sel_pocket(faces)))
        # windows' corners likewise, from the part's face (the shelf pockets above a
        # window leave the same corners) through the part
        for w in self.windows:
            r = w['corner_radius']
            if r is None or r >= ENDMILL_D / 2 - 1e-3:
                continue
            smallest = SMALL_ENDMILLS[-1][1] / 2
            if r < smallest:
                self.warnings.append('window has sharp or R%.2f inside corners; the %g mm end mill '
                                'leaves R%g' % (r, 2 * smallest, smallest))
            em, d = self.corner_tool(max(r, smallest), self.thickness + 0.5)
            if not em:
                self.warnings.append('window: corner radius %.2f mm needs a smaller tool than the '
                                'library has, or is deeper than its flute; the 3.175 end mill '
                                'leaves material in its corners' % r)
                continue
            params = dict(POCKET, doMultipleDepths='true', rampType="'plunge'",
                          useRestMachining='true', **self.top_at(0))
            params.update(self.through_part('bottom'))
            self.ops['main'].append(self.b.op(self.setup, 'pocket2d', em, 'Window corners %g' % d, params,
                                    _sel_chains([w['loop']], 'pockets')))
        small = {}
        for h, rough, _ in self.bored:
            if rough[0] != ENDMILL:
                small.setdefault(rough[0], []).append(h)
        for em, group in small.items():
            for hs in _group(group):
                params = dict(BORE, stepover='0.2mm', numberOfStepovers='2', **self.from_hole_top(hs))
                params.update({'bottomHeight_offset': '-0.5mm' if hs[0]['through'] else '0mm'})
                self.ops['main'].append(self.b.op(self.setup, 'bore', em, 'Bore %.2f' % hs[0]['dia'], params,
                                        _sel_faces('circularFaces', [x['face'] for x in hs])))

    def _finish_ops(self):
        """The drills, thread mills, finishing bores and finishing face."""
        # drills (plain holes, then tap drills) of the holes not in a pocket floor
        self.ops['main'] += self.drill_ops(False)
        # thread mills
        by_thread = {}
        for h, t in self.tapped:
            by_thread.setdefault(t, []).append(h)
        for t, all_hs in by_thread.items():
            for through in (True, False):
                hs = [h for h in all_hs if h['through'] == through]
                if hs:
                    self.ops['main'].append(self.b.op(
                        self.setup, 'thread', THREADS[t][2], 'Thread ' + t + ('' if through else ' (blind)'),
                        _thread_params(t, through, max(0.0, self.high[0]) + self.rise(hs)),
                        _sel_faces('circularFaces', [h['face'] for h in hs])))
        # the finishing bores after the drills and thread mills, to clean up the
        # burrs they leave on the bores' floors and edges (user, 2026-10-17); tap holes
        # bored to size keep theirs before the thread mill, which needs the final size
        self.end_bores('finish', 'false', ' (finish)')
        # the finishing face, after every cut that leaves burrs on the face
        if self.facing:
            self.ops['main'].append(self.face_op('finish', 'false', ' (finish)', 0))

    def _chamfer_ops(self):
        """The modelled chamfers and the deburrs."""
        # the modelled 45-degree chamfers facing this setup (see _slopes), by the
        # chamfering bit with its cone on the chamfer (Fusion's 2D chamfer of the
        # chamfer faces, its width and clearance 0), the point below the chamfer's
        # lower edge by as much as the floors and tabs below and the bit's reach allow
        # the tabs start at the outline, so an edge of the part comes within the
        # chamfering bit's reach of one only within its radius of the tab's points
        tab_near = _near_points([(q.x, q.y) for q in (_tab_points(self.a, self.plan['tab']['width'] / 10)
                                                      if self.plan['outline'] else [])],
                                3.175 / 20 + 0.02)
        def band_room(depth, edges=None):
            # mm from `depth` below the face down to the tab band, or None where the
            # band does not count: the chamfer lies past it, so the tabs are behind
            # it, or none of `edges` comes near a tab
            if not self.plan['outline']:
                return None
            near = self.a['ztop'] - self.band_top if self.top else self.band_bot - self.a['zbot']
            far = self.a['ztop'] - self.band_bot if self.top else self.band_top - self.a['zbot']
            if depth >= far - 1e-6:
                return None
            if edges is not None and not any(tab_near(x, y) for e in edges
                                             for x, y in _edge_points(e)):
                return None
            return max(0.0, near - depth)
        def chamfer_room(depth, edges=None):
            # mm from `depth` below the face down to the next floor, hole bottom or tab
            room = [f['depth'] - depth for f in self.floors
                    if f['role'] == 'pocket' and f['depth'] > depth + 1e-6]
            room += [h['depth'] - depth for h in self.holes
                     if not h['through'] and h['depth'] > depth + 1e-6]
            r = band_room(depth, edges)
            if r is not None:
                room.append(r)
            return min(room + [CHAMFER_TIP_MAX + CHAMFER_CLEAR])
        # every chamfer's edges, cut or not, are left out of the deburr: a cut one
        # has no burr, and one too close above a floor is left square anyway
        by_tip, chamfered = {}, _chamfer_edges(self.a, self.side)
        for c in self.a['chamfers']:
            if c['side'] != self.side:
                continue
            depth = self.a['ztop'] - c['z0'] if self.top else c['z1'] - self.a['zbot']
            tip = min(CHAMFER_TIP_MAX, chamfer_room(depth, list(c['face'].edges)) - CHAMFER_CLEAR,
                      CHAMFER_REACH - c['width'])
            if tip < CHAMFER_TIP_MIN - 1e-6:
                self.warnings.append('a %g mm chamfer on the %s with its lower edge %g mm deep has '
                                "a floor or the tabs too close below for the chamfering bit's "
                                'point: nothing cuts it' % (c['width'], self.side, round(depth, 3)))
                continue
            by_tip.setdefault(round(tip, 3), []).append(c)
        edges_of = (self.lay or {}).get('chamfer_edges') or []
        for k, (tip, cs) in enumerate(sorted(by_tip.items())):
            name = 'Chamfers' + (' %d' % (k + 1) if len(by_tip) > 1 else '')
            params = dict(CHAMFER_OP, chamferClearance='0mm', chamferTipOffset='%gmm' % tip)
            # the passes before the last, CHAMFER_STEPDOWN deeper each, along the sharp
            # edges sketched for them; a chamfer joins those narrower than itself
            widths = sorted({round(CHAMFER_STEPDOWN * j, 4) for c in cs
                             for j in range(1, int(math.ceil(c['width'] / CHAMFER_STEPDOWN - 1e-6)))})
            for w in widths:
                es = [edges_of[self.a['chamfers'].index(c)] for c in cs
                      if c['width'] > w + 1e-6 and edges_of]
                if es:
                    self.ops['main'].append(self.b.op(self.setup, 'chamfer2d', CHAMFER, '%s (%g mm)' % (name, w),
                                            dict(params, chamferWidth='%gmm' % w),
                                            _sel_chamfer_edges(es)))
            # the last pass: the chamfer as modelled
            self.ops['main'].append(self.b.op(self.setup, 'chamfer2d', CHAMFER, name,
                                    dict(params, chamferWidth='0mm'),
                                    _sel_face_contours([c['face'] for c in cs])))
        # deburr the outer face's edges, the bit's point kept off the shallowest floor
        # and blind hole bottom below the face, and off the tabs, which still stand in
        # the outline cut; Fusion leaves out any edge where the point would go deeper.
        # The other faces level with it, such as an island's top, go with it; each
        # pocket floor whose edges fall away, where it meets the part's sides or a
        # deeper pocket, or round a hole or window through it, gets the same below
        # its own height
        if self.face is not None and self.plan['chamfer']:
            width = float(CHAMFER_OP['chamferWidth'][:-2])

            def chamfer_tip(depth, what, faces=None):
                # the point's offset below a chamfer at `depth` mm below the face;
                # the tabs count only for `faces` with a falling edge near a tab.
                # Pocket floors take the point as deep as there is room: the deeper
                # the point, the farther out over the drop the bit's axis, and the
                # nearer a wall beside the edge it reaches it (user, 2026-10-24: 0.5 mm
                # left part of the rims of holes beside a pocket's walls; 2026-10-25:
                # at 0.5 mm Fusion found no toolpath at all along the side edges of
                # sample-lid-with-tabs-v2's 3.4 mm slots, which open at the part's
                # sides, and from 0.64 mm it does). A through opening or the part's
                # side sets no depth; a hole's width does: the axis lies the point's
                # offset plus the chamfer's width in from the rim, and Fusion keeps
                # the bit its clearance off the hole's far side (it refused 0.9 mm in
                # a 2.2 mm hole, "the given lead parameters would cause a collision")
                inner = faces is not None
                ids = {g.tempId for g in faces or []}
                rims = [h for h in self.holes if (lambda r: r is not None and r.face.tempId in ids)(
                    _hole_ends(h, self.top)[0])]
                room = [f['depth'] - depth for f in self.floors
                        if f['role'] == 'pocket' and f['depth'] > depth + 1e-6]
                room += [h['depth'] - depth for h in self.holes
                         if not h['through'] and h['depth'] > depth + 1e-6]
                r = band_room(depth, None if faces is None else [
                    ce.edge for g in faces for lp in g.loops for ce in lp.coEdges
                    if _falls_away(g, ce)])
                if r is not None:
                    room.append(r)
                # the cone's top, where the bit's 3.175 mm body starts, stays
                # CHAMFER_CLEAR above the floor: the body reaches in over the floor,
                # and at 1.437 mm it rubbed sample-lid-with-tabs-v2's slot floors
                # (Fusion's simulation, Stock+Shoulder; rule DB3)
                most = CHAMFER_REACH - width - CHAMFER_CLEAR if inner else CHAMFER_TIP_MAX
                if inner:
                    most = min([most] + [h['dia'] / 2 - width - CHAMFER_CLEAR - 0.01 for h in rims])
                room.append(most + width + CHAMFER_CLEAR)
                tip = max(CHAMFER_TIP_MIN, min(most, min(room) - width - CHAMFER_CLEAR))
                if width + tip > min(room) - 0.02:
                    self.warnings.append('a floor or the tabs lie %.2f mm below %s, too close for a '
                                    '%g mm chamfer: the deburr leaves those edges out or cuts the '
                                    'tabs' % (min(room), what, width))
                return tip
            tip = chamfer_tip(0.0, 'the %s face' % self.side)
            # the stock's own sides, where it fits the part, are left as they are when
            # the face is not faced: no tool cuts them, so they have no burrs
            axes = [] if self.plan['outline'] or self.facing else ['y'] if self.plan['ends'] else ['x', 'y']
            raw = _raw_edges(self.face, self.body, axes) if axes else set()
            level = [f['face'] for f in self.floors if f['role'] == 'face' and f['face'] != self.face
                     and _has_falling_edges(f['face'], chamfered)]
            # the edges along modelled chamfers have no burr: the chamfer took it off
            if level or any(ce.edge.tempId not in raw and ce.edge.tempId not in chamfered
                            for lp in self.face.loops for ce in lp.coEdges):
                self.ops['main'].append(self.b.op(self.setup, 'chamfer2d', CHAMFER, 'Deburr', dict(
                    CHAMFER_OP, chamferTipOffset='%gmm' % round(tip, 3)),
                    _sel_deburr_faces(level, self.face, raw, chamfered)))
            by_depth = {}
            for f in self.floors:
                if f['role'] == 'pocket' and _has_falling_edges(f['face'], chamfered):
                    by_depth.setdefault(round(f['depth'], 3), []).append(f['face'])
            for dep, fs in sorted(by_depth.items()):
                t, _, _ = _deburr_tip(fs, adsk.core.Vector3D.create(0, 0, 1 if self.top else -1), dep,
                                      chamfer_tip(dep, 'the pocket floor %g mm deep' % dep, fs),
                                      width, chamfered)
                self.ops['main'].append(self.b.op(
                    self.setup, 'chamfer2d', CHAMFER,
                    'Deburr pocket floors' + (' %g mm' % dep if len(by_depth) > 1 else ''),
                    dict(CHAMFER_OP, chamferTipOffset='%gmm' % round(t, 3)),
                    _sel_deburr_faces(fs, skip=chamfered)))
        # the edges of the holes under counterbores, at the counterbore's floor, which
        # the face's deburr does not reach; the point goes down the hole below
        if self.plan['chamfer']:
            width = float(CHAMFER_OP['chamferWidth'][:-2])
            by_tip = {}
            for h in self.holes:
                g = self.hole_below(h)
                if g is None:
                    continue
                fz = (h['z0'] if self.top else h['z1']) / 10  # cm
                floor = next((f for e in h['face'].edges for f in e.faces
                              if f != h['face'] and isinstance(f.geometry, adsk.core.Plane)
                              and abs(f.pointOnFace.z - fz) < 1e-4), None)
                if floor is None:
                    continue
                room = CHAMFER_TIP_MAX + width + CHAMFER_CLEAR if g['through'] else g['depth']
                tip = max(CHAMFER_TIP_MIN, min(CHAMFER_TIP_MAX, room - width - CHAMFER_CLEAR))
                by_tip.setdefault(round(tip, 3), []).append(floor)
            for tip, fl in sorted(by_tip.items()):
                self.ops['main'].append(self.b.op(self.setup, 'chamfer2d', CHAMFER, 'Deburr counterbores', dict(
                    CHAMFER_OP, chamferTipOffset='%gmm' % tip), _sel_inner_contours(fl)))

    def _tab_cut(self):
        """The tab cut, last and in its own NC program."""
        # the last setup: the tab cut, last and in its own NC program; from the
        # bottom with two sides, from the top with one
        if self.plan['outline'] and self.top == (self.plan['sides'] == 1):
            params = dict(CONTOUR, doMultipleDepths='true', maximumStepdown='0.2mm')
            if self.top:
                params.update(_top_below_part_top(self.a['ztop'] - self.band_top, self.m))
                params.update(_bottom_below_part_top(self.a['ztop'] - self.chain_bot, self.m))
            else:
                params.update(_top_below_part_top(self.band_bot - self.a['zbot'], self.m))
                params.update(_bottom_below_part_top(self.band_top + self.tab['overlap'] - self.a['zbot'], self.m))
            hi, set_hi = _at_tab('top', self.tab_top if self.top else self.tab_bottom)
            lo, set_lo = _at_tab('bottom', self.tab_bottom if self.top else self.tab_top, -self.tab['overlap'])
            params.update(hi, **lo)
            self.ops['tabs'].append(self.b.op(self.setup, 'contour2d', (ENDMILL, 'rough'), 'Cut tabs', params,
                                    _both(_sel_layout_chains(self.lay['tabs'] if self.lay else [], self.top),
                                          set_hi, set_lo)))

    def top_at(self, depth):
        """The top height of an operation starting `depth` mm below the part's
        face. One starting at the face or above it starts at the stock's highest
        point: after a face, that face's patch plus what the face left on it."""
        if depth > 1e-6:  # a stage of a cut deeper than the drill reaches
            return _top_below_part_top(depth, self.m)
        if self.facing and self.high[0] >= self.m - 1e-6:  # not faced yet
            return {'topHeight_mode': "'from stock top'", 'topHeight_offset': '0mm'}
        ref = self.patch if self.patch is not None else self.face
        if ref is None:
            return _top_below_part_top(-self.high[0], self.m)
        return self.at('top', ref, self.high[0])

    def at(self, which, face, offset=0.0):
        """A top or bottom height at a face of the model, `offset` mm along the
        tool axis: heights come from geometry, and offsets only add the
        lab's fixed allowances (FACE_ALLOWANCE, the 0.5 mm breakthrough)."""
        return {which + 'Height_mode': "'from point'", which + 'Height_offset': '%gmm' % offset,
                which + 'Height_ref': [face]}

    def rise(self, hs):
        # mm from Fusion's hole top, the cylinder's end, up to a countersink's top
        return max([h['rise'][self.side] for h in hs] + [0.0])

    def from_hole_top(self, hs=()):
        return {'topHeight_offset': '%gmm' % (max(0.0, self.high[0]) + self.rise(hs))}

    def depth_of(self, z):
        return (self.a['ztop'] - z) if self.top else (z - self.a['zbot'])

    def hole_below(self, h):
        """The widest narrower hole that goes on down from a blind hole's floor, on
        its axis (the hole under a counterbore), or None."""
        if h['through']:
            return None
        floor = h['z0'] if self.top else h['z1']
        near = lambda u, v: abs(u - v) < 1e-3
        below = [g for g in self.a['holes'] if g['dia'] < h['dia'] - 1e-3
                 and near(g['x'], h['x']) and near(g['y'], h['y'])
                 and near(g['z1'] if self.top else g['z0'], floor)]
        return max(below, key=lambda g: g['dia']) if below else None

    def pre_drill_ends(self, h):
        """A bore pre-drill's ends. Where a narrower hole goes on below a blind
        hole (a counterbore), the drill's point goes on down until its cone meets
        the floor at that hole's edge, or the hole's own bottom if that comes
        first and it is blind: as wide as it can be without marking the floor. A
        hole below as wide as the drill takes the tip through. In a blind hole
        the 3.175 mm end mill bores or pockets, with no hole below, T1's point
        goes on below the floor until its cone is half the end mill wide there,
        as a pocket's entry hole does: the end mill plunges to the floor, and
        enters copper only down a hole at least half its width (rule TH4; user,
        2026-10-25), at the cost of a dimple in the floor. A pre-drill for a
        small end mill's bore stops at the hole's bottom."""
        ends = _drill_ends(h['through'])
        g = self.hole_below(h)
        if g is None:
            if not h['through'] and self.by_endmill(h):
                return dict(ends, bottomHeight_offset='%gmm' % -round(self.blind_cone(), 3))
            return ends
        if g['dia'] >= ENDMILL_D - 1e-3:
            return dict(ends, drillTipThroughBottom='true')
        angle = 130.0 if self.b.dry else self.b.tools.tip_angle(DRILL_3175)
        point = g['dia'] / 2 / math.tan(math.radians(angle / 2))
        if not g['through']:
            point = min(point, g['depth'])
        return dict(ends, bottomHeight_offset='%gmm' % -round(point, 3))

    def drill_for(self, depth):
        if depth > self.reach + 1e-6:
            self.warnings.append('a %.1f mm hole is deeper than the %s reaches (%.1f mm)'
                            % (depth, DRILL_3175, self.reach))
        return DRILL_3175

    def small_pre_drill(self, h):
        """The widest library drill no wider than the hole less DRILL_UNDER that
        reaches its depth, or None."""
        fits = [d for d in self.drills if d[1] <= h['dia'] - DRILL_UNDER + 1e-6 and d[2] >= h['depth'] + 0.5]
        return max(fits, key=lambda d: d[1])[0] if fits else None

    def later_drills(self):
        """T1 after the rough face and outline: tab entries from the tab band's
        top, which the outline has cut down to there, and pocket entries and
        bore pre-drills from the face, on the material left for its finish."""
        out = []
        if self.top and self.plan['outline'] and (self.lay is None or len(self.lay['corner']) > 1):
            depth = self.a['ztop'] - self.chain_bot
            params = dict(DRILL, holeMode="'selection-points'", breakThroughDepth='0mm',
                          drillTipThroughBottom='true')
            params.update(_top_below_part_top(self.a['ztop'] - self.band_top, self.m))
            params.update(_bottom_below_part_top(depth, self.m))
            hi, set_hi = _at_tab('top', self.tab_top)
            lo, set_lo = _at_tab('bottom', self.tab_bottom, -self.tab['overlap'])
            params.update(hi, **lo)
            # the drill is as wide as the outline cut's slot: it feeds down the
            # slot from above the stock, since at rapid it would rub the slot's
            # walls (Fusion's simulation flags that as a rapid move into stock)
            params.update(feedHeight_mode="'from stock top'", feedHeight_offset='1mm')
            out.append(self.b.op(self.setup, 'drill', self.drill_for(self.band_top - self.chain_bot), 'Tab entry holes',
                            params, _both(self.points([c['entry'] for c in self.lay['corner'][1:]]
                                                 if self.lay else []), set_hi, set_lo)))
        out += self.entry_drills(self.pocket_bands[0])
        # window entries, through the part, where the window contours enter
        pts = [e['point'] for e in (self.window_entry(w) for w in self.windows) if e is not None]
        if pts or (self.lay is None and self.windows):
            params = dict(DRILL, holeMode="'selection-points'", breakThroughDepth='0mm',
                          drillTipThroughBottom='true', **self.top_at(0))
            params.update(self.through_part('bottom'))
            out.append(self.b.op(self.setup, 'drill', self.drill_for(self.thickness + 0.5), 'Window entry holes',
                            params, self.points(pts)))
        # pre-drill every bore: T1 for those the 3.175 end mill bores, and for those
        # a small end mill bores, the widest drill that fits the hole, so that no
        # end mill enters solid copper (rule TH4)
        pre = [(h, DRILL_3175) for h in self.end_bored + [h for h, _ in self.tap_bored] if not self.deep_bore(h)]
        for h in self.small_bored:
            d = self.small_pre_drill(h)
            if d is None:
                self.warnings.append('no drill fits the %.2f mm hole at (%g, %g) and reaches its depth: '
                                'its bore enters solid copper' % (h['dia'], h['x'], h['y']))
            else:
                pre.append((h, d))
        # a hole in a pocket floor is pre-drilled once the pocket is roughed, just
        # before the bores: drilled now, from its top on the floor, the drill would
        # rapid down into the copper still over the floor
        in_floor = lambda h: self.depth_of(h['z1'] if self.top else h['z0']) > 1e-6
        self.floor_pre.extend((h, d) for h, d in pre if in_floor(h))
        return out + self.pre_drills([(h, d) for h, d in pre if not in_floor(h)])

    def pre_drills(self, pre, suffix=''):
        groups = {}
        for h, d in pre:
            ends = self.pre_drill_ends(h)
            groups.setdefault((d, h['through'], tuple(sorted(ends.items())), h['rise'][self.side]),
                              (ends, []))[1].append(h)
        out = []
        # those starting nearest the face first: a hole under a counterbore starts
        # at the counterbore's floor, and its drill rapids down to there, through
        # the hole the counterbore's own pre-drill has drilled by then
        top_of = lambda hs: min(self.depth_of(h['z1'] if self.top else h['z0']) - h['rise'][self.side]
                                for h in hs)
        order = lambda kv: (kv[0][0] != DRILL_3175, kv[0][0], round(top_of(kv[1][1]), 3),
                            not kv[0][1])
        for (d, through, _, _), (ends, hs) in sorted(groups.items(), key=order):
            name = 'Pre-drill bores' if d == DRILL_3175 else 'Pre-drill bores %s' % d.split('*')[0]
            out.append(self.b.op(self.setup, 'drill',
                            self.drill_for(max(h['depth'] for h in hs)) if d == DRILL_3175 else d,
                            name + suffix + ('' if through else ' (blind)'),
                            dict(DRILL, **ends, **self.from_hole_top(hs)),
                            _sel_faces('holeFaces', [h['face'] for h in hs])))
        return out

    def window_entry(self, w):
        return self.lay['window_entries'][self.a['windows'].index(w)] if self.lay else None

    def face_op(self, which, multi, suffix, leave):
        params = dict(POCKET, doMultipleDepths=multi, bottomHeight_mode="'from contour'",
                      **self.top_at(-self.m))
        if leave:
            params.update(useStockToLeave='true', simpleStockToLeave='false',
                          stockToLeave='0mm', verticalStockToLeave='%gmm' % leave)
        # The patch over the face, not the face: see _add_face_patches. The rough
        # face selects the patch's edge, not the patch: nothing walls a selected
        # face in, so Fusion takes it for an open pocket and runs the tool past
        # its edge. The finishing face selects its patch, which stops at the
        # outline: the outline cut has cleared the stock past it, so the tool
        # may run past the edge, and it reaches the entry hole outside it.
        if which == 'finish' and self.plan.get('face_patches'):
            sel = _sel_pocket([self.plan['face_patches'][self.side + ' finish']])
        elif self.patch:
            sel = _sel_pocket_outline(self.patch)
        else:
            sel = _sel_pocket([f['face'] for f in self.facing])
        op = self.b.op(self.setup, 'pocket2d', (ENDMILL, which), 'Face' + suffix, params,
                  _both(sel, _sel_faces('predrillPositions', self.entry)), barrier=True)
        self.high[0] = leave  # the face is now the stock's highest point
        return op

    def end_cut(self, which, multi, suffix):
        """The cut that frees the part's ends, when the vise holds it by its
        sides: through the whole stock from its top, starting and ending in the
        air past the stock's sides (see _end_chains), first of all, so that the
        facing enters at the end of the part through its groove."""
        params = dict(CONTOUR, doMultipleDepths=multi, topHeight_mode="'from stock top'",
                      topHeight_offset='0mm', bottomHeight_mode="'from stock bottom'",
                      bottomHeight_offset='-0.5mm')
        return self.b.op(self.setup, 'contour2d', (ENDMILL, which), 'Ends' + suffix, params,
                    _sel_layout_chains(self.lay['ends'] if self.lay else [], True), barrier=True)

    def staged_bore(self, hs, which, tool, params, bottom_offset, name):
        """A bore op for the holes `hs`; a rough bore deeper than the drill
        reaches is pre-drilled and bored in stages (_stage_plan)."""
        h = hs[0]
        t0 = self.depth_of(h['z1'] if self.top else h['z0'])  # the hole's top, below the part's
        faces = [x['face'] for x in hs]
        seq = [('mill', 0, h['depth'])]
        if which == 'rough' and self.deep_bore(h):
            seq, _ = _stage_plan(0, h['depth'], h['depth'], self.reach)
        for kind, s, e in seq:
            if t0 > 1e-6 and s <= 0:
                # a hole in a floor: the rough pockets and bores leave 0.2 mm on
                # the floor, which the rough bore's entries would plunge through
                top_h = {'topHeight_mode': "'from hole top'", 'topHeight_offset': '%gmm' % (
                    POCKET_FLOOR_ALLOWANCE if which == 'rough' else 0.0)}
            else:
                top_h = self.top_at(t0 + s)
            last = e >= h['depth'] - 1e-6
            stage = '' if len(seq) == 1 else ' %g to %g mm' % (round(s, 2), round(e, 2))
            if kind == 'drill':
                p = dict(DRILL, **top_h)
                if last:
                    p.update(self.pre_drill_ends(h))
                else:
                    p.update(_bottom_below_part_top(t0 + e, self.m), breakThroughDepth='0mm',
                             drillTipThroughBottom='true')
                self.ops['main'].append(self.b.op(self.setup, 'drill', DRILL_3175,
                                        'Pre-drill ' + name[0].lower() + name[1:] + stage, p,
                                        _sel_faces('holeFaces', faces)))
                continue
            p = dict(params, **top_h)
            if last:
                p['bottomHeight_offset'] = '%gmm' % bottom_offset
            else:
                p.update(_bottom_below_part_top(t0 + e, self.m))
            self.ops['main'].append(self.b.op(self.setup, 'bore', tool, name + stage, p,
                                    _sel_faces('circularFaces', faces)))

    def hole_pocket(self, hs, multi, suffix, which):
        """The cuts of holes too wide for the lab's bore, a pocket entered down
        the hole T1 pre-drilled at its centre, as the lab cuts its 7 mm holes:
        rough, leaving the floor allowance in a blind hole, then a finishing
        pocket (user, 2026-10-21: big holes are pocketed, not bored). A rough
        cut deeper than the drill reaches is drilled and pocketed in stages
        (_stage_plan), as a deep bore is. None where the hole has no flat face
        round its rim or at its bottom for the pocket to select: it is bored."""
        h = hs[0]
        t0 = self.depth_of(h['z1'] if self.top else h['z0'])
        rims, bottoms = zip(*(_hole_ends(x, self.top) for x in hs))
        centres = [(self.lay or {}).get('hole_centres', {}).get(x['face'].tempId) for x in hs]
        if None in rims or None in centres or (not h['through'] and None in bottoms):
            return None
        rough = which == 'rough'
        allow = POCKET_FLOOR_ALLOWANCE if rough else 0.0
        seq = [('mill', 0, h['depth'])]
        if rough and self.deep_bore(h):
            seq, _ = _stage_plan(0, h['depth'], h['depth'], self.reach)
        out = []
        for kind, s_, e in seq:
            last = e >= h['depth'] - 1e-6
            stage = '' if len(seq) == 1 else ' %g to %g mm' % (round(s_, 2), round(e, 2))
            name = 'Hole pocket %.2f%s%s' % (h['dia'], suffix, stage)
            if s_ <= 0:
                # a hole in a floor starts at that floor, where the rough pocket
                # left the allowance
                top_h = self.top_at(0) if t0 < 1e-6 else self.at('top', rims[0].face, allow)
            else:
                top_h = self.top_at(t0 + s_)
            if kind == 'drill':
                p = dict(DRILL, **top_h)
                if last:
                    p.update(self.pre_drill_ends(h))
                else:
                    p.update(_bottom_below_part_top(t0 + e, self.m), breakThroughDepth='0mm',
                             drillTipThroughBottom='true')
                out.append(self.b.op(self.setup, 'drill', DRILL_3175, 'Pre-drill ' + name[0].lower() + name[1:],
                                p, _sel_faces('holeFaces', [x['face'] for x in hs])))
                continue
            params = dict(POCKET, doMultipleDepths=multi, **top_h)
            if not last:
                params.update(_bottom_below_part_top(t0 + e, self.m))
            elif h['through']:
                params.update(self.through_part('bottom'))
            else:
                params.update(self.at('bottom', bottoms[0], allow))
            out.append(self.b.op(self.setup, 'pocket2d', (ENDMILL, which), name, params,
                            _both(_sel_chains(list(rims), 'pockets', not self.top),
                                  _sel_faces('predrillPositions', centres))))
        return out

    def blind_tip_warning(self, desc, hs):
        if self.b.dry:
            return
        angle = self.b.tools.tip_angle(desc)
        # a hole whose modelled point matches the drill's is cut as modelled
        hs = [h for h in hs if h['point_angle'] is None or abs(h['point_angle'] - angle) > 0.5]
        if not hs:
            return
        tip = self.b.tools.tip_length(desc)
        name = desc.split('*')[0]
        model = 'Model the point in the design (in the Hole command, a %g° drill point) so '                 'the design shows what is cut' % round(angle, 1)
        plain = [h for h in hs if h['point_angle'] is None]
        if plain:
            where = ', '.join('(%g, %g)' % (h['x'], h['y']) for h in plain)
            self.warnings.append('the %d blind %s mm hole%s drilled by the %s mm drill %s no modelled '
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
            self.warnings.append("the blind %s mm hole at x, y = (%g, %g) mm has a %g° point modelled, "
                            "but the %s mm drill's point is %g°: it %s. %s"
                            % (name, h['x'], h['y'], round(h['point_angle'], 1), name,
                               round(angle, 1), what, model))
        thin = [h['depth'] for h in hs if self.thickness - h['depth'] < tip + 0.2]
        if thin:
            self.warnings.append("a blind %s mm hole's drill point comes within 0.2 mm of the part's "
                            'other face, or through it, under a hole %g mm deep' % (name, max(thin)))

    # small end mills: rest machining in pockets whose corners the 3.175 can't reach,
    # one operation per tool
    def corner_tool(self, r, depth):
        return _small_choice(self.plan, _small_fits(r, depth))


def _side_ops(b, setup, a, side, plan, warnings):
    """The operations of one setup, by stage (see _Side)."""
    s = _Side(b, setup, a, side, plan, warnings)
    s._hole_plan()
    s._entries()
    s._plan_cuts()
    s._passes()
    s._small_tool_ops()
    s._finish_ops()
    s._chamfer_ops()
    s._tab_cut()
    return s.ops


def _delete_previous(cam, design):
    for s in [s for s in cam.setups if s.name.endswith(SETUP_TAG)] if cam else []:
        s.deleteMe()
    for n in [n for n in cam.ncPrograms if SETUP_TAG in n.name] if cam else []:
        n.deleteMe()
    root = design.rootComponent
    for sk in [sk for sk in root.sketches if sk.name.startswith(LAYOUT_SKETCH)]:
        sk.deleteMe()
    for pl in [pl for pl in root.constructionPlanes if pl.name.startswith(LAYOUT_SKETCH)]:
        pl.deleteMe()
    for occ in [o for o in root.occurrences if o.component.name.startswith(VISE_PREFIX)
                or o.component.name in (TABS_COMPONENT, FACES_COMPONENT, PART_COMPONENT)]:
        occ.deleteMe()
    # what a build in the old recipe mode saved on the design
    attr = design.attributes.itemByName('carvera-cam', 'layout')
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


def _strategy(a, stock, sides):
    """How the stock is machined into the part.

    outline: the stock is larger than the part in both X and Y, by the outline cut
      and MIN_FRAME on every side, so the part is cut out of it (window
      machining): an outline cut round the part, which leaves four tabs holding
      it, and a last program that cuts the tabs. Stock of the part's own X and Y
      size needs neither; its outline must then be the rectangle the stock is.
    ends: the stock is the part's size along the design's Y only (see
      _quarter_turn for X), and the part is longer than the jaws are wide: the
      vise holds the part by its sides, and an end cut frees its ends first.
      Any other stock is refused.
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
    # the design's chamfers on the bottom, which only a bottom setup cuts
    chamfers = [c for c in a.get('chamfers', []) if c['side'] == 'bottom']
    if sides is None:
        sides = 2 if floors or holes or chamfers or stock.get('bottom', 0) > 1e-3 else 1
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


def _turned_size(body, top):
    """The part's size (mm) along X, Y and Z once turned so that `top` faces up."""
    bb = body.boundingBox
    size = dict(zip('xyz', (mm(q - p) for p, q in
                            zip(bb.minPoint.asArray(), bb.maxPoint.asArray()))))
    return [size[k] for k in _turned_axes(top)]


def _turned_stock(stock, top):
    """args['stock'], given in the design's axes, in the axes of the part turned so
    that `top` faces up; its other keys (y_mode, y_offset, bottom) are already in
    the turned part's axes."""
    out = dict(stock)
    for k, d in zip('xyz', _turned_axes(top)):
        out[k] = stock[d]
    return out


def _turned_tab_z(body, top, value, back=False):
    """args['tab_z'], given along the design's axis that `top` names, as the turned
    part's Z (mm); with back, the other way."""
    if top == '+Z':
        return value
    axis = 'xyz'.index(top[1].lower())
    bb = body.boundingBox
    p = [(u + v) / 2 for u, v in zip(bb.minPoint.asArray(), bb.maxPoint.asArray())]
    m = _top_matrix(body, top)
    if back:
        m.invert()
        p[2] = value / 10
    else:
        p[axis] = value / 10
    q = adsk.core.Point3D.create(*p)
    q.transformBy(m)
    return mm(q.z) if not back else mm(q.asArray()[axis])


def _quarter_turn(body, stock, top='+Z'):
    """Must the part be turned a quarter about Z? When the stock fits the part
    along the turned part's X only, the vise must grip that side, and the jaws
    close along the setups' X axis, the turned part's Y (see _setup). `stock` is
    in the turned part's axes."""
    sx, sy, _ = _turned_size(body, top)
    fx = abs(stock['x'] - sx) <= STOCK_FIT
    fy = abs(stock['y'] - sy) <= STOCK_FIT
    return fx and not fy


def build(args):
    """Create setups, operations and NC programs for the part.

    args:
      body:      the body's name in the root component, or its path
                 '<occurrence>/<body>' (optional when the design has one visible body)
      top:       the design's face to machine first, as the top: "+Z", "-Z", "+X",
                 "-X", "+Y" or "-Y" (default: "-Z" when the bottom face has more
                 features than the top face, else "+Z"; see _machined_body). The
                 bottom setup machines the opposite face
      stock:     {"x": mm, "y": mm, "z": mm, "y_mode": "back", "y_offset": 10,
                  "bottom": mm of stock below the part (default: centred with two
                  sides, 0 with one)}; x, y and z in the design's axes, so that
                  with "top" along X the stock's x is its thickness, while y_mode,
                  y_offset and bottom are as the part is machined. Stock of the part's
                  X and Y size, or of its size along one side for a part longer
                  than the jaws are wide, needs no outline cut or tabs (see
                  _strategy); build turns the part a quarter about Z when the
                  stock fits along the design's X only (see _quarter_turn)
      sides:     1 or 2 setups (default: 2 when the bottom has features to cut or
                 stock to face, else 1)
      taps:      {"1.6": "M2", ...} hole diameter (as analyze prints it) -> thread
      name:      program name prefix (default: document name)
      face:      face the top and bottom from stock (default true)
      chamfer:   deburr the machined edges: the outer faces, the pocket floors
                 where walls fall away from them, and the holes under
                 counterbores (default true)
      tab_width: tab width along the outline in mm (default 5)
      tab_z:     the tab band's centre in mm, along the design's axis that "top"
                 names, Z by default (default: the mid-plane,
                 or the nearest height clear of pockets that open to the side)
      fixture:   model the vise and attach the setups to the machine table, for
                 collision checks (default true)
    """
    t0 = time.time()
    if args.get('mode', 'script') != 'script' or 'recipes' in args:
        raise RuntimeError('build has no recipe mode any more: it makes every operation itself')
    if 'stock' not in args:
        raise RuntimeError('args["stock"] is required: {"x":..,"y":..,"z":..} in mm')
    design = _design()
    # before reading the body: deleting an earlier build's sketches and vises
    # recomputes any feature added after them, which would leave stale faces
    _delete_previous(_cam(create=False), design)
    # with the top chosen, the stock goes into the turned part's axes; without,
    # the part stays upright or is turned over, which keeps every axis
    top_face = _top_arg(args)
    if top_face is not None:
        args = dict(args, top=top_face, stock=_turned_stock(args['stock'], top_face))
    # check the stock against the part before making the CAM product: a script
    # that fails after activating the Manufacture workspace leaves the document
    # without a working workspace, so that every later build in it fails
    if not _cam(create=False):
        body0 = _find_body(args)
        stock0 = args['stock']
        q0 = _quarter_turn(body0, stock0, top_face or '+Z')
        if q0:
            stock0 = dict(stock0, x=stock0['y'], y=stock0['x'])
        a0, top0 = _machined_body(args, design, False, q0)[1:3]
        if args.get('tab_z') is not None:
            # the band must lie in the part, as _tab_height takes it
            band0 = min(1.0, 0.4 * (a0['ztop'] - a0['zbot']))
            tz = _turned_tab_z(body0, top0, args['tab_z'])
            if not a0['zbot'] + band0 / 2 - 1e-6 <= tz <= a0['ztop'] - band0 / 2 + 1e-6:
                lo, hi = sorted(_turned_tab_z(body0, top0, z, back=True) for z in
                                (a0['zbot'] + band0 / 2, a0['ztop'] - band0 / 2))
                raise RuntimeError('tab_z %g mm puts the %g mm tab band outside the part: '
                                   "give it between %g and %g mm, along the design's %s axis"
                                   % (args['tab_z'], band0, round(lo, 3), round(hi, 3),
                                      top0[1]))
        if stock0['z'] < a0['ztop'] - a0['zbot'] - 1e-3:
            raise RuntimeError('stock is %g mm thick but the part is %g mm'
                               % (stock0['z'], a0['ztop'] - a0['zbot']))
        _strategy(a0, stock0, args.get('sides'))
    cam = _cam()
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
    if _quarter_turn(_find_body(args), args['stock'], top_face or '+Z'):
        quarter = True
        args = dict(args, stock=dict(args['stock'], x=args['stock']['y'], y=args['stock']['x']))
    else:
        quarter = False
    body0 = _find_body(args)
    body, a, top_face, counts = _machined_body(args, design, True, quarter)
    flipped = top_face == '-Z'
    if args.get('tab_z') is not None:
        args = dict(args, tab_z=_turned_tab_z(body0, top_face, args['tab_z']))
    stock = args['stock']
    thickness = a['ztop'] - a['zbot']
    if stock['z'] < thickness - 1e-3:
        raise RuntimeError('stock is %g mm thick but the part is %g mm' % (stock['z'], thickness))
    log, warnings = [], []
    strategy = _strategy(a, stock, args.get('sides'))
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
    if a['partial_windows']:
        n_ = len(a['partial_windows'])
        warnings.append('%d opening%s through the part (at x, y = %s mm) %s not open all the '
                        'way through over %s whole outline: pockets round %s stop at a floor, or '
                        'something juts into %s. No window cut goes through %s; the pockets on '
                        'each side cut what they reach, so check with check_cam (rule U1) and '
                        'the simulation that nothing is left in %s'
                        % (n_, '' if n_ == 1 else 's',
                           ', '.join('(%g, %g)' % tuple(q) for q in a['partial_windows']),
                           'is' if n_ == 1 else 'are', 'its' if n_ == 1 else 'their',
                           'it' if n_ == 1 else 'them', 'it' if n_ == 1 else 'them',
                           'it' if n_ == 1 else 'them', 'it' if n_ == 1 else 'them'))
    if a['side_holes']:
        warnings.append('%d hole%s not along Z (%s mm across), such as holes into the '
                        "part's sides, which no tool reaches from the top or the bottom: "
                        'nothing cuts %s, so make %s by hand'
                        % (len(a['side_holes']), '' if len(a['side_holes']) == 1 else 's',
                           ', '.join(sorted({'%g' % h['dia'] for h in a['side_holes']})),
                           'it' if len(a['side_holes']) == 1 else 'them',
                           'it' if len(a['side_holes']) == 1 else 'them'))
    if a['sloped_faces']:
        sl = a['sloped_faces']
        warnings.append('%d sloped face%s (%s mm2 in all, at %s degrees from level), such as '
                        'chamfers at another angle than 45 degrees or wider than %g mm: the '
                        'end mills cut only level floors and upright walls, and the chamfering '
                        'bit only 45-degree chamfers up to that width, so nothing cuts %s and '
                        'the copper there is left square; chamfer %s by hand, or change the '
                        'design'
                        % (len(sl), '' if len(sl) == 1 else 's',
                           '%g' % round(sum(f['area_mm2'] for f in sl), 1),
                           ', '.join(sorted({'%g' % f['slope_deg'] for f in sl})),
                           round(CHAMFER_REACH - CHAMFER_TIP_MIN, 2),
                           'it' if len(sl) == 1 else 'them', 'it' if len(sl) == 1 else 'them'))
    under = [c for c in a['chamfers'] if c['side'] == 'bottom']
    if under and sides == 1:
        warnings.append('%d chamfer%s on the bottom face, which one setup does not machine: '
                        'nothing cuts %s; pass "sides": 2'
                        % (len(under), '' if len(under) == 1 else 's',
                           'it' if len(under) == 1 else 'them'))
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
    plan['layout']['chamfer_edges'] = _chamfer_edge_sketches(design, a)
    # dry run to learn which tools the part needs, then number them in slot order
    # a first dry run collects what each small end mill cut could be made
    # with, and the part takes the fewest small end mills that serve them
    # all (see _small_choice); a slot's end mill is as wide as the slot
    plan['small_needs'] = [frozenset([sl['tool'][0]]) for sl in plan['layout']['slots']]
    for side in side_names:
        _side_ops(Builder(cam, tools, [], dry=True), None, a, side, plan, [])
    plan['small_set'] = _small_set(plan.pop('small_needs'))
    dry = Builder(cam, tools, [], dry=True)
    for side in side_names:
        _side_ops(dry, None, a, side, plan, [])
    for desc, key in _slot_order(dry.needed):
        tools.get(desc, key)
    b = Builder(cam, tools, log, outline=_outline_polygon(
        [f for f in (a['top_face'], a['bottom_face']) if f is not None]))
    b.defer = True
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
    def made(ops):  # each setup's operations, in the order they run (see _run_order)
        return {k: [b.make(p) for p in (_run_order(v) if k == 'main' else v)]
                for k, v in ops.items()}
    top_ops = made(_side_ops(b, top, a, 'top', plan, warnings))
    programs = [(name + '_top', top_ops['main'])]
    tab_ops = top_ops['tabs']
    if sides == 2:
        bottom = _setup(cam, 'Bottom ' + SETUP_TAG, body, machine, stock, False, x_axis,
                        vises[1], warnings)
        bottom_ops = made(_side_ops(b, bottom, a, 'bottom', plan, warnings))
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
                      'machined first, as the top, and its top face (%d features) last; the '
                      'design\'s body is hidden behind the turned copy in "%s"'
                      % (counts['top'], counts['bottom'], PART_COMPONENT))
    elif top_face != '+Z':
        log.insert(0, 'the part is turned so that the design\'s %s face is machined first, '
                      'as the top (%d features), and its %s face last (%d features); the '
                      'design\'s body is hidden behind the turned copy in "%s"'
                      % (top_face, counts['top'], {'+': '-', '-': '+'}[top_face[0]] + top_face[1],
                         counts['bottom'], PART_COMPONENT))
    print(json.dumps({
        'top': top_face,
        'turned_over': flipped,
        'quarter_turned': quarter,
        'strategy': strategy,
        'tools': [{'T': n, 'tool': d, 'preset': p} for n, d, p in tools.order],
        'operations': log,
        'nc_programs': [prog for prog, _ in programs],
        'tabs': {'z_mm': tab_z, 'band_mm': band, 'width_mm': tab_width,
                 'count': len(plan['layout']['tabs'])} if outline else None,
        'pocket_entries': sum(1 for e in plan['layout']['pocket_entries'].values() if e['drill']),
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


def _fit_predrill(cam, adjusted):
    """Move a pocket's entry 0.01 mm when Fusion finds it unusable.

    Fusion now and then refuses a pre-drill position that lies well inside the
    pocket, apparently when it falls exactly on some point of its own construction:
    of two mirror-image D-shaped windows, each entered at its centre, one cut and the
    other was "Drill position is unusable", and the same point moved 0.01 mm either
    way cut. The pocket then plunges 0.01 mm off its hole's centre, well within the
    0.02 mm that check_cam's rule E4 allows; generate() lists each change.
    """
    for s in cam.setups:
        if not s.name.endswith(SETUP_TAG):
            continue
        for o in s.allOperations:
            if o.strategy != 'pocket2d' or 'Drill position is unusable' not in (o.warning or ''):
                continue
            p = o.parameters.itemByName('predrillPositions')
            pts = list(p.value.value) if p else []
            if not pts or not all(isinstance(q, adsk.fusion.SketchPoint) for q in pts):
                continue
            for dx, dy in ((0.001, 0), (0, 0.001), (-0.001, 0), (0, -0.001)):
                moved = [q.parentSketch.sketchPoints.add(adsk.core.Point3D.create(
                    q.geometry.x + dx, q.geometry.y + dy, q.geometry.z)) for q in pts]
                p.value.value = moved
                _wait(cam.generateToolpath(o), 20)
                if 'Drill position is unusable' not in (o.warning or ''):
                    adjusted.append('%s: entry moved 0.01 mm, since Fusion found it unusable'
                                    % o.name)
                    break
                for q in moved:
                    q.deleteMe()
            else:
                p.value.value = pts
                _wait(cam.generateToolpath(o), 20)


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
    _fit_predrill(cam, adjusted)
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

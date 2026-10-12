"""Toolpaths for copper parts on SchusterLab's Makera Carvera, in Autodesk Fusion.

This module runs inside Fusion's Python interpreter (through the Fusion MCP
`script` feature or a Fusion script). It encodes the lab's CAM conventions,
learned from the parts in the project 2026-01-06_smpm-sample-package-6port:

- machine "Makera Carvera 3-axis Community Version 1.17-slab" and the
  Carvera Community post, both from the Fusion Cloud Library;
- tools from the Cloud Library "Carvera Tools/My Tools", each with the feed
  preset "Copper 101-H02" (aluminum runs the same program at 200% override);
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
import time

import adsk.cam
import adsk.core
import adsk.fusion

TOOL_LIBRARY_URL = 'cloud://Carvera Tools/My Tools'
MACHINE_URL = 'cloud://Makera Carvera 3-axis Community Version 1.mch'
POST_URL = 'cloud://Carvera.cps'
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
# Tapped holes. The lab models a tapped hole as a plain hole at the minor
# diameter, drills it slightly oversize for the thread mill, then thread-mills.
# name: (modelled diameter, tap drill or None to bore, thread mill, pitch)
THREADS = {
    'M2': (1.6, '1.7*9.6mm Drill', 'Carvera M2 Threadmill', 0.4),
    'M2.5': (2.05, '2.2*8.8mm Drill', 'Carvera M2.5 Threadmill', 0.45),
    'M3': (2.5, '2.6*8.2mm Drill', 'Carvera M3 Threadmill', 0.5),
    'M4': (3.3, None, 'Carvera M4 Threadmill', 0.7),
}

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
        _app().userInterface.workspaces.itemById('CAMEnvironment').activate()
        cam = doc.products.itemByProductType('CAMProductType')
    return adsk.cam.CAM.cast(cam)


def _find_body(args):
    design = _design()
    bodies = []
    for b in design.rootComponent.bRepBodies:
        bodies.append(b)
    for occ in design.rootComponent.allOccurrences:
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
    return {k: v for k, v in feat.items() if k not in ('face', 'loop', 'body')}


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
        'has_top_face': a['top_face'] is not None,
        'has_bottom_face': a['bottom_face'] is not None,
    }
    print(json.dumps(out, indent=1))
    return out


# ------------------------------------------------------------- libraries

class Tools:
    """Tools from the Cloud Library, numbered into Carvera ATC slots."""

    def __init__(self):
        libs = adsk.cam.CAMManager.get().libraryManager.toolLibraries
        lib = libs.toolLibraryAtURL(adsk.core.URL.create(TOOL_LIBRARY_URL))
        if lib is None:
            raise RuntimeError('tool library not found: ' + TOOL_LIBRARY_URL)
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
                raise RuntimeError('tool %r is not in %s' % (desc, TOOL_LIBRARY_URL))
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


def _machine():
    lm = adsk.cam.CAMManager.get().libraryManager
    return lm.machineLibrary.machineAtURL(adsk.core.URL.create(MACHINE_URL))


def _post():
    lm = adsk.cam.CAMManager.get().libraryManager
    return lm.postLibrary.postConfigurationAtURL(adsk.core.URL.create(POST_URL))


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


def _sel_faces(param, faces):
    def f(params):
        params.itemByName(param).value.value = list(faces)
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
DRILL = {
    'cycleType': "'chip-breaking'",
    'incrementalDepth': '2mm',
}
BORE = {
    'tolerance': '0.01mm',
    'direction': "'climb'",
    'doMultiplePasses': 'true',
}
THREAD = {
    'direction': "'climb'",
    'stepover': 'tool_stepover',
    'numberOfStepovers': '3',
    'doMultiplePasses': 'true',
}
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


def _setup(cam, name, body, machine, stock, top):
    si = cam.setups.createInput(adsk.cam.OperationTypes.MillingOperation)
    si.name = name
    si.models = [body]
    if machine is not None:
        si.machine = machine
    setup = cam.setups.add(si)
    p = setup.parameters
    if top:
        _set(p, 'job_stockMode', "'fixedbox'")
        _set(p, 'job_stockFixedX', '%gmm' % stock['x'])
        _set(p, 'job_stockFixedY', '%gmm' % stock['y'])
        _set(p, 'job_stockFixedZ', '%gmm' % stock['z'])
        _set(p, 'job_stockFixedXMode', "'center'")
        _set(p, 'job_stockFixedYMode', "'%s'" % stock.get('y_mode', 'back'))
        _set(p, 'job_stockFixedYOffset', '%gmm' % stock.get('y_offset', 10))
        _set(p, 'job_stockFixedZMode', "'center'")
        _set(p, 'wcs_origin_mode', "'stockPoint'")
        _set(p, 'wcs_origin_boxPoint', "'top 4'")
    else:
        _set(p, 'job_stockMode', "'previoussetup'")
        _set(p, 'wcs_orientation_mode', "'axesZX'")
        _set(p, 'wcs_orientation_flipZ', 'true')
        _set(p, 'wcs_origin_mode', "'stockPoint'")
        _set(p, 'wcs_origin_boxPoint', "'top 2'")
    return setup


def _tab_params(width, height, distance):
    return {'group_tabs': 'true', 'tabShape': "'rectangular'", 'tabWidth': '%gmm' % width,
            'tabHeight': '%gmm' % height, 'tabPositioning': "'distance'",
            'tabDistance': '%gmm' % distance}


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
    m = plan['margin']
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
    # M4-style taps with no tap drill are bored to the modelled diameter first
    for h, t in tapped:
        if THREADS[t][1] is None:
            bored.append((h, (ENDMILL, 'rough'), (ENDMILL, 'finish')))

    def drill_for(depth):
        return DRILL_3175 if depth + 0.5 <= 6.66 else DRILL_3175_LONG

    # T1: pre-drill everything the 3.175 end mill will bore
    pre = [h for h, rough, _ in bored if rough[0] == ENDMILL]
    if pre:
        deep = max(h['depth'] for h in pre)
        ops['main'].append(b.op(setup, 'drill', drill_for(deep), 'Pre-drill bores', dict(
            DRILL, breakThroughDepth='0.5mm',
            drillTipThroughBottom='true' if all(h['through'] for h in pre) else 'false'),
            _sel_faces('holeFaces', [h['face'] for h in pre])))

    pockets = sorted([f for f in floors if f['role'] == 'pocket'], key=lambda f: f['depth'])
    facing = [f for f in floors if f['role'] == 'face']
    windows = [w for w in a['windows']] if top else []

    passes = (('rough', 'true', ''), ('finish', 'false', ' (finish)'))
    for which, multi, suffix in passes:
        tk = (ENDMILL, which)
        if facing and plan['face']:
            ops['main'].append(b.op(setup, 'pocket2d', tk, 'Face' + suffix, dict(
                POCKET, doMultipleDepths=multi, topHeight_mode="'from stock top'",
                topHeight_offset='0mm', bottomHeight_mode="'from contour'"),
                _sel_pocket([f['face'] for f in facing])))
        if pockets:
            ops['main'].append(b.op(setup, 'pocket2d', tk, 'Pockets' + suffix, dict(
                POCKET, doMultipleDepths=multi, bottomHeight_mode="'from contour'",
                **_top_below_part_top(0, m)),
                _sel_pocket([f['face'] for f in pockets])))
        for hs in _group([h for h, rough, _ in bored if rough[0] == ENDMILL]):
            h = hs[0]
            radial = max(0.1, (h['dia'] - ENDMILL_D) / 2)
            params = dict(BORE, stepover='0.3mm',
                          numberOfStepovers=str(max(1, math.ceil(radial / 0.3))))
            params.update(_top_below_part_top(depth_of(h['z1'] if top else h['z0']), m))
            params.update({'bottomHeight_offset': '-0.5mm' if h['through'] else '0mm'})
            ops['main'].append(b.op(setup, 'bore', tk, 'Bore %.2f%s' % (h['dia'], suffix),
                                    params, _sel_faces('circularFaces', [x['face'] for x in hs])))
        if windows:
            for w in windows:
                params = dict(CONTOUR, doMultipleDepths=multi)
                params.update(_top_below_part_top(depth_of(w['z']), m))
                params.update(_bottom_below_part_top(thickness + 0.5, m))
                ops['main'].append(b.op(setup, 'contour2d', tk, 'Window' + suffix, params,
                                        _sel_chains([w['loop']])))
        # outline with tabs (top) / up to the tab band (bottom)
        params = dict(CONTOUR, doMultipleDepths=multi, topHeight_mode="'from stock top'",
                      topHeight_offset='0mm')
        if top:
            params.update(_bottom_below_part_top(a['ztop'] - (zc - tab['band'] / 2 - tab['overlap']), m))
            params.update(_tab_params(tab['width'], tab['band'] + tab['overlap'], tab['distance']))
            op = b.op(setup, 'contour2d', tk, 'Outline' + suffix, params, _sel_silhouette())
        else:
            params.update(_bottom_below_part_top((zc - tab['band'] / 2) - a['zbot'], m))
            op = b.op(setup, 'contour2d', tk, 'Outline' + suffix, params, _sel_silhouette())
        ops['main'].append(op)

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
        through = all(h['through'] for h in hs)
        ops['main'].append(b.op(setup, 'drill', desc, 'Drill ' + desc.split('*')[0], dict(
            DRILL, breakThroughDepth='0.5mm' if through else '0mm',
            drillTipThroughBottom='true' if through else 'false'),
            _sel_faces('holeFaces', [h['face'] for h in hs])))

    # thread mills
    by_thread = {}
    for h, t in tapped:
        by_thread.setdefault(t, []).append(h)
    for t, hs in by_thread.items():
        _, _, mill, pitch = THREADS[t]
        through = all(h['through'] for h in hs)
        ops['main'].append(b.op(setup, 'thread', mill, 'Thread ' + t, dict(
            THREAD, threadPitch='%gmm' % pitch,
            bottomHeight_offset='-0.3mm' if through else '0mm'),
            _sel_faces('circularFaces', [h['face'] for h in hs])))

    # deburr the outer face's edges
    if face is not None and plan['chamfer']:
        ops['main'].append(b.op(setup, 'chamfer2d', CHAMFER, 'Deburr', dict(CHAMFER_OP),
                                _sel_face_contours([face])))

    # bottom side: the tab cut, last and in its own NC program
    if not top:
        params = dict(CONTOUR, doMultipleDepths='true', maximumStepdown='0.2mm')
        params.update(_top_below_part_top((zc - tab['band'] / 2) - a['zbot'], m))
        params.update(_bottom_below_part_top((zc + tab['band'] / 2 + tab['overlap']) - a['zbot'], m))
        ops['tabs'].append(b.op(setup, 'contour2d', (ENDMILL, 'rough'), 'Cut tabs', params,
                                _sel_silhouette()))
    return ops


def _delete_previous(cam):
    for s in [s for s in cam.setups if s.name.endswith(SETUP_TAG)]:
        s.deleteMe()
    for n in [n for n in cam.ncPrograms if SETUP_TAG in n.name]:
        n.deleteMe()


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
      stock:     {"x": mm, "y": mm, "z": mm, "y_mode": "back", "y_offset": 10}
      taps:      {"1.6": "M2", ...} hole diameter (as analyze prints it) -> thread
      name:      program name prefix (default: document name)
      face:      face the top and bottom from stock (default true)
      chamfer:   deburr the outer faces (default true)
      tab_width: tab width in mm (default 5)
    """
    t0 = time.time()
    body = _find_body(args)
    a = analyze_body(body)
    if 'stock' not in args:
        raise RuntimeError('args["stock"] is required: {"x":..,"y":..,"z":..} in mm')
    stock = args['stock']
    thickness = a['ztop'] - a['zbot']
    if stock['z'] < thickness:
        raise RuntimeError('stock is %g mm thick but the part is %g mm' % (stock['z'], thickness))
    band = min(1.0, thickness / 3)
    perimeter = 2 * (a['size'][0] + a['size'][1])
    plan = {
        'margin': (stock['z'] - thickness) / 2,
        'taps': {str(k): v for k, v in (args.get('taps') or {}).items()},
        'face': args.get('face', True),
        'chamfer': args.get('chamfer', True),
        'tab': {'band': band, 'overlap': min(0.5, thickness / 6),
                'width': args.get('tab_width', 5.0), 'distance': perimeter / 4},
    }
    cam = _cam()
    _delete_previous(cam)
    tools = Tools()
    log, warnings = [], []
    # dry run to learn which tools the part needs, then number them in slot order
    dry = Builder(cam, tools, [], dry=True)
    for side in ('top', 'bottom'):
        _side_ops(dry, None, a, side, plan, [])
    for desc, key in _slot_order(dry.needed):
        tools.get(desc, key)
    b = Builder(cam, tools, log)
    machine, post = _machine(), _post()
    if machine is None:
        warnings.append('machine not found at ' + MACHINE_URL)
    if post is None:
        warnings.append('post not found at ' + POST_URL)
    name = args.get('name') or _app().activeDocument.name
    top = _setup(cam, 'Top ' + SETUP_TAG, body, machine, stock, True)
    top_ops = _side_ops(b, top, a, 'top', plan, warnings)
    bottom = _setup(cam, 'Bottom ' + SETUP_TAG, body, machine, stock, False)
    bottom_ops = _side_ops(b, bottom, a, 'bottom', plan, warnings)
    _nc_program(cam, name + '_top', top_ops['main'], post)
    _nc_program(cam, name + '_bottom', bottom_ops['main'], post)
    _nc_program(cam, name + '_tabs', bottom_ops['tabs'], post)
    unreached = [h for h in a['holes'] if h['side'] == 'none']
    for h in unreached:
        warnings.append('hole %.2f mm at (%g, %g) is closed at both ends; skipped'
                        % (h['dia'], h['x'], h['y']))
    if len(tools.order) > 6:
        warnings.append('%d tools: the Carvera ATC holds 6, so T7+ need manual changes'
                        % len(tools.order))
    print(json.dumps({
        'tools': [{'T': n, 'tool': d, 'preset': p} for n, d, p in tools.order],
        'operations': log,
        'nc_programs': [name + s for s in ('_top', '_bottom', '_tabs')],
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

    The lab's 3 passes at the preset's 0.1 mm stepover exceed the radial depth of
    small threads; Fusion then drops the extra passes and warns. Fewer passes
    give the same cut without the warning.
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
    """Generate toolpaths of this module's setups, within a time budget (args['budget'] s)."""
    args = args or {}
    budget = args.get('budget', 50)
    cam = _cam()
    adjusted = []
    ops = adsk.core.ObjectCollection.create()
    for s in cam.setups:
        if s.name.endswith(SETUP_TAG):
            for o in s.allOperations:
                if not (o.hasToolpath and o.isToolpathValid):
                    ops.add(o)
    if ops.count:
        fut = cam.generateToolpath(ops)
        if not _wait(fut, budget):
            print(json.dumps({'status': 'running', 'completed': fut.numberOfCompleted,
                              'of': fut.numberOfOperations}))
            return
    _fit_thread_passes(cam, adjusted)
    report = []
    for s in cam.setups:
        if not s.name.endswith(SETUP_TAG):
            continue
        for o in s.allOperations:
            entry = {'setup': s.name, 'op': o.name, 'toolpath': o.hasToolpath and o.isToolpathValid}
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
    print(json.dumps({'status': 'done', 'adjusted': adjusted, 'operations': report}, indent=1))

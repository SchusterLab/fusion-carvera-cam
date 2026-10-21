"""Check a document's Carvera toolpaths against the lab's rules for avoiding
crashes, spindle stalls and excessive tool wear (references/rules.md).

Runs inside Fusion's Python, on the active document, on any setups: the lab's
own as well as those carvera_cam.build made. It changes nothing in the design:
it posts each setup to temporary G-code through a temporary NC program, which
it deletes again, and reads the moves from there.

    check({})                         every milling setup
    check({'setups': ['Setup1']})     only these
    check({'budget': 50})             stop after about 50 s; call again to go on
    summary({})                       each operation's tool, time, cut length,
                                      depth, extent and plunges, for comparing
                                      two builds of a part

Prints JSON: one entry per finding, with the rule's id (see rules.md), its
severity ('error': likely crash or broken tool; 'warning': review before
cutting), the setup, the operation and the numbers behind it.
"""

import json
import math
import os
import re
import runpy
import tempfile
import time

import adsk.cam
import adsk.core
import adsk.fusion

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(tempfile.gettempdir(), 'carvera-cam-check.json')

# Thresholds, in mm and mm/min, from the lab's setups (see rules.md for the evidence).
STEPDOWN_LARGE = 0.3         # end mills of 3 mm and up
STEPDOWN_SMALL = 0.2         # smaller end mills, at most 0.2 x diameter
POCKET_STEPOVER = 0.6        # x diameter
BORE_STEPOVER = 0.3
PECK_MAX = 0.1
ACCUMULATED_PECK_MAX = 1.0
BELOW_STOCK_MAX = 0.5        # cuts below the stock bottom, besides a drill's tip
STICKOUT_WARN = 0.75         # deepest cut below stock top / tool length below holder
STICKOUT_ERROR = 0.95
ATC_SLOTS = 6
RETRACT_MIN = 2.0            # lateral rapids at least this far above the stock top
CHAMFER_TIP_MIN = 0.1        # the chamfering bit's point kept this far off the edge
CHAMFER_WIDTH_MAX = 0.2      # a deburr's chamfer (the generator's: 0.1 mm)
CHAMFER_R = 1.5875           # the 1/8" chamfering bit's radius, where no deburr says otherwise
BREAKTHROUGH = 0.5           # a through hole's drill goes this far past its bottom
SURFACES = '(part surfaces)'  # the check's last step, after every setup
MILLING = ('flat end mill', 'ball end mill', 'bull nose end mill', 'chamfer mill',
           'face mill', 'slot mill', 'lollipop mill', 'tapered mill', 'dovetail mill')


def _app():
    return adsk.core.Application.get()


def _doc_id():
    """The active document's identity: unsaved documents all share a name."""
    d = _app().activeDocument
    return getattr(d, 'creationId', None) or d.name


def _v(p, name, default=None):
    x = p.itemByName(name)
    if x is None:
        return default
    try:
        return x.value.value
    except Exception:
        return default


def _expr(p, name):
    x = p.itemByName(name)
    return x.expression if x is not None else None


def _mm(cm):
    return None if cm is None else cm * 10


class Report:
    def __init__(self):
        self.items = []

    def add(self, rule, severity, setup, op, message, **data):
        self.items.append(dict(rule=rule, severity=severity, setup=setup, op=op,
                               message=message, **data))


# ------------------------------------------------------------- the tools

def _library_presets():
    """{description: Copper preset values} from the skill's tool library."""
    tl = runpy.run_path(os.path.join(HERE, 'tool_library.py'))
    out = {}
    for t in tl['read']()['data']:
        p = next((p for p in t.get('start-values', {}).get('presets', [])
                  if p['name'] == tl['PRESET']), None)
        out[_ascii(t['description'])] = p
    return out


def _ascii(name):
    # a chamfering bit's inch sign comes garbled in some exports
    return re.sub(r'[^ -~]+', '?', name)


def _tool(o):
    p = o.parameters
    return {
        'desc': _v(p, 'tool_description', ''),
        'type': _v(p, 'tool_type', ''),
        'number': _v(p, 'tool_number'),
        'd': _mm(_v(p, 'tool_diameter', 0)),
        'flutes': _v(p, 'tool_numberOfFlutes', 1) or 1,
        'flute_len': _mm(_v(p, 'tool_fluteLength', 0)),
        # a neck behind the flutes, narrower than the cut (a thread mill's), reaches
        # as deep as its length without rubbing
        'shoulder_len': _mm(_v(p, 'tool_shoulderLength', 0)),
        'shoulder_d': _mm(_v(p, 'tool_shoulderDiameter', 0)),
        'body_len': _mm(_v(p, 'tool_bodyLength', 0)),
        'tip_angle': _v(p, 'tool_tipAngle'),
        'rpm': _v(p, 'tool_spindleSpeed', 0),
        'feed': _v(p, 'tool_feedCutting', 0),
        'plunge': _v(p, 'tool_feedPlunge', 0),
        'holder': bool(_v(p, 'holder_attached', False)),
    }


# --------------------------------------------------------- parameter rules

def _wcs_fn(setup):
    """Function mapping a world Point3D (cm) to the setup's WCS (x, y, z) in mm."""
    org, ax, ay, az = setup.workCoordinateSystem.getAsCoordinateSystem()
    um = adsk.fusion.Design.cast(_app().activeDocument.products.itemByProductType(
        'DesignProductType')).unitsManager
    k = um.convert(1, um.defaultLengthUnits, 'cm')

    def f(p):
        d = adsk.core.Vector3D.create(p.x - org.x * k, p.y - org.y * k, p.z - org.z * k)
        return d.dotProduct(ax) * 10, d.dotProduct(ay) * 10, d.dotProduct(az) * 10
    return f


def _selected(o, name):
    """The entities an operation's selection parameter holds."""
    x = o.parameters.itemByName(name)
    try:
        return list(x.value.value) if x is not None else []
    except Exception:
        return []


def _hole(face, wcs):
    """A cylindrical hole face as seen from the setup: (diameter, centre x, y,
    bottom z, through), in mm in the WCS. `through` is False when a floor or a
    drill point closes the hole's bottom, None when the hole steps into another
    cylinder there. None for a face that is no cylinder."""
    cyl = adsk.core.Cylinder.cast(getattr(face, 'geometry', None))
    if cyl is None:
        return None
    r = cyl.radius * 10
    cx, cy, _ = wcs(cyl.origin)
    ends = [(wcs(e.pointOnEdge)[2], e) for e in face.edges]
    if not ends:
        return None
    bottom = min(z for z, _ in ends)
    through = True
    for z, e in ends:
        if z > bottom + 1e-3:
            continue
        for g in e.faces:
            if g == face:
                continue
            geo = g.geometry
            if isinstance(geo, adsk.core.Cone):
                through = False  # a drill point
            elif isinstance(geo, adsk.core.Plane) and g.area * 100 < math.pi * r * r * 1.05:
                through = False  # a flat floor
            elif isinstance(geo, adsk.core.Cylinder) and through:
                through = None
    return 2 * r, cx, cy, bottom, through


def _check_op(rep, s, o, t, earlier, lab, threaded=frozenset()):
    """Rules read from the operation's and the tool's parameters. `threaded`
    holds the entity tokens of the faces the document's thread mills cut."""
    p = o.parameters
    sn, on = s.name, o.name
    strat = o.strategy
    d = t['d'] or 0
    milling = t['type'] in MILLING
    if not t['rpm']:
        rep.add('F0', 'error', sn, on, 'spindle speed is 0')
        return
    # feeds: against the tool's preset
    preset = lab.get(_ascii(t['desc']))
    if preset is None:
        rep.add('N5', 'warning', sn, on, "%s is not in the skill's tool library, so its feeds "
                "are unchecked: use a library tool, whose Copper preset holds the lab's "
                "feeds" % _ascii(t['desc']))
    else:
        diffs = {}
        for key, val in (('n', t['rpm']), ('v_f', t['feed']), ('v_f_plunge', t['plunge'])):
            want = preset.get(key)
            if want and val and abs(val - want) > 0.5 and not (key == 'v_f' and t['type'] == 'drill'):
                diffs[key] = (val, want)
        if diffs:
            rep.add('F1', 'warning', sn, on, 'feeds differ from the tool\'s Copper preset: '
                    + ', '.join('%s %g (preset %g)' % (k, a, b) for k, (a, b) in diffs.items()))
    if not t['holder']:
        rep.add('N5', 'warning', sn, on, 'the tool has no holder, so simulation cannot check '
                'holder collisions')
    if t['number'] is not None and t['number'] > ATC_SLOTS:
        rep.add('N3', 'warning', sn, on, 'T%d needs a manual tool change (the tool changer '
                'holds %d)' % (t['number'], ATC_SLOTS))

    multi = _v(p, 'doMultipleDepths')
    rest = _v(p, 'useRestMachining', False)
    if strat in ('pocket2d', 'contour2d', 'adaptive2d', 'face', 'slot', 'trace') and milling \
            and t['type'] != 'chamfer mill':
        limit = STEPDOWN_LARGE if d >= 3 - 1e-6 else min(STEPDOWN_SMALL, 0.2 * d)
        step = _mm(_v(p, 'maximumStepdown'))
        if multi and step and step > limit + 1e-6:
            rep.add('D1', 'warning', sn, on, 'stepdown %.2f mm is above the lab\'s %.2f mm for '
                    'a %.3g mm end mill' % (step, limit, d), stepdown=step)
        if multi is False and not rest:
            # a contour may finish what a pocket roughed (a window's walls)
            kinds = (strat, 'pocket2d') if strat == 'contour2d' else (strat,)
            twin = [e for e in earlier if e['strategy'] in kinds and e['multi']
                    and abs(e['d'] - d) < 1e-3]
            if not twin:
                rep.add('D4', 'warning', sn, on, 'single full-depth pass, with no multi-depth '
                        'roughing by a %.3g mm tool before it in this setup: it may cut solid '
                        'stock at full depth' % d, limit_mm=limit)
        if strat == 'pocket2d':
            so = _mm(_v(p, 'stepover'))
            if so and so > POCKET_STEPOVER * d + 1e-3:
                rep.add('D6', 'warning', sn, on, 'pocket stepover %.2f mm is above 0.6 x tool '
                        'diameter' % so)
            # a plunge, wherever it lands, is checked in the G-code (E4)
            ramp = _v(p, 'rampType', '')
            if ramp == 'disabled':
                # plunge outside stock: outside the setup's stock box, unless stock
                # contours say where the stock ends
                sc = p.itemByName('stockContours')
                if not (sc and len(sc.value.getCurveSelections())):
                    rep.add('E1', 'warning', sn, on, 'the pocket plunges outside the setup\'s '
                            'stock box and feeds in from there: select stock contours')
            elif ramp and ramp != 'predrill' and ramp != 'plunge':
                rep.add('E1', 'warning', sn, on, 'the %.3g mm end mill ramps into the pocket '
                        '(%s) instead of entering a pre-drilled hole' % (d, ramp))
        if strat == 'contour2d' and _v(p, 'doRamp', False):
            rep.add('E1', 'warning', sn, on, 'the %.3g mm end mill ramps into the contour '
                    'instead of entering a pre-drilled hole' % d)
    if strat == 'bore':
        so = _mm(_v(p, 'stepover'))
        if so and so > BORE_STEPOVER + 1e-6:
            rep.add('D8', 'warning', sn, on, 'bore stepover %.2f mm is above %.2f mm' % (so, BORE_STEPOVER))
    if strat == 'thread':
        if _v(p, 'threadingDirection', _v(p, 'direction')) not in (None, 'climb'):
            rep.add('TH1', 'warning', sn, on, 'thread milling is not climb')
    if strat == 'drill':
        cycle = _v(p, 'cycleType', '')
        peck = _mm(_v(p, 'peckingDepth'))
        acc = _mm(_v(p, 'accumulatedPeckingDepth'))
        if cycle != 'chip-breaking':
            rep.add('K1', 'warning', sn, on, 'drill cycle %r is not chip breaking; the lab always '
                    'chip-breaks with 0.1 mm pecks and retracts fully every 1 mm' % cycle)
        elif peck and peck > PECK_MAX + 1e-6:
            rep.add('K1', 'warning', sn, on, 'pecking depth %.2f mm is above %.1f mm' % (peck, PECK_MAX))
        elif acc and acc > ACCUMULATED_PECK_MAX + 1e-6:
            rep.add('K1', 'warning', sn, on, 'accumulated pecking depth %.2f mm is above %.0f mm'
                    % (acc, ACCUMULATED_PECK_MAX))
        # the holes it drills, when it selects them; a drill stopping at a height
        # of its own, such as a stage of a deep hole, is not checked for breakthrough
        wcs = _wcs_fn(s)
        to_bottom = 'from hole bottom' in (_expr(p, 'bottomHeight_mode') or '')
        tip_through = bool(_v(p, 'drillTipThroughBottom', False))
        brk = _mm(_v(p, 'breakThroughDepth', 0)) or 0.0
        short, wide = [], []
        for f in _selected(o, 'holeFaces'):
            h = _hole(f, wcs)
            if h is None:
                continue
            dia, cx, cy, _, through = h
            if to_bottom and through and (not tip_through or brk < BREAKTHROUGH - 1e-6):
                short.append((cx, cy))
            if d > dia + 0.01 and f.entityToken not in threaded:
                wide.append((dia, cx, cy))
        if short:
            rep.add('K2', 'warning', sn, on, 'the drill does not break through %d through '
                    'hole%s (tip through the bottom %s, breakthrough %.2f mm; the lab sends the '
                    'tip %.1f mm past the bottom); first at X%.3f Y%.3f'
                    % (len(short), '' if len(short) == 1 else 's', 'on' if tip_through else 'off',
                       brk, BREAKTHROUGH, short[0][0], short[0][1]), holes=len(short))
        if wide:
            rep.add('K3', 'warning', sn, on, 'the %.3g mm drill is wider than %d hole%s it drills '
                    '(%.3g mm), and no thread mill threads %s; first at X%.3f Y%.3f'
                    % (d, len(wide), '' if len(wide) == 1 else 's', wide[0][0],
                       'it' if len(wide) == 1 else 'them', wide[0][1], wide[0][2]),
                    holes=len(wide))
    if strat == 'chamfer2d':
        tip = _mm(_v(p, 'chamferTipOffset'))
        if tip is not None and tip < CHAMFER_TIP_MIN - 1e-6:
            rep.add('CH1', 'warning', sn, on, "chamfer tip offset %.2f mm is below %.1f mm: the "
                    "bit's point cuts the edge" % (tip, CHAMFER_TIP_MIN), tip_offset=tip)
        width = _mm(_v(p, 'chamferWidth'))
        if width is not None and width > CHAMFER_WIDTH_MAX + 1e-6 and                 width > _modelled_chamfer_width(s) + 1e-6:
            rep.add('CH2', 'warning', sn, on, "chamfer width %.2f mm is above a deburr's %.1f mm: "
                    'it cuts a chamfer the design does not have' % (width, CHAMFER_WIDTH_MAX),
                    width=width)
    # a finishing pass: a single-depth pass after a multi-depth roughing pass of
    # the same kind by an end mill of the same size (as in D4)
    if strat in ('pocket2d', 'contour2d') and milling and t['type'] != 'chamfer mill' \
            and multi is False and not rest:
        rough = [e for e in earlier if e['strategy'] == strat and e['multi']
                 and abs(e['d'] - d) < 1e-3]
        if any(e['number'] == t['number'] for e in rough):
            rep.add('N6', 'warning', sn, on, 'the finishing pass uses T%s, the end mill that '
                    'roughed it: the lab finishes with a second %g mm end mill'
                    % (t['number'], d))


# ------------------------------------------------------------- the G-code

ARC_SAG = 0.002  # mm: how far the straight moves of an arc may run inside it
_WORD = re.compile(r'([A-Z])\s*([-+]?\d*\.?\d+)')


def _norm(name):
    return ' '.join(re.sub(r'[()]', ' ', name).split())


def parse_gcode(text):
    """[(comment, moves)] per section; a move is (G, x0, y0, z0, x1, y1, z1). An
    arc in the XY plane comes as straight moves at most 5 degrees round and at most
    ARC_SAG off the arc, so a cut round a corner sweeps the corner, not its chord,
    and a cut round a large arc does not seem to cut inside it."""
    sections = []
    moves = None
    x = y = z = None
    g = 0
    plane = 17
    started = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.fullmatch(r'\((.*)\)', line)
        if m:
            if started and not re.fullmatch(r'\d+', m.group(1).strip()):
                moves = []
                sections.append((_norm(m.group(1)), moves))
            continue
        line = re.sub(r'\(.*?\)|;.*', '', line).upper()
        if line.startswith('G90'):
            started = True
        words = _WORD.findall(line)
        nx, ny, nz = x, y, z
        ci = cj = None
        moved = False
        for k, v in words:
            v = float(v)
            if k == 'G' and v in (0, 1, 2, 3):
                g = int(v)
            elif k == 'G' and v in (17, 18, 19):
                plane = int(v)
            elif k == 'X':
                nx, moved = v, True
            elif k == 'Y':
                ny, moved = v, True
            elif k == 'Z':
                nz, moved = v, True
            elif k == 'I':
                ci = v
            elif k == 'J':
                cj = v
        if moved and moves is not None:
            if g in (2, 3) and plane == 17 and ci is not None and cj is not None \
                    and None not in (x, y, z, nx, ny, nz):
                cx, cy = x + ci, y + cj
                rad = math.hypot(x - cx, y - cy)
                a0 = math.atan2(y - cy, x - cx)
                a1 = math.atan2(ny - cy, nx - cx)
                sweep = (a0 - a1) if g == 2 else (a1 - a0)
                sweep %= 2 * math.pi
                if sweep < 1e-9:
                    sweep = 2 * math.pi  # a full circle
                step = math.radians(5)
                if rad > ARC_SAG:
                    step = min(step, 2 * math.acos(1 - ARC_SAG / rad))
                n = max(1, int(math.ceil(sweep / step)))
                px, py, pz = x, y, z
                for i in range(1, n + 1):
                    a = a0 + (sweep * i / n) * (-1 if g == 2 else 1)
                    qx, qy = (nx, ny) if i == n else (cx + rad * math.cos(a), cy + rad * math.sin(a))
                    qz = z + (nz - z) * i / n
                    moves.append((g, px, py, pz, qx, qy, qz))
                    px, py, pz = qx, qy, qz
            else:
                moves.append((g, x, y, z, nx, ny, nz))
        x, y, z = nx, ny, nz
    return sections


def _post_setup(cam, setup, ops, post, folder):
    ni = cam.ncPrograms.createInput()
    ni.displayName = 'carvera-cam check'
    ni.operations = ops
    nc = cam.ncPrograms.add(ni)
    try:
        nc.postConfiguration = post
        p = nc.parameters
        p.itemByName('nc_program_filename').expression = "'check'"
        p.itemByName('nc_program_output_folder').expression = "'%s'" % folder.replace('\\', '/')
        try:
            p.itemByName('nc_program_openInEditor').expression = 'false'
        except Exception:
            pass
        nc.postProcess(adsk.cam.NCProgramPostProcessOptions.create())
    finally:
        nc.deleteMe()
    files = [f for f in os.listdir(folder) if f.startswith('check')]
    if not files:
        raise RuntimeError('posting %s wrote no G-code' % setup.name)
    with open(os.path.join(folder, files[0]), encoding='utf-8', errors='replace') as f:
        return f.read()


def _fixture_bodies(setup):
    bodies = []
    try:
        fixtures = list(setup.fixtures) if _v(setup.parameters, 'job_fixture', False) else []
    except Exception:
        fixtures = []
    for f in fixtures:
        occ = adsk.fusion.Occurrence.cast(f)
        if occ is not None:
            bodies += list(occ.bRepBodies)
            stack = list(occ.childOccurrences)
            while stack:
                c = stack.pop()
                bodies += list(c.bRepBodies)
                stack += list(c.childOccurrences)
        elif adsk.fusion.BRepBody.cast(f) is not None:
            bodies.append(adsk.fusion.BRepBody.cast(f))
    return bodies


def _wcs_to_world(setup):
    """Function mapping a G-code point (mm, setup WCS) to a world Point3D (cm)."""
    m = setup.workCoordinateSystem
    o, x, y, z = m.getAsCoordinateSystem()
    # the WCS matrix comes in the document's length units
    um = adsk.fusion.Design.cast(_app().activeProduct).unitsManager \
        if adsk.fusion.Design.cast(_app().activeProduct) else None
    k = um.convert(1, um.defaultLengthUnits, 'cm') if um else 0.1

    # read once: each API property read is slow, and this runs for every tool point
    ox, oy, oz = o.x * k, o.y * k, o.z * k
    xx, xy, xz, yx, yy, yz, zx, zy, zz = x.x, x.y, x.z, y.x, y.y, y.z, z.x, z.y, z.z

    def f(px, py, pz):
        px, py, pz = px / 10, py / 10, pz / 10
        return adsk.core.Point3D.create(ox + px * xx + py * yx + pz * zx,
                                        oy + px * xy + py * yy + pz * zy,
                                        oz + px * xz + py * yz + pz * zz)
    f.coords = lambda px, py, pz: (ox + (px * xx + py * yx + pz * zx) / 10,
                                   oy + (px * xy + py * yy + pz * zy) / 10,
                                   oz + (px * xz + py * yz + pz * zz) / 10)
    return f


def _world_to_wcs(setup):
    """Function mapping a world point (mm, the design's frame) to the setup's WCS (mm)."""
    m = setup.workCoordinateSystem
    o, x, y, z = m.getAsCoordinateSystem()
    um = adsk.fusion.Design.cast(_app().activeProduct).unitsManager \
        if adsk.fusion.Design.cast(_app().activeProduct) else None
    k = um.convert(1, um.defaultLengthUnits, 'cm') if um else 0.1
    ox, oy, oz = o.x * k * 10, o.y * k * 10, o.z * k * 10
    xx, xy, xz, yx, yy, yz, zx, zy, zz = x.x, x.y, x.z, y.x, y.y, y.z, z.x, z.y, z.z

    def f(px, py, pz):
        dx, dy, dz = px - ox, py - oy, pz - oz
        return (dx * xx + dy * xy + dz * xz, dx * yx + dy * yy + dz * yz,
                dx * zx + dy * zy + dz * zz)
    return f


def _segment(to_world, x, y, z0, z1):
    """A vertical stretch of a setup's G-code, from Z z0 to Z z1 at (x, y), as
    (x, y, lowest z, highest z) in the design's frame, in mm."""
    a, b = to_world(x, y, z0), to_world(x, y, z1)
    return a.x * 10, a.y * 10, min(a.z, b.z) * 10, max(a.z, b.z) * 10


def _drilled(moves, t, to_world, top, sn):
    """[(x, y, lowest z, highest z, diameter, setup, point z)] in the design's
    frame, for each hole a drill operation makes: full width from the stock top
    down to the drill's deepest point at that spot, raised by the length of its
    point; below that, the drill's cone narrows to its point at `point z`."""
    tip = 0.0
    if t['tip_angle']:
        tip = t['d'] / 2 / math.tan(math.radians(t['tip_angle']) / 2)
    deepest = {}
    for m in moves:
        if None in m[1:]:
            continue
        k = (round(m[4], 3), round(m[5], 3))
        deepest[k] = min(deepest.get(k, m[6]), m[6])
    return [_segment(to_world, x, y, z + tip, top) + (t['d'], sn, to_world(x, y, z).z * 10)
            for (x, y), z in deepest.items() if z + tip < top]


def _cleared_to(x, y, r, top, milled, other=()):
    """How deep earlier milling passes cleared the whole footprint of a tool of
    radius r at (x, y): the WCS z, or `top` where they left some of it uncut.
    The footprint is sampled at its centre and at eight points 0.05 mm inside
    its edge (a sliver at the edge is harmless); a point is cut down to the
    deepest pass in this setup whose tool covered it. `milled` holds (moves,
    tool radius) per operation. `other` holds the cuts of earlier setups, each
    as (ax, ay, bx, by, lo, hi, tool radius) in this setup's WCS: the tool swept
    the column from lo to hi along the line from (ax, ay) to (bx, by). A point
    is also cleared as deep as those columns join the stock this setup cleared."""
    rs = max(r - 0.05, 0.0)
    pts = [(x, y)] + [(x + rs * math.cos(a * math.pi / 4), y + rs * math.sin(a * math.pi / 4))
                      for a in range(8)]
    depth = [top] * len(pts)
    cols = [[] for _ in pts]
    for ax, ay, bx, by, lo, hi, rm in other:
        if min(ax, bx) > x + r + rm or max(ax, bx) < x - r - rm or \
                min(ay, by) > y + r + rm or max(ay, by) < y - r - rm:
            continue
        dx, dy = bx - ax, by - ay
        ll = dx * dx + dy * dy
        for i, (qx, qy) in enumerate(pts):
            u = 0.0 if ll < 1e-12 else max(0.0, min(1.0, ((qx - ax) * dx + (qy - ay) * dy) / ll))
            if math.hypot(qx - ax - u * dx, qy - ay - u * dy) <= rm + 1e-3:
                cols[i].append((lo, hi))
    for moves, rm in milled:
        for m in moves:
            ax, ay, bx, by = m[1], m[2], m[4], m[5]
            if min(ax, bx) > x + r + rm or max(ax, bx) < x - r - rm or \
                    min(ay, by) > y + r + rm or max(ay, by) < y - r - rm:
                continue
            mz = max(m[3], m[6])
            dx, dy = bx - ax, by - ay
            ll = dx * dx + dy * dy
            for i, (qx, qy) in enumerate(pts):
                if mz >= depth[i]:
                    continue
                u = 0.0 if ll < 1e-12 else max(0.0, min(1.0, ((qx - ax) * dx + (qy - ay) * dy) / ll))
                if math.hypot(qx - ax - u * dx, qy - ay - u * dy) <= rm + 1e-3:
                    depth[i] = mz
    for i, cs in enumerate(cols):
        # down through the columns that join what is already cleared
        moved = True
        while moved:
            moved = False
            for lo, hi in cs:
                if hi >= depth[i] - 0.01 and lo < depth[i]:
                    depth[i] = lo
                    moved = True
    return max(depth)


def _check_plunges(rep, rule, severity, sn, o, moves, t, holes, milled, to_world, top,
                   rapids=False, other=(), box=None):
    """E4 (pockets, faces and contours) and TH2 (thread mills, bores and circular
    cuts): every plunge into the stock must go into stock earlier milling passes
    have cleared, in this setup or an earlier one, and below that down a hole
    drilled earlier, in this setup or an earlier one, at least as wide as the
    tool there and full width over the rest of the plunge, but for the drill's
    cone at its bottom. TH4: where the plunge ends in the cone, the cone must be
    at least half the tool's width there, centred on it. A plunge is a straight
    move down at the feed, or also at a rapid with `rapids`. A plunge with the
    whole tool outside `box`, the stock's (x0, x1, y0, y1), is in the air beside
    the stock: stock of the part's own size has no outline cut, and the facing
    and the cuts open to the side enter there."""
    r = t['d'] / 2
    kinds = (0, 1) if rapids else (1,)
    plunges = {}  # (x, y) -> deepest plunge
    for m in moves:
        if m[0] in kinds and None not in m[1:] and abs(m[4] - m[1]) < 1e-3 \
                and abs(m[5] - m[2]) < 1e-3 and m[6] < m[3] - 1e-3 and m[6] < top - 0.01:
            k = (round(m[4], 3), round(m[5], 3))
            plunges[k] = min(plunges.get(k, m[6]), m[6])
    bad, cones = [], []
    for (x, y), z in sorted(plunges.items()):
        if box and (x + r <= box[0] + 0.01 or x - r >= box[1] - 0.01 or
                    y + r <= box[2] + 0.01 or y - r >= box[3] - 0.01):
            continue
        cleared = _cleared_to(x, y, r, top, milled, other)
        if cleared <= z + 0.01:
            continue  # into stock an earlier cut has cleared
        px, py, plo, phi = _segment(to_world, x, y, z, cleared)
        ok, why, short_cone = False, 'no hole is drilled there before it', None
        for hx, hy, hlo, hhi, hd, hs, *pt in holes:
            e = math.hypot(px - hx, py - hy)
            if e > hd / 2:
                continue
            where = '' if hs == sn else ' (drilled in %s)' % hs
            zpt = pt[0] if pt else None
            if e + r > hd / 2 + 0.02:
                why = ('the %.3g mm hole there%s is too narrow for it, or off its centre by '
                       '%.3f mm' % (hd, where, e))
                continue
            if hlo <= plo + 0.01 and hhi >= phi - 0.01:
                ok = True
                break
            # the plunge's deep end in the drill's cone, its shallow part in the
            # full-width hole: the cone, centred on the tool, must be half its width
            if zpt is not None and zpt < hlo - 1e-6 and hhi >= phi - 0.01 and plo >= zpt:
                dist, tip = plo - zpt, hlo - zpt
            elif zpt is not None and zpt > hhi + 1e-6 and hlo <= plo + 0.01 and phi <= zpt:
                dist, tip = zpt - phi, zpt - hhi
            else:
                why = 'the hole there%s is not full width over %.2f mm of it' % (
                    where, max(hlo - plo, phi - hhi))
                continue
            need = (r + 2 * e) * tip / hd  # the cone's height where it is that wide
            if dist >= need - 0.01:
                ok = True
                break
            short_cone = need - dist
        if not ok and short_cone is not None:
            cones.append((x, y, z, short_cone))
        elif not ok:
            bad.append((x, y, z, why))
    if cones:
        x, y, z, more = cones[0]
        rep.add('TH4', 'warning', sn, o.name, "the %g mm %s plunges at X%.3f Y%.3f to Z%.3f, "
                "but the drill's cone there is narrower than half the %s; its point would have "
                'to go %.2f mm deeper%s' % (t['d'], t['type'], x, y, z, t['type'], more,
                                            ' (%d of its %d plunges)' % (len(cones), len(plunges))
                                            if len(plunges) > 1 else ''),
                plunges=len(cones))
    if bad:
        x, y, z, why = bad[0]
        rep.add(rule, severity, sn, o.name, 'the %g mm %s plunges at X%.3f Y%.3f to '
                'Z%.3f, but %s%s' % (t['d'], t['type'], x, y, z, why,
                                    ' (%d of its %d plunges)' % (len(bad), len(plunges))
                                    if len(plunges) > 1 else ''),
                plunges=len(bad))


def _check_rapids(rep, sn, o, moves, t, milled, top, box=None, other=(), deadline=None,
                  points=()):
    """H2: a rapid move below the stock top runs only through stock that earlier
    passes, this operation's own included, have cleared: a move sideways sampled
    every 0.5 mm along it, and a move straight down at its end, where a hole an
    earlier drill in this setup made (`points`, see _drill_points) counts too,
    down to where its point starts. A sample with the whole tool outside `box`,
    the stock's (x0, x1, y0, y1), is in the air beside the stock."""
    r = (t['d'] or 0) / 2
    done = []  # this operation's cuts so far
    for m in moves:
        if None in m[1:]:
            continue
        if m[0] != 0:
            done.append(m)
            continue
        run = math.hypot(m[4] - m[1], m[5] - m[2])
        if min(m[3], m[6]) >= top - 0.01:
            continue
        if run < 1e-3:
            x, y, z = m[4], m[5], m[6]
            if z >= m[3] or (box and (x + r <= box[0] + 0.01 or x - r >= box[1] - 0.01 or
                                      y + r <= box[2] + 0.01 or y - r >= box[3] - 0.01)):
                continue
            cleared = _cleared_to(x, y, r, top, milled + [(done, r)], other)
            for px, py, pz, tip, d in points:
                if d >= 2 * r - 0.01 and math.hypot(px - x, py - y) <= d / 2 - r + 0.01:
                    cleared = min(cleared, pz + tip)
            if cleared > z + 0.01:
                rep.add('H2', 'error', sn, o.name, 'a rapid move runs down to Z%.3f into uncut '
                        'stock at X%.3f Y%.3f, which is cut only down to Z%.3f'
                        % (z, x, y, cleared))
                return
            continue
        n = max(1, int(math.ceil(run / 0.5)))
        for i in range(n + 1):
            if deadline and time.time() > deadline:
                return
            f = i / n
            x, y, z = (m[1] + (m[4] - m[1]) * f, m[2] + (m[5] - m[2]) * f,
                       m[3] + (m[6] - m[3]) * f)
            if z >= top - 0.01:
                continue
            if box and (x + r <= box[0] + 0.01 or x - r >= box[1] - 0.01 or
                        y + r <= box[2] + 0.01 or y - r >= box[3] - 0.01):
                continue
            cleared = _cleared_to(x, y, r, top, milled + [(done, r)], other)
            if cleared > z + 0.01:
                rep.add('H2', 'error', sn, o.name, 'a rapid move runs at Z%.3f through uncut '
                        'stock at X%.3f Y%.3f, which is cut only down to Z%.3f'
                        % (z, x, y, cleared))
                return


def _check_thread_depth(rep, sn, o, cuts, setup, holes):
    """TH3: a thread mill never goes deeper than the hole it threads. The holes
    are the operation's selected faces; each reaches its modelled bottom, or as
    deep as an earlier drill (`holes`, see _drilled) cut it at full width, if
    that is deeper. Depths are along the setup's tool axis."""
    wcs = _wcs_fn(setup)
    for f in _selected(o, 'circularFaces'):
        h = _hole(f, wcs)
        if h is None:
            continue
        dia, cx, cy, hole_bottom, _ = h
        r = dia / 2
        for hx, hy, hlo, hhi, hd, *_ in holes:
            p, q = (wcs(adsk.core.Point3D.create(hx / 10, hy / 10, z / 10)) for z in (hlo, hhi))
            if hd >= 2 * r - 0.01 and math.hypot(p[0] - cx, p[1] - cy) < 0.05:
                hole_bottom = min(hole_bottom, p[2], q[2])
        inside = [min(m[3], m[6]) for m in cuts
                  if math.hypot(m[1] - cx, m[2] - cy) < r and math.hypot(m[4] - cx, m[5] - cy) < r]
        if inside and min(inside) < hole_bottom - 0.01:
            rep.add('TH3', 'error', sn, o.name, 'the thread mill goes %.2f mm deeper than the '
                    'hole at X%.3f Y%.3f' % (hole_bottom - min(inside), cx, cy),
                    z_min=round(min(inside), 3), hole_bottom=round(hole_bottom, 3))


def _drill_points(moves, t):
    """[(x, y, point z, point length, diameter)] in the WCS, in mm, for each hole a
    drill operation makes: its deepest point at each spot."""
    if not t['tip_angle']:
        return []
    tip = t['d'] / 2 / math.tan(math.radians(t['tip_angle']) / 2)
    deepest = {}
    for m in moves:
        if None in m[1:]:
            continue
        k = (round(m[4], 3), round(m[5], 3))
        deepest[k] = min(deepest.get(k, m[6]), m[6])
    return [(x, y, z, tip, t['d']) for (x, y), z in deepest.items()]


def _check_cones(rep, sn, o, cuts, setup, points, milled, top, em):
    """TH4: a bore needs a pre-drilled hole under it at least half as wide as its
    end mill (`em`, mm) all the way down: wherever it cuts below the depth where
    the drill's cone narrows to half the end mill's diameter, or where no drill
    went, it bores into copper the drill never cut, unless an earlier pass in
    the setup (`milled`, as _cleared_to takes it) has cleared it. `points` are
    this setup's drill points (see _drill_points)."""
    wcs = _wcs_fn(setup)
    half = em / 2
    for f in _selected(o, 'circularFaces'):
        h = _hole(f, wcs)
        if h is None:
            continue
        dia, cx, cy, _, _ = h
        inside = [min(m[3], m[6]) for m in cuts if math.hypot(m[4] - cx, m[5] - cy) < dia / 2]
        if not inside:
            continue
        zb = min(inside)
        if _cleared_to(cx, cy, half / 2, top, milled) <= zb + 0.01:
            continue
        drills = [(zp, tip, d) for x, y, zp, tip, d in points
                  if math.hypot(x - cx, y - cy) <= 0.05 and d <= dia + 0.01]
        if not drills:
            why = 'no hole is drilled there before it'
        else:
            # the drill's width at a height z above its point: its cone, then its diameter
            width = lambda z, zp, tip, d: min(d, max(0.0, z - zp) * d / tip)
            zp, tip, d = max(drills, key=lambda k: width(zb, *k))
            if width(zb, zp, tip, d) >= half - 0.01:
                continue
            if d < half - 0.01:
                why = 'the %g mm drill there is narrower than half the end mill' % d
            else:
                need = zp + tip * half / d  # where the cone is half the end mill wide
                why = ("the %g mm drill's cone is narrower than half the end mill below Z%.3f; "
                       'its point would have to go %.2f mm deeper' % (d, need, need - zb))
        rep.add('TH4', 'warning', sn, o.name, 'the %g mm end mill bores the hole at X%.3f '
                'Y%.3f down to Z%.3f, but %s' % (em, cx, cy, zb, why), z_min=round(zb, 3))


def _modelled_chamfer_width(setup):
    """The widest 45-degree chamfer (mm, as deep as it is wide) the setup's model
    has facing its tool, flat or a cone about the tool axis: a 2D chamfer that wide
    or narrower cuts that chamfer, in passes, and is no extra chamfer."""
    zx = setup.workCoordinateSystem.getAsCoordinateSystem()[3]
    widest = 0.0
    for body in _setup_bodies(setup):
        for f in body.faces:
            g = f.geometry
            if not isinstance(g, (adsk.core.Plane, adsk.core.Cone)):
                continue
            ok, n = f.evaluator.getNormalAtPoint(f.pointOnFace)
            if not ok or abs(n.dotProduct(zx) - math.sqrt(0.5)) > 0.01:
                continue
            bb = f.boundingBox
            lo, hi = bb.minPoint, bb.maxPoint
            # its extent along the tool axis
            ext = abs((hi.x - lo.x) * zx.x + (hi.y - lo.y) * zx.y + (hi.z - lo.z) * zx.z)
            widest = max(widest, ext * 10)
    return widest


def _setup_bodies(setup):
    """The solid bodies of a setup's model, as the setup sees them."""
    out = []
    try:
        models = list(setup.models)
    except RuntimeError:  # a model selection the design no longer has
        return []
    for m in models:
        b = adsk.fusion.BRepBody.cast(m)
        if b is not None:
            out.append(b)
            continue
        occ = adsk.fusion.Occurrence.cast(m)
        if occ is not None:
            stack = [occ]
            while stack:
                c = stack.pop()
                try:
                    out += list(c.bRepBodies)
                    stack += list(c.childOccurrences)
                except RuntimeError:
                    pass
    return [b for b in out if b.isSolid]


def _chamfer_cone(t):
    """(tip radius, tan of the half angle, radius) of a chamfering bit, in mm."""
    p = t['params']
    tip_d = _mm(_v(p, 'tool_tipDiameter', 0) or 0)
    taper = _v(p, 'tool_taperAngle', math.pi / 4) or math.pi / 4
    if taper > 2 * math.pi + 1e-6:  # degrees
        taper = math.radians(taper)
    return tip_d / 2, math.tan(taper), t['d'] / 2


def _seg_dist(px, py, m):
    """Horizontal distance from (px, py) to a move, and the move's Z there."""
    ax, ay, az, bx, by, bz = m[1:]
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    u = 0.0 if ll < 1e-12 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll))
    return math.hypot(px - ax - u * dx, py - ay - u * dy), az + u * (bz - az)


def _deburr_edges(setup):
    """The edges a chamfering bit in this setup should deburr, as sample points
    in the WCS: every convex edge where a flat face facing up in the setup meets
    a wall falling away below it (the face's outline, and its holes' and
    pockets' rims). Concave edges, where a wall rises from a floor, are inside
    corners. Each edge comes with points along the walls rising from its face,
    every 0.2 mm, in the WCS: the bit's body cannot come nearer them than its
    radius."""
    wcs = _wcs_fn(setup)
    zx = setup.workCoordinateSystem.getAsCoordinateSystem()[3]
    out = []
    for body in _setup_bodies(setup):
        for face in body.faces:
            if not isinstance(face.geometry, adsk.core.Plane):
                continue
            ok, n = face.evaluator.getNormalAtPoint(face.pointOnFace)
            if not ok or n.dotProduct(zx) < 0.999:
                continue
            rising, edges = [], []
            for loop in face.loops:
                for ce in loop.coEdges:
                    e = ce.edge
                    other = [g for g in e.faces if g != face]
                    if not other:
                        continue
                    ev = e.evaluator
                    ok, p0, p1 = ev.getParameterExtents()
                    mid = (p0 + p1) / 2
                    ok, pt = ev.getPointAtParameter(mid)
                    ok, tan = ev.getFirstDerivative(mid)
                    if ce.isOpposedToEdge:
                        tan.scaleBy(-1)
                    # into the face: the face lies left of its co-edges, seen from its normal
                    into = n.crossProduct(tan)
                    ok, nw = other[0].evaluator.getNormalAtPoint(pt)
                    if not ok or abs(nw.dotProduct(zx)) > 0.999:
                        continue  # a face beside it in the same plane
                    if nw.dotProduct(into) < -1e-6 and abs(nw.dotProduct(zx)) > 0.01:
                        # a slope falling away, such as a modelled chamfer: an obtuse
                        # edge with no burr, which the chamfer's own cut takes
                        continue
                    if nw.dotProduct(into) >= -1e-6:
                        # the wall rises from the face: an inside corner
                        n_ = max(2, int(e.length / 0.02))
                        for k in range(n_ + 1):
                            try:  # the end parameters can fall just outside the edge's range
                                ok, q = ev.getPointAtParameter(p0 + (p1 - p0) * k / n_)
                            except RuntimeError:
                                continue
                            if ok:
                                rising.append(wcs(q))
                        continue
                    pts = []
                    for k in range(1, 6):
                        ok, q = ev.getPointAtParameter(p0 + (p1 - p0) * k / 6)
                        pts.append(wcs(q))
                    edges.append(pts)
            out += [(pts, rising) for pts in edges]
    return out


def _raw_stock_edge(pts, box, top, tol=0.01):
    """Is the edge (sample points, WCS mm) one of the stock's own, at its top and
    on a side of the stock box? No tool cuts such an edge, where the stock fits the
    part and its face is not faced, so it has no burr to take off."""
    if box is None or top is None:
        return False
    x0, x1, y0, y1 = box
    return all(abs(z - top) < tol and min(abs(x - x0), abs(x - x1), abs(y - y0), abs(y - y1)) < tol
               for x, y, z in pts)


def _check_chamfers(rep, setup, chamfers, to_world, box=None, top=None):
    """DB1: every edge _deburr_edges lists is chamfered: at each of its sample
    points, a chamfering bit's cone in this setup passes through it. Fusion
    leaves out edges where the point would go below a floor, so the G-code, not
    the selection, decides. DB2: the bit does not cut into the tabs (the bodies
    in the generator's tabs component, which still stand when it deburrs).
    `chamfers` holds (operation, tool, cuts) per chamfering operation."""
    sn = setup.name
    cones = [(o, _chamfer_cone(t), cuts) for o, t, cuts in chamfers]
    # where a wall rises from the face within the bit's radius of an edge, the
    # bit's body meets the wall before its point reaches the edge
    reach = min((c[2] for _, c, _ in cones), default=CHAMFER_R)
    missed = []
    for pts, rising in _deburr_edges(setup):
        if _raw_stock_edge(pts, box, top):
            continue
        bad = 0
        for px, py, pz in pts:
            if any(math.hypot(px - wx, py - wy) < reach for wx, wy, _ in rising):
                continue
            hit = False
            for o, (tip_r, k, rad), cuts in cones:
                for m in cuts:
                    if min(m[1], m[4]) > px + rad or max(m[1], m[4]) < px - rad or \
                            min(m[2], m[5]) > py + rad or max(m[2], m[5]) < py - rad:
                        continue
                    d, z = _seg_dist(px, py, m)
                    h = pz - z
                    if h > 0 and d < min(rad, tip_r + h * k) - 0.02:
                        hit = True
                        break
                if hit:
                    break
            bad += not hit
        if bad:
            missed.append(pts[len(pts) // 2])
    if missed:
        x, y, z = missed[0]
        rep.add('DB1', 'warning', sn, None, '%d edge%s not deburred, the first at X%.3f Y%.3f '
                'Z%.3f' % (len(missed), '' if len(missed) == 1 else 's', x, y, z),
                edges=len(missed), at=[[round(v, 3) for v in q] for q in missed[:10]])
    # DB2: the tabs
    des = adsk.fusion.Design.cast(_app().activeDocument.products.itemByProductType(
        'DesignProductType'))
    tabs = [b for occ in des.rootComponent.allOccurrences
            if occ.component.name == 'carvera-cam tabs' for b in occ.bRepBodies]
    if not tabs or not cones:
        return
    wcs = _wcs_fn(setup)
    boxes = []
    for b in tabs:
        bb = b.boundingBox
        lo, hi = bb.minPoint, bb.maxPoint
        cs = [wcs(adsk.core.Point3D.create(x, y, z)) for x in (lo.x, hi.x)
              for y in (lo.y, hi.y) for z in (lo.z, hi.z)]
        boxes.append((b, min(c[0] for c in cs), min(c[1] for c in cs),
                      max(c[0] for c in cs), max(c[1] for c in cs), max(c[2] for c in cs)))
    for o, (tip_r, k, rad), cuts in cones:
        hit = None
        for m in cuts:
            for px, py, pz in ((m[4], m[5], m[6]), ((m[1] + m[4]) / 2, (m[2] + m[5]) / 2,
                                                     (m[3] + m[6]) / 2)):
                for b, x0, y0, x1, y1, ztop in boxes:
                    if pz > ztop - 0.01:
                        continue
                    r = min(rad, tip_r + (ztop - pz) * k)
                    qx, qy = min(max(px, x0), x1), min(max(py, y0), y1)
                    if math.hypot(qx - px, qy - py) > r - 0.005:
                        continue
                    # the tab point nearest the bit, just inside the tab's top
                    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                    l = math.hypot(cx - qx, cy - qy) or 1.0
                    qx, qy = qx + 0.02 * (cx - qx) / l, qy + 0.02 * (cy - qy) / l
                    if b.pointContainment(to_world(qx, qy, ztop - 0.005)) == \
                            adsk.fusion.PointContainment.PointInsidePointContainment:
                        hit = (px, py, pz, ztop)
                        break
                if hit:
                    break
            if hit:
                break
        if hit:
            rep.add('DB2', 'error', sn, o.name, 'the chamfering bit cuts into a tab: its point '
                    'is at Z%.3f at X%.3f Y%.3f, below the tab top at Z%.3f'
                    % (hit[2], hit[0], hit[1], hit[3]))


def _check_gcode(rep, cam, s, ops, tools, sections, deadline, holes=None, swept=None,
                 surf=None, tab_ids=frozenset()):
    """The rules read from the setup's G-code. `holes` lists the holes earlier
    setups drilled (see _drilled), and `swept` the stock their end mills cut, as
    (ax, ay, az, bx, by, bz, cz, tool radius) in the design's frame, in mm: the
    tool swept the column from z = az to z = cz along the line from a to b.
    This setup's holes and cuts are added to them. `surf` collects every cut
    and the setup's stock for _check_surfaces; `tab_ids` holds the operation
    ids of the tab programs."""
    sn = s.name
    sp = s.parameters
    top = _mm(_v(sp, 'stockZHigh', 0))
    bottom = _mm(_v(sp, 'stockZLow', 0))
    box = [_mm(_v(sp, n)) for n in ('stockXLow', 'stockXHigh', 'stockYLow', 'stockYHigh')]
    box = None if None in box else box
    # match the sections to the operations, in order
    names = [_norm(o.name) for o in ops]
    by_op = {}
    i = 0
    for name, moves in sections:
        if i < len(names) and name == names[i]:
            by_op[i] = moves
            i += 1
        elif i and (i - 1) in by_op:
            by_op[i - 1] += moves  # a comment inside the operation
    fixture = _fixture_bodies(s)
    to_world = _wcs_to_world(s)
    if surf is not None and box is not None:
        # the stock block in the design's frame, for the faces no tool needs to cut
        cs = [to_world.coords(x, y, z) for x in box[:2] for y in box[2:] for z in (top, bottom)]
        surf['stocks'].append([round(10 * f(c[j] for c in cs), 4) for j in range(3)
                               for f in (min, max)])
    holes = holes if holes is not None else []
    swept = swept if swept is not None else []
    to_wcs = _world_to_wcs(s)
    other = []  # the earlier setups' cuts, in this setup's WCS
    for ax, ay, az, bx, by, bz, cz, rm in swept:
        a, b, c = to_wcs(ax, ay, az), to_wcs(bx, by, bz), to_wcs(ax, ay, cz)
        other.append((a[0], a[1], b[0], b[1], min(a[2], c[2]), max(a[2], c[2]), rm))
    milled = []  # (moves, tool radius) of the milling operations so far in this setup
    points = []  # this setup's drill points, for TH4
    chamfers = []  # (operation, tool, cuts) of the chamfering bits, for DB1 and DB2
    fx_boxes = []
    for b in fixture:
        bb = b.boundingBox
        lo, hi = bb.minPoint, bb.maxPoint
        fx_boxes.append((b, (lo.x, lo.y, lo.z, hi.x, hi.y, hi.z)))
    hits = 0
    for idx, o in enumerate(ops):
        moves = by_op.get(idx)
        t = tools[idx]
        if not moves:
            rep.add('G0', 'warning', sn, o.name, 'no G-code found for this operation')
            continue
        cuts = [m for m in moves if m[0] != 0 and None not in m[1:]]
        if not cuts:
            continue
        _check_rapids(rep, sn, o, moves, t, milled, top, box, other, deadline, points)
        if t['type'] == 'chamfer mill':
            chamfers.append((o, dict(t, params=o.parameters), cuts))
        if t['type'] == 'drill':
            holes += _drilled(moves, t, to_world, top, sn)
            points += _drill_points(moves, t)
        elif o.strategy in ('thread', 'bore', 'circular'):
            _check_plunges(rep, 'TH2', 'error' if o.strategy == 'thread' else 'warning', sn, o,
                           moves, t, holes, milled, to_world, top, rapids=True, other=other)
            if o.strategy != 'thread':
                _check_cones(rep, sn, o, cuts, s, points, milled, top, t['d'])
            if t['type'] in MILLING:
                milled.append((cuts, t['d'] / 2))
        elif t['type'] in MILLING:
            if o.strategy in ('pocket2d', 'contour2d') and t['type'] != 'chamfer mill':
                _check_plunges(rep, 'E4', 'warning', sn, o, moves, t, holes, milled, to_world, top,
                               other=other, box=box)
            milled.append((cuts, t['d'] / 2))
        if surf is not None and t['type'] != 'chamfer mill' and t['d']:
            _add_cuts(surf, s, o, t, cuts, to_world, top, o.operationId in tab_ids)
        if t['type'] in MILLING and t['type'] != 'chamfer mill':
            # what this cut clears, for the setups after this one: the column
            # from the move's shallower end up to the stock top
            for m in cuts:
                a = to_world.coords(m[1], m[2], max(m[3], m[6]))
                b = to_world.coords(m[4], m[5], max(m[3], m[6]))
                c = to_world.coords(m[1], m[2], top)
                swept.append(tuple(round(v * 10, 4) for v in a + b + (c[2],)) + (t['d'] / 2,))
        if o.strategy == 'thread':
            _check_thread_depth(rep, sn, o, cuts, s, holes)
        # the toolpath's retract plane against the operation's current heights: a
        # toolpath computed for another stock or model keeps its old heights
        p = o.parameters
        retract = _mm(_v(p, 'retractHeight_value'))
        lateral = [max(m[3], m[6]) for m in moves if m[0] == 0 and None not in m[1:]
                   and (abs(m[4] - m[1]) > 1e-3 or abs(m[5] - m[2]) > 1e-3)]
        if retract is not None and lateral and _v(p, 'retractHeight_mode') == 'from stock top':
            plane = max(lateral)
            if plane < retract - 0.05:
                rep.add('P3', 'error', sn, o.name, 'the toolpath moves across at Z %.2f, below '
                        'its retract height Z %.2f: it was computed for another stock or model; '
                        'regenerate it' % (plane, retract))
                continue
        if retract is not None and _v(p, 'retractHeight_mode') == 'from stock top' \
                and retract - top < RETRACT_MIN - 1e-6:
            rep.add('H1', 'warning', sn, o.name, 'retract height is only %.2f mm above the stock '
                    'top (the lab uses 5 mm)' % (retract - top))
        zmin = min(min(m[3], m[6]) for m in cuts)
        # the operation's own top: where its feed moves start
        feed_z = next((m[3] for m in moves if m[0] == 1 and m[3] is not None), top)
        off = _mm(_v(o.parameters, 'feedHeight_offset')) if \
            (_v(o.parameters, 'feedHeight_mode') == 'from top') else None
        if off is not None:
            op_top = min(top, feed_z - off)
        else:  # no feed height from the top: the operation's top height, if set
            top_h = _mm(_v(o.parameters, 'topHeight_value'))
            op_top = min(top, top_h) if top_h is not None else top
        depth = op_top - zmin
        tip = 0.0
        if t['type'] == 'drill' and t['tip_angle']:
            tip = t['d'] / 2 / math.tan(math.radians(t['tip_angle']) / 2)
        below = bottom - zmin
        if below > BELOW_STOCK_MAX + tip + 0.01:
            rep.add('H3', 'error', sn, o.name, 'cuts %.2f mm below the stock bottom (the lab '
                    'stops %.1f mm below%s)' % (below, BELOW_STOCK_MAX,
                                               ', plus the drill tip' if tip else ''),
                    z_min=round(zmin, 3))
        reach_len = t['flute_len']
        if t['shoulder_d'] and t['d'] and t['shoulder_d'] < t['d'] - 0.01:
            reach_len = max(reach_len, t['shoulder_len'])
        if reach_len and depth > reach_len + tip + 0.01 and t['type'] != 'chamfer mill':
            rep.add('H5', 'error' if t['type'] == 'drill' else 'warning', sn, o.name,
                    'cuts %.2f mm deep with a %.2f mm %s' % (
                        depth, reach_len, 'flute' if reach_len == t['flute_len'] else 'neck'),
                    depth=round(depth, 2))
        reach = top - zmin
        if t['body_len']:
            ratio = reach / t['body_len']
            if ratio > STICKOUT_WARN:
                rep.add('H6', 'error' if ratio > STICKOUT_ERROR else 'warning', sn, o.name,
                        'reaches %.2f mm below the stock top, %.0f%% of the %.1f mm the tool '
                        'sticks out of its holder' % (reach, 100 * ratio, t['body_len']))
        # the tool against the fixture's bodies
        if fx_boxes and time.time() < deadline and hits < 3:
            # every 0.5 mm along each move: the tip's centre, and eight points
            # round the tool's full diameter, which a drill reaches its point's
            # length above its tip. That also catches a cut through the stock's
            # bottom into a jaw's ledge under the stock.
            r = t['d'] / 2
            lift = max(0.05, tip)
            pts = []
            for m in moves:
                if None in m[1:]:
                    continue
                n = max(1, int(math.ceil(math.dist(m[1:4], m[4:7]) / 0.5)))
                for k in range(1, n + 1):
                    f = k / n
                    px, py, pz = (m[1] + (m[4] - m[1]) * f, m[2] + (m[5] - m[2]) * f,
                                  m[3] + (m[6] - m[3]) * f)
                    pts.append((px, py, pz + 0.05))
                    for a in range(8):
                        pts.append((px + r * math.cos(a * math.pi / 4),
                                    py + r * math.sin(a * math.pi / 4), pz + lift))
            hit = None
            for px, py, pz in pts:
                wx, wy, wz = to_world.coords(px, py, pz)
                for b, (x0, y0, z0, x1, y1, z1) in fx_boxes:
                    if not (x0 <= wx <= x1 and y0 <= wy <= y1 and z0 <= wz <= z1):
                        continue
                    w = adsk.core.Point3D.create(wx, wy, wz)
                    if b.pointContainment(w) == adsk.fusion.PointContainment.PointInsidePointContainment:
                        hit = (px, py, pz, '%s/%s' % (b.parentComponent.name, b.name))
                        break
                if hit:
                    break
            if hit:
                hits += 1
                rep.add('X1', 'error', sn, o.name, 'the tool enters the fixture body %r at '
                        'X%.2f Y%.2f Z%.2f' % (hit[3], hit[0], hit[1], hit[2]))
    _check_chamfers(rep, s, chamfers, to_world, box, top)


# ------------------------------------------------- the part's own surfaces

GOUGE_TOL = 0.02             # a tool this far into the part is a gouge (a drill: 0.03)
UNCUT_TOL = 0.03             # stock this thin left on a face counts as cut
FACE_POINTS = 4000           # sample points per face, at most, 0.5 mm apart at least
EDGE_MARGINS = (0.1, 0.3, 0.55)  # sample points kept this far off a face's edges


def _add_cuts(surf, setup, o, t, cuts, to_world, top, in_tabs):
    """Adds an operation's cutting moves to `surf`, in the design's frame, in mm,
    as (ax, ay, az, bx, by, bz, cz, tool radius, operation index): the tool's
    tip ran from a to b, and its body rises from there towards cz, the stock top.
    A drill's tip is raised to where its point reaches full width."""
    kind = 'drill' if t['type'] == 'drill' else 'thread' if o.strategy == 'thread' else 'mill'
    lift = 0.0
    if kind == 'drill' and t['tip_angle']:
        lift = t['d'] / 2 / math.tan(math.radians(t['tip_angle']) / 2)
    surf['ops'].append([setup.name, o.name, kind, bool(in_tabs)])
    k = len(surf['ops']) - 1
    for m in cuts:
        a = to_world.coords(m[1], m[2], m[3] + lift)
        b = to_world.coords(m[4], m[5], m[6] + lift)
        c = to_world.coords(m[1], m[2], top)
        surf['cuts'].append([round(v * 10, 4) for v in a + b + (c[2],)] + [t['d'] / 2, k])


def _face_points(face):
    """Sample points on a face: a flat face square to the Z axis ('floor'), a
    wall parallel to it, flat or round ('wall'), or another flat face
    ('slanted'). [(x, y, z), margin, kind)] in the design's frame, in mm, where
    margin counts the EDGE_MARGINS the point keeps off the face's edges. [] for
    other faces."""
    geo = face.geometry
    ev = face.evaluator
    bb = face.boundingBox
    lo, hi = bb.minPoint, bb.maxPoint
    sp = max(0.05, math.sqrt(face.area / FACE_POINTS))  # cm
    P, V = adsk.core.Point3D.create, adsk.core.Vector3D.create
    plane = adsk.core.Plane.cast(geo)
    cyl = adsk.core.Cylinder.cast(geo)
    grid = []  # (point, u direction, v direction), unit vectors along the face
    if plane is not None:
        n = plane.normal
        if abs(n.z) > 0.999:
            kind, u, v = 'floor', V(1, 0, 0), V(0, 1, 0)
        elif abs(n.z) < 1e-3:
            kind, u, v = 'wall', V(-n.y, n.x, 0), V(0, 0, 1)
            u.normalize()
        else:  # slanted: no flat end mill cuts it to size, but none may cut into it
            kind, u = 'slanted', V(-n.y, n.x, 0)
            u.normalize()
            v = n.crossProduct(u)
            v.normalize()
        o = plane.origin
        corners = [(x - o.x, y - o.y, z - o.z) for x in (lo.x, hi.x) for y in (lo.y, hi.y)
                   for z in (lo.z, hi.z)]
        ra = [c[0] * u.x + c[1] * u.y + c[2] * u.z for c in corners]
        rb = [c[0] * v.x + c[1] * v.y + c[2] * v.z for c in corners]
        na = max(1, int((max(ra) - min(ra)) / sp))
        nb = max(1, int((max(rb) - min(rb)) / sp))
        for i in range(na):
            a = min(ra) + (max(ra) - min(ra)) * (i + 0.5) / na
            for j in range(nb):
                b = min(rb) + (max(rb) - min(rb)) * (j + 0.5) / nb
                grid.append((P(o.x + a * u.x + b * v.x, o.y + a * u.y + b * v.y,
                               o.z + a * u.z + b * v.z), u, v))
    elif cyl is not None and abs(cyl.axis.z) > 0.999:
        kind = 'wall'
        o, rad = cyl.origin, cyl.radius
        na = max(4, int(2 * math.pi * rad / sp))
        nb = max(1, int((hi.z - lo.z) / sp))
        for i in range(na):
            th = 2 * math.pi * (i + 0.5) / na
            u = V(-math.sin(th), math.cos(th), 0)
            for j in range(nb):
                z = lo.z + (hi.z - lo.z) * (j + 0.5) / nb
                grid.append((P(o.x + rad * math.cos(th), o.y + rad * math.sin(th), z), u,
                             V(0, 0, 1)))
    else:
        return []

    def on(p):
        ok, prm = ev.getParameterAtPoint(p)
        return ok and ev.isParameterOnFace(prm)

    out = []
    for p, u, v in grid:
        if not on(p):
            continue
        level = 0
        for m in EDGE_MARGINS:
            m /= 10
            if not all(on(P(p.x + su * m * u.x + sv * m * v.x, p.y + su * m * u.y + sv * m * v.y,
                            p.z + su * m * u.z + sv * m * v.z))
                       for su, sv in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                break
            level += 1
        if level:
            out.append(((p.x * 10, p.y * 10, p.z * 10), level, kind))
    return out


def _cut_index(cuts, cell=1.0):
    """{(i, j): [cut]}: each cut under every cell of `cell` mm its footprint reaches."""
    idx = {}
    for c in cuts:
        r = c[7] + 0.1
        for i in range(int(math.floor((min(c[0], c[3]) - r) / cell)),
                       int(math.floor((max(c[0], c[3]) + r) / cell)) + 1):
            for j in range(int(math.floor((min(c[1], c[4]) - r) / cell)),
                           int(math.floor((max(c[1], c[4]) + r) / cell)) + 1):
                idx.setdefault((i, j), []).append(c)
    return idx


def _probe(px, py, pz, cands, ops):
    """What the cuts did at a point on a face: (covered, [(depth, operation
    index)]): covered when some tool passed through or against the point, and
    a gouge for each tool body the point lies inside, more than GOUGE_TOL deep."""
    covered = False
    gouges = []
    for ax, ay, az, bx, by, bz, cz, r, k in cands:
        dx, dy = bx - ax, by - ay
        ll = dx * dx + dy * dy
        if ll < 1e-12:  # straight down or up: the deeper end
            u = 0.0
            tip = az if abs(cz - az) >= abs(cz - bz) else bz
        else:
            u = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll))
            tip = az + u * (bz - az)
        d = math.hypot(px - ax - u * dx, py - ay - u * dy)
        if d > r + UNCUT_TOL:
            continue
        s = 1.0 if cz >= tip else -1.0
        h = (pz - tip) * s  # how far the point is above the tool's tip, along the tool
        if (cz - pz) * s < -UNCUT_TOL:
            continue  # above the stock
        if h >= -UNCUT_TOL:
            covered = True
        kind = ops[k][2]
        if kind == 'thread':
            continue  # it cuts the thread into the wall
        # a drill may be a little wider than its hole (carvera_cam's DRILL_OVER)
        if r - d > GOUGE_TOL + (0.01 if kind == 'drill' else 0) and h > GOUGE_TOL:
            gouges.append((min(r - d, h), k))
    return covered, gouges


def _check_surfaces(rep, cam, surf, threaded, deadline):
    """G1: no tool cuts into the part; U1: every face a tool can reach from above
    or below, floors and walls, is cut to size; TB1: no operation outside the
    tab program cuts into a tab. Read from every setup's cuts at once, in the
    design's frame, against sample points on the faces of the setups' models."""
    bodies, seen = [], set()
    for s in cam.setups:
        if s.operationType != adsk.cam.OperationTypes.MillingOperation:
            continue
        for b in _setup_bodies(s):
            if b.entityToken not in seen:
                seen.add(b.entityToken)
                bodies.append(b)
    des = adsk.fusion.Design.cast(_app().activeDocument.products.itemByProductType(
        'DesignProductType'))
    tabs = [b for occ in des.rootComponent.allOccurrences
            if occ.component.name == 'carvera-cam tabs' for b in occ.bRepBodies]
    ops = surf['ops']
    idx = _cut_index(surf['cuts'])
    stocks = surf['stocks']

    def raw(p, kind):
        # a face of the stock itself: its top or bottom, or a side of its box
        x, y, z = p
        for x0, x1, y0, y1, z0, z1 in stocks:
            if kind == 'floor' and min(abs(z - z0), abs(z - z1)) < 0.01:
                return True
            if kind == 'wall' and min(abs(x - x0), abs(x - x1), abs(y - y0), abs(y - y1)) < 0.01:
                return True
        return False

    gouged = {}  # operation index -> [deepest, point, points]
    uncut = []   # (point, kind) per face left uncut
    points = 0
    late = 0     # faces and tool tips left out for want of time
    for b in bodies:
        for f in b.faces:
            if time.time() > deadline:
                late += 1
                continue
            thread = f.entityToken in threaded
            first = None
            for p, level, kind in _face_points(f):
                points += 1
                cands = idx.get((int(math.floor(p[0])), int(math.floor(p[1]))), ())
                covered, gs = _probe(*p, cands, ops)
                if not thread:
                    for depth, k in gs:
                        g = gouged.setdefault(k, [0.0, None, 0])
                        g[2] += 1
                        if depth > g[0]:
                            g[0], g[1] = depth, p
                if not covered and first is None and kind != 'slanted' \
                        and level >= (3 if kind == 'wall' else 2) and not raw(p, kind):
                    first = (p, kind)
            if first:
                uncut.append(first)
    # a cut smaller than the points' spacing on a large face: the tool's tip,
    # just above it along the tool, inside the part
    boxes = []
    for b in bodies:
        bb = b.boundingBox
        boxes.append((b, bb.minPoint.x * 10, bb.minPoint.y * 10, bb.minPoint.z * 10,
                      bb.maxPoint.x * 10, bb.maxPoint.y * 10, bb.maxPoint.z * 10))
    tips = {}
    for c in surf['cuts']:
        k = c[8]
        if ops[k][2] == 'thread':
            continue
        ends = (c[0:3], c[3:6])
        if (c[3] - c[0]) ** 2 + (c[4] - c[1]) ** 2 < 1e-12:  # down or up: the deeper end
            ends = (max(ends, key=lambda e: abs(c[6] - e[2])),)
        for x, y, z in ends:
            z += GOUGE_TOL if c[6] >= z else -GOUGE_TOL
            if any(x0 < x < x1 and y0 < y < y1 and z0 < z < z1
                   for _, x0, y0, z0, x1, y1, z1 in boxes):
                tips.setdefault((round(x / 0.25), round(y / 0.25), round(z / 0.25)),
                                (x, y, z, k))
    inside = adsk.fusion.PointContainment.PointInsidePointContainment
    for x, y, z, k in tips.values():
        if time.time() > deadline:
            late += 1
            break
        if k in gouged:
            continue
        q = adsk.core.Point3D.create(x / 10, y / 10, z / 10)
        if any(b.pointContainment(q) == inside for b, *_ in boxes):
            gouged[k] = [None, (x, y, z), 1]
    if late:
        rep.add('G0', 'warning', None, None, "the check of the part's faces ran out of time, so "
                'G1, U1 and TB1 are incomplete: run check again with a larger budget')
    for k, (depth, p, n) in sorted(gouged.items()):
        sn, on, kind = ops[k][:3]
        if depth is None:
            rep.add('G1', 'error', sn, on, "the tool's tip runs inside the part at X%.3f Y%.3f "
                    'Z%.3f (design frame)' % p, at=[round(v, 3) for v in p])
            continue
        rep.add('G1', 'error', sn, on, 'the tool cuts %.3f mm into the part at X%.3f Y%.3f '
                'Z%.3f (design frame)%s' % (depth, p[0], p[1], p[2],
                                            ', and at %d more points' % (n - 1) if n > 1 else ''),
                depth=round(depth, 3), at=[round(v, 3) for v in p])
    if uncut:
        p, kind = uncut[0]
        rep.add('U1', 'warning', None, None, '%d face%s of the part %s left uncut or oversize: '
                'no tool reaches %s, the first a %s at X%.3f Y%.3f Z%.3f (design frame)'
                % (len(uncut), '' if len(uncut) == 1 else 's', 'is' if len(uncut) == 1 else 'are',
                   'it' if len(uncut) == 1 else 'them', kind, p[0], p[1], p[2]),
                faces=len(uncut), at=[[kind] + [round(v, 3) for v in q] for q, kind in uncut[:10]])
    # the tabs: only the tab program may cut them
    hit = {}
    for b in tabs:
        for f in b.faces:
            for p, level, kind in _face_points(f):
                cands = [c for c in idx.get((int(math.floor(p[0])), int(math.floor(p[1]))), ())
                         if not ops[c[8]][3]]
                for depth, k in _probe(*p, cands, ops)[1]:
                    if depth > hit.get(k, (0,))[0]:
                        hit[k] = (depth, p)
    for k, (depth, p) in sorted(hit.items()):
        sn, on = ops[k][:2]
        rep.add('TB1', 'error', sn, on, 'cuts %.3f mm into a tab at X%.3f Y%.3f Z%.3f (design '
                'frame), but only the tab program may cut the tabs' % (depth, *p),
                depth=round(depth, 3))
    return points


# ------------------------------------------------------------------ check

def check(args=None):
    args = args or {}
    t0 = time.time()
    deadline = t0 + args.get('budget', 50)
    cam = adsk.cam.CAM.cast(_app().activeDocument.products.itemByProductType('CAMProductType'))
    if cam is None:
        raise RuntimeError('the active document has no Manufacture data')
    lab = _library_presets()
    cc = runpy.run_path(os.path.join(HERE, 'carvera_cam.py'))
    post = cc['_post'](cc['_fetch_community']([])['post'])
    wanted = args.get('setups')
    # the faces the document's thread mills cut: their drills may be wider than the hole
    threaded = frozenset(f.entityToken for s in cam.setups for o in s.allOperations
                         if o.strategy == 'thread' and not o.isSuppressed
                         for f in _selected(o, 'circularFaces'))
    setups = [s for s in cam.setups if (not wanted or s.name in wanted)
              and s.operationType == adsk.cam.OperationTypes.MillingOperation]
    try:
        with open(STATE, encoding='utf-8') as f:
            state = json.load(f)
        if state.get('doc') != _doc_id() or state.get('setups') != [s.name for s in setups]:
            state = None
    except (OSError, ValueError):
        state = None
    state = state or {'doc': _doc_id(), 'setups': [s.name for s in setups],
                      'done': [], 'items': []}
    rep = Report()
    rep.items = state['items']
    # the operations of the tab programs, the only ones that may cut the tabs
    tab_ids = frozenset(x.operationId for nc in cam.ncPrograms if re.search(r'_tabs\b', nc.name)
                        for item in nc.operations
                        for x in (list(item.allOperations) if hasattr(item, 'allOperations')
                                  else [item]))
    numbers = {}  # tool number -> description, across the document
    for s in setups:
        for o in s.allOperations:
            if o.isSuppressed:
                continue
            t = _tool(o)
            prev = numbers.setdefault(t['number'], (t['desc'], s.name))
            if prev[0] != t['desc'] and prev[1] != s.name and s.name not in state['done']:
                rep.add('N1', 'warning', s.name, o.name, 'T%s is %r here but %r in %s: reload '
                        'the tool changer between them' % (t['number'], t['desc'], prev[0], prev[1]))
    for s in setups:
        if s.name in state['done']:
            continue
        if time.time() > deadline:
            break
        sn = s.name
        sp = s.parameters
        if s.machine is None:
            rep.add('M1', 'warning', sn, None, 'no machine is set, so simulation cannot check '
                    'the machine')
        if not _v(sp, 'job_fixture', False) or not list(s.fixtures):
            rep.add('M2', 'warning', sn, None, 'no fixture is set, so simulation cannot check '
                    'the vise')
        ops = [o for o in s.allOperations if not o.isSuppressed]
        earlier, slots, tools, ok = [], {}, [], []
        for o in ops:
            t = _tool(o)
            prev = slots.setdefault(t['number'], t['desc'])
            if prev != t['desc']:
                rep.add('N1', 'error', sn, o.name, 'T%s is %r here but %r earlier in this setup'
                        % (t['number'], t['desc'], prev))
            if not o.hasToolpath:
                rep.add('P1', 'error', sn, o.name, 'no toolpath')
            elif not o.isToolpathValid:
                rep.add('P1', 'error', sn, o.name, 'the toolpath is out of date')
            else:
                if o.hasError:
                    rep.add('P2', 'error', sn, o.name, 'Fusion reports an error: %s'
                            % getattr(o, 'error', ''))
                if o.hasWarning:
                    rep.add('P2', 'warning', sn, o.name, 'Fusion reports a warning: %s'
                            % getattr(o, 'warning', ''))
                ok.append(o)
                tools.append(t)
            _check_op(rep, s, o, t, earlier, lab, threaded)
            earlier.append({'strategy': o.strategy, 'type': t['type'], 'd': t['d'] or 0,
                            'multi': bool(_v(o.parameters, 'doMultipleDepths')),
                            'number': t['number']})
        # N4: the document's NC programs hold some of this setup's operations
        # but leave one out, so the machine never cuts it
        held, programmed = set(), False
        for nc in cam.ncPrograms:
            for item in nc.operations:
                inner = list(item.allOperations) if hasattr(item, 'allOperations') else [item]
                held |= {x.operationId for x in inner}
                programmed |= any(x.parentSetup.name == sn for x in inner)
        if programmed:
            for o in ops:
                if o.operationId not in held:
                    rep.add('N4', 'error', sn, o.name, 'no NC program includes this operation')
        for o in s.allOperations:
            if o.isSuppressed:
                rep.add('N4', 'warning', sn, o.name, 'the operation is suppressed, so no NC '
                        'program runs it')
        if ok:
            folder = tempfile.mkdtemp(prefix='carvera-check-')
            text = _post_setup(cam, s, ok, post, folder)
            # holes drilled and stock cut in an earlier setup can serve this
            # one's entries too
            _check_gcode(rep, cam, s, ok, tools, parse_gcode(text), deadline,
                         state.setdefault('holes', []), state.setdefault('swept', []),
                         state.setdefault('surf', {'ops': [], 'cuts': [], 'stocks': []}),
                         tab_ids)
        state['done'].append(sn)
        state['items'] = rep.items
        with open(STATE, 'w', encoding='utf-8') as f:
            json.dump(state, f)
    pending = [s.name for s in setups if s.name not in state['done']]
    # the part's faces against every setup's cuts: only once all of them are in
    whole = {s.name for s in setups} >= {s.name for s in cam.setups
                                         if s.operationType == adsk.cam.OperationTypes.MillingOperation}
    if not pending and whole and SURFACES not in state['done']:
        if time.time() > deadline - 10:
            pending = [SURFACES]
        else:
            state['surface_points'] = _check_surfaces(
                rep, cam, state.get('surf') or {'ops': [], 'cuts': [], 'stocks': []}, threaded,
                deadline + 30)
            state['done'].append(SURFACES)
            state['items'] = rep.items
            with open(STATE, 'w', encoding='utf-8') as f:
                json.dump(state, f)
    out = {'status': 'running' if pending else 'done', 'pending': pending,
           'errors': sum(1 for i in rep.items if i['severity'] == 'error'),
           'warnings': sum(1 for i in rep.items if i['severity'] == 'warning'),
           'findings': rep.items, 'seconds': round(time.time() - t0, 1)}
    if 'surface_points' in state:
        out['surface_points'] = state['surface_points']  # sampled on the part's faces
    if not pending:
        try:
            os.remove(STATE)
        except OSError:
            pass
    print(json.dumps(out, indent=1))


# ------------------------------------------------------------------ summary

def summary(args=None):
    """Facts per operation, read from the G-code, for comparing two builds of a
    part (the script's and the recipes'): the tool, machining time, cut length,
    deepest Z, the area the cuts span, and every plunge (a straight move down at
    the feed), in each setup's WCS, in mm, with the plunges also in the design's
    frame. Prints them as JSON. args['max_points'] caps the plunges listed per
    operation (default 40)."""
    args = args or {}
    cam = adsk.cam.CAM.cast(_app().activeDocument.products.itemByProductType('CAMProductType'))
    cc = runpy.run_path(os.path.join(HERE, 'carvera_cam.py'))
    post = cc['_post'](cc['_fetch_community']([])['post'])
    out = {'document': _app().activeDocument.name, 'setups': []}
    for s in cam.setups:
        if s.operationType != adsk.cam.OperationTypes.MillingOperation:
            continue
        ops = [o for o in s.allOperations if not o.isSuppressed and o.hasToolpath
               and o.isToolpathValid]
        if not ops:
            continue
        text = _post_setup(cam, s, ops, post, tempfile.mkdtemp(prefix='carvera-summary-'))
        names = [_norm(o.name) for o in ops]
        by_op, i = {}, 0
        for name, moves in parse_gcode(text):
            if i < len(names) and name == names[i]:
                by_op[i] = moves
                i += 1
            elif i and (i - 1) in by_op:
                by_op[i - 1] += moves
        tw = _wcs_to_world(s)
        rows = []
        for k, o in enumerate(ops):
            moves = [m for m in by_op.get(k, []) if None not in m[1:]]
            cuts = [m for m in moves if m[0] != 0]
            plunges = sorted({(round(m[4], 3), round(m[5], 3), round(m[6], 3)) for m in cuts
                              if m[0] == 1 and abs(m[4] - m[1]) < 1e-3 and abs(m[5] - m[2]) < 1e-3
                              and m[6] < m[3] - 1e-3})
            t = _tool(o)
            try:
                sec = cam.getMachiningTime(o, 1, 1, 1).machiningTime
            except RuntimeError:
                sec = None
            rows.append({
                'operation': o.name, 'strategy': o.strategy, 'T': t['number'], 'tool': t['desc'],
                'seconds': round(sec, 1) if sec is not None else None,
                'cut_length_mm': round(sum(math.dist(m[1:4], m[4:7]) for m in cuts), 1),
                'z_min': round(min((min(m[3], m[6]) for m in cuts), default=0), 3),
                'xy_extent': [round(f(m[j] for m in cuts), 2) for f, j in
                              ((min, 4), (min, 5), (max, 4), (max, 5))] if cuts else None,
                'plunges': len(plunges),
                'plunge_points': [list(p) for p in plunges[:args.get('max_points', 40)]],
                # the same points in the design's frame, as analyze gives features
                'plunge_points_design': [[round(v * 10, 3) for v in tw.coords(*p)]
                                         for p in plunges[:args.get('max_points', 40)]]})
        out['setups'].append({'setup': s.name, 'operations': rows})
    print(json.dumps(out, indent=1))
    return out

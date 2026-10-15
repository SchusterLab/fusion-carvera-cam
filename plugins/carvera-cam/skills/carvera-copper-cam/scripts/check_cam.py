"""Check a document's Carvera toolpaths against the lab's rules for avoiding
crashes, spindle stalls and excessive tool wear (references/rules.md).

Runs inside Fusion's Python, on the active document, on any setups: the lab's
own as well as those carvera_cam.build made. It changes nothing in the design:
it posts each setup to temporary G-code through a temporary NC program, which
it deletes again, and reads the moves from there.

    check({})                         every milling setup
    check({'setups': ['Setup1']})     only these
    check({'budget': 50})             stop after about 50 s; call again to go on

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
CHIP_LOAD_MILL = 0.034       # mm/tooth, end mills and chamfer bits (lab: 0.0333)
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
BREAKTHROUGH = 0.5           # a through hole's drill goes this far past its bottom
MILLING = ('flat end mill', 'ball end mill', 'bull nose end mill', 'chamfer mill',
           'face mill', 'slot mill', 'lollipop mill', 'tapered mill', 'dovetail mill')


def _app():
    return adsk.core.Application.get()


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
    """{description: Copper 101-H02 preset values} from the skill's tool library."""
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
    # feeds: against the tool's preset, or, with none, against the lab's chip load
    preset = lab.get(_ascii(t['desc']))
    if preset is None:
        rep.add('N5', 'warning', sn, on, '%s is not in the skill\'s tool library, so its feeds '
                'are unchecked against a Copper 101-H02 preset' % _ascii(t['desc']))
        if milling and t['feed']:
            fz = t['feed'] / t['rpm'] / t['flutes']
            if fz > CHIP_LOAD_MILL:
                rep.add('F2', 'warning', sn, on, 'chip load %.4f mm/tooth is above the lab\'s %.4f'
                        % (fz, CHIP_LOAD_MILL), feed=t['feed'], rpm=t['rpm'])
    else:
        diffs = {}
        for key, val in (('n', t['rpm']), ('v_f', t['feed']), ('v_f_plunge', t['plunge'])):
            want = preset.get(key)
            if want and val and abs(val - want) > 0.5 and not (key == 'v_f' and t['type'] == 'drill'):
                diffs[key] = (val, want)
        if diffs:
            rep.add('F1', 'warning', sn, on, 'feeds differ from the tool\'s Copper 101-H02 preset: '
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
            twin = [e for e in earlier if e['strategy'] == strat and e['multi']
                    and abs(e['d'] - d) < 1e-3]
            sides = _v(p, 'doMultiplePasses', False) and (_mm(_v(p, 'maximumStepover')) or 9) <= 0.3
            if not twin and not sides:
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
                rep.add('E1', 'warning', sn, on, 'pocket entry ramp is disabled')
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

_WORD = re.compile(r'([A-Z])\s*([-+]?\d*\.?\d+)')


def _norm(name):
    return ' '.join(re.sub(r'[()]', ' ', name).split())


def parse_gcode(text):
    """[(comment, moves)] per section; a move is (G, x0, y0, z0, x1, y1, z1)."""
    sections = []
    moves = None
    x = y = z = None
    g = 0
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
        moved = False
        for k, v in words:
            v = float(v)
            if k == 'G' and v in (0, 1, 2, 3):
                g = int(v)
            elif k == 'X':
                nx, moved = v, True
            elif k == 'Y':
                ny, moved = v, True
            elif k == 'Z':
                nz, moved = v, True
        if moved and moves is not None:
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

    def f(px, py, pz):
        px, py, pz = px / 10, py / 10, pz / 10
        return adsk.core.Point3D.create(o.x * k + px * x.x + py * y.x + pz * z.x,
                                        o.y * k + px * x.y + py * y.y + pz * z.y,
                                        o.z * k + px * x.z + py * y.z + pz * z.z)
    return f


def _segment(to_world, x, y, z0, z1):
    """A vertical stretch of a setup's G-code, from Z z0 to Z z1 at (x, y), as
    (x, y, lowest z, highest z) in the design's frame, in mm."""
    a, b = to_world(x, y, z0), to_world(x, y, z1)
    return a.x * 10, a.y * 10, min(a.z, b.z) * 10, max(a.z, b.z) * 10


def _drilled(moves, t, to_world, top, sn):
    """[(x, y, lowest z, highest z, diameter, setup)] in the design's frame, for
    each hole a drill operation makes: full width from the stock top down to the
    drill's deepest point at that spot, raised by the length of its point."""
    tip = 0.0
    if t['tip_angle']:
        tip = t['d'] / 2 / math.tan(math.radians(t['tip_angle']) / 2)
    deepest = {}
    for m in moves:
        if None in m[1:]:
            continue
        k = (round(m[4], 3), round(m[5], 3))
        deepest[k] = min(deepest.get(k, m[6]), m[6])
    return [_segment(to_world, x, y, z + tip, top) + (t['d'], sn)
            for (x, y), z in deepest.items() if z + tip < top]


def _cleared_to(x, y, r, top, milled):
    """How deep earlier milling passes in this setup cleared the whole footprint
    of a tool of radius r at (x, y): the WCS z, or `top` where they left some of
    it uncut. The footprint is sampled at its centre and at eight points 0.05 mm
    inside its edge (a sliver at the edge is harmless); a point is cut down to
    the deepest pass whose tool covered it. `milled` holds (moves, tool radius)
    per operation."""
    rs = max(r - 0.05, 0.0)
    pts = [(x, y)] + [(x + rs * math.cos(a * math.pi / 4), y + rs * math.sin(a * math.pi / 4))
                      for a in range(8)]
    depth = [top] * len(pts)
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
    return max(depth)


def _check_plunges(rep, rule, severity, sn, o, moves, t, holes, milled, to_world, top,
                   rapids=False):
    """E4 (pockets and contours) and TH2 (thread mills, bores and circular cuts):
    every plunge into the stock must go into stock earlier milling passes in
    this setup have cleared, and below that down a hole drilled earlier, in
    this setup or an earlier one, at least as wide as the tool there and full
    width over the rest of the plunge. A plunge is a straight move down at the
    feed, or also at a rapid with `rapids`."""
    r = t['d'] / 2
    kinds = (0, 1) if rapids else (1,)
    plunges = {}  # (x, y) -> deepest plunge
    for m in moves:
        if m[0] in kinds and None not in m[1:] and abs(m[4] - m[1]) < 1e-3 \
                and abs(m[5] - m[2]) < 1e-3 and m[6] < m[3] - 1e-3 and m[6] < top - 0.01:
            k = (round(m[4], 3), round(m[5], 3))
            plunges[k] = min(plunges.get(k, m[6]), m[6])
    bad = []
    for (x, y), z in sorted(plunges.items()):
        cleared = _cleared_to(x, y, r, top, milled)
        if cleared <= z + 0.01:
            continue  # into stock an earlier cut has cleared
        px, py, plo, phi = _segment(to_world, x, y, z, cleared)
        ok, why = False, 'no hole is drilled there before it'
        for hx, hy, hlo, hhi, hd, hs in holes:
            e = math.hypot(px - hx, py - hy)
            if e > hd / 2:
                continue
            where = '' if hs == sn else ' (drilled in %s)' % hs
            if e + r > hd / 2 + 0.02:
                why = ('the %.3g mm hole there%s is too narrow for it, or off its centre by '
                       '%.3f mm' % (hd, where, e))
            elif hlo > plo + 0.01 or hhi < phi - 0.01:
                short = max(hlo - plo, phi - hhi)
                why = 'the hole there%s is not full width over %.2f mm of it' % (where, short)
            else:
                ok = True
                break
        if not ok:
            bad.append((x, y, z, why))
    if bad:
        x, y, z, why = bad[0]
        rep.add(rule, severity, sn, o.name, 'the %g mm %s plunges at X%.3f Y%.3f to '
                'Z%.3f, but %s%s' % (t['d'], t['type'], x, y, z, why,
                                    ' (%d of its %d plunges)' % (len(bad), len(plunges))
                                    if len(plunges) > 1 else ''),
                plunges=len(bad))


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
        for hx, hy, hlo, hhi, hd, _ in holes:
            p, q = (wcs(adsk.core.Point3D.create(hx / 10, hy / 10, z / 10)) for z in (hlo, hhi))
            if hd >= 2 * r - 0.01 and math.hypot(p[0] - cx, p[1] - cy) < 0.05:
                hole_bottom = min(hole_bottom, p[2], q[2])
        inside = [min(m[3], m[6]) for m in cuts
                  if math.hypot(m[1] - cx, m[2] - cy) < r and math.hypot(m[4] - cx, m[5] - cy) < r]
        if inside and min(inside) < hole_bottom - 0.01:
            rep.add('TH3', 'error', sn, o.name, 'the thread mill goes %.2f mm deeper than the '
                    'hole at X%.3f Y%.3f' % (hole_bottom - min(inside), cx, cy),
                    z_min=round(min(inside), 3), hole_bottom=round(hole_bottom, 3))


def _check_gcode(rep, cam, s, ops, tools, sections, deadline, holes=None):
    """The rules read from the setup's G-code. `holes` lists the holes earlier
    setups drilled (see _drilled); this setup's holes are added to it."""
    sn = s.name
    sp = s.parameters
    top = _mm(_v(sp, 'stockZHigh', 0))
    bottom = _mm(_v(sp, 'stockZLow', 0))
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
    holes = holes if holes is not None else []
    milled = []  # (moves, tool radius) of the milling operations so far in this setup
    fx_boxes = []
    for b in fixture:
        bb = b.boundingBox
        fx_boxes.append((b, bb))
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
        if t['type'] == 'drill':
            holes += _drilled(moves, t, to_world, top, sn)
        elif o.strategy in ('thread', 'bore', 'circular'):
            _check_plunges(rep, 'TH2', 'error' if o.strategy == 'thread' else 'warning', sn, o,
                           moves, t, holes, milled, to_world, top, rapids=True)
            if t['type'] in MILLING:
                milled.append((cuts, t['d'] / 2))
        elif t['type'] in MILLING:
            if o.strategy in ('pocket2d', 'contour2d') and t['type'] != 'chamfer mill':
                _check_plunges(rep, 'E4', 'warning', sn, o, moves, t, holes, milled, to_world, top)
            milled.append((cuts, t['d'] / 2))
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
        op_top = min(top, feed_z - off) if off is not None else top
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
        if t['flute_len'] and depth > t['flute_len'] + tip + 0.01 and t['type'] != 'chamfer mill':
            rep.add('H5', 'error' if t['type'] == 'drill' else 'warning', sn, o.name,
                    'cuts %.2f mm deep with a %.2f mm flute' % (depth, t['flute_len']),
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
            r = t['d'] / 2
            pts = []
            for m in moves:
                if None in m[1:]:
                    continue
                for (px, py, pz) in ((m[4], m[5], m[6]), ((m[1] + m[4]) / 2, (m[2] + m[5]) / 2,
                                                         (m[3] + m[6]) / 2)):
                    for a in range(8):
                        pts.append((px + r * math.cos(a * math.pi / 4),
                                    py + r * math.sin(a * math.pi / 4), pz + 0.05))
            hit = None
            for px, py, pz in pts:
                w = to_world(px, py, pz)
                for b, bb in fx_boxes:
                    if not bb.contains(w):
                        continue
                    if b.pointContainment(w) == adsk.fusion.PointContainment.PointInsidePointContainment:
                        hit = (px, py, pz, '%s/%s' % (b.parentComponent.name, b.name))
                        break
                if hit:
                    break
            if hit:
                hits += 1
                rep.add('X1', 'error', sn, o.name, 'the tool enters the fixture body %r at '
                        'X%.2f Y%.2f Z%.2f' % (hit[3], hit[0], hit[1], hit[2]))


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
        if state.get('doc') != _app().activeDocument.name or state.get('setups') != [s.name for s in setups]:
            state = None
    except (OSError, ValueError):
        state = None
    state = state or {'doc': _app().activeDocument.name, 'setups': [s.name for s in setups],
                      'done': [], 'items': []}
    rep = Report()
    rep.items = state['items']
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
        if ok:
            folder = tempfile.mkdtemp(prefix='carvera-check-')
            text = _post_setup(cam, s, ok, post, folder)
            # holes drilled in an earlier setup can serve this one's entries too
            _check_gcode(rep, cam, s, ok, tools, parse_gcode(text), deadline,
                         state.setdefault('holes', []))
        state['done'].append(sn)
        state['items'] = rep.items
        with open(STATE, 'w', encoding='utf-8') as f:
            json.dump(state, f)
    pending = [s.name for s in setups if s.name not in state['done']]
    out = {'status': 'running' if pending else 'done', 'pending': pending,
           'errors': sum(1 for i in rep.items if i['severity'] == 'error'),
           'warnings': sum(1 for i in rep.items if i['severity'] == 'warning'),
           'findings': rep.items, 'seconds': round(time.time() - t0, 1)}
    if not pending:
        try:
            os.remove(STATE)
        except OSError:
            pass
    print(json.dumps(out, indent=1))

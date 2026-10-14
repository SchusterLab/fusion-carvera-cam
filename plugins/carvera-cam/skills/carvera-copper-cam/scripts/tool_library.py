"""The skill's tool library: the tools carvera_cam.py assigns, which a user buys
from Makera and McMaster-Carr.

assets/carvera-tools.json is built from two public sources:
  - Makera's tool libraries, as the Carvera Community repository copies them
    (MAKERA_TOOLS): the Spiral O end mills for metal, the thread mills and the
    chamfering bit that carvera_cam.py uses (MAKERA_PICKS);
  - McMaster-Carr's uncoated expanded-shank carbide drills, part numbers
    2841A..., from a copy of the catalogue's table (MCMASTER_DRILLS). Each is
    Makera's 1/8" shank drill (DRILL_TEMPLATE) given the catalogue's diameter,
    maximum drilling depth, overall length and point angle.
Every tool gets one feed preset, Copper 101-H02, by the skill's rule for its
kind of tool (copper_values).

Run it with plain Python to rebuild the file:

    python -I tool_library.py
"""

import copy
import csv
import io
import json
import os
import sys
import urllib.request
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_FILE = os.path.join(os.path.dirname(HERE), 'assets', 'carvera-tools.json')
MAKERA_TOOLS = ('https://raw.githubusercontent.com/Carvera-Community/Carvera_Community_Profiles/'
                'main/CAM_Post_Processors/Fusion360-profiles/Tool%20Files/')
MCMASTER_DRILLS = os.path.join(os.path.dirname(HERE), 'assets', 'mcmaster-expanded-shank-carbide-drills.csv')
PRESET = 'Copper 101-H02'
PRESET_NOTE = ('carvera-cam\'s settings for C10100 copper, half hard (H02); aluminum 6063-T5 '
               'runs the same programs at 200% feed and spindle override.')

# The Makera tools carvera_cam.py assigns: its end mills (ENDMILL, SMALL_ENDMILLS),
# thread mills (references/threads.toml) and chamfering bit (CHAMFER).
MAKERA_PICKS = {
    'Makera O Flute Bits': ('Spiral O Metal 1*3mm', 'Spiral O Metal 1.5*6mm', 'Spiral O Metal 2*8mm',
                            'Spiral O Metal 2.5*12mm', 'Spiral O Metal 3.175*12mm'),
    'Makera Thread Mills': ('Carvera M2 Threadmill', 'Carvera M2.5 Threadmill',
                            'Carvera M3 Threadmill', 'Carvera M4 Threadmill'),
    'Makera Other Tools': ('Chamfering Bit - 1/8″ Shank',),
}
DRILL_TEMPLATE = ('Makera Drill Bits', '3.175*12mm Drill')


def copper_values(tool, makera_copper):
    """The skill's Copper 101-H02 settings for a tool of this kind and size.

    Spiral O end mills run at 6000 rpm and 200 mm/min with 200 mm/min plunge,
    stepping down 0.3 mm (ramp 300 mm/min) for the 3.175 mm one and
    0.2 mm (ramp 200 mm/min) for the smaller ones; drills at 10000 rpm, plunge 50 mm/min,
    retract 500 mm/min; thread mills at 10000 rpm and 0.1 mm stepover, with
    the rest of Makera's own Copper preset (feed M2 100, M2.5 150, M3 and
    M4 200 mm/min; stepdown the thread's pitch); the chamfering
    bit at 6000 rpm and 200 mm/min, 0.1 mm steps.
    """
    kind = tool['type']
    d = tool['geometry']['DC']
    if kind == 'flat end mill':
        big = tool['description'] == 'Spiral O Metal 3.175*12mm'
        return {'n': 6000, 'v_f': 200, 'v_f_plunge': 200, 'v_f_ramp': 300 if big else 200,
                'stepdown': 0.3 if big else 0.2, 'ramp-angle': 1.2}
    if kind == 'drill':
        return {'n': 10000, 'v_f_plunge': 50, 'v_f_retract': 500}
    if kind == 'thread mill':
        keep = ('v_f', 'v_f_plunge', 'v_f_ramp', 'stepdown', 'ramp-angle')
        return dict({k: makera_copper[k] for k in keep}, n=10000, stepover=0.1)
    if kind == 'chamfer mill':
        return {'n': 6000, 'v_f': 200, 'v_f_plunge': 200, 'v_f_ramp': 300,
                'stepdown': 0.1, 'stepover': 0.1, 'ramp-angle': 2}
    raise ValueError('no Copper 101-H02 rule for a %s' % kind)


def _fmt(v):
    return ('%.6f' % v).rstrip('0').rstrip('.')


def with_preset(tool):
    """`tool` with the Copper 101-H02 preset as its only preset, made from
    Makera's own Copper preset with copper_values."""
    presets = tool.get('start-values', {}).get('presets', [])
    makera = next((p for p in presets if p['name'] == 'Copper'), presets[0])
    values = copper_values(tool, makera)
    p = copy.deepcopy(makera)
    p.update(values)
    n, z, d = p['n'], tool['geometry'].get('NOF') or 1, tool['geometry']['DC']
    if 'v_f' in values:
        for k in ('v_f_leadIn', 'v_f_leadOut', 'v_f_transition'):
            p[k] = p['v_f']
        p['f_n'] = p['v_f'] / n
        p['f_z'] = p['v_f'] / n / z
    else:
        for k in ('v_f', 'v_f_leadIn', 'v_f_leadOut', 'v_f_transition', 'v_f_ramp', 'f_n', 'f_z'):
            if k in p:
                p[k] = 0
    if 'n_ramp' in p:
        p['n_ramp'] = n
    p['v_c'] = 3.141592653589793 * d * n / 1000
    p['name'] = PRESET
    p['description'] = PRESET_NOTE
    p['tool-coolant'] = 'disabled'
    p['guid'] = str(uuid.uuid5(uuid.NAMESPACE_URL, tool['guid'] + '/' + PRESET))
    names = {'n': ('tool_spindleSpeed', '%s rpm'), 'v_f': ('tool_feedCutting', '%s mmpm'),
             'v_f_plunge': ('tool_feedPlunge', '%s mmpm'), 'v_f_ramp': ('tool_feedRamp', '%s mmpm'),
             'v_f_retract': ('tool_feedRetract', '%s mmpm'), 'stepdown': ('tool_stepdown', '%s mm'),
             'stepover': ('tool_stepover', '%s mm'), 'ramp-angle': ('tool_rampAngle', '%s degrees')}
    p['expressions'] = {name: unit % _fmt(p[k]) for k, (name, unit) in names.items() if k in values}
    p['expressions']['tool_coolant'] = "'disabled'"
    tool['start-values'] = {'presets': [p]}
    return tool


def fetch_makera(names=None):
    """{library name: [tools]} from the Carvera Community repository."""
    out = {}
    for name in names or list(MAKERA_PICKS) + [DRILL_TEMPLATE[0]]:
        url = MAKERA_TOOLS + urllib.request.quote(name) + '.tools'
        req = urllib.request.Request(url, headers={'User-Agent': 'carvera-cam'})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        out[name] = json.loads(zipfile.ZipFile(io.BytesIO(data)).read('tools.json'))['data']
    return out


def read_mcmaster(path=MCMASTER_DRILLS):
    with open(path, encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def mcmaster_drill(r, template):
    """The drill of one row of MCMASTER_DRILLS, made from Makera's drill `template`."""
    d = round(float(r['diameter_mm']), 4)
    depth = round(float(r['max_drilling_depth_mm']), 1)
    tip = int(r['point_angle_deg'])
    t = copy.deepcopy(template)
    desc = '%s*%smm Drill' % (_fmt(round(d, 3)), _fmt(depth))
    if not r['size'].endswith(' mm'):  # an inch or wire-gauge size
        desc += ' (%s)' % r['size']
    t['geometry'].update({'DC': d, 'LCF': depth, 'shoulder-length': depth, 'SIG': tip,
                          'SFDM': 3.175, 'OAL': round(float(r['overall_length_mm']))})
    for seg in t.get('shaft', {}).get('segments', []):  # the taper up to the shank
        seg['lower-diameter'] = d
    t['description'] = desc
    t['vendor'] = 'McMaster-Carr'
    t['guid'] = str(uuid.uuid5(uuid.NAMESPACE_URL, 'https://www.mcmaster.com/' + r['part']))
    t['product-id'] = r['part']
    t['product-link'] = 'https://www.mcmaster.com/%s/' % r['part']
    t['expressions'].update({
        'tool_description': "'%s'" % desc.replace("'", "\\'"),
        'tool_vendor': "'McMaster-Carr'",
        'tool_diameter': '%s mm' % _fmt(d), 'tool_fluteLength': '%s mm' % _fmt(depth),
        'tool_shoulderLength': '%s mm' % _fmt(depth), 'tool_tipAngle': '%d degrees' % tip,
        'tool_productId': "'%s'" % r['part'], 'tool_productLink': "'%s'" % t['product-link']})
    return t


def build(makera, rows):
    """The library's tools: Makera's picks, then a drill for each of McMaster-Carr's
    uncoated expanded-shank carbide drills (the TiN and TiAlN ones add no size)."""
    out = []
    for name, picks in MAKERA_PICKS.items():
        by_desc = {t['description']: t for t in makera[name]}
        for desc in picks:
            if desc not in by_desc:
                raise RuntimeError('%r is no longer in Makera\'s "%s"' % (desc, name))
            out.append(copy.deepcopy(by_desc[desc]))
    template = next(t for t in makera[DRILL_TEMPLATE[0]] if t['description'] == DRILL_TEMPLATE[1])
    out += [mcmaster_drill(r, template) for r in rows if r['coating'] == 'uncoated']
    for t in out:
        t['post-process'].update(number=0, comment='')
        with_preset(t)
    out.sort(key=lambda t: (t['type'], t['geometry']['DC'], t['geometry'].get('LCF', 0)))
    return out


def read(path=TOOLS_FILE):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def write(data, path=TOOLS_FILE):
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.write('\n')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    data = read() if os.path.exists(TOOLS_FILE) else {'version': 22}
    data['data'] = build(fetch_makera(), read_mcmaster())
    write(data)
    print('%d tools:' % len(data['data']))
    for t in data['data']:
        p = t['start-values']['presets'][0]
        print('  %-36s %-13s n=%-5s feed=%-4s plunge=%s' % (
            t['description'], t['type'], p['n'], _fmt(p.get('v_f', 0)), _fmt(p['v_f_plunge'])))


if __name__ == '__main__':
    main()

"""The lab's tool library, completed with McMaster-Carr drills and the Makera
tools that cut copper.

assets/carvera-tools.json holds two kinds of tools:
  - the lab's own, exported from the Fusion library "Carvera Tools/My Tools"
    (carvera_cam.export_tools), each with the lab's "Copper 101-H02" preset;
  - added tools, marked in their post-processor comment (ADDED): a drill of
    each diameter of McMaster-Carr's expanded-shank carbide drills, the series
    most lab drills come from (read from MCMASTER_DRILLS, a copy of the
    catalogue's table), and the end mills, engravers and thread mills for
    metal from the Carvera Community's copies of Makera's tool libraries. The lab has not cut with these, so their
    Copper 101-H02 preset is derived from the lab's presets for the same kind
    of tool (lab_preset), and says so.

Run it with plain Python to fetch the community tool files again and rewrite
the added tools:

    python -I tool_library.py
"""

import copy
import csv
import io
import json
import os
import re
import sys
import urllib.request
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_FILE = os.path.join(os.path.dirname(HERE), 'assets', 'carvera-tools.json')
COMMUNITY_TOOLS = ('https://raw.githubusercontent.com/Carvera-Community/Carvera_Community_Profiles/'
                   'main/CAM_Post_Processors/Fusion360-profiles/Tool%20Files/')
MCMASTER_DRILLS = os.path.join(os.path.dirname(HERE), 'assets', 'mcmaster-expanded-shank-carbide-drills.csv')
MCMASTER_SERIES = ('6415N', '2841A', '2598N')  # TiN, uncoated, TiAlN
PRESET = 'Copper 101-H02'
ADDED = 'carvera-cam: added from '
DERIVED = ('Derived by carvera-cam from the lab\'s Copper 101-H02 presets for this kind of '
           'tool; the lab has not cut copper with this tool yet.')

# Makera's libraries, and which of their tools are made for metal. The others
# (wood and plastic O-flutes and engravers, corn bits for PCB and carbon fibre,
# the solder mask tool, lasers) do not cut copper. Makera's drills are left out:
# the McMaster-Carr drills cover their diameters.
COMMUNITY_PICKS = {
    'Makera O Flute Bits': lambda t: t['description'].startswith('Spiral O Metal'),
    'Makera Ball Endmills': lambda t: 'Metal' in t['description'],
    'Makera Engraving Bits': lambda t: 'Metal' in t['description'],
    'Makera Thread Mills': lambda t: True,
    'Makera Other Tools': lambda t: t['description'].startswith('Chamfering Bit'),
}

# the lab tool whose Copper 101-H02 preset is the template for each kind
TEMPLATES = {
    'flat end mill': 'Spiral O Metal 3.175*12mm',
    'ball end mill': 'Spiral O Metal 3.175*12mm',
    'drill': '3.175*7.4mm Drill',
    'thread mill': 'Carvera M3 Threadmill',
    'chamfer mill': 'Chamfering Bit - 1/8',  # the lab's name has a garbled inch sign
}


def lab_preset(tool, makera_copper):
    """The lab's Copper 101-H02 settings for a tool of this kind and size.

    From the lab's presets: every Spiral O end mill runs at 6000 rpm and
    200 mm/min with 200 mm/min plunge, stepping down 0.3 mm (ramp 300 mm/min)
    from 3.175 mm diameter up and 0.2 mm (ramp 200 mm/min) below; every drill
    at 10000 rpm with 50 mm/min plunge; every thread mill at 15000 rpm and
    Makera's copper feed (M2 100, M2.5 150, M3 and M4 200 mm/min), 0.1 mm
    stepover; the chamfering bit at 6000 rpm and 200 mm/min, 0.1 mm steps.
    Ball end mills get the end mill settings, engravers the chamfering bit's.
    """
    kind = tool['type']
    d = tool['geometry']['DC']
    if kind in ('flat end mill', 'ball end mill'):
        big = d >= 3.175 - 1e-6
        return {'n': 6000, 'v_f': 200, 'v_f_plunge': 200, 'v_f_ramp': 300 if big else 200,
                'stepdown': 0.3 if big else 0.2, 'ramp-angle': 1.2}
    if kind == 'drill':
        return {'n': 10000, 'v_f_plunge': 50, 'v_f_retract': 500}
    if kind == 'thread mill':
        if not makera_copper:
            raise ValueError('no Makera copper feed for %s' % tool['description'])
        return {'n': 15000, 'v_f': makera_copper['v_f'], 'v_f_plunge': makera_copper['v_f'],
                'v_f_ramp': 333.333333333333, 'stepover': 0.1, 'ramp-angle': 2}
    if kind == 'chamfer mill':
        return {'n': 6000, 'v_f': 200, 'v_f_plunge': 200, 'v_f_ramp': 300,
                'stepdown': 0.1, 'stepover': 0.1, 'ramp-angle': 2}
    raise ValueError('no lab preset rule for a %s' % kind)


def _fmt(v):
    return ('%.6f' % v).rstrip('0').rstrip('.')


def make_preset(tool, template, values):
    """A Copper 101-H02 preset for `tool`: the template preset with `values`."""
    p = copy.deepcopy(template)
    p.update(values)
    n, z, d = p['n'], tool['geometry'].get('NOF') or 1, tool['geometry']['DC']
    if 'v_f' in values:
        for k in ('v_f_leadIn', 'v_f_leadOut', 'v_f_transition'):
            p[k] = p['v_f']
        p['f_n'] = p['v_f'] / n
        p['f_z'] = p['v_f'] / n / z
    if 'n_ramp' in p:
        p['n_ramp'] = n
    p['v_c'] = 3.141592653589793 * d * n / 1000
    p['name'] = PRESET
    p['description'] = DERIVED
    p['guid'] = str(uuid.uuid5(uuid.NAMESPACE_URL, tool['guid'] + '/' + PRESET))
    e = p.setdefault('expressions', {})
    names = {'n': ('tool_spindleSpeed', '%s rpm'), 'v_f': ('tool_feedCutting', '%s mmpm'),
             'v_f_plunge': ('tool_feedPlunge', '%s mmpm'), 'v_f_ramp': ('tool_feedRamp', '%s mmpm'),
             'v_f_retract': ('tool_feedRetract', '%s mmpm'), 'stepdown': ('tool_stepdown', '%s mm'),
             'stepover': ('tool_stepover', '%s mm'), 'ramp-angle': ('tool_rampAngle', '%s degrees')}
    for k, (name, unit) in names.items():
        if k in values or (name in e and k in p):
            e[name] = unit % _fmt(p[k])
    return p


def _key(t):
    """Tools with the same kind and geometry are the same tool."""
    g = t.get('geometry', {})
    return (t['type'], t['description'] if t['type'] not in TEMPLATES else None,
            round(g.get('DC', 0), 3), round(g.get('LCF', 0), 2), round(g.get('OAL', 0), 2),
            round(g.get('SFDM', 0), 3), g.get('NOF'), round(g.get('tip-diameter', 0), 3),
            round(g.get('TA', 0) or g.get('SIG', 0), 1))


def _name(t):
    # the community files spell some lengths "17m" for "17mm"
    return re.sub(r'(\d)m\b', r'\1mm', ' '.join(t['description'].lower().split()))


def is_added(t):
    return t.get('post-process', {}).get('comment', '').startswith(ADDED)


def _templates(lab):
    out = {}
    for kind, desc in TEMPLATES.items():
        t = next(t for t in lab if t['description'].startswith(desc) and t['type'] == kind
                 or t['description'] == desc)
        out[kind] = next(p for p in t['start-values']['presets'] if p['name'] == PRESET)
    return out


def _with_preset(t, templates, makera_copper=None):
    """`t` with a Copper 101-H02 preset added if it has none (holders get none)."""
    if t['type'] not in TEMPLATES:
        return t
    presets = t.setdefault('start-values', {}).setdefault('presets', [])
    if not any(p['name'] == PRESET for p in presets):
        presets.insert(0, make_preset(t, templates[t['type']], lab_preset(t, makera_copper)))
    return t


def fetch_community():
    """{library name: [tools]} from the Carvera Community repository."""
    out = {}
    for name in COMMUNITY_PICKS:
        url = COMMUNITY_TOOLS + urllib.request.quote(name) + '.tools'
        req = urllib.request.Request(url, headers={'User-Agent': 'carvera-cam'})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        out[name] = json.loads(zipfile.ZipFile(io.BytesIO(data)).read('tools.json'))['data']
    return out


def read_mcmaster(path=MCMASTER_DRILLS):
    with open(path, encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def mcmaster_drills(lab, rows, templates):
    """One drill per diameter of McMaster-Carr's expanded-shank carbide drills
    (rows of MCMASTER_DRILLS), for the diameters the lab has no drill of.

    The series comes coated (TiN, TiAlN) and uncoated, and the coatings of one
    diameter differ in their maximum drilling depth. The drill takes the least
    of those depths as its flute length, and the part number of the variant
    with it, preferring the TiN series 6415N, which most lab drills are."""
    have = [t['geometry']['DC'] for t in lab if t['type'] == 'drill']
    by_d = {}
    for r in rows:
        by_d.setdefault(round(float(r['diameter_mm']), 4), []).append(r)
    template = next(t for t in lab if t['description'] == TEMPLATES['drill'])
    out = []
    for d, variants in sorted(by_d.items()):
        if any(abs(d - h) < 0.005 for h in have):
            continue
        r = min(variants, key=lambda r: (float(r['max_drilling_depth_mm']),
                                         MCMASTER_SERIES.index(r['part'][:5])))
        depth = round(float(r['max_drilling_depth_mm']), 1)
        tip = 130 if r['point'] == 'four facet' else 118
        t = copy.deepcopy(template)
        desc = '%s*%smm Drill' % (_fmt(round(d, 3)), _fmt(depth))
        if not r['size'].endswith(' mm'):  # an inch or wire-gauge size
            desc += ' (%s)' % r['size']
        g = t['geometry']
        g.update({'DC': d, 'LCF': depth, 'shoulder-length': depth, 'SIG': tip, 'SFDM': 3.175,
                  'OAL': round(float(r['overall_length_mm']))})
        t['description'] = desc
        t['guid'] = str(uuid.uuid5(uuid.NAMESPACE_URL, 'https://www.mcmaster.com/' + r['part']))
        t['product-id'] = r['part']
        t['product-link'] = 'https://www.mcmaster.com/%s/' % r['part']
        e = t['expressions']
        e.update({'tool_description': "'%s'" % desc.replace("'", "\\'"),
                  'tool_diameter': '%s mm' % _fmt(d), 'tool_fluteLength': '%s mm' % _fmt(depth),
                  'tool_shoulderLength': '%s mm' % _fmt(depth),
                  'tool_tipAngle': '%d degrees' % tip, 'tool_productId': "'%s'" % r['part'],
                  'tool_productLink': "'%s'" % t['product-link']})
        others = sorted(v['part'] for v in variants if v is not r)
        t['post-process']['comment'] = ADDED + 'McMaster-Carr "Expanded-Shank Carbide Drill Bits"' + (
            ' (other coatings: %s)' % ', '.join(others) if others else '')
        t['start-values'] = {'presets': []}
        out.append(_with_preset(t, templates))
    return out


def complete(lab, community, mcmaster=()):
    """The lab's tools, then the McMaster-Carr drills and the community tools for
    metal that the lab lacks."""
    templates = _templates(lab)
    lab = [_with_preset(copy.deepcopy(t), templates) for t in lab]
    seen = {_key(t) for t in lab} | {_name(t) for t in lab}
    added = mcmaster_drills(lab, mcmaster, templates)
    seen |= {_key(t) for t in added} | {_name(t) for t in added}
    for name, tools in community.items():
        for t in tools:
            if not COMMUNITY_PICKS[name](t) or _key(t) in seen or _name(t) in seen:
                continue
            seen |= {_key(t), _name(t)}
            t = copy.deepcopy(t)
            t['post-process']['comment'] = ADDED + 'Carvera Community "%s"' % name
            t['post-process']['number'] = 0
            copper = next((p for p in t.get('start-values', {}).get('presets', [])
                           if p['name'] == 'Copper'), None)
            added.append(_with_preset(t, templates, copper))
    added.sort(key=lambda t: (t['type'], t['geometry']['DC'], t['geometry'].get('LCF', 0)))
    return lab + added


def merge(lab, current):
    """After an export from Fusion: the lab's tools, plus the added tools already in
    the library that do not duplicate one of them."""
    templates = _templates(lab)
    lab = [_with_preset(copy.deepcopy(t), templates) for t in lab]
    seen = {_key(t) for t in lab} | {_name(t) for t in lab}
    return lab + [t for t in current if is_added(t) and _key(t) not in seen and _name(t) not in seen]


def read(path=TOOLS_FILE):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def write(data, path=TOOLS_FILE):
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.write('\n')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    data = read()
    lab = [t for t in data['data'] if not is_added(t)]
    data['data'] = complete(lab, fetch_community(), read_mcmaster())
    write(data)
    added = [t for t in data['data'] if is_added(t)]
    print('%d lab tools, %d added:' % (len(lab), len(added)))
    for t in added:
        p = next(p for p in t['start-values']['presets'] if p['name'] == PRESET)
        print('  %-46s %-13s n=%-5s feed=%-4s plunge=%s' % (
            t['description'], t['type'], p['n'], _fmt(p.get('v_f', 0)), _fmt(p['v_f_plunge'])))


if __name__ == '__main__':
    main()

"""Dump the CAM setups of a Fusion project's parts to JSON, to re-learn the lab's conventions.

Fusion takes a few seconds to open each document, and an MCP script call times
out after about a minute, so `dump(args)` handles documents until its time
budget runs out and prints MORE; call it again until it prints ALL DONE.

args:
  project: Fusion project name
  out:     output folder (one <path>__<document>.json per document with CAM,
           and an empty .none file per document without)
  skip:    regular expression of document names to skip (assemblies, vendor parts)
  budget:  seconds per call (default 30)
"""

import json
import os
import re
import time

import adsk.cam
import adsk.core


def _params(pp):
    d = {}
    for p in pp:
        try:
            d[p.name] = p.expression
        except Exception:
            pass
    return d


def _files(folder, path=''):
    for f in folder.dataFiles:
        if f.fileExtension == 'f3d':
            yield path, f
    for sub in folder.dataFolders:
        yield from _files(sub, path + sub.name + '/')


def _dump_doc(doc, key, label, out):
    try:
        cam = adsk.cam.CAM.cast(doc.products.itemByProductType('CAMProductType'))
    except RuntimeError:
        cam = None
    if not cam or cam.setups.count == 0:
        open(os.path.join(out, key + '.none'), 'w').close()
        return label + ': no CAM'
    res = {'doc': label, 'setups': []}
    for s in cam.setups:
        sd = {'name': s.name, 'machine': s.machine.description if s.machine else None,
              'params': _params(s.parameters), 'ops': []}
        for o in s.allOperations:
            od = {'name': o.name, 'strategy': o.strategy, 'suppressed': o.isSuppressed,
                  'params': _params(o.parameters)}
            try:
                od['tool'] = _params(o.tool.parameters)
            except Exception as e:
                od['tool'] = str(e)
            sd['ops'].append(od)
        res['setups'].append(sd)
    with open(os.path.join(out, key), 'w') as fh:
        json.dump(res, fh, indent=1)
    return '%s: %d setups' % (label, cam.setups.count)


def dump(args):
    t0 = time.time()
    app = adsk.core.Application.get()
    out = args['out']
    os.makedirs(out, exist_ok=True)
    skip = re.compile(args.get('skip', r'^(assembly|Makera)'))
    done = set(os.listdir(out))
    proj = [p for p in app.data.dataProjects if p.name == args['project']][0]
    for path, f in _files(proj.rootFolder):
        key = (path + f.name).replace('/', '__') + '.json'
        if skip.search(f.name) or key in done or key + '.none' in done:
            continue
        if time.time() - t0 > args.get('budget', 30):
            print('MORE')
            return
        doc = app.documents.open(f, False)
        print(_dump_doc(doc, key, path + f.name, out))
        doc.close(False)
    print('ALL DONE')

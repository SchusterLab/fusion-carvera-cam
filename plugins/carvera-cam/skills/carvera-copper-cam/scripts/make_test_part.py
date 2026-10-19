"""Build a throwaway 30 x 20 x 4 mm test part in a new, unsaved Fusion document.

It has one of each feature the generator handles: an outline with R2 corners,
a top pocket with R0.8 corners (needs a small end mill), a bottom pocket, a
through window, two 3.8 mm bores, two M2 tapped holes (Hole command, tapped,
not modeled), a blind 2.2 mm hole, and a slot open to the side
(args {"side_slot": false} leaves it out). {"angled_notch": true} adds a notch
open to the side whose walls meet the outline at a slant, and {"pocket_hole":
true} a 2.2 mm hole through the top pocket's floor. Used to test
carvera_cam.py.
"""

import adsk.core
import adsk.fusion


def P(x, y, z=0.0):
    return adsk.core.Point3D.create(x / 10, y / 10, z / 10)


def _nz(f):
    ok, n = f.evaluator.getNormalAtPoint(f.pointOnFace)
    return n.z


def make(args=None):
    app = adsk.core.Application.get()
    app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    des = adsk.fusion.Design.cast(app.activeProduct)
    des.designType = adsk.fusion.DesignTypes.ParametricDesignType
    root = des.rootComponent
    ext = root.features.extrudeFeatures
    V = adsk.core.ValueInput.createByString
    CUT = adsk.fusion.FeatureOperations.CutFeatureOperation
    xy = root.xYConstructionPlane

    def pick(s, area_mm2, n=1):
        ps = sorted(list(s.profiles), key=lambda p: abs(p.areaProperties().area * 100 - area_mm2))
        oc = adsk.core.ObjectCollection.create()
        for p in ps[:n]:
            oc.add(p)
        return oc

    def vert_edges(faces):
        ec = adsk.core.ObjectCollection.create()
        for f in faces:
            for e in f.edges:
                g = e.geometry
                if (isinstance(g, adsk.core.Line3D) and abs(g.startPoint.z - g.endPoint.z) > 0.05
                        and not ec.contains(e)):
                    ec.add(e)
        return ec

    def fillet(ec, r):
        fi = root.features.filletFeatures.createInput()
        fi.edgeSetInputs.addConstantRadiusEdgeSet(ec, V(r), True)
        root.features.filletFeatures.add(fi)

    sk = root.sketches.add(xy)
    sk.sketchCurves.sketchLines.addCenterPointRectangle(P(0, 0), P(15, 10))
    base = ext.addSimple(sk.profiles.item(0), V('4 mm'),
                         adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    body = base.bodies.item(0)
    body.name = 'TestPart'
    fillet(vert_edges(list(body.faces)), '2 mm')

    pi = root.constructionPlanes.createInput()
    pi.setByOffset(xy, V('4 mm'))
    top_plane = root.constructionPlanes.add(pi)

    s = root.sketches.add(top_plane)
    s.sketchCurves.sketchLines.addCenterPointRectangle(P(-3, 0), P(3, 4))
    ei = ext.createInput(pick(s, 96), CUT)
    ei.setDistanceExtent(False, V('-1.5 mm'))
    fillet(vert_edges(list(ext.add(ei).sideFaces)), '0.8 mm')

    s = root.sketches.add(xy)
    s.sketchCurves.sketchLines.addCenterPointRectangle(P(10, 0), P(12, 3))
    ei = ext.createInput(pick(s, 24), CUT)
    ei.setAllExtent(adsk.fusion.ExtentDirections.PositiveExtentDirection)
    fillet(vert_edges(list(ext.add(ei).sideFaces)), '1.6 mm')

    def circles(plane, pts, dia, depth=None):
        s = root.sketches.add(plane)
        for (x, y) in pts:
            s.sketchCurves.sketchCircles.addByCenterRadius(P(x, y), dia / 20)
        ei = ext.createInput(pick(s, 3.14159 * dia * dia / 4, len(pts)), CUT)
        if depth is None:
            ei.setAllExtent(adsk.fusion.ExtentDirections.PositiveExtentDirection)
        else:
            ei.setDistanceExtent(False, V(depth))
        ext.add(ei)

    circles(xy, [(-12, -6), (-12, 6)], 3.8)
    # a hole through the top pocket's floor, whose rim the pocket floor's deburr
    # takes (args {"pocket_hole": true})
    if (args or {}).get('pocket_hole'):
        circles(xy, [(0, 0)], 2.2)
    circles(top_plane, [(6, -7)], 2.2, '-2 mm')

    s = root.sketches.add(xy)
    s.sketchCurves.sketchLines.addCenterPointRectangle(P(-3, -6.5), P(0, -4))
    ei = ext.createInput(pick(s, 30), CUT)
    ei.setDistanceExtent(False, V('1 mm'))
    ext.add(ei)

    # a slot open to the +Y side, 2.2 mm deep: the tool enters it sideways, so it
    # pushes the tabs off the mid-plane (z = 2 mm) to below z = 1.8 mm
    if (args or {}).get('side_slot', True):
        s = root.sketches.add(top_plane)
        s.sketchCurves.sketchLines.addTwoPointRectangle(P(2, 7), P(8, 11))
        ei = ext.createInput(pick(s, 18), CUT)
        ei.setDistanceExtent(False, V('-2.2 mm'))
        ext.add(ei)

    # a notch open to the +X side, 1.5 mm deep, whose side walls meet the
    # outline at a slant, with R2 inside corners (args {"angled_notch": true})
    if (args or {}).get('angled_notch'):
        s = root.sketches.add(top_plane)
        ls = s.sketchCurves.sketchLines
        pts = [P(16, -8.8), P(13.2, -7.6), P(13.2, -5.2), P(16, -4.0)]
        for p0, p1 in zip(pts, pts[1:] + pts[:1]):
            ls.addByTwoPoints(p0, p1)
        prof = min(s.profiles, key=lambda p: p.areaProperties().area)
        ei = ext.createInput(prof, CUT)
        ei.setDistanceExtent(False, V('-1.5 mm'))
        side = ext.add(ei).sideFaces
        inner = adsk.core.ObjectCollection.create()
        for e in vert_edges(list(side)):
            if e.geometry.startPoint.x < 1.5 - 0.05:  # the two corners inside the part
                inner.add(e)
        fillet(inner, '2 mm')

    # M2 tapped holes: Hole command, tapped, threads not modeled
    hf = root.features.holeFeatures
    ti = root.features.threadFeatures.createThreadInfo(True, 'ISO Metric profile', 'M2x0.4', '6H')
    top = max([f for f in body.faces if isinstance(f.geometry, adsk.core.Plane) and _nz(f) > 0.9],
              key=lambda f: f.pointOnFace.z)
    s = root.sketches.add(top)
    pts = adsk.core.ObjectCollection.create()
    for (x, y) in [(-7, -7), (-7, 7)]:
        pts.add(s.sketchPoints.add(s.modelToSketchSpace(P(x, y, 4))))
    hi = hf.createSimpleInput(V('1.6 mm'))
    hi.setPositionBySketchPoints(pts)
    hi.setAllExtent(adsk.fusion.ExtentDirections.PositiveExtentDirection)
    hi.setToTappedHole(ti)
    hi.isFullLength = True
    hf.add(hi)

    bad = [des.timeline.item(i).name for i in range(des.timeline.count)
           if des.timeline.item(i).healthState != adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState]
    print('test part: %d faces, %.1f mm3, unhealthy: %s' % (body.faces.count, body.volume * 1000, bad))


def make_open_pockets(args=None):
    """A throwaway 40 x 30 x 4 mm plate with only pockets open to the side, for
    the open-pocket recipe: a wide notch on the +X edge whose walls meet the
    outline about 30 degrees off square (it takes several passes), a channel
    across the part open at both ends (two runs of walls), and a notch on the
    -Y edge whose long wall runs about 10 degrees off the outline (a pass along
    it does not reach the outline cut). Each is cut from the top and has R2
    inside corners."""
    app = adsk.core.Application.get()
    app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    des = adsk.fusion.Design.cast(app.activeProduct)
    des.designType = adsk.fusion.DesignTypes.ParametricDesignType
    root = des.rootComponent
    ext = root.features.extrudeFeatures
    V = adsk.core.ValueInput.createByString
    CUT = adsk.fusion.FeatureOperations.CutFeatureOperation
    xy = root.xYConstructionPlane

    sk = root.sketches.add(xy)
    sk.sketchCurves.sketchLines.addCenterPointRectangle(P(0, 0), P(20, 15))
    body = ext.addSimple(sk.profiles.item(0), V('4 mm'),
                         adsk.fusion.FeatureOperations.NewBodyFeatureOperation).bodies.item(0)
    body.name = 'OpenPockets'

    def fillet(ec, r):
        fi = root.features.filletFeatures.createInput()
        fi.edgeSetInputs.addConstantRadiusEdgeSet(ec, V(r), True)
        root.features.filletFeatures.add(fi)

    def vertical(faces, keep=lambda p: True):
        ec = adsk.core.ObjectCollection.create()
        for f in faces:
            for e in f.edges:
                g = e.geometry
                if isinstance(g, adsk.core.Line3D) and abs(g.startPoint.z - g.endPoint.z) > 0.05 \
                        and keep(g.startPoint) and not ec.contains(e):
                    ec.add(e)
        return ec

    fillet(vertical(list(body.faces)), '2 mm')
    pi = root.constructionPlanes.createInput()
    pi.setByOffset(xy, V('4 mm'))
    top_plane = root.constructionPlanes.add(pi)

    def notch(pts, depth, inside):
        s = root.sketches.add(top_plane)
        for p0, p1 in zip(pts, pts[1:] + pts[:1]):
            s.sketchCurves.sketchLines.addByTwoPoints(p0, p1)
        prof = min(s.profiles, key=lambda p: p.areaProperties().area)
        ei = ext.createInput(prof, CUT)
        ei.setDistanceExtent(False, V(depth))
        side = ext.add(ei).sideFaces
        ec = vertical(list(side), inside)
        if ec.count:
            fillet(ec, '2 mm')

    inside = lambda p: abs(p.x) < 2.0 - 0.05 and abs(p.y) < 1.5 - 0.05
    # +X edge: walls from the outline in to x = 14, about 30 degrees off square
    notch([P(21, 0.5), P(14, 4.5), P(14, 9), P(21, 13)], '-1.5 mm', inside)
    # a channel across the part along Y, 6 mm wide
    notch([P(-15, -16), P(-9, -16), P(-9, 16), P(-15, 16)], '-1.5 mm', inside)
    # -Y edge: one wall square to the outline, the long one about 10 degrees off it
    notch([P(-2, -16), P(-2, -12.5), P(12, -15.5), P(12, -16)], '-1 mm', inside)

    bad = [des.timeline.item(i).name for i in range(des.timeline.count)
           if des.timeline.item(i).healthState != adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState]
    print('open-pocket part: %d faces, %.1f mm3, unhealthy: %s' % (body.faces.count, body.volume * 1000, bad))


def make_open_floor(args=None):
    """A throwaway 30 x 20 x 3 mm plate whose top is cut 1.2 mm down all over but
    for two islands: an 8 x 8 mm boss and a 2.2 mm pin, close enough that the
    passes round them meet. The floor runs out to every side of the part, so
    it has no walls along its sides, for the passes round islands, and a 3 mm
    hole goes through it, for the deburr of its rim beside them. Its stock
    must be larger than the part (for example 45 x 35 x 4 mm, "y_offset" 7):
    in stock of the part's own size, the passes would run into the jaws."""
    app = adsk.core.Application.get()
    app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    des = adsk.fusion.Design.cast(app.activeProduct)
    des.designType = adsk.fusion.DesignTypes.ParametricDesignType
    root = des.rootComponent
    ext = root.features.extrudeFeatures
    V = adsk.core.ValueInput.createByString

    sk = root.sketches.add(root.xYConstructionPlane)
    sk.sketchCurves.sketchLines.addCenterPointRectangle(P(0, 0), P(15, 10))
    body = ext.addSimple(sk.profiles.item(0), V('3 mm'),
                         adsk.fusion.FeatureOperations.NewBodyFeatureOperation).bodies.item(0)
    body.name = 'OpenFloor'
    top = max((f for f in body.faces if isinstance(f.geometry, adsk.core.Plane)),
              key=lambda f: f.pointOnFace.z)
    s = root.sketches.add(top)
    M = s.modelToSketchSpace
    s.sketchCurves.sketchLines.addCenterPointRectangle(M(P(-5, 0, 3)), M(P(-1, 4, 3)))
    s.sketchCurves.sketchCircles.addByCenterRadius(M(P(8, 4, 3)), 0.11)
    # everything but the two islands: the profile with the largest area
    prof = max(s.profiles, key=lambda p: p.areaProperties().area)
    ext.addSimple(prof, V('-1.2 mm'), adsk.fusion.FeatureOperations.CutFeatureOperation)
    # a 3 mm hole through the floor, whose rim the deburr takes beside the islands
    s2 = root.sketches.add(root.xYConstructionPlane)
    s2.sketchCurves.sketchCircles.addByCenterRadius(P(9, -5), 0.15)
    ext.addSimple(s2.profiles.item(0), V('3 mm'), adsk.fusion.FeatureOperations.CutFeatureOperation)

    bad = [des.timeline.item(i).name for i in range(des.timeline.count)
           if des.timeline.item(i).healthState != adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState]
    print('open-floor part: %d faces, %.1f mm3, unhealthy: %s' % (body.faces.count, body.volume * 1000, bad))

"""Build a throwaway 30 x 20 x 4 mm test part in a new, unsaved Fusion document.

It has one of each feature the generator handles: an outline with R2 corners,
a top pocket with R0.8 corners (needs a small end mill), a bottom pocket, a
through window, two 3.8 mm bores, two M2 tapped holes (Hole command, tapped,
not modeled) and a blind 2.2 mm hole. Used to test carvera_cam.py.
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
    circles(top_plane, [(6, -7)], 2.2, '-2 mm')

    s = root.sketches.add(xy)
    s.sketchCurves.sketchLines.addCenterPointRectangle(P(-3, -6.5), P(0, -4))
    ei = ext.createInput(pick(s, 30), CUT)
    ei.setDistanceExtent(False, V('1 mm'))
    ext.add(ei)

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

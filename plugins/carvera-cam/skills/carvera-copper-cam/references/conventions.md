# SchusterLab Carvera CAM conventions

These are the conventions `carvera_cam.py` applies.
They come from the lab's own Fusion setups: about 40 setups and 530 operations in the project `2026-01-06_smpm-sample-package-6port`, with `sample-puck-v2-6port10x10` as the reference part.

## Machine, post and material

| Item | Value | Where the generator gets it |
|---|---|---|
| Machine | Makera Carvera 3-axis Community Version, latest | [Carvera Community profiles](https://github.com/Carvera-Community/Carvera_Community_Profiles/tree/main/CAM_Post_Processors/Fusion360-profiles), `main` branch |
| Post processor | Makera Carvera Community Post (`Carvera.cps`), latest | the same folder |
| Tools | 132 tools to buy from Makera and McMaster-Carr | [`assets/carvera-tools.json`](../assets/carvera-tools.json), which `scripts/tool_library.py` builds from Makera's [tool files](https://github.com/Carvera-Community/Carvera_Community_Profiles/tree/main/CAM_Post_Processors/Fusion360-profiles/Tool%20Files) and McMaster-Carr's table of [expanded-shank carbide drill bits](https://www.mcmaster.com/products/drill-bits/expanded-shank-carbide-drill-bits~~/) (copied to [`assets/mcmaster-expanded-shank-carbide-drills.csv`](../assets/mcmaster-expanded-shank-carbide-drills.csv)) |

`build` downloads the machine and post again whenever they change upstream, and keeps them in `%LOCALAPPDATA%\carvera-cam\community`; without network it uses the copies there.
The community machine's 3D model links to a file in its author's Fusion account, which no one else can open, so `build` uploads the machine's `.f3d` once per upstream version to a Fusion project named `carvera-cam` in the user's hub and links the machine to that copy.
If the upload fails, the setups get the machine without its 3D model and `build` warns that simulation does not check the machine frame.

The lab's own setups before October 2026 used the machine `...Community Version 1.17-slab` and post v1.1.21 from the lab's Cloud Library.
That machine's settings differ from upstream 1.4.6 only in its tool change time (30 s against 45 s), which changes time estimates, not toolpaths.
Its 3D model differs in the bed, where the fixture plate sits: the wasteboard and its anchors are gone, the bed is 18 mm lower (z = −21.175 mm) for x < 342 mm, and a construction point `Point1` at (−15, −15, −21.175) mm marks the corner of that recess.
Before uploading the upstream model, `build` makes the same three changes to it (`_slab_model` in `carvera_cam.py`).

Every tool has one feed preset, **Copper** (C10100 oxygen-free copper, H02 half-hard temper).
Aluminum (6063-T5) runs the same programs at 200% feed and spindle override on the Carvera, so there is one set of toolpaths per part.
No coolant.

The library holds only the tools `build` assigns, so a user can buy them all:

| Tools | Source | Copper |
|---|---|---|
| Spiral O Metal end mills of 1, 1.5, 2, 2.5 and 3.175 mm | Makera | 6000 rpm, 200 mm/min, plunge 200 mm/min; stepdown 0.3 mm and ramp 300 mm/min for the 3.175 mm end mill, 0.2 mm and 200 mm/min for the smaller ones |
| Carvera thread mills, M2, M2.5, M3 and M4 | Makera | 10000 rpm and 0.1 mm stepover; the rest is Makera's own copper preset: M2 100, M2.5 150, M3 and M4 200 mm/min, stepping down one pitch |
| Chamfering bit, 1/8″ shank | Makera | 6000 rpm, 200 mm/min, 0.1 mm steps |
| 122 uncoated expanded-shank carbide drills, 0.152 to 3.175 mm (part numbers 2841A…) | McMaster-Carr | 10000 rpm, plunge 50 mm/min, retract 500 mm/min |

The settings are the ones SchusterLab's parts use (see [rules.md](rules.md) for the evidence); Makera's own copper presets run its end mills faster, at 12000 rpm and 300 mm/min.
A preset cannot hold the drilling cycle, so `build` sets it in each drill operation (see "Operation parameters").

Every drill has a 1/8″ (3.175 mm) shank for the Carvera's collet, whatever its diameter.
`tool_library.py` makes each drill from Makera's 1/8″ drill, with the catalogue's diameter, maximum drilling depth (as the flute length), overall length and point angle (130°, checked on product pages), and names it like `1.613*9.7mm Drill (No. 52)`.
The series also comes coated with TiN (6415N…) and TiAlN (2598N…), but every diameter comes uncoated, so the library holds only the uncoated drills.
For a hole, `build` takes the drill closest in diameter of those that fit: within −0.02 to +0.05 mm of the hole's diameter, with a flute at least 0.5 mm longer than the hole's depth.
The lab's smallest drill was 0.7 mm, so for smaller drills the feed of 0.005 mm per revolution has no evidence behind it.

To rebuild the library, for example after Makera changes its tool files, run `python -I scripts/tool_library.py` with plain Python.
It stops with an error if a tool it picks is no longer in Makera's files.

## Tool slots

The Carvera's automatic tool changer holds six tools.
T1, T2 and T3 are always the same three tools; every other tool takes the next slot in the order the programs first use it, the top program's operations first, then the bottom's.

| Slot | Tool | Copper preset |
|---|---|---|
| T1 | 3.175 × 7.4 mm drill (McMaster 2841A98) | 10000 rpm, plunge 50 mm/min, retract 500 mm/min |
| T2 | Spiral O Metal 3.175 × 12 mm, one flute: roughing | 6000 rpm, 200 mm/min, ramp 300 mm/min, stepdown 0.3 mm |
| T3 | the same end mill, a second one: finishing | as T2 |
| T4 on, by first use | small end mills: Spiral O Metal 2.5, 2, 1.5 or 1 mm | 6000 rpm, 200 mm/min, stepdown 0.2 mm |
| | other drills: tap drills and plain holes (0.152–3.175 mm) | as T1 |
| | Carvera thread mills, M2 to M4 | 10000 rpm; M2 100 mm/min, M2.5 150 mm/min, M3 and M4 200 mm/min |
| | chamfering bit, 1/8″ shank | 6000 rpm, 200 mm/min |

With the lab's operation order (see below), that usually puts the small end mills first, then the drills, the thread mills and the chamfering bit.

T2 roughs every feature in steps; T3, a fresh tool of the same kind, then runs each roughing operation again at full depth in one pass, which cleans the walls. The rough pockets leave 0.2 mm on their floors, which T3's pass takes off.

## Setups

The face with more features, pocket floors and blind holes, is machined first, as the top; through holes and windows count for neither face.
When the design's bottom face has more, `build` machines a copy of the body turned half a turn about its X axis in place (`carvera-cam part` component) and hides the design's body.

Both setups' X axis runs along the part's Y axis: the vise's jaws close along the Carvera's X axis and grip the stock's Y edges (see "Fixture"), so the part lies on the machine turned 90° from its design.
The top setup's X is the model's +Y; the bottom setup's is −Y.

**Top setup.**
The stock is a fixed box: commonly a 2″ × 150 mm bar 6.35 mm (0.25″) or 19 mm thick, or a 150 × 150 mm copper plate 6.2 mm thick.
The part sits centred in X, 10 mm from the back of the stock in Y, and centred in the stock's thickness unless the user moves it.
The lab's parts sit at various heights in the stock; the height decides how much facing each program does, and so when the operator must come back to flip the stock.
The work coordinate system (WCS) origin is the stock box's `top 2` corner: the corner at the model's +X and +Y, which is the setup's +X and −Y.
The box's corners and sides follow the setup's axes, so the box is set as `stock y` long along the setup's X.

**Bottom setup.**
The stock is turned over about the setup's X axis, the axis the jaws close along, so its back edge stays on the fixed jaw: Z flipped (`flipZ`) and X kept, stock from the previous setup, WCS origin at the stock box's `top 2` corner.

## Fixture

Each setup carries a model of the vise, so Fusion's simulation catches collisions with the vise and with the Carvera itself.
The lab's pattern is `2026-01-20_sample-puck-6port7x7` (project `2026-01-06_smpm-sample-package-6port`, folder `old-versions`).

- The lab's fixture design is `2026-01-18_slab-fixture-3-axis` (project `2026-01-18_slab-fixture`): Saunders Machine Works' [Carvera fixture plate](https://saundersmachineworks.com/products/makera-carvera-fixture-tooling-plate) (inch), a [Gen3 hobby mod vise](https://saundersmachineworks.com/products/modular-vise-system-hobby-gen3) (1/4″) with two [smooth insert bars](https://saundersmachineworks.com/products/smooth-jaw-insert-for-modular-vise), and the Carvera's 4th-axis module.
- The generator downloads those three Saunders STEP files and places them as that design does (`SMW_PLACEMENTS` in `carvera_cam.py`), so it needs no access to the lab's design. It leaves out the 4th-axis module, which Saunders does not publish, so simulation does not check it. It caches the files and the assembled fixture in `%LOCALAPPDATA%\carvera-cam\fixture`, and stops with an error if a changed Saunders model no longer puts the jaw faces where the lab's design has them.
- The jaws grip the stock's Y edges, 4.3 mm deep; the fixed jaw is on the back edge, the one the part is 10 mm from, and the adjustable jaw opens to the stock's Y size.
- The bottom setup has its own copy of the fixture, turned 180° about X with the part, gripping the stock left after the top program.
- Both setups list their copy as the setup's fixture and put it on the machine (Part Position): the fixture's point `Point1`, 2 mm inside the plate's corner and 1.27 mm above its bottom, goes on the machine's `Point1` (the Table Attach Point), so the machine's own geometry is checked too.

The lab built this with a Manufacturing Model holding the stock, the part and two embedded copies of the fixture.
The generator works in the part's design instead, and adds the fixture's bodies to it as fixed geometry: an inserted copy of the fixture assembly comes in through the API with its jointed parts unsolved and scattered.

## Operation order

Top setup:

1. T1 drills the main entry hole through the stock, or in stages with the outline when the stock is deeper than T1 reaches (see "Entry holes").
2. T2 faces the stock top down to 0.2 mm above the part's top face and cuts the outline down to the tab band, both entering through the main entry hole.
3. T1 drills the other entry holes and pre-drills the holes the end mill will bore, each from where the end mill has left the stock: the corner-chain entries from the top of the tab band, the pocket and window entries and pre-drills from 0.2 mm above the face.
4. T2 roughs the pockets, bores, through windows, and the outline between the tabs down through the band.
5. T3 finishes the features of steps 2 and 4, except the face and the plain bores (step 9), in single full-depth passes. The pockets are finished with a 2D pocket at the floor, which takes off the 0.2 mm the roughing left there. The windows have no floor, so they are finished with a 2D contour along their walls.
6. Small end mills cut slots the 3.175 mm end mill cannot enter and the pockets beyond them (see "Slots"), then clear the pocket and window corners it cannot reach (rest machining, plunge entry). Each corner gets the largest small end mill that fits its radius and reaches its depth, unless one already chosen for another corner fits too: that one is used again, so the part needs fewer tools.
7. Drills cut plain holes and tap drills.
8. Thread mills cut the threads.
9. T3 finishes the plain bores, after the drills and thread mills, so that the finishing pass cleans up the burrs they leave on the bores' floors and edges (user, 2026-10-17). Holes bored to a tap size keep their finishing pass in step 5, before the thread mill, which needs the hole at its final size.
10. T3 finishes the face, taking off the last 0.2 mm in one pass.
11. The chamfering bit deburrs the edges of the top face, then, in "Deburr counterbores", the edge of each hole that goes on down from a counterbore's floor, which the face's deburr does not reach (user, 2026-10-17); there the point goes down the hole below, 0.5 mm past the chamfer, or less above a blind hole's bottom.

The bottom setup repeats steps 2–11 for the bottom-side features, with no corner chains, and with no main entry hole of its own when the top setup's goes through the stock; its outline stops at the bottom of the tab band.

A drill only cuts what the end mill cannot cut first: the main entry hole is drilled from the stock top because the first end mill cut enters through it, and every other hole waits until the end mill has cleared the stock above it.
This saves wear on the drills, at the cost of two more tool changes in the top setup and one in the bottom.
The lab's parts from October 2026 on (`kiutra-beam`, `tee`) drill this way.

The face is finished last because facing raises fewer burrs than side milling and thread milling, so the last cut across the face removes the burrs the earlier cuts left on it.
The generator keeps track of the stock's highest point in each setup, which is where the latest facing pocket left the stock: the stock top until the rough face, 0.2 mm above the face until the finishing face, and the face after it.
Every operation that would start at the face or above it starts at that point instead, so its first pass cuts the stock that is there and no air above it:
- the outline, pockets, bores, windows, pocket corners, entry holes and the finishing face take their top height from the face's patch (`from point`), 0.2 mm above it until the finishing face;
- drills, bores and thread mills that start at the hole top start 0.2 mm above it until the finishing face.
Operations that start below the face, such as the cuts round the tabs and the deeper stages of a staged cut, keep their own top heights.

Every top and bottom height is taken from geometry, not computed from the stock top: the stock top, the face patches, the tab bodies' faces, the pocket floors, a window's top face, the part's far face, or the hole top.
An offset from that geometry is only ever one of the lab's fixed numbers: the 0.2 mm the rough face leaves, the 0.5 mm a drill or cut goes past a face it breaks through, or a thread's offset from threads.toml.
The one exception is a cut deeper than T1 reaches, which goes down in stages; the stage depths come from the drill's reach and have no geometry.

The lab's parts from October 2026 on (`kiutra-beam`, `tee`) work this way; older parts finish the face with the other T3 passes.

## Entry holes

The end mill never ramps or plunges into solid metal.
It enters every cut through a hole the T1 drill (3.175 mm, the end mill's diameter) makes first, at points kept in a sketch:

- **The main entry**, just outside the outline at the edge of a tab, one tool radius out, so the end mill's centre sits on the outline's toolpath. T1 drills it through the whole stock with 0.5 mm breakthrough. Both facing pockets and both outline contours enter there.
- **One entry per corner chain** (the outline between two tabs; see "Holding tabs"), at the same offset from the start of the chain, drilled through the tab band from its top, which the outline cut has already reached there.
- **One entry per pocket**, at the pocket's centre (the centre of its floor's outline, else its centroid) where the end mill has room there clear of the walls rising from the floor, a deeper pocket, hole or window in the floor counting as open; else at the point of the pocket farthest from its walls. It is drilled from 0.2 mm above the face until the drill's cone is half the end mill wide at the pocket floor: the end mill enters copper only down a hole at least half its width, the cone included (rule TH4; user, 2026-10-17), and going no deeper leaves the smallest dimple, 1.59 mm across and 0.37 mm deep for T1's 130° point; `build` warns about each one. A stage of the drill's reach above the floor takes the tip through. A pocket toolpath enters there only if the end mill has 0.05 mm of room all round, five times the pocket's tolerance; with less, Fusion refuses the entry. A closed pocket with less room, but wide enough for the end mill, still gets the entry hole, and a 2D contour along its wall cuts it instead, entering through the hole (`build` warns about it). A shelf, a floor that steps down into a deeper pocket inside it, is pocketed with that deeper pocket as one region (the opening just above the shelf), entered at the region's centre, chosen the same way, and drilled to the shelf's depth. The rough pocket of a region inside a shallower one starts at that one's rough floor, 0.2 mm above its shelf, as the window's does: the end mill has cleared the region down to there, and from the top it would mill air (user, 2026-10-17). The rough pocket is then one operation per such starting height (`Pockets from 0.9 mm`). The finishing pocket cuts such a shelf region on its own (`Pockets (finish) at 0.7 mm`), with stock contours that leave out the deeper regions inside it: they are roughed below its floor by then, and the tool runs over them only as far as its radius, to reach the shelf's edge.
A shelf the region reaches only through a slot narrower than the end mill gets no hole of its own; the slot's small end mill comes in through the slot (see "Slots").
- **One entry per through window**, at its centre, drilled from 0.2 mm above the face through the part, 0.5 mm past its bottom, with the drill tip through. A pocket mills the window, entering there, from the floor of the deepest shelf region over it (that region's pocket has already cleared the window's footprint down to it) through the part. A pocket entry, on either face, that lands on a window's hole uses that hole and is not drilled. A window with too little room at its centre and elsewhere for a pocket to enter, but wider than the end mill, gets its hole on the path of a 2D contour that cuts it instead.

**Slots.** A straight slot as wide as a small end mill (within 0.02 mm), too narrow for the 3.175 mm end mill, open at one end where its floor drops into a deeper cut and closed at the other, is cut along its centreline by that end mill: a 2D contour with the tool centre on a line in the layout sketch, from 0.3 mm past the open end, in stock the deeper cut has cleared, to the centre of the closed end's round, in 0.2 mm steps down to the slot floor.
A shallower pocket the closed end reaches into is then milled by the same end mill, without rest machining, entering at the slot's end, which the slot has cut deeper than the pocket's floor.
- **A pocket open to the side** gets no hole, and is cut by 2D contour passes along its walls instead of a pocket, each pass starting and ending on the outline cut's path, where the outline cut has already cleared the stock. Fusion picks where a pocket enters an open pocket, whatever the ramp type or lead-in, and there the tool's edge plunges into stock nothing has cut. `build` cuts them itself, or with `"recipes": true` leaves them to [recipes/open-pocket.md](recipes/open-pocket.md) and lists them for it, which does the same by hand; the roughing passes leave 0.2 mm on the floor, and the finishing passes take it off.

The pockets use the `predrill` ramp with these points as their predrill positions; the outline contours use the main entry as their entry position, and the window and narrow-pocket contours their own entries.

T1 drills at most 6.9 mm (its 7.4 mm flute less 0.5 mm) into the material it starts in.
A hole deeper than that goes down in stages with the cut that enters through it, as the lab does by hand (`eccosorb-clamp-6port`, Setup1): T1 drills as deep as it reaches, the end mill cuts down to there, T1 drills on from the milled floor, and so on.
- The main entry and the outline's rough cut go down together; if T1 still cannot get through the stock once the outline reaches the tab band, the bottom setup drills its own main entry, staged with its outline.
- The pocket entries and the pockets' rough cuts go down together in bands of 6.9 mm from the face; a pocket whose floor lies in a band is cut to its floor there.
- A bore deeper than 6.9 mm, an M4 tap hole's included, is pre-drilled and rough-bored in stages at the bore.

The finishing passes still cut each feature in one pass to full depth.

## Holding tabs

The part stays attached to the stock by four bridges ("tabs") until a separate program cuts them.
They sit on the longest straight or arc edges of the outline, one at the middle of each, 5 mm wide along the outline.
The tab band is 1 mm thick (0.4 × the part's thickness for parts under 2.5 mm) and is centred on the plane halfway between the part's top and bottom.
A pocket open to the side of the part is reached sideways at its own depth, so the band moves to the nearest height clear of every such opening.

- The top setup's outline cuts from the faced stock down to the top of the band.
- The **outline between tabs** (four corner chains, each running from one tab to the next) then cuts from the top of the band to 0.5 mm below its bottom.
- The bottom setup's outline cuts from the bottom face up to the bottom of the band.
- `<part>_tabs`, the last program, cuts each tab span through the band and 0.5 mm past it, in 0.2 mm steps, and frees the part.

Each tab span is 5 mm along the outline, so the bridge left after the end mill's radius on each side is about 1.8 mm wide.
The corner chains and tab spans are curves in the `carvera-cam` sketch; the generator orients each chain so a left-compensated tool stays outside the part in both setups.

`build` models the tabs as bodies in a `carvera-cam tabs` component: each is the strip of stock the outline cut leaves under a tab, one end mill wide outward from the outline and as thick as the band, with concave ends where the cut between the tabs stops (an arc of the end mill's radius, around the tab entry hole).
The heights above are set from the first tab's top and bottom faces (Fusion's "Selection" height), with 0.5 mm offsets past the band: the top setup's outline stops at the tab's top face, the bottom setup's at its bottom face, and the tab entry holes, the outline between tabs and the tab cut run between the two.
Fusion reads such a height when it is set, not when the tabs change: to move the tabs, run `build` again with a new `tab_z` or `tab_width`.

## Operation parameters

Feeds, speeds and stepdowns come from the tool preset; the operations set only the following.

| Operation | Parameters |
|---|---|
| 2D pocket | tolerance 0.01 mm (at the lab's 0.1 mm, Fusion refuses an entry hole with less than about 0.5 mm of room round the end mill), stepover 0.6 × tool diameter, compensation left, no stock to leave, `predrill` ramp at the entry holes; floors selected as pocket faces, shelves as the closed opening above them (one counter-clockwise chain per opening); the rough facing pocket starts at the stock top, the finishing one 0.2 mm above the face, and the roughing pass leaves 0.2 mm on the face (axial stock to leave 0.2 mm, radial 0). The rough pockets leave 0.2 mm on their floors in the same way, and the finishing pocket takes it off. It selects a planar patch filling the part's outline at that face (`carvera-cam faces` component), not the face itself: a pocket on the face goes round the face's openings and leaves the stock above them uncut, while the patch has no holes and, taken from the larger of the top and bottom outlines, also covers a pocket open to the side. The patch reaches 3.23 mm past the outline: the end mill's diameter, plus the 0.05 mm of room Fusion wants round the main entry hole, which is the outline cut's and sits one radius outside the outline. The pocket selects the patch's outer edge, not the patch: a selected face has nothing walling it in, so Fusion takes it for an open pocket and runs the tool past its edge, so the face also clears the stock the outline cut comes down through, one end mill wide outside the part. The finishing face selects a second patch that stops at the outline, since by then the outline cut has cleared the stock past it. It selects that patch as a pocket face, so the tool may run past the outline, and it enters at the main entry hole like the rough face. No pocket helixes or ramps into copper: each enters at a pre-drilled hole |
| 2D contour | tolerance 0.01 mm, compensation left, two finishing stepovers, no ramp, no stock to leave; a closed contour enters at its entry hole. A contour along a path sketched for the tool's centre (a slot's centreline, a pass in a pocket open to the side) has compensation off |
| Drill | chip breaking - partial retract cycle, 0.1 mm pecking depth, 1 mm accumulated pecking depth, 0.1 mm chip break; through holes break through 0.5 mm with the drill tip past the bottom; blind holes are drilled with the tip through the bottom too, so they are full diameter to it, and the point goes its length deeper. `build` warns about each blind hole with no modelled point, or one whose modelled point angle differs from the drill's, and asks for the point to be modelled; a hole whose modelled point matches the drill (within 0.5°) raises no warning. A blind bore's pre-drill stops at the bore's bottom, except over a narrower hole below it (a counterbore): there the point goes on down until the drill's cone meets the floor at the lower hole's edge (0.51 mm below the floor over a 2.2 mm hole), or the bottom of a blind lower hole if that comes first, and the tip goes through when the lower hole is as wide as the drill. The pre-drill is then as wide as it can be without marking the floor (user, 2026-10-17) |
| Bore | climb, tolerance 0.01 mm, the rough bore at stepover 0.3 mm with enough passes to reach the wall, the finishing bore one pass at the wall (multiple passes off); through holes go 0.5 mm past the bottom; in a blind hole the rough bore stops 0.2 mm above the floor (a bore has no axial stock to leave, so its bottom height is raised) and the finishing bore takes it off (user, 2026-10-17); a rough bore of a hole that opens in a pocket or counterbore floor starts 0.2 mm above the hole top, on the floor the rough cut left, so that its entries never plunge through that allowance |
| Thread | per thread size, from [threads.toml](threads.toml); the top offset there is from the finished face, and 0.2 mm is added while the face still carries its finishing allowance. The thread mill never goes deeper than the hole it threads. A through hole is drilled 0.5 mm past its bottom, and the thread mill goes 0.3 mm past it. A blind hole ends at its bottom, so there the thread mill stops at the bottom. It enters along a flat lead, because a helical lead starts below the bottom height |
| 2D chamfer | 0.1 mm chamfer width, 0.1 mm clearance, lead-in and lead-out; the tip offset is 0.5 mm, less where the bit's point would come within 0.1 mm of the shallowest pocket floor or blind hole bottom, or of the tabs, which still stand in the outline cut (never below 0.1 mm). Fusion leaves out every edge where the point would go below a floor, so with a 0.5 mm offset the bit skips a pocket 0.6 mm deep or less. `build` warns when even a 0.1 mm offset brings the point to within 0.02 mm of a floor or the tabs |

## Tapped holes

Lab parts model a tapped hole with Fusion's Hole command, Tapped type, threads not modeled: a plain hole at the minor diameter that carries the thread data.
The lab pre-drills it slightly oversize and thread-mills it; M4 has no matching drill, so it is drilled at 3.175 mm and bored to size with the end mill first.

[threads.toml](threads.toml) holds the recipe for M2, M2.5, M3 and M4: the modelled hole, the drill, the thread mill, the pitch diameter offset, the stepover and the depths, with the parts each value came from.
The generator reads it on every build, so a change there changes the next build.

## NC programs

Each part gets three programs, all posted with the Carvera Community post:

| Program | Contents |
|---|---|
| `<part>_top` | the top setup |
| `<part>_bottom` | the bottom setup except the tab cut |
| `<part>_tabs` | the tab cut only |

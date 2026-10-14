# SchusterLab Carvera CAM conventions

These are the conventions `carvera_cam.py` applies.
They come from the lab's own Fusion setups: about 40 setups and 530 operations in the project `2026-01-06_smpm-sample-package-6port`, with `sample-puck-v2-6port10x10` as the reference part.

## Machine, post and material

| Item | Value | Where the generator gets it |
|---|---|---|
| Machine | Makera Carvera 3-axis Community Version, latest | [Carvera Community profiles](https://github.com/Carvera-Community/Carvera_Community_Profiles/tree/main/CAM_Post_Processors/Fusion360-profiles), `main` branch |
| Post processor | Makera Carvera Community Post (`Carvera.cps`), latest | the same folder |
| Tools | the lab's 29 tools, 243 more McMaster-Carr drills and 23 of Makera's tools for metal | [`assets/carvera-tools.json`](../assets/carvera-tools.json): the lab's exported from the Fusion library `Carvera Tools/My Tools`, the drills from McMaster-Carr's table of [expanded-shank carbide drill bits](https://www.mcmaster.com/products/drill-bits/expanded-shank-carbide-drill-bits~~/) (copied to [`assets/mcmaster-expanded-shank-carbide-drills.csv`](../assets/mcmaster-expanded-shank-carbide-drills.csv)), Makera's from the [community tool files](https://github.com/Carvera-Community/Carvera_Community_Profiles/tree/main/CAM_Post_Processors/Fusion360-profiles/Tool%20Files) |

`build` downloads the machine and post again whenever they change upstream, and keeps them in `%LOCALAPPDATA%\carvera-cam\community`; without network it uses the copies there.
The community machine's 3D model links to a file in its author's Fusion account, which no one else can open, so `build` uploads the machine's `.f3d` once per upstream version to a Fusion project named `carvera-cam` in the user's hub and links the machine to that copy.
If the upload fails, the setups get the machine without its 3D model and `build` warns that simulation does not check the machine frame.

The lab's own setups before October 2026 used the machine `...Community Version 1.17-slab` and post v1.1.21 from the lab's Cloud Library.
That machine's settings differ from upstream 1.4.6 only in its tool change time (30 s against 45 s), which changes time estimates, not toolpaths.
Its 3D model differs in the bed, where the fixture plate sits: the wasteboard and its anchors are gone, the bed is 18 mm lower (z = −21.175 mm) for x < 342 mm, and a construction point `Point1` at (−15, −15, −21.175) mm marks the corner of that recess.
Before uploading the upstream model, `build` makes the same three changes to it (`_slab_model` in `carvera_cam.py`).

Every tool in `My Tools` has a feed preset named **Copper 101-H02** (C10100 oxygen-free copper, H02 half-hard temper), and the lab uses it for everything.
Aluminum (6063-T5) runs the same programs at 200% feed and spindle override on the Carvera, so there is one set of toolpaths per part.
No coolant.
After changing `My Tools` in Fusion, run `export_tools({})` from `carvera_cam.py` to rewrite the lab's tools in `assets/carvera-tools.json`, and commit it.

The library also holds tools the lab lacks: drills from McMaster-Carr, and tools from Makera.

Most of the lab's drills are McMaster-Carr's expanded-shank carbide drills, which have a 1/8″ (3.175 mm) shank for the Carvera's collet whatever their diameter.
The library adds the other 243 drills of that series, from 0.152 mm (0.006″) to 3.175 mm, in metric, fractional-inch and wire-gauge sizes.
A diameter can come coated with TiN (part numbers 6415N…) or TiAlN (2598N…) or uncoated (2841A…), and the coatings differ in their maximum drilling depth, so each part number is a drill of its own, with that depth as its flute length and its coating at the end of its name, for example `1.613*9.1mm Drill (No. 52) TiAlN`.
The TiN and uncoated drills have a 130° point and the TiAlN drills 135°, whether the catalogue calls the point four-facet or standard (checked on the product pages of eleven drills across the three series).

The 23 tools from Makera are its tools for metal: Spiral O end mills of 4 and 6 mm, ball end mills of 1 to 6 mm, engraving bits, and thread mills for M1, M5, M6 and M8.
Makera's drills are left out, because the McMaster-Carr drills cover their diameters.
Makera's own copper presets spin its end mills at 12000 rpm and 300 mm/min against the lab's 6000 rpm and 200 mm/min.
So each added tool, from either source, gets a Copper 101-H02 preset made by the lab's rule for its kind of tool, and the preset's description says so:

| Kind | Copper 101-H02, as in the lab's own presets |
|---|---|
| end mills, flat or ball | 6000 rpm, 200 mm/min, plunge 200 mm/min; stepdown 0.3 mm and ramp 300 mm/min from 3.175 mm diameter up, 0.2 mm and 200 mm/min below |
| drills | 10000 rpm, plunge 50 mm/min |
| thread mills | 15000 rpm, and Makera's copper feed: M1 50 mm/min, M5 to M8 200 mm/min |
| engraving bits | as the chamfering bit: 6000 rpm, 200 mm/min, 0.1 mm steps |

The lab's smallest drill is 0.7 mm, so for smaller drills the drill preset's feed of 0.005 mm per revolution has no evidence behind it.

The lab has not cut with any added tool, so `build` warns when it uses one, and the toolpath check flags them (N6 in [rules.md](rules.md)).
For a hole, `build` takes a lab drill whenever one fits (within −0.02 to +0.05 mm of the hole's diameter, and a flute at least 0.5 mm longer than the hole's depth); otherwise it takes the added drill closest in diameter, and of those the TiN drill before the uncoated one before the TiAlN one.
`scripts/tool_library.py` makes these additions; run it with plain Python (`python -I tool_library.py`) to fetch the community tool files again and rebuild the drills from the CSV file, which holds McMaster-Carr's table with the point angle and coating of each part.
It leaves out Makera's tools for wood, plastic and circuit boards.

## Tool slots

The Carvera's automatic tool changer holds six tools.
T1, T2 and T3 are always the same three tools; every other tool takes the next slot in the order the programs first use it, the top program's operations first, then the bottom's.

| Slot | Tool | Copper 101-H02 preset |
|---|---|---|
| T1 | 3.175 × 7.4 mm drill (McMaster 6415N117) | 10000 rpm, plunge 50 mm/min |
| T2 | Spiral O Metal 3.175 × 12 mm, one flute: roughing | 6000 rpm, 200 mm/min, ramp 300 mm/min, stepdown 0.3 mm |
| T3 | the same end mill, a second one: finishing | as T2 |
| T4 on, by first use | small end mills: Spiral O Metal 2.5, 2, 1.5 or 1 mm | 6000 rpm, 200 mm/min, stepdown 0.2 mm |
| | other drills: tap drills and plain holes (0.7–4 mm) | 10000 rpm, plunge 50 mm/min |
| | Carvera thread mills, M2 to M4 | 15000 rpm; M2 100 mm/min, M2.5 150 mm/min, M3 and M4 200 mm/min |
| | chamfering bit, 1/8″ shank | 6000 rpm, 200 mm/min |

With the lab's operation order (see below), that usually puts the small end mills first, then the drills, the thread mills and the chamfering bit.

T2 roughs every feature in steps; T3, a fresh tool of the same kind, then runs each roughing operation again at full depth in one pass, which cleans the walls.

## Setups

Both setups' X axis runs along the part's Y axis: the vise's jaws close along the Carvera's X axis and grip the stock's Y edges (see "Fixture"), so the part lies on the machine turned 90° from its design.
The top setup's X is the model's +Y; the bottom setup's is −Y.

**Top setup.**
The stock is a fixed box: commonly a 2″ × 150 mm bar 6.35 mm (0.25″) or 19 mm thick, or a 150 × 150 mm copper plate 6.2 mm thick.
The part sits centred in X, 10 mm from the back of the stock in Y, and centred in the stock's thickness unless the user moves it.
The lab's parts sit at various heights in the stock; the height decides how much facing each program does, and so when the operator must come back to flip the stock.
The work coordinate system (WCS) origin is the stock box's `top 2` corner: the corner at the model's +X and +Y, which is the setup's +X and −Y.
The box's corners and sides follow the setup's axes, so the box is set as `stock y` long along the setup's X.

**Bottom setup.**
The part is flipped about the model's X axis: Z and X flipped (`flipZ`, `flipX`), stock from the previous setup, WCS origin at the stock box's `top 2` corner.

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

1. T1 drills the main entry hole (see "Entry holes"), through the stock.
2. T2 faces the stock top down to 0.2 mm above the part's top face and cuts the outline down to the tab band, both entering through the main entry hole.
3. T1 drills the other entry holes and pre-drills the holes the end mill will bore, each from where the end mill has left the stock: the corner-chain entries from the top of the tab band, the pocket entries and pre-drills from 0.2 mm above the face.
4. T2 roughs the pockets, bores, through windows, and the outline between the tabs down through the band.
5. T3 finishes the features of steps 2 and 4, except the face, in single full-depth passes.
6. Small end mills clear pocket corners the 3.175 mm end mill cannot reach (rest machining, plunge entry).
7. Drills cut plain holes and tap drills.
8. Thread mills cut the threads.
9. T3 finishes the face, taking off the last 0.2 mm in one pass.
10. The chamfering bit deburrs the edges of the top face.

The bottom setup repeats steps 2–10 for the bottom-side features, with no main entry hole of its own (the top setup's goes through the stock) and no corner chains; its outline stops at the bottom of the tab band.

A drill only cuts what the end mill cannot cut first: the main entry hole is drilled from the stock top because the first end mill cut enters through it, and every other hole waits until the end mill has cleared the stock above it.
This saves wear on the drills, at the cost of two more tool changes in the top setup and one in the bottom.
The lab's parts from October 2026 on (`kiutra-beam`, `tee`) drill this way.

The face is finished last because facing raises fewer burrs than side milling and thread milling, so the last cut across the face removes the burrs the earlier cuts left on it.
Until then the face still carries 0.2 mm of material, and every operation that starts at the face starts 0.2 mm higher, on that material:
- pockets, bores, windows and pocket corners that open in the face start 0.2 mm above it;
- drills, bores and thread mills that start at the hole top start 0.2 mm above it.

The lab's parts from October 2026 on (`kiutra-beam`, `tee`) work this way; older parts finish the face with the other T3 passes.

## Entry holes

The end mill never ramps or plunges into solid metal.
It enters every cut through a hole the T1 drill (3.175 mm, the end mill's diameter) makes first, at points kept in a sketch:

- **The main entry**, just outside the outline at the edge of a tab, one tool radius out, so the end mill's centre sits on the outline's toolpath. T1 drills it through the whole stock with 0.5 mm breakthrough. Both facing pockets and both outline contours enter there.
- **One entry per corner chain** (the outline between two tabs; see "Holding tabs"), at the same offset from the start of the chain, drilled through the tab band from its top, which the outline cut has already reached there.
- **One entry per pocket**, at the point of the pocket farthest from its walls, drilled from 0.2 mm above the face to the pocket floor. A shelf, a floor that steps down into a deeper pocket inside it, is pocketed with that deeper pocket as one region (the opening just above the shelf), and its entry is the deeper pocket's, drilled to the deeper floor.
- **A pocket open to the side** gets no hole: its entry sits just outside the opening, where the outline cut has already cleared the stock down to the pocket's floor, and the end mill enters sideways.

The pockets use the `predrill` ramp with these points as their predrill positions, and the outline contours use the main entry as their entry position.

## Holding tabs

The part stays attached to the stock by four bridges ("tabs") until a separate program cuts them.
They sit on the longest straight or arc edges of the outline, one at the middle of each, 5 mm wide along the outline.
The tab band is 1 mm thick (0.4 × the part's thickness for parts under 2.5 mm) and is centred on the plane halfway between the part's top and bottom.
A pocket open to the side of the part is reached sideways at its own depth, so the band moves to the nearest height clear of every such opening.

- The top setup's outline cuts from the stock top down to the top of the band.
- The **outline between tabs** (four corner chains, each running from one tab to the next) then cuts from the top of the band to 0.5 mm below its bottom.
- The bottom setup's outline cuts from the bottom face up to the bottom of the band.
- `<part>_tabs`, the last program, cuts each tab span through the band and 0.5 mm past it, in 0.2 mm steps, and frees the part.

Each tab span is 5 mm along the outline, so the bridge left after the end mill's radius on each side is about 1.8 mm wide.
The corner chains and tab spans are curves in the `carvera-cam` sketch; the generator orients each chain so a left-compensated tool stays outside the part in both setups.

## Operation parameters

Feeds, speeds and stepdowns come from the tool preset; the operations set only the following.

| Operation | Parameters |
|---|---|
| 2D pocket | tolerance 0.1 mm, stepover 0.6 × tool diameter, compensation left, no stock to leave, `predrill` ramp at the entry holes; floors selected as pocket faces, shelves as the closed opening above them (one counter-clockwise chain per opening); the facing pocket starts at the stock top, and its roughing pass leaves 0.2 mm on the face (axial stock to leave 0.2 mm, radial 0) |
| 2D contour | tolerance 0.01 mm, compensation left, two finishing stepovers, no ramp, no stock to leave |
| Drill | chip-breaking cycle, 2 mm pecks, 0.1 mm chip break; through holes break through 0.5 mm with the drill tip past the bottom |
| Bore | climb, tolerance 0.01 mm, stepover 0.3 mm with enough passes to reach the wall; through holes go 0.5 mm past the bottom |
| Thread | per thread size, from [threads.toml](threads.toml); the top offset there is from the finished face, and 0.2 mm is added while the face still carries its finishing allowance |
| 2D chamfer | 0.1 mm chamfer width, 0.5 mm tip offset, 0.1 mm clearance, lead-in and lead-out |

## Tapped holes

Lab parts model a tapped hole with Fusion's Hole command, Tapped type, threads not modeled: a plain hole at the minor diameter that carries the thread data.
The lab pre-drills it slightly oversize and thread-mills it; M4 has no matching drill, so it is drilled at 3.175 mm and bored to size with the end mill first.

[threads.toml](threads.toml) holds the recipe for M2, M2.5, M3 and M4: the modelled hole, the drill, the thread mill, the pitch diameter offset, the passes and the depths, with the parts each value came from.
The generator reads it on every build, so a change there changes the next build.

## NC programs

Each part gets three programs, all posted with the Carvera Community post:

| Program | Contents |
|---|---|
| `<part>_top` | the top setup |
| `<part>_bottom` | the bottom setup except the tab cut |
| `<part>_tabs` | the tab cut only |

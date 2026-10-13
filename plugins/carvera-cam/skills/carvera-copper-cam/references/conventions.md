# SchusterLab Carvera CAM conventions

These are the conventions `carvera_cam.py` applies.
They come from the lab's own Fusion setups: about 40 setups and 530 operations in the project `2026-01-06_smpm-sample-package-6port`, with `sample-puck-v2-6port10x10` as the reference part.

## Machine, post and material

| Item | Value | Where the generator gets it |
|---|---|---|
| Machine | Makera Carvera 3-axis Community Version, latest | [Carvera Community profiles](https://github.com/Carvera-Community/Carvera_Community_Profiles/tree/main/CAM_Post_Processors/Fusion360-profiles), `main` branch |
| Post processor | Makera Carvera Community Post (`Carvera.cps`), latest | the same folder |
| Tools | the lab's tool library | [`assets/carvera-tools.json`](../assets/carvera-tools.json), exported from the Fusion library `Carvera Tools/My Tools` |

`build` downloads the machine and post again whenever they change upstream, and keeps them in `%LOCALAPPDATA%\carvera-cam\community`; without network it uses the copies there.
The community machine's 3D model links to a file in its author's Fusion account, which no one else can open, so `build` uploads the machine's `.f3d` once per upstream version to a Fusion project named `carvera-cam` in the user's hub and links the machine to that copy.
If the upload fails, the setups get the machine without its 3D model and `build` warns that simulation does not check the machine frame.

The lab's own setups before October 2026 used the machine `...Community Version 1.17-slab` and post v1.1.21 from the lab's Cloud Library.
That machine differs from upstream 1.4.6 only in its tool change time (30 s against 45 s), which changes time estimates, not toolpaths.

Every tool in `My Tools` has a feed preset named **Copper 101-H02** (C10100 oxygen-free copper, H02 half-hard temper), and the lab uses it for everything.
Aluminum (6063-T5) runs the same programs at 200% feed and spindle override on the Carvera, so there is one set of toolpaths per part.
No coolant.
The Cloud Library folder `Carvera Tools` also holds Makera's stock libraries (O Flute, Drill, Thread Mills, Engraving, Other); their faster presets are not the lab's.
After changing `My Tools` in Fusion, run `export_tools({})` from `carvera_cam.py` to rewrite `assets/carvera-tools.json`, and commit it.

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

**Top setup.**
The stock is a fixed box: commonly a 2″ × 150 mm bar 6.35 mm (0.25″) or 19 mm thick, or a 150 × 150 mm copper plate 6.2 mm thick.
The part sits centred in X, 10 mm from the back of the stock in Y, and centred in the stock's thickness unless the user moves it.
The lab's parts sit at various heights in the stock; the height decides how much facing each program does, and so when the operator must come back to flip the stock.
The work coordinate system (WCS) origin is the stock box's `top 4` corner, with the model's orientation.

**Bottom setup.**
The part is flipped about the X axis: Z axis flipped (`axesZX`, `flipZ`), stock from the previous setup, WCS origin at the stock box's `top 2` corner.

## Fixture

Each setup carries a model of the vise, so Fusion's simulation catches collisions with the vise and with the Carvera itself.
The lab's pattern is `2026-01-20_sample-puck-6port7x7` (project `2026-01-06_smpm-sample-package-6port`, folder `old-versions`).

- The lab's fixture design is `2026-01-18_slab-fixture-3-axis` (project `2026-01-18_slab-fixture`): Saunders Machine Works' [Carvera fixture plate](https://saundersmachineworks.com/products/makera-carvera-fixture-tooling-plate) (inch), a [Gen3 hobby mod vise](https://saundersmachineworks.com/products/modular-vise-system-hobby-gen3) (1/4″) with two [smooth insert bars](https://saundersmachineworks.com/products/smooth-jaw-insert-for-modular-vise), and the Carvera's 4th-axis module.
- The generator downloads those three Saunders STEP files and places them as that design does (`SMW_PLACEMENTS` in `carvera_cam.py`), so it needs no access to the lab's design. It leaves out the 4th-axis module, which Saunders does not publish, so simulation does not check it. It caches the files and the assembled fixture in `%LOCALAPPDATA%\carvera-cam\fixture`, and stops with an error if a changed Saunders model no longer puts the jaw faces where the lab's design has them.
- The jaws grip the stock's Y edges, 4.3 mm deep; the fixed jaw is on the back edge, the one the part is 10 mm from, and the adjustable jaw opens to the stock's Y size.
- The bottom setup has its own copy of the fixture, turned 180° about X with the part, gripping the stock left after the top program.
- Both setups list their copy as the setup's fixture and attach it to the machine table at the plate's corner (Part Position, Table Attach Point), so the machine's own geometry is checked too.

The lab built this with a Manufacturing Model holding the stock, the part and two embedded copies of the fixture.
The generator works in the part's design instead, and adds the fixture's bodies to it as fixed geometry: an inserted copy of the fixture assembly comes in through the API with its jointed parts unsolved and scattered.

## Operation order

Top setup:

1. T1 drills the entry holes (see "Entry holes") and pre-drills the holes the end mill will bore.
2. T2 roughs: facing the stock top down to 0.2 mm above the part's top face, the outline down to the tab band, pockets, bores, through windows, and the outline between the tabs down through the band.
3. T3 finishes the same features, except the face, in single full-depth passes.
4. Small end mills clear pocket corners the 3.175 mm end mill cannot reach (rest machining, plunge entry).
5. Drills cut plain holes and tap drills.
6. Thread mills cut the threads.
7. T3 finishes the face, taking off the last 0.2 mm in one pass.
8. The chamfering bit deburrs the edges of the top face.

The bottom setup drills the entry holes of its own pockets, then repeats steps 2–8 for the bottom-side features; its outline stops at the bottom of the tab band.

The face is finished last because facing raises fewer burrs than side milling and thread milling, so the last cut across the face removes the burrs the earlier cuts left on it.
Until then the face still carries 0.2 mm of material, and every operation that starts at the face starts 0.2 mm higher, on that material:
- pockets, bores, windows and pocket corners that open in the face start 0.2 mm above it;
- drills, bores and thread mills that start at the hole top start 0.2 mm above it.

The lab's parts from October 2026 on (`kiutra-beam`, `tee`) work this way; older parts finish the face with the other T3 passes.

## Entry holes

The end mill never ramps or plunges into solid metal.
It enters every cut through a hole the T1 drill (3.175 mm, the end mill's diameter) makes first, at points kept in a sketch:

- **The main entry**, just outside the outline at the edge of a tab, one tool radius out, so the end mill's centre sits on the outline's toolpath. T1 drills it through the whole stock with 0.5 mm breakthrough. Both facing pockets and both outline contours enter there.
- **One entry per corner chain** (the outline between two tabs; see "Holding tabs"), at the same offset from the start of the chain, drilled from the top through the tab band.
- **One entry per pocket**, at the point of the pocket farthest from its walls, drilled to the pocket floor. A shelf, a floor that steps down into a deeper pocket inside it, is pocketed with that deeper pocket as one region (the opening just above the shelf), and its entry is the deeper pocket's, drilled to the deeper floor.
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

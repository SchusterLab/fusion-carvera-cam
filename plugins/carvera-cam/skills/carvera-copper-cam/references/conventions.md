# SchusterLab Carvera CAM conventions

These are the conventions `carvera_cam.py` applies.
They come from the lab's own Fusion setups: about 40 setups and 530 operations in the project `2026-01-06_smpm-sample-package-6port`, with `sample-puck-v2-6port10x10` as the reference part.

## Machine, post and material

| Item | Value | Fusion Cloud Library location |
|---|---|---|
| Machine | Makera Carvera 3-axis Community Version 1.17-slab | `cloud://Makera Carvera 3-axis Community Version 1.mch` |
| Post processor | Makera Carvera Community Post v1.1.21 | `cloud://Carvera.cps` |
| Tools | the lab's tool library | `cloud://Carvera Tools/My Tools` |

Every tool in `My Tools` has a feed preset named **Copper 101-H02** (C10100 oxygen-free copper, H02 half-hard temper), and the lab uses it for everything.
Aluminum (6063-T5) runs the same programs at 200% feed and spindle override on the Carvera, so there is one set of toolpaths per part.
No coolant.
The same Cloud Library folder also holds Makera's stock libraries (O Flute, Drill, Thread Mills, Engraving, Other); their faster presets are not the lab's.

## Tool slots

The Carvera's automatic tool changer holds six tools.
The lab fills the slots in this order:

| Slot | Tool | Copper 101-H02 preset |
|---|---|---|
| T1 | 3.175 × 7.4 mm drill (McMaster 6415N117) | 10000 rpm, plunge 50 mm/min |
| T2 | Spiral O Metal 3.175 × 12 mm, one flute: roughing | 6000 rpm, 200 mm/min, ramp 300 mm/min, stepdown 0.3 mm |
| T3 | the same end mill, a second one: finishing | as T2 |
| next | small end mills: Spiral O Metal 2.5, 2, 1.5 or 1 mm | 6000 rpm, 200 mm/min, stepdown 0.2 mm |
| next | other drills: tap drills and plain holes (0.7–4 mm) | 10000 rpm, plunge 50 mm/min |
| next | Carvera thread mills, M2 to M4 | 15000 rpm; M2 100 mm/min, M2.5 150 mm/min, M3 and M4 200 mm/min |
| last | chamfering bit, 1/8″ shank | 6000 rpm, 200 mm/min |

T2 roughs every feature in steps; T3, a fresh tool of the same kind, then runs each roughing operation again at full depth in one pass, which cleans the walls.

## Setups

**Top setup.**
The stock is a fixed box: commonly a 2″ × 150 mm bar 6.35 mm (0.25″) or 19 mm thick, or a 150 × 150 mm copper plate 6.2 mm thick.
The part sits centred in X, 10 mm from the back of the stock in Y, and centred in the stock's thickness.
The work coordinate system (WCS) origin is the stock box's `top 4` corner, with the model's orientation.

**Bottom setup.**
The part is flipped about the X axis: Z axis flipped (`axesZX`, `flipZ`), stock from the previous setup, WCS origin at the stock box's `top 2` corner.

## Operation order

Top setup:

1. T1 pre-drills the holes the end mill will bore.
2. T2 roughs: facing the stock top down to the part's top face, pockets, bores, through windows, and the outline down past the part's mid-thickness, with tabs.
3. T3 finishes the same features in single full-depth passes.
4. Small end mills clear pocket corners the 3.175 mm end mill cannot reach (rest machining, plunge entry).
5. Drills cut plain holes and tap drills.
6. Thread mills cut the threads.
7. The chamfering bit deburrs the edges of the top face.

The bottom setup repeats steps 2–7 for the bottom-side features; its outline stops at the bottom of the tab band.

## Holding tabs

The part stays attached to the stock by four bridges until a separate program cuts them:

- The top setup's outline cuts from the stock top to 0.5 mm below the bottom of a 1 mm-thick band at the part's mid-thickness, leaving rectangular tabs 5 mm wide and 1.5 mm tall at its bottom.
- The bottom setup's outline cuts from the bottom up to the band, leaving only the 1 mm band at the tabs.
- `<part>_tabs`, the last program, runs the outline through the band in 0.2 mm steps and frees the part.

For a part thinner than 3 mm the band is a third of the thickness.

## Operation parameters

Feeds, speeds and stepdowns come from the tool preset; the operations set only the following.

| Operation | Parameters |
|---|---|
| 2D pocket | tolerance 0.1 mm, stepover 0.6 × tool diameter, compensation left, no stock to leave; floors selected as pocket faces; the facing pocket starts at the stock top |
| 2D contour | tolerance 0.01 mm, compensation left, two finishing stepovers, no ramp, no stock to leave |
| Drill | chip-breaking cycle, 2 mm pecks; through holes break through 0.5 mm with the drill tip past the bottom |
| Bore | climb, tolerance 0.01 mm, stepover 0.3 mm with enough passes to reach the wall; through holes go 0.5 mm past the bottom |
| Thread | climb, the tool's stepover, up to 3 passes (fewer when Fusion would discard passes), 0.3 mm past the bottom of a through hole |
| 2D chamfer | 0.1 mm chamfer width, 0.5 mm tip offset, 0.1 mm clearance, lead-in and lead-out |

## Tapped holes

Lab parts model a tapped hole with Fusion's Hole command, Tapped type, threads not modeled: a plain hole at the minor diameter that carries the thread data.
The lab drills it slightly oversize for the thread mill:

| Thread | Modelled hole (mm) | Drill | Thread mill | Pitch (mm) |
|---|---|---|---|---|
| M2 | 1.6 | 1.7 × 9.6 mm | Carvera M2 | 0.4 |
| M2.5 | 2.05 | 2.2 × 8.8 mm | Carvera M2.5 | 0.45 |
| M3 | 2.5 | 2.6 × 8.2 mm | Carvera M3 | 0.5 |
| M4 | 3.3 | 3.175 drill, then bored | Carvera M4 | 0.7 |

## NC programs

Each part gets three programs, all posted with the Carvera Community post:

| Program | Contents |
|---|---|
| `<part>_top` | the top setup |
| `<part>_bottom` | the bottom setup except the tab cut |
| `<part>_tabs` | the tab cut only |

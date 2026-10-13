# SchusterLab Fusion tools

This repository is a Claude Code plugin marketplace with one plugin, `carvera-cam`.
Its skill, `carvera-copper-cam`, generates Autodesk Fusion CAM toolpaths for machining copper parts on the lab's Makera Carvera.
Given a part, it creates the top and flipped bottom setups with the lab's vise modelled for collision checks, every operation with the lab's tools and feeds, and three NC programs: `<part>_top`, `<part>_bottom` and `<part>_tabs`.
The same programs machine aluminum at 200% feed and spindle override.

## Install

In Claude Code:

```
/plugin marketplace add SchusterLab/fusion-carvera-cam
/plugin install carvera-cam@schusterlab-fusion
```

The skill drives Fusion through the Fusion MCP server, so Claude Code needs that server connected, and Fusion must be running and signed in.

- **Tools** come with the skill, in `assets/carvera-tools.json`, each with the lab's `Copper 101-H02` feed preset.
- **The machine and post processor** are the latest Carvera 3-axis machine and `Carvera.cps` in the [Carvera Community profiles](https://github.com/Carvera-Community/Carvera_Community_Profiles/tree/main/CAM_Post_Processors/Fusion360-profiles), which the skill downloads. The first build uploads the machine's 3D model to a Fusion project named `carvera-cam` in your hub, so that simulation shows the machine.
- **The fixture plate and vise** are Saunders Machine Works' published models of the Carvera fixture plate, the Gen3 hobby mod vise and its smooth insert bars, which the skill downloads from saundersmachineworks.com and assembles as the lab mounts them.

## Use

Ask Claude Code, for example, "Generate Carvera toolpaths for `my-part` in copper, from a 2-inch bar 0.25 inch thick".
The skill opens the part, reports the features it recognized, confirms the stock and any tapped holes with you, builds the setups and generates the toolpaths.
It does not save the document or write G-code unless you ask.

## Contents

| Path | What it is |
|---|---|
| `plugins/carvera-cam/skills/carvera-copper-cam/SKILL.md` | the skill's instructions |
| `.../references/conventions.md` | the lab's CAM conventions the generator applies |
| `.../references/threads.toml` | how the lab pre-drills and thread-mills M2 to M4 tapped holes |
| `.../assets/carvera-tools.json` | the lab's tool library, exported from Fusion |
| `.../scripts/carvera_cam.py` | the generator, run inside Fusion's Python: `analyze`, `build`, `generate`, `export_tools` |
| `.../scripts/dump_cam.py` | dumps a project's existing CAM setups to JSON, for re-learning the conventions |
| `.../scripts/make_test_part.py` | builds a test part with one of each feature, for testing changes |

## Limits

The generator handles 2.5-axis parts: pockets, holes, tapped holes, through windows and outlines, all reached along Z from the top or the bottom.
Pocket corners finish at the radius of the smallest end mill that fits.
`SKILL.md` lists the remaining limits.

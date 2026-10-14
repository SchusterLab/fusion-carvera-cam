# SchusterLab Fusion tools

This repository is a Claude Code plugin marketplace with one plugin, `carvera-cam`.
Its skill, `carvera-copper-cam`, generates Autodesk Fusion CAM toolpaths for machining copper parts on the lab's Makera Carvera.
Given a part, it creates the top and flipped bottom setups with the lab's vise modelled for collision checks, every operation with the lab's tools and feeds, and three NC programs: `<part>_top`, `<part>_bottom` and `<part>_tabs`.
The same programs machine aluminum at 200% feed and spindle override.
The skill also checks existing Carvera toolpaths, the lab's own as well as those it generates, against rules mined from the lab's toolpaths for avoiding crashes, spindle stalls and tool wear.

## Install

In Claude Code:

```
/plugin marketplace add SchusterLab/fusion-carvera-cam
/plugin install carvera-cam@schusterlab-fusion
```

The skill drives Fusion through the Fusion MCP server, so Claude Code needs that server connected, and Fusion must be running and signed in.

- **Tools** come with the skill, in `assets/carvera-tools.json`: the lab's 14 tools other than drills; all 122 uncoated drills from McMaster-Carr's [expanded-shank carbide drill bits](https://www.mcmaster.com/products/drill-bits/expanded-shank-carbide-drill-bits~~/), the series the lab's drills came from, as the library's only drills; and 9 of Makera's end mills and thread mills for metal, from the [Carvera Community tool files](https://github.com/Carvera-Community/Carvera_Community_Profiles/tree/main/CAM_Post_Processors/Fusion360-profiles/Tool%20Files). Each has a `Copper 101-H02` feed preset; the drills have the lab's drill preset, and Makera's tools one derived from the lab's presets.
- **The machine and post processor** are the latest Carvera 3-axis machine and `Carvera.cps` in the [Carvera Community profiles](https://github.com/Carvera-Community/Carvera_Community_Profiles/tree/main/CAM_Post_Processors/Fusion360-profiles), which the skill downloads. The first build uploads the machine's 3D model to a Fusion project named `carvera-cam` in your hub, so that simulation shows the machine.
- **The fixture plate and vise** are Saunders Machine Works' published models of the Carvera fixture plate, the Gen3 hobby mod vise and its smooth insert bars, which the skill downloads from saundersmachineworks.com and assembles as the lab mounts them.

## Use

Ask Claude Code, for example, "Generate Carvera toolpaths for `my-part` in copper, from a 2-inch bar 0.25 inch thick".
The skill opens the part, reports the features it recognized, confirms the stock and any tapped holes with you, builds the setups and generates the toolpaths.
It does not save the document or write G-code unless you ask.

To check toolpaths you already have, open the part and ask, for example, "Check the Carvera toolpaths in `my-part`".
The skill lists every finding with its rule, from out-of-date toolpaths and cuts below the stock to feeds above the lab's, and `references/rules.md` explains each rule.

## Contents

| Path | What it is |
|---|---|
| `plugins/carvera-cam/skills/carvera-copper-cam/SKILL.md` | the skill's instructions |
| `.../references/conventions.md` | the lab's CAM conventions the generator applies |
| `.../references/rules.md` | the rules the toolpath check applies, with their thresholds and evidence |
| `.../references/threads.toml` | how the lab pre-drills and thread-mills M2 to M4 tapped holes |
| `.../assets/carvera-tools.json` | the tool library: the lab's tools exported from Fusion, McMaster-Carr drills, and Makera's tools for metal |
| `.../assets/mcmaster-expanded-shank-carbide-drills.csv` | McMaster-Carr's table of expanded-shank carbide drills, which the added drills are made from |
| `.../scripts/carvera_cam.py` | the generator, run inside Fusion's Python: `analyze`, `build`, `generate`, `export_tools` |
| `.../scripts/check_cam.py` | the toolpath check, run inside Fusion's Python: `check` |
| `.../scripts/tool_library.py` | adds the McMaster-Carr drills and Makera's tools for metal to the tool library, with derived presets; run with plain Python |
| `.../scripts/dump_cam.py` | dumps a project's existing CAM setups to JSON, for re-learning the conventions |
| `.../scripts/make_test_part.py` | builds a test part with one of each feature, for testing changes |

## Limits

The generator handles 2.5-axis parts: pockets, holes, tapped holes, through windows and outlines, all reached along Z from the top or the bottom.
Pocket corners finish at the radius of the smallest end mill that fits.
`SKILL.md` lists the remaining limits.

# SchusterLab Fusion tools

This repository is a Claude Code plugin marketplace with one plugin, `carvera-cam`.
Its skill, `carvera-copper-cam`, generates Autodesk Fusion CAM toolpaths for machining copper parts on the lab's Makera Carvera.
Given a part and the stock, it chooses how to machine it: cut out of larger stock, held by four tabs that a last program cuts; held in the vise by its own sides, from stock of its own size, or of its size along one side and longer the other way, with its ends cut free; and from the top alone when nothing opens at the bottom face, else from the top and then flipped. It checks that no cut reaches the vise.
It creates the setups with the lab's vise modelled for collision checks, every operation with tools from Makera and McMaster-Carr and the lab's feeds, and the NC programs: `<part>_top`, `<part>_bottom` with two setups, and `<part>_tabs` with larger stock.
The same programs machine aluminum at 200% feed and spindle override.
The skill also checks existing Carvera toolpaths, the lab's own as well as those it generates, against rules mined from the lab's toolpaths for avoiding crashes, spindle stalls and tool wear.

## Install

In Claude Code:

```
/plugin marketplace add SchusterLab/fusion-carvera-cam
/plugin install carvera-cam@schusterlab-fusion
```

The skill drives Fusion through the Fusion MCP server, so Claude Code needs that server connected, and Fusion must be running and signed in.

- **Tools** come with the skill, in `assets/carvera-tools.json`: only the tools it assigns, to buy from Makera and McMaster-Carr. From Makera: the Spiral O Metal end mills of 1 to 3.175 mm, the M2 to M4 thread mills and the 1/8″ chamfering bit. From McMaster-Carr: all 122 uncoated [expanded-shank carbide drills](https://www.mcmaster.com/products/drill-bits/expanded-shank-carbide-drill-bits~~/), 0.152 to 3.175 mm. Each has a `Copper` feed preset.
- **The machine and post processor** are the latest Carvera 3-axis machine and `Carvera.cps` in the [Carvera Community profiles](https://github.com/Carvera-Community/Carvera_Community_Profiles/tree/main/CAM_Post_Processors/Fusion360-profiles), which the skill downloads. The first build uploads the machine's 3D model to a Fusion project named `carvera-cam` in your hub, so that simulation shows the machine.
- **The fixture plate and vise** are Saunders Machine Works' published models of the Carvera fixture plate, the Gen3 hobby mod vise and its smooth insert bars, which the skill downloads from saundersmachineworks.com and assembles as the lab mounts them.

## Use

Ask Claude Code, for example, "Generate Carvera toolpaths for `my-part` in copper, from a 2-inch bar 0.25 inch thick".
The skill opens the part, reports the features it recognized, asks whether the stock has the part's own size and thickness and, if not, its dimensions, tells you the machining strategy that follows, confirms any tapped holes with you, builds the setups and generates the toolpaths.
Some features, such as pockets open to the side and inside corners tighter than the main end mill, it cuts two ways: by its script, and by following a written recipe step by step in a throwaway copy of the part.
It checks the toolpaths two ways too, with its checker and by reading the G-code, compares the results, and tells you how they compared; where one way falls short, it fixes the skill and offers the fix to this repository as a pull request, once you agree.
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
| `.../references/recipes/` | the written methods for features the generator can also leave to them: pockets open to the side, inside corners |
| `.../references/review.md` | how to judge toolpaths by reading their G-code and screenshots, the written counterpart of the check |
| `.../assets/carvera-tools.json` | the tool library: Makera's end mills, thread mills and chamfering bit, and McMaster-Carr's drills |
| `.../assets/mcmaster-expanded-shank-carbide-drills.csv` | McMaster-Carr's table of expanded-shank carbide drills, which the library's drills are made from |
| `.../scripts/carvera_cam.py` | the generator, run inside Fusion's Python: `analyze`, `build`, `generate`, and the primitives the recipes use |
| `.../scripts/check_cam.py` | the toolpath check, run inside Fusion's Python: `check`, and `summary`, for comparing two builds |
| `.../scripts/tool_library.py` | builds the tool library from Makera's tool files and McMaster-Carr's table, with the presets; run with plain Python |
| `.../scripts/dump_cam.py` | dumps a project's existing CAM setups to JSON, for re-learning the conventions |
| `.../scripts/make_test_part.py` | builds test parts, one with one of each feature and one with pockets open to the side, for testing changes |

## Limits

The generator handles 2.5-axis parts: pockets, holes, tapped holes, through windows and outlines, all reached along Z from the top or the bottom.
Pocket corners finish at the radius of the smallest end mill that fits.
`SKILL.md` lists the remaining limits.

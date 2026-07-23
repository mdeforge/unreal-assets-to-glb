# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

`unreal-assets-to-glb` extracts **static meshes (`.glb`)** and **base-color textures (`.png`)**
from **uncooked** Unreal Engine `.uasset` files, and can serve a browser-based 3D preview of a
`.umap` level. It parses the binary UE package format directly — **no Unreal Engine installation
is required at runtime**.

- Package name: `unreal-assets-to-glb` (PyPI), import package `uasset`
- Console script: `unreal-assets-to-glb` (→ `uasset.cli:main`)
- **The distribution version mirrors the targeted UE version.** This branch (`5.5`) targets
  UE 5.5 and ships as `5.5.0`; the UE 4.27.2 line ships as `4.27.2.0`. `pyproject.toml` reads
  the version dynamically from `uasset.__version__`, so the same `pyproject.toml` works on every
  UE branch. **Bump `uasset/__init__.py::__version__`, not `pyproject.toml`.**
- License: GPL-3.0-or-later

## Running it

```bash
# Extract meshes + textures from ./Input into ./Export
python main.py ./Input

# Extract, then preview a level in the browser on port 3050
python main.py ./Input --preview L_Showcase.umap

# Preview an already-exported project without re-exporting
python main.py ./Input --skip-export --preview L_Showcase.umap

# Meshes only — no separate PNGs (textures are still embedded in the GLBs)
python main.py ./Input --skip-textures

# Only export meshes whose name contains "Pipe" (case-insensitive substring)
python main.py ./Input --filter Pipe
```

Equivalent invocations: `python -m uasset ...` and, once installed, `unreal-assets-to-glb ...`.
`main.py` is a dev-only thin wrapper around `uasset.cli:main` and is deliberately **not** shipped
in the wheel/sdist.

### CLI flags (`uasset/cli.py`)

| Flag | Meaning |
| --- | --- |
| `input_dir` (positional) | Folder containing `.uproject` + `Content/` (default `./Input`) |
| `--preview LEVEL.umap` | Parse the level and serve a Three.js preview on port 3050 |
| `--export-dir DIR` | Output directory (default `./Export`) |
| `--skip-export` | Reuse an existing `Export/` directory |
| `--skip-textures` | Skip PNG file output (textures still cached and embedded into GLBs) |
| `--filter SUBSTRING` | Export only meshes whose name contains the substring |

### Input / output layout

- **Input** must be an *uncooked* project folder: a `.uproject` at the top and a `Content/` tree of
  `.uasset` / `.umap` files. Cooked/packaged builds are not supported.
- **Output** goes to `./Export/`:
  - `Export/Meshes/<Name>.glb`
  - `Export/Textures/<Name>.png`
  - `Export/texture_cache.pkl` — pickle cache of decoded texture pixels, keyed by an md5
    fingerprint of the input textures' names/mtimes/sizes. Delete it to force a re-decode.
- `Input/`, `Export/`, and `UnrealEngine-5.5.0-release/` are gitignored — never commit them.

## Environment

- Python 3.10+
- Deps: `numpy`, `tqdm`, `pyooz` (provides the `ooz` Oodle-decompression module), `pygltflib`, `Pillow`
- A local `venv/` exists in the repo root (gitignored); use `venv/Scripts/python.exe` on Windows.
- `pip install -r requirements.txt` or `pip install -e .`

## Architecture

Everything lives in the `uasset/` package. Parsing pipeline, roughly:

```
.uasset → Package (header/name-map/imports/exports)
        → property tags
        → package trailer → FCompressedBuffer → Oodle (ooz) decompress
        → FMeshDescription / FEditorBulkData source art
        → GLB (pygltflib) / PNG (Pillow)
```

| Module | Responsibility |
| --- | --- |
| `cli.py` | Argument parsing, asset discovery/classification, the export loop, texture cache, preview launch |
| `reader.py` | `BinaryReader` — typed reads over the raw byte stream |
| `package.py` | `.uasset` package header: summary, name map, imports/exports, UE version constants. Note UE5 puts `FileVersionUE5` **before** `FileVersionLicenseeUE4` |
| `properties.py` | Property-tag parsing/skipping; handles both the UE4-style and the newer `PROPERTY_TAG_COMPLETE_TYPE_NAME` format. `skip_properties` for mesh/texture, `read_properties` for umap, `read_property_tag`/`end_property_tag`/`has_serialization_control_byte` for callers that walk tags themselves |
| `mesh.py` | `StaticMesh.from_package`, FMeshDescription parsing, `export_glb` |
| `texture.py` | `Texture2D.from_package` (FEditorBulkData source art, Oodle + optional `TSCF_UEDELTA` delta decode), `export_png` |
| `scene.py` | Asset index (keyed by parsed export object name) and mesh → material → base-color texture resolution: parent-chain walk, `TextureParameterValues` overrides, cross-package BaseColor expression graph, material layers |
| `umap.py` | `.umap` level parsing: actors, transforms, static-mesh references, level instancing |
| `transform.py` | UE `FRotator`/transform math → matrices for the renderer |
| `preview_server.py` | stdlib `http.server` on port 3050; serves `preview.html`, a scene JSON, and files from `Export/Meshes` + `Export/Textures` |
| `preview.html` | Three.js viewer (shipped as package data) |

### Texture resolution, in detail

Meshes carry a `StaticMaterials` array of `(slot_name, material_name)` and polygon groups carry an
`ImportedMaterialSlotName`. `cli.py` maps polygon group → material slot index via `SectionInfoMap`
(falling back to "polygon group index == material import index" when `StaticMaterials` data is
missing), then `scene.py` resolves that material to a base-color texture.

`scene.py` walks the material's parent chain (instance → … → master `Material`, max 8 levels) and
collects two things:

- the `TextureParameterValues` overrides declared at each level — nearest level wins;
- the texture samplers the base colour reads from, found by a breadth-first walk of the expression
  graph (`MaterialEditorOnlyData` → `BaseColor` → `Expression` → …). A sampler is recognised by
  having a `Texture` ObjectProperty, so every `MaterialExpressionTextureSample*` subclass is
  covered. Materials with `bUseMaterialAttributes` leave `BaseColor` unconnected, so
  `MaterialAttributes` is searched too.

Matching the two is what identifies the base colour: the master says *which parameter* feeds
`BaseColor` (e.g. `BC`), the instance says *which texture* that parameter holds. Fallbacks, in
order: the graph's own default texture, a parameter named after a base-colour slot
(`_BASE_COLOR_PARAM_NAMES` — only reachable when the master material lives outside the project,
e.g. `/Engine/`), then any texture parameter at all.

The graph walk (`_MaterialGraph`) crosses package boundaries. At a
`MaterialExpressionMaterialFunctionCall` it opens the named `MaterialFunction` and continues from
whatever that function returns — `MakeMaterialAttributes.BaseColor` if the function has one (layer
functions do), otherwise its `FunctionOutput`/`MaterialLayerOutput` expressions. Engine functions
aren't in the project, so they resolve to nothing and that branch simply stops. Expression inputs
nested inside arrays of structs (`SetMaterialAttributes.Inputs`, `MaterialFunctionCall.FunctionInputs`)
are unpacked rather than skipped.

### Material Layers

A layered master has a `MaterialExpressionMaterialAttributeLayers` node and no layers of its own —
the *instance* supplies them via `StaticParametersRuntime.MaterialLayers.Layers`. Index 0 is the
base layer; the rest are blended over it, so index 0 carries the base colour. A layer slot normally
holds a layer **instance** (`MaterialFunctionMaterialLayerInstance`), which contributes parameter
values but no graph — `Parent` is followed to the layer function, and the values collected on the
way replace that function's defaults.

Because a layered material reuses the same parameter name once per layer, overrides are keyed by
`FMaterialParameterInfo` — `(Name, Association, Index)`, where `Association` is serialized as an
FName (`GlobalParameter` / `LayerParameter` / `BlendParameter`). Keying by bare name silently
returns whichever layer serialized last. A `LayerParameter` lookup never falls back to another
layer's value for the same name; only `GlobalParameter` accepts a name-only match.

Asset lookup goes through `_build_uasset_index`, which keys on the **object name of each `bIsAsset`
export**, not the file name — `MI_SpaceShip_1.uasset` can contain an asset called `MI_SpaceShip`,
and that is the name importing packages use. File names are registered only for names no package
claimed. Where several packages export the same name the lexicographically first path wins, so
runs are reproducible.

## Project rules

From `.roo/rules/rule n1.txt` (originally in Russian) — these are binding:

1. **Never use heuristics based on file name or suffix — parse the `.uasset` file.**
   Asset type, material role, texture channel, etc. must come from parsed package data
   (export class names, property tags), not from `_BC` / `_N` style naming conventions.
2. The Unreal Engine source is expected at `./UnrealEngine-5.5.0-release` (gitignored) and is the
   reference for the binary formats. Consult it when a format detail is unclear rather than guessing.

## Scope — deliberately not supported

Do not "fix" these as if they were bugs; they are out of scope by design:
graph-based/complex material shaders, lights, colliders, PBR texture channels (base color only),
vertex colors, tangents, LODs, shaders, texture baking, Nanite, animation decompression,
skeletons/bones. Only **one UV channel** is exported.

Also note: each branch targets exactly one UE version (`5.5` here, `4.27.2` elsewhere). Format
handling for a different UE version belongs on that version's branch, not behind runtime switches.

## Working conventions

- There is no test suite. Verify changes by running the exporter against a real uncooked project
  in `Input/` and checking the counts printed at the end plus the resulting GLB/PNG files.
- Per-asset failures are caught and reported as `  <name>: ERROR <e>` rather than aborting the run —
  keep that behavior when touching the export loops; one bad asset must not kill a batch.
- Progress output uses `tqdm`; use `tqdm.write(...)` instead of `print(...)` inside those loops.
- When changing texture parsing, remember `Export/texture_cache.pkl` will happily serve stale
  pixels — delete it when validating.

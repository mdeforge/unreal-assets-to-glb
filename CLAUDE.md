# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

`unreal-assets-to-glb` extracts **static meshes (`.glb`)** and **base-color textures (`.png`)**
from **uncooked** Unreal Engine `.uasset` files, can assemble a whole `.umap` level into a single
positioned GLB, and can serve a browser-based 3D preview of that level. It parses the binary UE
package format directly — **no Unreal Engine installation is required at runtime**.

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

# Assemble a whole level into one GLB with every actor already placed
python main.py ./Input --export-level L_Showcase.umap

# Same, reusing an existing Export/ (skips the per-mesh GLBs entirely)
python main.py ./Input --skip-export --export-level L_Showcase.umap

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
| `--export-level LEVEL.umap` | Assemble the level into `Export/Levels/<Level>.glb`, actors positioned |
| `--scale FACTOR` | UE-unit → glTF-unit scale (default `0.01`: UE cm → glTF m). `1.0` keeps centimetres |
| `--export-dir DIR` | Output directory (default `./Export`) |
| `--skip-export` | Reuse an existing `Export/` directory |
| `--skip-textures` | Skip PNG file output (textures still cached and embedded into GLBs) |
| `--filter SUBSTRING` | Export only meshes whose name contains the substring |

### Input / output layout

- **Input** must be an *uncooked* project folder: a `.uproject` at the top and a `Content/` tree of
  `.uasset` / `.umap` files. Cooked/packaged builds are not supported.
- **Output** goes to `./Export/`:
  - `Export/Meshes/<Name>.glb`
  - `Export/Levels/<Level>.glb` — only with `--export-level`
  - `Export/Textures/<Name>.png` — `<Name>_2.png` etc. where two assets share a name
  - `Export/texture_cache.pkl` — pickle cache of decoded texture pixels keyed by package path,
    guarded by an md5 fingerprint of the input textures' paths/mtimes/sizes. Delete it to force a
    re-decode.
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
| `cli.py` | Argument parsing, asset discovery/classification, the export loop, path-keyed texture cache and PNG naming, preview launch |
| `reader.py` | `BinaryReader` — typed reads over the raw byte stream |
| `package.py` | `.uasset` package header: summary, name map, imports/exports, UE version constants. Note UE5 puts `FileVersionUE5` **before** `FileVersionLicenseeUE4` |
| `properties.py` | Property-tag parsing/skipping; handles both the UE4-style and the newer `PROPERTY_TAG_COMPLETE_TYPE_NAME` format. `skip_properties` for mesh/texture, `read_properties` for umap, `read_property_tag`/`end_property_tag`/`has_serialization_control_byte` for callers that walk tags themselves |
| `mesh.py` | `StaticMesh.from_package`, FMeshDescription parsing, `_GLBBuilder` and the `export_glb` / `export_level_glb` writers |
| `texture.py` | `Texture2D.from_package` (FEditorBulkData source art, Oodle + optional `TSCF_UEDELTA` delta decode), `export_png` |
| `scene.py` | Asset index (keyed by parsed export object name) and mesh → material → base-color texture resolution: parent-chain walk, `TextureParameterValues` overrides, cross-package BaseColor expression graph, material layers |
| `umap.py` | `.umap` level parsing: actors, transforms, static-mesh references, level instancing, World Partition external actors |
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

### Texture identity

**An asset name is not unique across a project.** `T_Statue_M` exists in both `Sci_Fi_SpaceShio/`
and `StarterContent/` with different pixels, and there are seven such names in the sample project.
Textures are therefore identified by **package path** (`/Game/Sci_Fi_SpaceShio/Textures/T_Statue_M`),
which is what keys `texture_cache` and `tex_name_map`:

- `scene.py` resolves a texture reference with `_texture_reference`, which walks the import's outer
  chain to the `Package` import naming it (`_import_package_path`). A same-package reference has no
  path, so it falls back to the object name.
- `cli.py` derives each texture file's own path with `package_path_for_file` — a project's
  `Content/` is mounted at `/Game/`.
- `tex_name_map` registers the package path always, and the bare asset name **only when exactly one
  texture has it**. An ambiguous name resolves by path or not at all; guessing is what silently
  swapped textures between assets.
- PNGs still go to one flat folder, so `assign_texture_filenames` suffixes colliding stems
  (`T_Statue_M.png`, `T_Statue_M_2.png`), ordered by package path so reruns match. Nothing reads
  these back — GLBs embed their own pixels — so the suffix is cosmetic.

The same ambiguity exists for *materials* (`MI_GrateMaterial` names four files) and is not yet
handled: material lookup is still by name, resolved to the lexicographically first path.

## Level assembly (`--export-level`)

`Export/Meshes/*.glb` are individual assets at the origin. `--export-level` instead writes one GLB
per level with every actor already positioned, so it imports as an assembled scene.

- `umap.parse_level` yields `LevelActor`s carrying a world transform.
- `cli.export_level` loads each **distinct** mesh once (with the same base-colour textures the
  per-mesh GLBs get) and produces one placement per actor.
- `mesh.export_level_glb` writes one glTF mesh per distinct asset and one **node** per placement.
  A level that puts the same wall panel down eighty times stores its geometry once.

Two things make the file size sane, and both matter: mesh instancing (above) and texture
deduplication. `_GLBBuilder` keys embedded images on the texture identity passed as the optional
third element of the `(material_index, pixels, key)` tuples `resolve_mesh_textures` returns — the
texture's package path. Without it, MainLevel came out at **1.6 GB** instead of **225 MB**, because
96 meshes each embedded their own copy of the same 13 textures.

`ue_matrix_to_gltf` converts a UE world transform into a glTF node matrix. Vertices are already
baked into glTF axes by `_GLBBuilder`, so a UE transform must be **re-expressed** in that basis
(`C·M·C⁻¹`), not merely applied. It returns column-major, as glTF requires.

### Units

glTF is metres, UE is centimetres, so distances are scaled by **0.01** by default (`--scale`,
`_UE_TO_GLTF_SCALE`). Without it a 5-metre wall imports as a 500-metre wall. The conversion
`C = scale · axis-swap` is applied in exactly two places, both driven off the same `_GLBBuilder.scale`:
mesh vertices in `_build_primitives`, and placement matrices via `ue_matrix_to_gltf`. The uniform
scale cancels out of a node matrix's rotation/scale part and survives only in its translation —
`C·M·C⁻¹` — so a placed instance keeps its own proportions while moving to scaled coordinates. Both
the per-mesh and level exporters take the same factor, so individual assets and the assembled level
always agree; `--scale 1.0` keeps UE centimetres throughout.

Actors whose mesh isn't in the project are skipped and reported — engine content such as
`/Engine/BasicShapes/Cube` is the common case. Skeletal-mesh actors are out of scope, so a level
assembles from its static meshes only.

### World Partition

A World Partition level's `.umap` is nearly empty: each actor lives in its own package under
`Content/__ExternalActors__/<level path>/`. Parsing only the map returns **zero** actors —
MainLevel has 1001 static-mesh actors spread over 1416 external packages. `parse_level` therefore
also walks that directory (`_find_external_actor_packages`) and parses each package with the same
code path, since each holds a complete actor with its components.

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

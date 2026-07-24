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
# No arguments: print usage and the worked examples, then exit (same as --help)
python main.py

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

# Look at a converted GLB on its own — no project, no re-export.  This is the
# file exactly as written, so it is what to check a conversion against.
python main.py --preview-glb ./Export/Levels/L_Showcase.glb

# Run it alongside a preview that already holds port 3050
python main.py --preview-glb ./Export/Levels/L_Showcase.glb --port 3060

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
| `input_dir` (positional) | Folder containing `.uproject` + `Content/` (default `./Input`; omitting *every* argument prints help instead of running) |
| `--preview LEVEL.umap` | Parse the level and serve a Three.js preview, assembled live from the per-mesh GLBs |
| `--preview-glb FILE.glb` | Serve a preview of one already-converted GLB, as written. No project or export needed; ignores `--scale` |
| `--port N` | Preview port (default `3050`) |
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
| `reader.py` | `BinaryReader` — typed reads over the raw byte stream; `resolve_fname` renders an FName from its `(index, number)` pair |
| `package.py` | `.uasset` package header: summary, name map, imports/exports, UE version constants. Note UE5 puts `FileVersionUE5` **before** `FileVersionLicenseeUE4` |
| `properties.py` | Property-tag parsing/skipping; handles both the UE4-style and the newer `PROPERTY_TAG_COMPLETE_TYPE_NAME` format. `skip_properties` for mesh/texture, `read_properties` for umap, `read_property_tag`/`end_property_tag`/`has_serialization_control_byte` for callers that walk tags themselves |
| `mesh.py` | `StaticMesh.from_package`, FMeshDescription parsing, `_GLBBuilder` and the `export_glb` / `export_level_glb` writers |
| `texture.py` | `Texture2D.from_package` (FEditorBulkData source art, Oodle + optional `TSCF_UEDELTA` delta decode), `export_png` |
| `scene.py` | Asset index (keyed by parsed export object name) and mesh → material → base-color texture resolution: parent-chain walk, `TextureParameterValues` overrides, cross-package BaseColor expression graph, material layers |
| `umap.py` | `.umap` level parsing: actors, transforms, static-mesh references, level instancing, World Partition external actors |
| `transform.py` | UE `FRotator`/transform math → matrices for the renderer |
| `preview_server.py` | stdlib `http.server` on port 3050 (`--port`); serves `preview.html`, a scene JSON, and either `Export/Meshes` + `Export/Textures` (level mode) or one GLB at `/api/model.glb` (`--preview-glb`) |
| `preview.html` | Three.js viewer (shipped as package data); level mode and GLB mode |

### FNames carry a number

An FName is a **pair** — a name-table index and a *number* — and the trailing `_N` of a name lives
in the number, not in the table. `MI_SpaceShip_1` is the entry `MI_SpaceShip` with number 2;
`FName::ToString` appends `_(number - 1)` whenever the number is non-zero. Every numbered sibling
therefore shares one table entry, and reading only the index silently collapses `MI_SpaceShip_1`,
`MI_SpaceShip_2` and `MI_SpaceShip_3` into a single name.

`reader.resolve_fname(name_map, index, number)` is the one place that rendering happens; every
site that reads the pair goes through it — the import and export maps in `package.py`, every
property name/type/struct/enum name and every `NameProperty` **value** in `properties.py`,
`_read_fname_value` in `scene.py`, `_parse_fname` in `texture.py`. **Read the number wherever you
read an index.** Dropping it made `SM_Ship_A`'s three material slots — `MI_SpaceShip_1/_2/_3` —
resolve to one material and gave all three sections the same texture.

Two exceptions, both deliberate: FName attributes inside an FMeshDescription (`ImportedMaterialSlotName`
and friends) are serialized as `FString`, not as a pair; and the byte-scans in `mesh.py`
(`_parse_static_materials`, `_parse_section_info_map`) search for a literal `(index, 0)` because the
property names they look for — `StaticMaterials`, `MaterialIndex` — never carry a number.

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
export**, not the file name, because that is the name importing packages use. The two mostly agree
for ordinary assets; World Partition external actors are where they part company, since those have
generated file names — `4R5KVGUQV9EML9A63KFY9E.uasset` holds `StaticMeshActor_UAID_…_1937645662`.
File names are registered only for names no package claimed. Where several packages export the same
name the lexicographically first path wins, so runs are reproducible.

### A channel mask means "this is a number, not a colour"

An `FExpressionInput` carries `Mask` plus `MaskR/G/B/A` alongside the expression it points at, and
those say **which channels** the consumer reads. `read_expression_input` decodes them. A sampler
reached through a mask selecting exactly one channel is being read as a *scalar* — a blend weight,
a roughness, a fresnel term — so its pixels are not an albedo and `_MaterialGraph._search` skips it.
The flag is sticky: once an edge narrows the signal to one channel nothing downstream widens it.

This is what `MM_StatueGlass` needs. Its base colour is `Tint * T_Statue_M.r` — one channel of a
packed mask, used as a multiplier. Treating that sampler as the base-colour map embedded the mask's
raw RGB and rendered the glass white-and-magenta; UE shows a near-flat pale blue. `M_Statue` is the
same shape (`grey * T_Statue_M.r`). Note the rule comes from the parsed mask, **not** from the `_M`
suffix — rule 1 below forbids the latter.

### Material appearance → glTF

`scene.resolve_material_appearance` returns a `MaterialAppearance`: the base-colour map, the
constant `factor` that multiplies it, and the blend mode. glTF splits what UE keeps in one graph,
and two of the three map exactly:

- **Tint.** UE's `BaseColor = Tint * Texture` *is* glTF's `baseColorFactor * baseColorTexture`, so a
  constant colour feeding BaseColor survives rather than being dropped. Instance
  `VectorParameterValues` override the master's default, keyed by parameter name.
- **Blend mode.** `EBlendMode` → `alphaMode` via `_ALPHA_MODE_BY_BLEND`; `BLEND_Masked` becomes
  `MASK` with `alphaCutoff` from `OpacityMaskClipValue` (default `0.3333`). Non-opaque materials are
  also written `doubleSided`. An instance's `BasePropertyOverrides` only count when the matching
  **`bOverride_<name>` flag is set** — instances serialize a stale inherited `BlendMode` otherwise,
  and honouring it would invent overrides.
- **Opacity does not map cleanly.** UE evaluates it per pixel (fresnel, lerps); glTF wants one
  number or a texture channel. A constant is used where the graph offers one — `MM_StatueGlass` has
  an `Opacity` scalar parameter defaulting to `0.3` — and anything less certain is reported through
  `MaterialAppearance.notes`, which the export loop prints. It is never guessed at silently.

**Never map `BLEND_Masked` to `BLEND`.** `T_CleanMetal_B` and `T_DirtyMetal_B` carry a *uniform*
alpha of 191 with no mask data, and between them they clothe 65 meshes. As `MASK` with a 0.3333
cutoff they stay solid, which is correct; as `BLEND` they would turn most of the level 75%
see-through. Only `T_Bush_D` (alpha 0–255) is a genuine cutout in the sample project.

### The importer contract — two silent failures

The GLBs feed an engine whose importer fails *quietly* in two places, so both are asserted at write
time in `mesh._check_import_contract` rather than left to documentation:

1. **A non-opaque material's base-colour image must be RGBA.** The importer decodes at native
   channel count and pads 3-channel data to RGBA with alpha `0xFF`, so an RGB image on a `BLEND`
   material loads fully opaque with no error anywhere. There is **no separate opacity slot**: where a
   non-opaque material has a texture, `_GLBBuilder` forces RGBA (`_as_rgba`) and composites the
   constant opacity into the alpha channel, then resets `factor[3]` to 1.0 so it is not applied
   twice. Where it has no texture, `factor[3]` carries the alpha.
2. **`emissiveFactor` must be non-zero wherever `emissiveTexture` is set.** The shader computes
   `emissive = factor.rgb; if (tex) emissive *= tex`, and glTF defaults the factor to `[0,0,0]` —
   which multiplies any emissive texture to nothing. Nothing emits `emissiveTexture` today; the
   assert exists so adding it cannot regress.

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

Materials are still looked up by name rather than by package path, and would break the same way if
two folders held a material of the same name. Nothing in the sample project does. The ambiguity
that used to appear here — `MI_GrateMaterial` "naming four files", `MI_SpaceShip` three — was the
dropped FName number, not a real collision: those assets are `MI_GrateMaterial_1/_3/_4/_5` and
`MI_SpaceShip_1/_2/_3`, one per file. Genuine duplicates stay possible in another project, so
keying materials by path is still the robust fix.

## Level assembly (`--export-level`)

`Export/Meshes/*.glb` are individual assets at the origin. `--export-level` instead writes one GLB
per level with every actor already positioned, so it imports as an assembled scene.

- `umap.parse_level` yields `LevelActor`s carrying a world transform.
- `cli.export_level` loads each **distinct** mesh once (with the same base-colour textures the
  per-mesh GLBs get) and produces one placement per actor.
- `mesh.export_level_glb` writes one glTF mesh per distinct asset and one **node** per placement.
  A level that puts the same wall panel down eighty times stores its geometry once.

Two things make the file size sane, and both matter: mesh instancing (above) and texture
deduplication. `_GLBBuilder` keys embedded images on `MaterialSpec.texture_key` — the texture's
package path. Without it, MainLevel came out at **1.6 GB** instead of its present **282 MiB**,
because 96 meshes each embedded their own copy of the same handful of textures. (It was 225 MiB
before FNames were read with their number — the numbered `MI_GrateMaterial_*` and `MI_SpaceShip_*`
siblings had all collapsed onto one texture each, so the file was small and wrong.) Where opacity is
composited into a texture's
alpha (see below) the key gains the opacity value, so two materials sharing a texture at different
opacities stay distinct instead of silently sharing the first one's pixels.

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

`--preview LEVEL.umap` is a third consumer of the same conversion and has to be kept in step: it
loads the per-mesh GLBs, whose vertices are already scaled, but places them from raw UE actor
transforms. (`--preview-glb` does not — see below — since a finished GLB needs no placement.)
`preview_server` therefore reports the factor as `unit_scale` in the scene JSON and `preview.html`
rebuilds `C`/`C⁻¹` from it in `ueToThreeBasis` — the JS mirror of `mesh._basis`. Everything else the
viewer measures in UE centimetres (camera clip planes and start height, grid extent, fly speed) is
scaled by the same `unitScale`, which is why it is fetched *before* `initThree`. Leave it at 1.0 and
geometry comes out 100× too small for its placements. `--scale` must match the factor `Export/` was
written with; `--skip-export --preview` with a different value will disagree.

Actors whose mesh isn't in the project are skipped and reported — engine content such as
`/Engine/BasicShapes/Cube` is the common case. Skeletal-mesh actors are out of scope, so a level
assembles from its static meshes only.

### Checking a conversion: `--preview-glb`

There is no test suite, so the way to check a conversion is to look at the file that came out.
`--preview-glb FILE.glb` serves exactly that — the finished GLB, loaded and shown as written:

```bash
unreal-assets-to-glb --preview-glb ./Export/Levels/MainLevel.glb
```

It needs no project, no `Input/`, no `Content/` and no re-export, so it runs before any of the
project checks in `main()` and returns straight after. Any GLB works — an assembled level or one
mesh out of `Export/Meshes/`.

**Units do not enter into it.** The viewer measures the model (`frameModel`) and sizes clip planes,
grid, fly speed and the opening camera from its bounding-box diagonal, so a level exported in metres
and the same level in centimetres are framed identically. Nothing here consults `unit_scale` or
`--scale`; that plumbing exists only for `--preview LEVEL.umap`, which still places per-mesh GLBs
from raw UE actor transforms and so has to agree with how they were written.

The viewer branches on `mode` in the scene JSON:

| | `--preview LEVEL.umap` (`mode: "level"`) | `--preview-glb FILE.glb` (`mode: "glb"`) |
| --- | --- | --- |
| Source | `.umap` + `Export/Meshes/*.glb` | the one file, via `/api/model.glb` |
| Placement | `ueToThreeMatrix` per actor, at load | already baked into the file |
| Sidebar | actor hierarchy, editable UE transforms | node list, model stats, Focus only |
| Camera readout | UE centimetres | the file's own coordinates |
| Sizing | `unit_scale` from the server | the model's bounding box |

A level GLB runs to hundreds of MB (MainLevel is 282 MiB), so `_serve_model` streams it rather than
buffering a second copy, and the loader reports percentage while it arrives.

Both modes take `--port` (default 3050). It exists because a second preview on a port that is
already serving used to bind anyway: Windows honours `SO_REUSEADDR` against a live listener, so the
two servers then answered requests at random — the page from one, its scene JSON from the other.
`_PreviewServer` disables the flag on Windows so the collision fails loudly instead.

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

The one thing carried out of a material graph beyond the base-colour map is what glTF represents
natively: a constant tint (`baseColorFactor`), the blend mode (`alphaMode`/`alphaCutoff`), and a
constant opacity. Evaluating the graph — per-pixel fresnel, lerps, channel arithmetic — remains out
of scope; where opacity is not a constant the exporter says so instead of approximating it.

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

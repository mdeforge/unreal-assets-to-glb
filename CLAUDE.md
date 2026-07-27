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
| `--export-level LEVEL.umap` | Assemble the level into `Export/Levels/<Level>.glb` — actors positioned, plus its lights (`KHR_lights_punctual`) and cameras |
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
  - `Export/texture_cache.pkl` — pickle cache of decoded texture pixels and their `SRGB` flags,
    keyed by package path, guarded by an md5 fingerprint of the input textures' paths/mtimes/sizes
    **and** a `version` key (the fingerprint catches changed input, the version catches a changed
    cache *shape*). Delete it to force a re-decode.
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
| `cli.py` | Argument parsing, asset discovery/classification, the export loop, path-keyed texture cache and PNG naming, surface-coverage guard, preview launch |
| `reader.py` | `BinaryReader` — typed reads over the raw byte stream; `resolve_fname` renders an FName from its `(index, number)` pair |
| `package.py` | `.uasset` package header: summary, name map, imports/exports, UE version constants. Note UE5 puts `FileVersionUE5` **before** `FileVersionLicenseeUE4` |
| `properties.py` | Property-tag parsing/skipping; handles both the UE4-style and the newer `PROPERTY_TAG_COMPLETE_TYPE_NAME` format. `skip_properties` for mesh/texture, `read_properties` for umap, `read_property_tag`/`end_property_tag`/`has_serialization_control_byte` for callers that walk tags themselves |
| `mesh.py` | `StaticMesh.from_package`, FMeshDescription parsing (positions, normals, tangents + BinormalSign, UVs), `_GLBBuilder` and the `export_glb` / `export_level_glb` writers |
| `texture.py` | `Texture2D.from_package` (FEditorBulkData source art, Oodle + optional `TSCF_UEDELTA` delta decode, BGRA→RGBA for both raw *and* PNG/JPEG sources, `UTexture::SRGB` and `bFlipGreenChannel`), `export_png` |
| `scene.py` | Asset index (keyed by parsed export object name) and mesh → material resolution: base-colour, emissive, metallic/roughness, normal and occlusion, parent-chain walk, `bUseMaterialAttributes`/material-layer attribute sourcing (`_attribute_source`), `TextureParameterValues` overrides, cross-package BaseColor/EmissiveColor expression graphs, material layers, blend mode |
| `umap.py` | `.umap` level parsing: actors, transforms, static-mesh references, level instancing, World Partition external actors, and light/camera components (`LevelEmitter`) |
| `lights.py` | UE light and camera components → `KHR_lights_punctual` and glTF cameras: per-light `ELightUnits` conversion to candela/lux, UE defaults, sRGB + blackbody colour, rect-light encoding |
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

The same mask is read positively elsewhere: it is exactly what tells the metallic/roughness walk
*which* channel of a packed mask feeds each scalar, and what the emissive walk bakes as a shape.
One decoder, three consumers — base colour rejects a one-channel edge, the other two require it.

### Material appearance → glTF

`scene.resolve_material_appearance` returns a `MaterialAppearance`: the base-colour map, the
constant `factor` that multiplies it, the blend mode, and the `Emissive` (see "Emissive → glTF"
below). glTF splits what UE keeps in one graph, and two of the three base-colour parts map exactly:

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

### Emissive → glTF

`scene._resolve_emissive` reduces a material's `EmissiveColor` graph, which is a product
(`tint × texture × intensity`), by walking it: a `Multiply` is the product of its inputs, a
`LinearInterpolate` is taken as its first input (the same "first endpoint" rule base colour uses for
a lerp — the other side is usually an animated or blended-away state), a vector parameter is a tint,
a scalar an intensity, and the first texture sampler the emissive map. Nodes that are not a product
(a `Sine` driving a glitch, a `Panner`) are not descended into, so the LCD panels' animated overlay
does not bleed into the static emission. This mirrors the base-colour discipline; evaluating the
graph per pixel stays out of scope.

The result splits into glTF's fields the way the importer folds them back (`factor × strength`):

- **`emissiveFactor` is the normalized tint, ≤1**, and the magnitude goes to
  `KHR_materials_emissive_strength`. glTF core clamps the factor to [0,1], so an over-1 factor is
  silently capped and brights are lost — the split is the only way `MI_ReparingTools_01`'s ×19.5 or
  `MI_IM4`'s red ×8 survive. `_EMISSIVE_STRENGTH_CEILING` (64, from the engine material) is flagged,
  not clamped.
- **A colour-map emissive texture passes through** — it is already sRGB in the source, and the
  importer loads emissive sRGB. A **single-channel** emissive (a packed mask read as a *shape*, like
  `M_Lamp`'s `T_Lamp_M.B`) has no glTF equivalent — the importer has no scalar emissive input — so
  `_emissive_rgb` bakes that channel to a grey RGB image, sRGB-encoding it so it survives the
  importer's decode. Fold-to-constant would be wrong here because the channel varies spatially (only
  the lens glows); the pixels decide (`min == max` ⇒ uniform).
- **A wired-but-black emissive emits nothing.** An instance that does not override the master's
  `T_Black` default, or whose emissive texture is an all-zero image (`T_1_Ship_2_E` is blank — the
  ship does *not* glow, despite the `_E` file existing), multiplies to zero. `Emissive.texture_required`
  says the chain sampled a texture at all, and the drop happens where the fact is knowable: `cli.py`
  drops a slot whose texture is absent from the project (a cache miss — the `T_Black` default), and
  the writer's `_emissive_rgb` drops one whose texture *is* present but all-black, returning None.
  Both are data-driven, not a name check (rule 1): `max == 0` distinguishes the blank placeholder
  from a real sparse emissive like `T_ReparingTools_E`.

### Metallic and roughness → glTF

`scene._resolve_scalar_input` reduces the master's `Metallic` and `Roughness` inputs to glTF's
`factor × texture.channel`, which is the only shape glTF has for either. It **evaluates** the
constant part of the graph rather than accumulating a product — a lerp has to be computed, not
folded — and captures the first texture sampler as the map, with the channel its mask selects. A
sampler contributes 1.0 to the constant, since its texels travel separately. Four node kinds are
understood, each because glTF holds the result *exactly*:

- a **constant or scalar parameter**, an instance's `ScalarParameterValues` winning over it;
- a **Multiply**, the product of its pins;
- a **LinearInterpolate**, which is where the care goes (below);
- a **OneMinus** wrapping the whole input — the gloss idiom, carried as `invert`. Exact only
  outermost, since `1-(a·b)` is not `(1-a)·(1-b)`.

**An unconnected pin is not an absent pin.** `Multiply` and `LinearInterpolate` each keep a
`Const<Pin>` member that UE compiles in place of an unwired input (`ConstA` 0.0, `ConstB` 1.0,
`ConstAlpha` 0.5), and leave it unserialized while it still holds the constructor's value. Reading
only the *wired* pin turns `M_Shelf`'s `lerp(0, 1, T_Shelf_M.G)` metallic into a solid 1.0. For the
same reason a `Constant`/`ScalarParameter` that serializes no value is **0.0**, not unparseable —
UObject zero-initialises it — and treating that as a failure made `M_Glass`'s roughness (a
parameter left at 0) look like a graph the walk could not handle.

A **lerp needs three cases**, because `lerp(a, b, t) = a + (b-a)·t` is affine and glTF is not:

- **Alpha constant** → compute it. Exact.
- **Alpha per-pixel, A wired** → take A. Something is authored into A, so the lerp is that signal
  with a second state blended over it (a distance fade, a wetness pass) — the same "first endpoint"
  rule base colour and emissive use. `M_Wood_Floor_Walnut_Polished`'s roughness texture is here.
- **Alpha per-pixel, both endpoints bare constants** → the *alpha* carries the detail, and only two
  shapes survive: `a == 0` gives `b·t` (a plain mask — `M_Shelf`), and `lerp(1, 0, t)` gives `1-t`
  (an inverted one — `M_Tech_Hex_Tile`). **Anything else is a range, not a value.**
  `M_Brick_Clay_New` blends `ConstA -0.3` with `ConstB 0.8` through a texture; reading an endpoint
  off it yields a *negative roughness* UE never renders. It is reported, not guessed.

Anything else — a fresnel, a clamp over a bump-offset chain, a material function call, a second
texture in one input — is **not reduced**: UE's default comes back with `reduced=False` and the
export loop prints it. On StarterContent that is 0% of metallic and 15% of roughness.

The writer composes one **new** RGB image (`mesh._metallic_roughness_image`) with **G = roughness,
B = metallic**, R left at 1.0 (occlusion is out of scope, so a consumer reading R finds none rather
than a wrong one). Composing is not optional: UE reads each scalar from whichever channel of
whichever packed mask the graph names, which is rarely glTF's layout — and it is what keeps rule 4
satisfiable, since the source `_M` file stays free to feed a colour slot. A channel with no source
is left at 1.0 so its factor passes through. Images are deduped on
`(source key, channel)` per side, so a level sharing one mask across dozens of materials embeds it
once while each material keeps its own factors.

Two things are baked rather than carried as factors, because glTF cannot express them otherwise:
an **inverted** channel becomes `1 - factor·texel` with the factor reset to 1.0 (the baked constant
joins the dedup key, so two gloss materials at different scales stay distinct), and an **sRGB
source** is decoded to linear. That second one is why `Texture2D` now parses `UTexture::SRGB`
(default true, serialized only when it differs): an MR map is linear data, and UE decodes an
sRGB-flagged source before the shader sees it. The flag rides through `texture_cache.pkl`, which
gained a `version` key so a cache written before it is rebuilt rather than half-read — no
fingerprint can notice a changed *shape*.

**UE's defaults are the engine's answer, not a fallback we invented.** An unwired input is
Metallic 0.0 / Roughness 0.5 (`MaterialAttributeDefinitionMap.cpp`), which is also what a slot with
no spec at all writes. Neither glTF default is relied on: glTF's `metallicFactor` defaults to
**1.0**, so a dielectric UE renders as plastic would import as a mirror.

### Normal maps, occlusion, and the tangent basis

`scene._resolve_normal_texture` finds the map a master's `Normal` input reads. There is nothing to
evaluate here — glTF's `normalTexture` *is* the map — so the walk only needs the right sampler: it
follows the **first** input of the blend nodes a normal graph uses (`LinearInterpolate`, `Add` for a
detail normal layered over a base, `Multiply`) and descends into material functions, since a shared
"blend two normals" function is where the sampler often lives. A `Normal` left at a constant flat
`(0,0,1)` reaches no sampler and writes no map, which is right — the interpolated vertex normal is
the answer. Occlusion reuses `_resolve_scalar_input` on `AmbientOcclusion`, with the reduced
constant going to `occlusionTexture.strength`; UE's unwired default is 1.0, which is glTF's too, so
an unwired AO writes nothing rather than a neutral map.

Both are written as **separate images**, never packed together. ORM packing (AO in R beside
roughness in G and metallic in B) is the usual glTF optimisation and is deliberately *not* used: the
consuming importer wants a standalone `occlusionTexture` that multiplies ambient, alongside its own
screen-space GTAO. AO is written grey into all three channels even though glTF reads only R — it
costs almost nothing after PNG compression and keeps the map readable rather than red-tinted.

**The tangent basis is exported, and its handedness is negated.** FMeshDescription carries `Tangent`
(vec3) and `BinormalSign` (float) per vertex instance; glTF wants them as one `TANGENT` vec4 whose
`w` orients the bitangent. The sign cannot be copied across: the axis swap has **determinant −1**,
and a cross product does not survive a mirror unchanged — `cross(Ma, Mb) = −M·cross(a, b)` for
orthogonal `M` with `det −1`. Since glTF defines the bitangent as `cross(N, T.xyz)·T.w`, the export
writes `w = −BinormalSign`. Get it wrong and every normal map lights from the wrong side, which
reads as "the normal maps look inverted" rather than as anything to do with winding. `TANGENT` is
written only alongside `NORMAL` and `TEXCOORD_0`, as the spec requires. It is not free — about 2.2 MB
across StarterContent's 50 meshes — but a consumer without it has to invent a basis, and the invented
one is not what the map was baked against.

`normalTexture.scale` is left at 1.0. UE expresses normal strength as arithmetic on the sampled
vector, and reading one number back out of that would be a guess.

### `bUseMaterialAttributes` moves the whole graph somewhere else

A master that sets `bUseMaterialAttributes` leaves **every** individual pin on `MaterialEditorOnlyData`
unconnected and routes everything through `MaterialAttributes` — usually straight into a layer stack,
so the graph is not in the master's package at all. `MM_Base02` is three nodes and nothing else
(`MaterialAttributeLayers`, `GetMaterialAttributes`, `SetMaterialAttributes`); the real Metallic,
Roughness, Normal and AmbientOcclusion live in the layer function `ML_Base`, whose
`MakeMaterialAttributes` wires them, and the *maps* come from the layer instance's parameters
(`BC` → `T_DirtyMetal_B`, `NM` → `T_DirtyMetal_N`, `ORM` → `T_DirtyMetal_ORM`).

`_attribute_source` resolves this for every scalar/normal channel, in order: the direct pin, then the
master's own `MakeMaterialAttributes`, then **layer 0** — the base layer the rest blend over, which is
the layer base colour already reads. One layer has to stand for the surface (blending the stack per
pixel is out of scope) and it must be the *same* layer for every channel: base colour describing
layer 0 while roughness described a rust pass is worse than either alone.

`_layer_graph_package` follows a layer slot to the function holding the graph, since a slot normally
holds a layer *instance* that contributes only parameter values. Overrides then stack nearest-first:
the material instance's `LayerParameter` values for that slot, then the layer instance's, then the
function's defaults. Keying matters here — `_parse_scalar_parameter_values_keyed` exists because the
bare-name form collapses a layered material's per-layer values onto whichever serialized last.

The payoff on MainLevel: roughness resolved from a texture on **307 of 330 slots**, up from ~30.
The ORM packing lines up channel-for-channel with glTF — occlusion R, roughness G, metallic B.

**This failure was invisible per material and obvious in aggregate.** An unwired Metallic really is
0.0 and an unwired Roughness really is 0.5, so the per-material path was right to stay silent; 282 of
313 slots sat on those defaults and nothing said a word. `cli.report_surface_coverage` now reports
when at least half the slots fall back, because that is the scale at which "the artist left it blank"
stops being a credible explanation.

### An array input's element type is not in the outer tag before 5.4

Expression inputs nested in an array of structs — `MaterialFunctionCall.FunctionInputs`,
`SetMaterialAttributes.Inputs` — were unpacked only when the outer `ArrayProperty` tag named
`StructProperty` in its `inner_types`. That information only reaches the outer tag from
`PROPERTY_TAG_COMPLETE_TYPE_NAME` (file version 1012) on; before it, `inner_types` is **empty** and
the element type lives in an FPropertyTag written inside the value, which `_iter_struct_array`
already knows how to read. So on any older package every array input was skipped.

That is the whole reason a normal could look unreachable. When a graph blends through an engine
function — `BlendAngleCorrectedNormals` is the common one — the function itself is not in the
project and cannot be opened, so the pins on the **calling node** are the only route to the map.
`M_Rock` (file version 1006) has `T_RockMesh_N` and `T_Detail_Rocky_N` sitting in `FunctionInputs`,
and the walk reported "normal is wired but reaches no texture in the project" for both.

`_is_struct_array` now also accepts an empty `inner_types`. An array holding something else parses
to nothing, because using an element still requires an expression-input struct name.

Note what this did *not* explain: base colour stayed at 2 of 53 material slots afterwards, and that
is correct. `M_Chair`'s base colour is `Multiply(Lerp(Lerp(colour, colour, T.B), ColorMetal, T.G),
T.R)` — constants blended by mask channels, with the only texture read one channel at a time. There
is no albedo map to find, the channel-mask rule rightly rejects the sampler, and the tint travels in
`baseColorFactor`.

### Source art is stored in its *declared* format, PNG container or not

`TSCF_PNG` source art is not an RGB image — it is the source in its declared pixel format wearing a
PNG's labels. For `TSF_BGRA8`, that is B, G, R, A bytes. `_decode_encoded_source` trusted the
container and skipped the swap the raw path applies, so **every PNG-compressed texture came out with
red and blue transposed**: StarterContent's clay brick decoded bluer than it was red, oak wood
decoded blue-dominant, and `T_Chair_N` gave `(254, 128, 128)` instead of a tangent-space
`(128, 128, 255)`.

It survived a long time because it is invisible without a reference: a swapped albedo is still a
plausible-looking texture. It was found by checking decoded channel means against colours known in
advance — brick is red, grass is green, a tangent-space normal map is blue — which is the cheapest
test available and worth repeating whenever texture decoding changes. The swap is keyed on the
**declared** source format (`_BGRA_ORDERED_FORMATS`), not applied blanket, so a future `TSF_*` that
is genuinely RGB-ordered is not broken by it.

This also silently corrupted channel *selection*: a metallic or roughness read from `.R` was reading
what the mask stored in B.

### The importer contract — four silent failures

The GLBs feed an engine (`AxConvert` → Axon) whose importer fails *quietly* in four places. The
governing rule is **emit spec-standard glTF 2.0 with no accommodations for that engine and no
pre-tuning of values to look right there**; these four are the exceptions, because a *spec-legal*
file still imports wrong. All four are asserted at write time — `mesh._check_import_contract` and
`_GLBBuilder._note_texture_slot` — rather than left to documentation:

1. **A non-opaque material's base-colour image must be RGBA.** The importer decodes at native
   channel count and pads 3-channel data to RGBA with alpha `0xFF`, so an RGB image on a `BLEND`
   material loads fully opaque with no error anywhere. There is **no separate opacity slot**: where a
   non-opaque material has a texture, `_GLBBuilder` forces RGBA (`_as_rgba`) and composites the
   constant opacity into the alpha channel, then resets `factor[3]` to 1.0 so it is not applied
   twice. Where it has no texture, `factor[3]` carries the alpha.
2. **`emissiveFactor` must be non-zero wherever `emissiveTexture` is set.** The shader computes
   `emissive = emissiveFactor.rgb * texture(...)`, and glTF defaults the factor to `[0,0,0]` — which
   multiplies any emissive texture to nothing. `_apply_emissive` forces the factor non-zero (a
   normalized tint already has a component at 1; a degenerate all-zero factor falls back to white),
   and `_check_import_contract` asserts it. This one fires for real now — emissive textures *are*
   written.
3. **`alphaMode` is always written.** Absent means `OPAQUE` and their shader then hard-forces alpha
   to 1, discarding transparency carried only in texture alpha. pygltflib serializes the field
   unconditionally, so this holds today; it is listed because it is not free if the writer changes.
4. **No image is used as both a colour and a data map.** They dedupe by image and the first
   colour-space classification wins, so an image used as both base-colour/emissive (sRGB) and
   normal/MR/AO (linear) decodes wrong in one of the two. This is live now that all five channels
   are written: base colour and emissive are `'color'`, while metallic-roughness, normal and
   occlusion are `'data'`, and the same packed `_M` texture routinely feeds several. It stays
   satisfiable because every data map gets **its own image** — MR and AO are composed fresh, and
   the normal map is embedded under its own key namespace — so a colour slot and a data slot can
   never land on one image however many materials share the source.

Also binding, and already true: **one UV set** (everything on `TEXCOORD_0`; they read attribute 0
and ignore `texCoord`), no `KHR_texture_transform` (bake into UVs), PNG only — no KTX2 — and
`pbrMetallicRoughness` rather than spec-gloss or unlit. If emissive above 1.0 is ever needed, use
`KHR_materials_emissive_strength` (normalized factor + strength); `emissiveFactor > 1` is invalid.

Their shader caps at **8 lights**, so a 94-light level renders with the first 8 in scene order. That
is not an export problem, but it is the usual cause of "the imported level looks unlit".

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

### Lights and cameras (`lights.py`)

`--export-level` also writes the level's lights as `KHR_lights_punctual` and its cameras as standard
glTF cameras. Everything goes out in the units the spec asks for — **candela** for point and spot,
**lux** for directional, **radians** for angles — with no scaling to suit a particular renderer.

- **Collected per component, not per actor.** `umap.parse_level` walks every light and camera
  *component* and resolves its world transform through the same parent chain the meshes use. A
  `PointLight` actor holds its component as the root, but `BP_StandLight_C` holds a
  `RectLightComponent` as one child among several — MainLevel's 11 spot lights exist only inside
  Blueprints, so per-actor collection would find none of them.
- **No extra basis correction.** glTF aims lights and cameras down **−Z** with **+Y** up; UE aims
  them down **+X** with **+Z** up. The mesh axis swap already sends UE +X → glTF −Z and +Z → +Y, so
  `ue_matrix_to_gltf` is correct unchanged. Scale is dropped: a punctual light has no extent, UE
  ignores it too (a rect light's `SourceWidth`/`SourceHeight` reach the renderer unscaled), and 8 of
  this level's lights inherit a 2.47× from the Blueprint placing them.
- **Intensity units are read per light.** UE stores `ELightUnits` on each component and a level
  mixes them — MainLevel has 66 Candelas and 28 Unitless. The default is **Unitless**, which is why
  a deliberately authored candela light always serializes the property. Each conversion mirrors that
  component's `ComputeLightBrightness()` divided by UE's cm²→m² factor of 100·100: Candelas passes
  through, Lumens divides by 4π (point), 2π(1−cos θ) (spot) or π (rect), Unitless is the legacy ×16,
  and EV is `2^EV`.
- **Colour.** `LightColor` is an sRGB `FColor` (stored B, G, R, A) converted to linear, then
  multiplied by the blackbody tint when `bUseTemperature` — exactly what
  `ULightComponent::GetColoredLightBrightness` does. `color_temperature_to_linear` reproduces UE's
  Planckian-locus approximation coefficient for coefficient. That tint carries unit *luminance*, not
  unit maximum, so components can exceed 1 (1500 K is `(3.27, 0.43, 0.00)`); the excess is moved into
  `intensity`, which is exact since a renderer only uses `color × intensity`.
- **Rect lights have no glTF equivalent** — `KHR_lights_area` was closed in 2023 and never shipped —
  so the encoding is lossy by necessity. Each becomes a **spot** at the rect centre aimed along its
  normal with `outerConeAngle = π/2`, `innerConeAngle = 0`: spot because a rect light is one-sided
  and spot is the only punctual type that says so. Intensity is `Φ/π`, the on-axis intensity of a
  Lambertian emitter (`Φ = L·A·π`); `Φ/4π` would under-light the forward direction 4×. The full
  description goes in the **node's `extras`** under the draft proposal's field names (`shape`,
  `width`, `height`, barn door, source texture) together with the original intensity **and its unit
  string**, so a real area-light path later is an importer change rather than a re-export. Emissive
  geometry is deliberately *not* substituted — their renderer has no GI, so it would light nothing.
- **`zfar` is always written.** glTF treats an absent zfar as an infinite projection, which a
  consumer then has to invent a number for. UE has no per-camera far plane to copy, so one is derived
  from the level's bounding-box diagonal and written explicitly; `znear` is UE's `NearClipPlane=10`
  cm. `yfov` comes from `CurrentHorizontalFOV` and `AspectRatio` — glTF wants a *vertical* fov in
  radians, UE stores a horizontal one in degrees. Orthographic cameras are reported rather than
  guessed at, since glTF needs `xmag`/`ymag` that UE's `OrthoWidth` alone does not give.

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
2. The Unreal Engine source is the reference for the binary formats and for **engine defaults**.
   Consult it when a detail is unclear rather than guessing. It may be a source checkout at
   `./UnrealEngine-5.5.0-release` (gitignored), but an installed build works and is what is present
   here: `C:\Program Files\Epic Games\UE_5.5\Engine\Source\Runtime\...` ships the Runtime `.cpp` as
   well as the headers, which is where every light constant in `lights.py` came from
   (`Components/LocalLightComponent.cpp`, `PointLightComponent.cpp`, `SpotLightComponent.cpp`,
   `RectLightComponent.cpp`, `Engine/Scene.h`, `ColorManagement/ColorSpace.cpp`,
   `Config/BaseEngine.ini`). A property that a package does not serialize was left at its default,
   so the default has to come from there — `Intensity` alone defaults to 5000.

## Scope — deliberately not supported

Do not "fix" these as if they were bugs; they are out of scope by design:
graph-based/complex material shaders, colliders, vertex colors, LODs, shaders, texture baking,
Nanite, animation decompression, skeletons/bones. Only **one UV channel** is exported.

All five PBR channels glTF represents natively are now carried — base colour, emissive,
metallic/roughness, **normal** and **occlusion** — along with the **tangent basis** the normal map
needs. Tangents were previously listed here as out of scope; they stopped being optional the moment
a normal map shipped.

Lights and cameras *are* exported, but only into `--export-level` GLBs, where they are level actors;
a per-mesh GLB has neither. Sky lights, IES profiles, light functions and area-light shape (beyond
the `extras` block) remain out of scope.

What is carried out of a material graph is what glTF represents natively: the base-colour map with a
constant tint (`baseColorFactor`), the blend mode (`alphaMode`/`alphaCutoff`), a constant opacity,
the **emissive** as `emissiveFactor` + `emissiveTexture` + `KHR_materials_emissive_strength`,
**metallic/roughness** as `metallicFactor`/`roughnessFactor` + a composed `metallicRoughnessTexture`,
and the **normal** and **occlusion** maps as `normalTexture` / `occlusionTexture` (+ `strength`),
each its own image.
Evaluating the graph in general — per-pixel fresnel, channel arithmetic, a clamp over a bump-offset
chain — remains out of scope. Each walk reduces the shapes glTF holds exactly and stops at anything
else: the emissive walk a *product* of tint/texture/intensity, the metallic/roughness walk a
constant/multiply/lerp/one-minus expression (a lerp *is* computed, since folding it to an endpoint
invents values). Where the result is not representable — a non-constant opacity, a roughness that is
a per-pixel range — the exporter says so instead of approximating it.

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

"""
UE 5.5 UAsset Parser, Exporter, and Level Previewer

Run with no arguments for usage and worked examples; the same text lives in
``_EXAMPLES`` below and is shown by ``--help``.
"""
import argparse
import os
import sys
import json
import traceback
from collections import Counter
import numpy as np
from tqdm import tqdm

from uasset.package import Package
from uasset.mesh import StaticMesh, export_glb, _UE_TO_GLTF_SCALE
import pickle
import hashlib
from uasset.texture import Texture2D, export_png
from uasset.properties import read_properties
from uasset.scene import (
    _build_uasset_index,
    _get_material_names_from_mesh,
    _get_base_color_texture_from_material,
    package_path_for_file,
)


# Default port for both preview modes; --port overrides it.
_PREVIEW_PORT = 3050


# Shown by --help and by a bare invocation.  argparse substitutes %(prog)s, so
# this reads correctly whether it was reached through main.py or the installed
# console script — but no other '%' may appear here or that substitution fails.
_EXAMPLES = """\
examples:
  %(prog)s ./Input
      Extract every static mesh to Export/Meshes/<Name>.glb and every
      base-colour texture to Export/Textures/<Name>.png.

  %(prog)s ./Input --export-level MainLevel.umap
      Assemble the whole level into one GLB in Export/Levels/, with every
      actor already positioned.

  %(prog)s ./Input --skip-export --export-level MainLevel.umap
      The same, reusing an existing Export/ (skips the per-mesh GLBs).

  %(prog)s ./Input --preview L_Showcase.umap
      Export, then serve a browser preview of the level on port 3050.

  %(prog)s ./Input --skip-export --preview L_Showcase.umap
      Preview an already-exported project without re-exporting it.

  %(prog)s --preview-glb ./Export/Levels/MainLevel.glb
      Preview a converted .glb on its own — no project, no re-export.  This is
      the file as written, so it is what to look at to check a conversion.

  %(prog)s ./Input --skip-textures
      Meshes only, no separate PNGs (textures are still embedded in the GLBs).

  %(prog)s ./Input --filter Pipe
      Only export meshes whose name contains "Pipe" (case-insensitive).

  %(prog)s ./Input --scale 1.0
      Keep UE centimetres instead of converting to glTF metres.

INPUT_DIR must be an *uncooked* project folder: a .uproject at the top and a
Content/ tree of .uasset / .umap files.  Cooked or packaged builds are not
supported.  --preview-glb needs no project at all.
"""


def find_uproject(input_dir):
    """Find .uproject file in input directory. Verify UE version."""
    for f in os.listdir(input_dir):
        if f.endswith('.uproject'):
            path = os.path.join(input_dir, f)
            with open(path, 'r') as fh:
                data = json.load(fh)
            engine = data.get('EngineAssociation', '')
            print(f"Found project: {f} (Engine: {engine})")
            if not engine.startswith('5.'):
                print(f"WARNING: Engine version {engine} may not be compatible (expected 5.x)")
            return path
    print("WARNING: No .uproject file found in input directory")
    return None


def find_uasset_files(input_dir):
    """Recursively find all .uasset and .umap files."""
    uassets = []
    umaps = []
    for root, dirs, files in os.walk(input_dir):
        for f in files:
            full = os.path.join(root, f)
            if f.endswith('.uasset'):
                uassets.append(full)
            elif f.endswith('.umap'):
                umaps.append(full)
    return uassets, umaps


def classify_uasset(filepath, input_dir):
    """Determine asset type by parsing package header.

    Returns ``(kind, name, package_path)`` where *kind* is 'mesh', 'texture'
    or 'other', *name* is the file stem used for output files, and
    *package_path* is the ``/Game/...`` path that identifies the asset
    uniquely — two folders can hold assets of the same name.
    """
    name = os.path.splitext(os.path.basename(filepath))[0]
    package_path = package_path_for_file(input_dir, filepath) or filepath
    try:
        pkg = Package(filepath)
        # Check exports for known types
        for i in range(pkg.export_count):
            class_name = pkg.get_export_class_name(i)
            if class_name == "StaticMesh":
                return 'mesh', name, package_path
            elif class_name in ("Texture2D", "TextureCube", "VolumeTexture"):
                return 'texture', name, package_path
        return 'other', name, package_path
    except Exception:
        return 'other', name, package_path


def assign_texture_filenames(textures):
    """Give every texture a unique PNG stem.

    Textures are identified by package path, but the PNGs are written to one
    flat folder, so assets sharing a name need distinct file names or they
    overwrite each other.  Sorted by package path so a rerun produces the
    same assignment.
    """
    stems = {}
    used = set()
    for _filepath, name, package_path in sorted(textures, key=lambda t: t[2]):
        stem, suffix = name, 2
        while stem.lower() in used:
            stem = f"{name}_{suffix}"
            suffix += 1
        used.add(stem.lower())
        stems[package_path] = stem
    return stems


def resolve_mesh_textures(mesh, name, uasset_index, tex_name_map, texture_cache):
    """Resolve a base-colour texture for each of a mesh's polygon groups.

    Each polygon group carries an ``ImportedMaterialSlotName`` which maps to a
    concrete material through the ``StaticMaterials`` array, via
    ``SectionInfoMap``.  Where that data is missing the polygon group index is
    assumed to be the material import index.

    Returns ``[(material_index, pixels, texture_key), ...]``.  The key is the
    texture's package path, which lets a level GLB embed shared textures once
    instead of once per mesh that uses them.
    """
    mesh_textures = []

    if mesh.material_slots and mesh.material_slot_names:
        section_map = getattr(mesh, 'section_info_map', None)
        for pg_idx, slot_name in enumerate(mesh.material_slot_names):
            if slot_name is None:
                continue
            # Resolve pg_idx → material slot index
            slot_idx = (section_map[pg_idx]
                        if section_map and pg_idx < len(section_map)
                        else pg_idx)
            if slot_idx < len(mesh.material_slots):
                mat_name = mesh.material_slots[slot_idx][1]
            else:
                mat_name = None
            if mat_name is None:
                continue
            tex_name = _get_base_color_texture_from_material(
                mat_name, uasset_index, tex_name_map)
            if tex_name and tex_name in texture_cache:
                mesh_textures.append(
                    (pg_idx, texture_cache[tex_name], tex_name))
    else:
        material_names = _get_material_names_from_mesh(name, uasset_index)
        for mat_idx, mat_name in enumerate(material_names):
            tex_name = _get_base_color_texture_from_material(
                mat_name, uasset_index, tex_name_map)
            if tex_name and tex_name in texture_cache:
                mesh_textures.append(
                    (mat_idx, texture_cache[tex_name], tex_name))

    return mesh_textures


class ExportContext:
    """What a level export needs from the asset pass that precedes it."""

    __slots__ = ('input_dir', 'meshes', 'uasset_index', 'texture_cache',
                 'tex_name_map')

    def __init__(self, input_dir, meshes, uasset_index, texture_cache,
                 tex_name_map):
        self.input_dir = input_dir
        self.meshes = meshes                # [(filepath, name), ...]
        self.uasset_index = uasset_index
        self.texture_cache = texture_cache
        self.tex_name_map = tex_name_map

    def mesh_path(self, mesh_name):
        """The .uasset providing *mesh_name*, or None."""
        for filepath, name in self.meshes:
            if name == mesh_name:
                return filepath
        return self.uasset_index.get(mesh_name)


def process_assets(input_dir, export_dir, skip_textures=False, mesh_filter=None,
                   skip_meshes=False, scale=_UE_TO_GLTF_SCALE):
    """Find and export all meshes as GLB and base color textures as PNG.

    Args:
        mesh_filter: If set, only export meshes whose name contains this
            substring (case-insensitive).  Textures are still fully cached.
        skip_meshes: Prepare the asset index and texture cache without writing
            per-mesh GLBs — what a level export on its own needs.
        scale: UE-unit → glTF-unit factor passed to the GLB writer.
    """
    os.makedirs(os.path.join(export_dir, "Meshes"), exist_ok=True)
    os.makedirs(os.path.join(export_dir, "Textures"), exist_ok=True)

    uassets, umaps = find_uasset_files(input_dir)
    print(f"Found {len(uassets)} .uasset files, {len(umaps)} .umap files")

    meshes = []
    textures = []
    others = []

    for filepath in uassets:
        asset_type, name, package_path = classify_uasset(filepath, input_dir)
        if asset_type == 'mesh':
            meshes.append((filepath, name))
        elif asset_type == 'texture':
            textures.append((filepath, name, package_path))
        else:
            others.append((filepath, name))

    print(f"Classified: {len(meshes)} meshes, {len(textures)} textures, {len(others)} other")

    # Build uasset index for texture resolution (mesh → material → texture chain)
    uasset_index = _build_uasset_index(input_dir)

    # ------------------------------------------------------------------
    # Export base color textures as PNG  (also cache pixel data for GLB)
    # ------------------------------------------------------------------
    # Use pickle cache to avoid re-parsing textures when Input/ is unchanged
    cache_path = os.path.join(export_dir, "texture_cache.pkl")

    # Compute a quick fingerprint of the Input/ texture files
    def _input_fingerprint():
        h = hashlib.md5()
        for fp, n, path in sorted(textures, key=lambda t: t[2]):
            h.update(path.encode())
            h.update(str(os.path.getmtime(fp)).encode())
            h.update(str(os.path.getsize(fp)).encode())
        return h.hexdigest()

    tex_success = 0
    texture_cache = {}  # package path -> numpy RGBA pixels
    tex_name_map = {}

    base_color_textures = list(textures)  # export ALL textures
    png_stems = assign_texture_filenames(textures)
    renamed = sum(1 for fp, n, path in textures if png_stems[path] != n)
    print(f"Textures to export: {len(base_color_textures)}")
    if renamed:
        print(f"  ({renamed} share an asset name with another texture and are "
              f"written under a suffixed file name)")

    # Try loading from pickle cache
    fp_hash = _input_fingerprint()
    cache_loaded = False
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            if isinstance(cached, dict) and cached.get("fingerprint") == fp_hash:
                texture_cache = cached["textures"]
                tex_success = len(texture_cache)
                cache_loaded = True
                print(f"  Loaded {tex_success} textures from pickle cache")
        except Exception:
            pass  # Corrupt cache — rebuild

    if not cache_loaded:
        for filepath, name, package_path in tqdm(
                sorted(base_color_textures, key=lambda t: t[2]),
                desc="Exporting textures", unit="tex"):
            try:
                pkg = Package(filepath)
                texture = Texture2D.from_package(pkg)
                if texture and texture.pixels is not None:
                    if not skip_textures:
                        png_path = os.path.join(
                            export_dir, "Textures",
                            f"{png_stems[package_path]}.png")
                        export_png(texture, png_path)
                    texture_cache[package_path] = texture.pixels
                    tex_success += 1
            except Exception as e:
                tqdm.write(f"  {name}: ERROR {e}")

        # Save pickle cache
        try:
            with open(cache_path, "wb") as f:
                pickle.dump({"fingerprint": fp_hash, "textures": texture_cache}, f)
            print(f"  Saved texture cache ({tex_success} textures) to {cache_path}")
        except Exception as e:
            print(f"  Warning: could not save texture cache: {e}")

    if skip_textures:
        print(f"  (PNG file export skipped --skip-textures, {tex_success} textures cached for GLB)")

    # Lookup table for _get_base_color_texture_from_material, keyed by the
    # package path a material's import table gives.  Bare asset names are
    # registered too, for references that carry no path — but only where the
    # name identifies exactly one texture, since resolving an ambiguous name
    # is what silently swapped textures between assets before.
    name_counts = Counter(name for _fp, name, _path in textures)
    tex_name_map = {}
    for _filepath, name, package_path in textures:
        if package_path not in texture_cache:
            continue
        tex_name_map[package_path] = package_path
        if name_counts[name] == 1:
            tex_name_map[name] = package_path
    ambiguous = sum(1 for n, c in name_counts.items() if c > 1)
    if ambiguous:
        print(f"  ({ambiguous} texture names are used by more than one asset "
              f"and resolve by package path only)")

    # ------------------------------------------------------------------
    # Export meshes as GLB with embedded textures
    # ------------------------------------------------------------------
    if skip_meshes:
        meshes_to_export = []
        print("  (per-mesh GLB export skipped)")
    elif mesh_filter:
        filtered = [(fp, n) for fp, n in meshes
                    if mesh_filter.lower() in n.lower()]
        print(f"Mesh filter '{mesh_filter}': {len(filtered)} of {len(meshes)} meshes match")
        meshes_to_export = filtered
    else:
        meshes_to_export = meshes

    mesh_success = 0
    for filepath, name in tqdm(sorted(meshes_to_export), desc="Exporting meshes", unit="mesh"):
        try:
            pkg = Package(filepath)
            mesh = StaticMesh.from_package(pkg)
            if mesh and mesh.vertices:
                mesh_textures = resolve_mesh_textures(
                    mesh, name, uasset_index, tex_name_map, texture_cache)
                glb_path = os.path.join(export_dir, "Meshes", f"{name}.glb")
                export_glb(mesh, glb_path,
                           textures=mesh_textures if mesh_textures else None,
                           scale=scale)
                mesh_success += 1
        except Exception as e:
            tqdm.write(f"  {name}: ERROR {e}")

    print(f"Export complete: {mesh_success} meshes, {tex_success} textures")
    return ExportContext(input_dir, meshes, uasset_index, texture_cache,
                         tex_name_map)


def export_level(input_dir, export_dir, umap_filename, context,
                 scale=_UE_TO_GLTF_SCALE):
    """Assemble one level's actors into a single positioned GLB.

    Args:
        input_dir:     Project root.
        export_dir:    Output directory; the level lands in ``Levels/``.
        umap_filename: Level file name, e.g. ``MainLevel.umap``.
        context:       :class:`ExportContext` from :func:`process_assets`.
        scale:         UE-unit → glTF-unit factor.

    Returns:
        Output path, or None when nothing could be assembled.
    """
    from uasset.umap import parse_level
    from uasset.mesh import export_level_glb
    from uasset.transform import rotator_to_matrix

    umap_path = find_umap_path(input_dir, umap_filename)
    if not umap_path:
        print(f"ERROR: Level file '{umap_filename}' not found in {input_dir}")
        return None

    level_name = os.path.splitext(os.path.basename(umap_path))[0]
    print(f"\nAssembling level: {level_name}")
    level = parse_level(umap_path)
    if not level.actors:
        print("  No placed actors found — nothing to assemble")
        return None

    wanted = sorted({a.mesh_name for a in level.actors if a.mesh_name})
    print(f"  {len(level.actors)} placed actors, {len(wanted)} distinct meshes")

    # Load each distinct mesh once, with the same textures the per-mesh GLBs get
    meshes = {}
    missing = []
    for mesh_name in tqdm(wanted, desc="Loading meshes", unit="mesh"):
        filepath = context.mesh_path(mesh_name)
        if filepath is None:
            missing.append(mesh_name)
            continue
        try:
            mesh = StaticMesh.from_package(Package(filepath))
            if mesh is None or not mesh.vertices:
                missing.append(mesh_name)
                continue
            textures = resolve_mesh_textures(
                mesh, mesh_name, context.uasset_index, context.tex_name_map,
                context.texture_cache)
            meshes[mesh_name] = (mesh, textures)
        except Exception as e:
            tqdm.write(f"  {mesh_name}: ERROR {e}")
            missing.append(mesh_name)

    if missing:
        print(f"  {len(missing)} meshes unavailable, their actors are skipped: "
              f"{', '.join(missing[:6])}{' …' if len(missing) > 6 else ''}")

    # UE world transform per actor
    placements = []
    for actor in level.actors:
        if actor.mesh_name not in meshes:
            continue
        matrix = np.eye(4)
        matrix[:3, :3] = (rotator_to_matrix(*actor.world_rotation)
                          * np.array(actor.world_scale)[np.newaxis, :])
        matrix[:3, 3] = actor.world_location
        placements.append((actor.name or actor.mesh_name,
                           actor.mesh_name, matrix))

    out_path = os.path.join(export_dir, "Levels", f"{level_name}.glb")
    mesh_count, node_count = export_level_glb(meshes, placements, out_path,
                                              scale=scale)
    if not node_count:
        print("  Nothing to write")
        return None

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"  Wrote {out_path}")
    print(f"  {mesh_count} meshes, {node_count} placed instances, "
          f"{size_mb:.1f} MB")
    return out_path


def find_umap_path(input_dir, umap_filename):
    """Find a .umap file in the input directory by filename."""
    uassets, umaps = find_uasset_files(input_dir)
    for u in umaps:
        if os.path.basename(u).lower() == umap_filename.lower():
            return u
    # Try with .uasset extension (umap files might be stored as uasset)
    for u in uassets:
        if os.path.basename(u).lower() == umap_filename.lower().replace('.umap', '.uasset'):
            return u
    return None


def preview_glb(glb_path, port=_PREVIEW_PORT):
    """Serve a browser preview of one already-converted GLB.

    Nothing is parsed or converted here — the file is shown as it was written,
    which is the point of looking at it.  Units come from the file itself, so
    no project and no ``--scale`` are involved.
    """
    from uasset.preview_server import start_glb_server

    print("=" * 60)
    print("UE 5.5 GLB Previewer")
    print("=" * 60)
    print(f"Model: {glb_path}")

    start_glb_server(glb_path=glb_path, port=port)


def preview_level(input_dir, export_dir, umap_filename,
                  scale=_UE_TO_GLTF_SCALE, port=_PREVIEW_PORT):
    """Parse a .umap file and show a 3D preview in the browser.

    ``scale`` must match the factor the GLBs in ``export_dir`` were exported
    with — the viewer places actors in the units their geometry is baked in.
    """
    from uasset.preview_server import start_server

    # Find the umap file
    umap_path = find_umap_path(input_dir, umap_filename)
    if not umap_path:
        print(f"ERROR: Level file '{umap_filename}' not found in {input_dir}")
        sys.exit(1)

    start_server(
        umap_path=umap_path,
        export_dir=export_dir,
        content_dir=input_dir,
        port=port,
        scale=scale,
    )


def main():
    parser = argparse.ArgumentParser(
        description="UE 5.5 UAsset Parser, Exporter, and Level Previewer",
        epilog=_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        'input_dir', nargs='?', default='./Input',
        help='Path to folder with .uproject and Content/ (default: ./Input)'
    )
    parser.add_argument(
        '--preview', metavar='LEVEL.umap',
        help='Parse a .umap level and show 3D preview'
    )
    parser.add_argument(
        '--preview-glb', metavar='FILE.glb',
        help='Preview an already-converted .glb as it is — no project or '
             'export needed. Use this to check a conversion.'
    )
    parser.add_argument(
        '--export-dir', default='./Export',
        help='Output directory (default: ./Export)'
    )
    parser.add_argument(
        '--skip-export', action='store_true',
        help='Skip export step, use existing Export/ directory'
    )
    parser.add_argument(
        '--skip-textures', action='store_true',
        help='Skip texture export and embedding (meshes only, no PNGs)'
    )
    parser.add_argument(
        '--filter', metavar='SUBSTRING', dest='mesh_filter',
        help='Only export meshes whose name contains this substring (case-insensitive)'
    )
    parser.add_argument(
        '--export-level', metavar='LEVEL.umap',
        help='Assemble the level into one positioned GLB in Export/Levels/'
    )
    parser.add_argument(
        '--port', type=int, default=_PREVIEW_PORT,
        help='Port for the preview server (default %(default)s). Use another '
             'one to run a second preview alongside the first.'
    )
    parser.add_argument(
        '--scale', type=float, default=_UE_TO_GLTF_SCALE,
        help='UE-unit to glTF-unit scale (default %(default)s: UE centimetres '
             'to glTF metres). Pass 1.0 to keep UE centimetres.'
    )

    # A bare invocation is far more likely to be someone asking "what does this
    # do?" than a request to export ./Input, so show the usage instead.
    if len(sys.argv) == 1:
        parser.print_help()
        return

    args = parser.parse_args()

    if args.scale <= 0:
        print(f"ERROR: --scale must be positive, got {args.scale}")
        sys.exit(1)

    # Previewing a converted GLB stands alone: it reads one finished file, so
    # it runs before — and instead of — everything the project path needs.
    if args.preview_glb:
        if args.preview:
            print("ERROR: --preview and --preview-glb are alternatives; "
                  "pass one or the other")
            sys.exit(1)
        glb_path = os.path.abspath(args.preview_glb)
        if not os.path.isfile(glb_path):
            print(f"ERROR: GLB not found: {glb_path}")
            sys.exit(1)
        if not glb_path.lower().endswith('.glb'):
            print(f"ERROR: --preview-glb expects a .glb file, got {glb_path}")
            sys.exit(1)
        preview_glb(glb_path, port=args.port)
        return

    input_dir = os.path.abspath(args.input_dir)
    export_dir = os.path.abspath(args.export_dir)

    if not os.path.isdir(input_dir):
        print(f"ERROR: Input directory not found: {input_dir}")
        sys.exit(1)

    print("=" * 60)
    print("UE 5.5 UAsset Parser, Exporter, and Level Previewer")
    print("=" * 60)
    print(f"Input:  {input_dir}")
    print(f"Output: {export_dir}")

    # Check .uproject
    find_uproject(input_dir)

    # Export assets
    context = None
    if not args.skip_export:
        context = process_assets(input_dir, export_dir,
                                 skip_textures=args.skip_textures,
                                 mesh_filter=args.mesh_filter,
                                 scale=args.scale)
    else:
        print("Skipping export (using existing Export/ directory)")

    # Assemble a level into one positioned GLB
    if args.export_level:
        if context is None:
            # --skip-export still needs the asset index and texture pixels;
            # the pickle cache makes this cheap on a second run.
            context = process_assets(input_dir, export_dir,
                                     skip_textures=True, skip_meshes=True,
                                     scale=args.scale)
        export_level(input_dir, export_dir, args.export_level, context,
                     scale=args.scale)

    # Preview if requested
    if args.preview:
        print(f"\n{'=' * 60}")
        preview_level(input_dir, export_dir, args.preview, scale=args.scale,
                      port=args.port)


if __name__ == "__main__":
    main()

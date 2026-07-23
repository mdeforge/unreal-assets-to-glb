"""Asset-based texture resolution for UE5 mesh export.

Provides utilities to walk the UE asset import chain
(mesh → material → texture) and resolve base-color textures
for GLB export.  Used by main.py during the export pipeline.

Texture resolution walks the parent chain (MI → parent MI → … → master
Material), collecting the ``TextureParameterValues`` overrides each level
declares and the texture samplers the master Material's BaseColor input
reads from.  Matching the two is what identifies the base colour: the
master says which parameter feeds BaseColor, the instance says which
texture that parameter holds.

Legacy preview code (MatplotlibPreviewer, PygletPreviewer, build_preview_scene,
show_preview) has been removed in favour of the browser-based preview
(preview_server.py + preview.html).
"""
import os
import struct
import logging
from collections import deque
from typing import Optional, Dict, List, Tuple

from .package import Package, UE5_PROPERTY_TAG_COMPLETE_TYPE_NAME
from .properties import (
    PropertyTag, end_property_tag, has_serialization_control_byte,
    read_property_tag,
)
from .reader import BinaryReader

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base-color parameter names (matched against *parameter* name, not texture
# filename).  These are the names UE material authors use for the diffuse /
# albedo slot inside TextureParameterValues.
# ---------------------------------------------------------------------------
_BASE_COLOR_PARAM_NAMES = frozenset({
    'BaseTexture', 'BaseColor', 'Diffuse', 'Albedo',
    'Base Color', 'DiffuseTexture',
})

# Struct type names used for the links between material expression nodes.
# FExpressionInput and the FMaterialInput<T> family all start with the
# Expression FPackageIndex, so one walker handles them all.
_EXPRESSION_INPUT_STRUCTS = frozenset({
    'ExpressionInput', 'MaterialInput',
    'ColorMaterialInput', 'ScalarMaterialInput', 'VectorMaterialInput',
    'Vector2MaterialInput', 'ShadingModelMaterialInput',
    'SubstrateMaterialInput', 'MaterialAttributesInput',
})

# How many expression nodes a single BaseColor walk may visit.
_MAX_EXPRESSION_NODES = 256


# ---------------------------------------------------------------------------
# Tagged-property helpers
#
# Every reader below goes through properties.read_property_tag, which picks
# the UE4 or UE5 tag layout from the package version.  Parsing these blocks
# by hand is what previously made all pre-5.4 packages (StarterContent) fail.
# ---------------------------------------------------------------------------

# One parsed property: its tag plus the raw bytes of its value.
TaggedProperty = Tuple[PropertyTag, bytes]

# A texture sampler in a material graph: the parameter name it exposes (if it
# is a parameter at all) and the texture it samples by default.
Sampler = Tuple[Optional[str], Optional[str]]


def _read_tagged_block(reader: BinaryReader, name_map: List[str],
                       file_version_ue5: int) -> List[TaggedProperty]:
    """Read tagged properties up to the ``None`` terminator.

    Returns a list rather than a dict because UE happily repeats a property
    name across array indices (``CustomizedUVs`` and friends).
    """
    props: List[TaggedProperty] = []
    while True:
        try:
            tag = read_property_tag(reader, name_map, file_version_ue5)
            if tag is None:
                break
            if tag.size < 0:
                break
            value = reader.read_bytes(tag.size) if tag.size else b''
            end_property_tag(reader, tag, file_version_ue5)
        except Exception:
            # Truncated or unexpected data — keep whatever parsed cleanly.
            break
        props.append((tag, value))
    return props


def _read_export_properties(pkg: Package,
                            export_index: int) -> List[TaggedProperty]:
    """Read the tagged properties at the head of an export's serialized data."""
    reader = pkg.get_export_data(export_index)
    if reader is None:
        return []
    if has_serialization_control_byte(pkg.file_version_ue5):
        reader.skip(1)
    return _read_tagged_block(reader, pkg.name_map, pkg.file_version_ue5)


def _find_prop(props: List[TaggedProperty], name: str,
               type_name: Optional[str] = None) -> Optional[TaggedProperty]:
    """Return the first property called *name*, optionally of *type_name*."""
    for tag, value in props:
        if tag.name == name and (type_name is None or tag.type_name == type_name):
            return tag, value
    return None


def _read_int32(value: bytes, offset: int = 0) -> Optional[int]:
    """Read a little-endian int32 out of a property value blob."""
    if len(value) < offset + 4:
        return None
    return struct.unpack_from('<i', value, offset)[0]


def _resolve_package_index(pkg: Package, index: Optional[int]) -> Optional[str]:
    """Resolve an FPackageIndex to the object name it points at."""
    if not index:
        return None
    if index > 0:
        exp_idx = index - 1
        if 0 <= exp_idx < len(pkg.exports):
            return pkg.exports[exp_idx].object_name
    else:
        imp_idx = -index - 1
        if 0 <= imp_idx < len(pkg.imports):
            return pkg.imports[imp_idx].object_name
    return None


def _iter_struct_array(pkg: Package, value: bytes):
    """Yield the tagged properties of each element of an array-of-struct value.

    The element count is followed by the elements themselves, except before
    PROPERTY_TAG_COMPLETE_TYPE_NAME, where UE writes one extra FPropertyTag
    describing the element type first (the outer tag's type tree carries that
    information from 5.4 on).
    """
    r = BinaryReader(value)
    try:
        count = r.read_int32()
        if pkg.file_version_ue5 < UE5_PROPERTY_TAG_COMPLETE_TYPE_NAME:
            read_property_tag(r, pkg.name_map, pkg.file_version_ue5)
    except Exception:
        return
    for _ in range(count):
        element = _read_tagged_block(r, pkg.name_map, pkg.file_version_ue5)
        if not element:
            return
        yield element


def _find_material_instance_export(pkg: Package) -> Optional[int]:
    """Index of the MaterialInstanceConstant export, if the package has one.

    Master Materials do not, which is how the callers tell the two apart —
    only instances carry parameter overrides and a parent.
    """
    for i in range(pkg.export_count):
        if pkg.get_export_class_name(i) == 'MaterialInstanceConstant':
            return i
    return None


def _parameter_info_name(pkg: Package, element: List[TaggedProperty]
                         ) -> Optional[str]:
    """Read ``ParameterInfo.Name`` out of an F*ParameterValue element."""
    info = _find_prop(element, 'ParameterInfo', 'StructProperty')
    if info is None:
        return None
    inner = _read_tagged_block(BinaryReader(info[1]), pkg.name_map,
                               pkg.file_version_ue5)
    entry = _find_prop(inner, 'Name', 'NameProperty')
    if entry is None or len(entry[1]) < 8:
        return None
    idx = struct.unpack_from('<i', entry[1], 0)[0]
    return pkg.name_map[idx] if 0 <= idx < len(pkg.name_map) else None


def _parse_parameter_values(pkg: Package, array_name: str, value_type: str):
    """Parse one of the F*ParameterValue arrays of a material instance.

    Yields ``(parameter_name, ParameterValue_bytes)`` pairs.
    """
    export_index = _find_material_instance_export(pkg)
    if export_index is None:
        return
    props = _read_export_properties(pkg, export_index)
    entry = _find_prop(props, array_name, 'ArrayProperty')
    if entry is None:
        return
    for element in _iter_struct_array(pkg, entry[1]):
        name = _parameter_info_name(pkg, element)
        value = _find_prop(element, 'ParameterValue', value_type)
        if name is not None and value is not None:
            yield name, value[1]


def _parse_texture_parameter_values(pkg: Package) -> Dict[str, str]:
    """Parse ``TextureParameterValues`` from a material instance package.

    Returns ``{parameter_name: texture_asset_name}`` for every texture
    parameter override present in the material instance export data.
    """
    result: Dict[str, str] = {}
    for name, value in _parse_parameter_values(
            pkg, 'TextureParameterValues', 'ObjectProperty'):
        texture = _resolve_package_index(pkg, _read_int32(value))
        if texture is not None:
            result[name] = texture
    return result


def _parse_scalar_parameter_values(pkg: Package) -> Dict[str, float]:
    """Parse ``ScalarParameterValues`` from a material instance package.

    Returns ``{parameter_name: float_value}`` for every scalar parameter
    override present in the material instance export data.
    """
    result: Dict[str, float] = {}
    for name, value in _parse_parameter_values(
            pkg, 'ScalarParameterValues', 'FloatProperty'):
        if len(value) >= 4:
            result[name] = struct.unpack_from('<f', value, 0)[0]
    return result


# ---------------------------------------------------------------------------
# Asset index & material resolution
# ---------------------------------------------------------------------------

def _build_uasset_index(input_dir: str) -> Dict[str, str]:
    """Scan *input_dir* for .uasset files and return ``{asset name: filepath}``.

    The key is the *object name* of each export flagged ``bIsAsset`` in the
    package — the name other packages use to import it.  It is not the file
    name: ``MI_SpaceShip_1.uasset`` can perfectly well contain an asset called
    ``MI_SpaceShip``, and a mesh importing that material refers to it by the
    latter.  File names are registered too, but only for names no package
    claimed, so unparsable packages stay reachable.

    Where several packages export the same asset name, the lexicographically
    first path wins so that repeated runs resolve identically.

    Args:
        input_dir: Project root containing ``Content/`` or the Content
            directory itself.

    Returns:
        Dictionary mapping asset name to the package file that provides it.
    """
    index: Dict[str, str] = {}
    by_filename: Dict[str, str] = {}

    content_dir = os.path.join(input_dir, 'Content')
    if not os.path.isdir(content_dir):
        content_dir = input_dir

    for root, _dirs, files in os.walk(content_dir):
        for f in files:
            if not f.endswith('.uasset'):
                continue
            path = os.path.join(root, f)
            stem = os.path.splitext(f)[0]
            if stem not in by_filename or path < by_filename[stem]:
                by_filename[stem] = path
            try:
                pkg = Package(path)
            except Exception as e:
                logger.debug(f"Failed to index '{path}': {e}")
                continue
            for entry in pkg.exports:
                if not entry.b_is_asset:
                    continue
                previous = index.get(entry.object_name)
                if previous is None:
                    index[entry.object_name] = path
                elif path < previous:
                    logger.debug(
                        f"Asset '{entry.object_name}' provided by both "
                        f"'{previous}' and '{path}' — using the latter")
                    index[entry.object_name] = path

    for stem, path in by_filename.items():
        index.setdefault(stem, path)

    return index


def _get_material_names_from_mesh(mesh_name: str,
                                  uasset_index: Dict[str, str]) -> List[str]:
    """Return the ordered list of material asset names referenced by a mesh.

    Opens the mesh's .uasset package and collects all imports whose class
    is ``MaterialInstanceConstant`` or ``Material``.

    Args:
        mesh_name:      Static mesh asset name (no extension).
        uasset_index:   Index from :func:`_build_uasset_index`.

    Returns:
        List of material object names, possibly empty.
    """
    filepath = uasset_index.get(mesh_name)
    if filepath is None:
        return []
    try:
        pkg = Package(filepath)
        return [imp.object_name for imp in pkg.imports
                if imp.class_name in ('MaterialInstanceConstant', 'Material')]
    except Exception as e:
        logger.debug(f"Failed to read mesh package for '{mesh_name}': {e}")
        return []


def _find_parent_material_name(pkg: Package,
                               material_name: str) -> Optional[str]:
    """Find the material a material instance inherits from.

    Reads the ``Parent`` ObjectProperty out of the MaterialInstanceConstant
    export.  Falls back to scanning the import table for a material other
    than *material_name* when the property is absent.  Master Materials have
    no parent, so they return ``None`` rather than the first material they
    happen to reference.
    """
    export_index = _find_material_instance_export(pkg)
    if export_index is None:
        return None

    props = _read_export_properties(pkg, export_index)
    entry = _find_prop(props, 'Parent', 'ObjectProperty')
    if entry is not None:
        parent = _resolve_package_index(pkg, _read_int32(entry[1]))
        if parent is not None and parent != material_name:
            return parent

    for imp in pkg.imports:
        if (imp.class_name in ('MaterialInstanceConstant', 'Material')
                and imp.object_name != material_name):
            return imp.object_name
    return None


def _lookup_texture(tex_map: Dict[str, str], name: str) -> Optional[str]:
    """Look *name* up in *tex_map*, falling back to a case-insensitive match."""
    mapped = tex_map.get(name)
    if mapped is not None:
        return mapped
    lowered = name.lower()
    for exported_name, exported in tex_map.items():
        if exported_name.lower() == lowered:
            return exported
    return None


def _get_base_color_texture_from_material(material_name: str,
                                          uasset_index: Dict[str, str],
                                          tex_map: Dict[str, str]
                                          ) -> Optional[str]:
    """Resolve a base-colour texture by walking the material parent chain.

    The chain is walked from the material itself up to the master Material it
    ultimately derives from (at most 8 parents), collecting two things: the
    ``TextureParameterValues`` overrides declared at each level, nearest
    material winning, and the texture samplers the master's BaseColor input
    actually reads from.

    Those two halves are what makes a material instance render: the master
    says "base colour comes from parameter *BC*", the instance says
    "*BC* is T_Ship_B".  Matching them is preferred over every other signal.
    Failing that the master's own default texture is used, then a parameter
    named after a base-colour slot (``BaseColor``, ``Diffuse``, ``Albedo``, …)
    for materials whose master is missing from the project, and finally any
    texture parameter at all.

    Args:
        material_name:  Material asset name.
        uasset_index:   Index from :func:`_build_uasset_index`.
        tex_map:        Mapping of texture asset name → exported name / identifier.

    Returns:
        The resolved texture name from *tex_map*, or ``None``.
    """
    overrides: Dict[str, str] = {}
    samplers: List[Sampler] = []
    visited = set()
    name: Optional[str] = material_name

    while name is not None and name not in visited and len(visited) <= 8:
        visited.add(name)
        filepath = uasset_index.get(name)
        if filepath is None:
            break
        try:
            pkg = Package(filepath)
        except Exception as e:
            logger.debug(f"Failed to open material package for '{name}': {e}")
            break

        for param_name, tex_asset_name in _parse_texture_parameter_values(pkg).items():
            # The nearest instance in the chain wins.
            overrides.setdefault(param_name, tex_asset_name)
        if not samplers:
            samplers = _get_base_color_samplers(pkg)
        name = _find_parent_material_name(pkg, name)

    logger.debug(f"Material '{material_name}': overrides={overrides} "
                 f"base-color samplers={samplers}")

    # 1. A sampler the BaseColor input reads from, overridden by the instance.
    for param_name, _default in samplers:
        override = overrides.get(param_name) if param_name else None
        mapped = _lookup_texture(tex_map, override) if override else None
        if mapped is not None:
            logger.debug(f"  '{material_name}': BaseColor parameter "
                         f"'{param_name}' → '{overrides[param_name]}'")
            return mapped

    # 2. The texture the master material samples by default.
    for _param_name, default in samplers:
        mapped = _lookup_texture(tex_map, default) if default else None
        if mapped is not None:
            logger.debug(f"  '{material_name}': master-material BaseColor "
                         f"default texture '{default}'")
            return mapped

    # 3. No usable graph — fall back to a parameter named like a base-colour
    #    slot, which is all there is to go on when the master material lives
    #    outside the project.
    for param_name, tex_asset_name in overrides.items():
        if param_name in _BASE_COLOR_PARAM_NAMES:
            mapped = _lookup_texture(tex_map, tex_asset_name)
            if mapped is not None:
                logger.debug(f"  '{material_name}': base color param "
                             f"'{param_name}' → texture '{tex_asset_name}'")
                return mapped

    # 4. Last resort: any texture parameter that maps into tex_map.
    for param_name, tex_asset_name in overrides.items():
        mapped = _lookup_texture(tex_map, tex_asset_name)
        if mapped is not None:
            logger.debug(f"  '{material_name}': fallback to first available "
                         f"texture '{tex_asset_name}' (parameter '{param_name}')")
            return mapped

    return None


def _get_base_color_samplers(pkg: Package) -> List[Sampler]:
    """List the texture samplers a master Material's BaseColor input reads.

    Walks the chain:
      MaterialEditorOnlyData -> BaseColor (FColorMaterialInput)
        -> Expression (FPackageIndex -> material expression export)
          -> … -> texture samplers

    A material with ``bUseMaterialAttributes`` set leaves BaseColor
    unconnected and feeds everything through ``MaterialAttributes`` instead,
    so that input is searched as well.

    Returns an empty list for material instances, which have no expression
    graph of their own, and for masters whose base colour is unconnected.
    """
    eod_idx = None
    for i in range(pkg.export_count):
        if pkg.get_export_class_name(i) == 'MaterialEditorOnlyData':
            eod_idx = i
            break
    if eod_idx is None:
        return []

    props = _read_export_properties(pkg, eod_idx)
    for input_name in ('BaseColor', 'MaterialAttributes'):
        entry = _find_prop(props, input_name, 'StructProperty')
        if entry is None or entry[0].struct_name not in _EXPRESSION_INPUT_STRUCTS:
            continue
        # FMaterialInput opens with the Expression FPackageIndex.
        samplers = _collect_samplers(pkg, _read_int32(entry[1]))
        if samplers:
            return samplers

    return []


def _read_sampler(pkg: Package,
                  props: List[TaggedProperty]) -> Optional[Sampler]:
    """Read ``(parameter name, default texture)`` from an expression export.

    Texture samplers — ``MaterialExpressionTextureSample`` and the parameter
    variants that derive from it — all expose the same ``Texture``
    ObjectProperty, so that property is what identifies a sampler here rather
    than a list of class names.  Returns ``None`` for any other expression.
    """
    entry = _find_prop(props, 'Texture', 'ObjectProperty')
    if entry is None:
        return None

    texture = None
    index = _read_int32(entry[1])
    # Textures used by a material live in another package, so the reference
    # is always an import.
    if index is not None and index < 0:
        imp_idx = -index - 1
        if (0 <= imp_idx < len(pkg.imports)
                and pkg.imports[imp_idx].class_name == 'Texture2D'):
            texture = pkg.imports[imp_idx].object_name

    parameter = None
    named = _find_prop(props, 'ParameterName', 'NameProperty')
    if named is not None:
        name_idx = _read_int32(named[1])
        if name_idx is not None and 0 <= name_idx < len(pkg.name_map):
            parameter = pkg.name_map[name_idx]

    if parameter is None and texture is None:
        return None
    return parameter, texture


def _collect_samplers(pkg: Package, root: Optional[int]) -> List[Sampler]:
    """Breadth-first search from a material input for texture samplers.

    A base-colour input rarely points straight at a sampler — tint multiplies
    and blend lerps sit in between, and a blend reads more than one texture.
    Searching breadth-first returns them ordered by distance from the material
    output.  Evaluating the graph is out of scope; this only locates textures.
    """
    queue = deque([root])
    visited = set()
    samplers: List[Sampler] = []

    while queue and len(visited) < _MAX_EXPRESSION_NODES:
        index = queue.popleft()
        # Expressions are exports of the material package; a non-positive
        # index means the input is unconnected or refers elsewhere.
        if not index or index <= 0 or index in visited:
            continue
        visited.add(index)

        exp_idx = index - 1
        if not 0 <= exp_idx < len(pkg.exports):
            continue

        props = _read_export_properties(pkg, exp_idx)

        sampler = _read_sampler(pkg, props)
        if sampler is not None:
            samplers.append(sampler)

        for tag, value in props:
            if (tag.type_name == 'StructProperty'
                    and tag.struct_name in _EXPRESSION_INPUT_STRUCTS):
                queue.append(_read_int32(value))

    return samplers


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

# Expressions that stand in for the result of a material function: whatever
# feeds these is what the function returns to its caller.
_FUNCTION_OUTPUT_CLASSES = frozenset({
    'MaterialExpressionFunctionOutput',
    'MaterialExpressionMaterialLayerOutput',
})

# EMaterialParameterAssociation, as serialized in FMaterialParameterInfo.
_GLOBAL_PARAMETER = 'GlobalParameter'
_LAYER_PARAMETER = 'LayerParameter'

# Exports that carry parameter overrides for something else's graph.
_PARAMETER_OWNER_CLASSES = frozenset({
    'MaterialInstanceConstant',
    'MaterialFunctionMaterialLayerInstance',
    'MaterialFunctionMaterialLayerBlendInstance',
})

# How many expression nodes one base-colour search may visit, across every
# package it follows into.
_MAX_EXPRESSION_NODES = 512

# How deep a chain of material functions calling material functions may go.
_MAX_FUNCTION_DEPTH = 8


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

# How FMaterialParameterInfo identifies a parameter override: its name, which
# kind of slot it belongs to, and — for layer parameters — which layer.
ParameterKey = Tuple[str, str, int]


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


def _import_package_path(pkg: Package, index: Optional[int]) -> Optional[str]:
    """The ``/Game/...`` path of the package an import lives in.

    An import's outer chain ends at the Package import naming it, which is
    what distinguishes two assets that share a name.
    """
    if index is None or index >= 0:
        return None
    for _ in range(16):
        imp_idx = -index - 1
        if not 0 <= imp_idx < len(pkg.imports):
            return None
        imp = pkg.imports[imp_idx]
        if imp.class_name == 'Package':
            return imp.object_name
        index = imp.outer_index
        if index >= 0:
            return None
    return None


def _texture_reference(pkg: Package, index: Optional[int]) -> Optional[str]:
    """How a texture reference is identified downstream.

    The package path where the reference carries one, since asset names are
    not unique across a project — ``T_Statue_M`` exists twice in StarterContent
    alone.  Falls back to the bare object name, which is all a same-package
    reference has.
    """
    return (_import_package_path(pkg, index)
            or _resolve_package_index(pkg, index))


def package_path_for_file(input_dir: str, filepath: str) -> Optional[str]:
    """The ``/Game/...`` path UE knows a package file by.

    A project's ``Content/`` directory is mounted at ``/Game/``, so the path
    follows from the file's location beneath it.  Returns None for anything
    outside that tree.
    """
    content_dir = os.path.join(input_dir, 'Content')
    if not os.path.isdir(content_dir):
        content_dir = input_dir
    try:
        relative = os.path.relpath(filepath, content_dir)
    except ValueError:
        return None      # different drive on Windows
    if relative.startswith(os.pardir):
        return None
    relative = os.path.splitext(relative)[0].replace(os.sep, '/')
    return f"/Game/{relative}"


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


def _iter_object_array(pkg: Package, value: bytes) -> List[int]:
    """Read an array of ObjectProperty values as FPackageIndex ints.

    Unlike an array of structs this carries no inner FPropertyTag in any
    version — the count is followed straight by the indices.
    """
    r = BinaryReader(value)
    try:
        count = r.read_int32()
        return [r.read_int32() for _ in range(count)]
    except Exception:
        return []


def _read_fname_value(pkg: Package, value: bytes) -> Optional[str]:
    """Resolve a property value that holds an FName."""
    idx = _read_int32(value)
    if idx is None or not 0 <= idx < len(pkg.name_map):
        return None
    return pkg.name_map[idx]


def _find_material_instance_export(pkg: Package) -> Optional[int]:
    """Index of the MaterialInstanceConstant export, if the package has one.

    Master Materials do not, which is how the callers tell the two apart —
    only instances carry a parent and a layer stack.
    """
    for i in range(pkg.export_count):
        if pkg.get_export_class_name(i) == 'MaterialInstanceConstant':
            return i
    return None


def _find_parameter_export(pkg: Package) -> Optional[int]:
    """Index of the export holding parameter overrides, if there is one.

    Material instances are not the only thing that carries them: a material
    layer is normally used through a layer *instance*, which supplies values
    for the layer function's parameters the same way.
    """
    for i in range(pkg.export_count):
        if pkg.get_export_class_name(i) in _PARAMETER_OWNER_CLASSES:
            return i
    return None


def _function_parent_name(pkg: Package) -> Optional[str]:
    """The material function an instance of one derives from."""
    index = _find_parameter_export(pkg)
    if index is None:
        return None
    entry = _find_prop(_read_export_properties(pkg, index),
                       'Parent', 'ObjectProperty')
    if entry is None:
        return None
    return _resolve_package_index(pkg, _read_int32(entry[1]))


def _parameter_info(pkg: Package, element: List[TaggedProperty]
                    ) -> Optional[ParameterKey]:
    """Read ``ParameterInfo`` out of an F*ParameterValue element.

    A layered material reuses the same parameter name once per layer, so the
    name alone does not identify an override — ``Association`` and ``Index``
    say which layer it belongs to.
    """
    info = _find_prop(element, 'ParameterInfo', 'StructProperty')
    if info is None:
        return None
    inner = _read_tagged_block(BinaryReader(info[1]), pkg.name_map,
                               pkg.file_version_ue5)

    entry = _find_prop(inner, 'Name', 'NameProperty')
    name = _read_fname_value(pkg, entry[1]) if entry is not None else None
    if name is None:
        return None

    association = _GLOBAL_PARAMETER
    entry = _find_prop(inner, 'Association')
    if entry is not None:
        # A ByteProperty backed by an enum stores the value as an FName.
        resolved = (_read_fname_value(pkg, entry[1]) if len(entry[1]) >= 8
                    else None)
        if resolved is not None:
            association = resolved

    entry = _find_prop(inner, 'Index', 'IntProperty')
    index = _read_int32(entry[1]) if entry is not None else 0

    return name, association, index or 0


def _parse_parameter_values(pkg: Package, array_name: str, value_type: str):
    """Parse one of the F*ParameterValue arrays of a material instance.

    Yields ``(ParameterKey, ParameterValue_bytes)`` pairs.
    """
    export_index = _find_parameter_export(pkg)
    if export_index is None:
        return
    props = _read_export_properties(pkg, export_index)
    entry = _find_prop(props, array_name, 'ArrayProperty')
    if entry is None:
        return
    for element in _iter_struct_array(pkg, entry[1]):
        key = _parameter_info(pkg, element)
        value = _find_prop(element, 'ParameterValue', value_type)
        if key is not None and value is not None:
            yield key, value[1]


def _parse_texture_parameter_values(pkg: Package) -> Dict[ParameterKey, str]:
    """Parse ``TextureParameterValues`` from a material instance package.

    Returns ``{parameter key: texture reference}`` for every texture
    parameter override present in the material instance export data.
    """
    result: Dict[ParameterKey, str] = {}
    for key, value in _parse_parameter_values(
            pkg, 'TextureParameterValues', 'ObjectProperty'):
        texture = _texture_reference(pkg, _read_int32(value))
        if texture is not None:
            result[key] = texture
    return result


def _parse_scalar_parameter_values(pkg: Package) -> Dict[str, float]:
    """Parse ``ScalarParameterValues`` from a material instance package.

    Returns ``{parameter_name: float_value}`` for every scalar parameter
    override present in the material instance export data.
    """
    result: Dict[str, float] = {}
    for (name, _association, _index), value in _parse_parameter_values(
            pkg, 'ScalarParameterValues', 'FloatProperty'):
        if len(value) >= 4:
            result[name] = struct.unpack_from('<f', value, 0)[0]
    return result


def _parse_material_layers(pkg: Package) -> List[str]:
    """Return the material function names making up a layered instance.

    A master Material with a ``MaterialAttributeLayers`` node holds no layers
    itself — the instance supplies them through its static parameters.  Index
    0 is the base layer; the rest are blended over it.
    """
    export_index = _find_material_instance_export(pkg)
    if export_index is None:
        return []

    props = _read_export_properties(pkg, export_index)
    entry = _find_prop(props, 'StaticParametersRuntime', 'StructProperty')
    if entry is None:
        return []

    runtime = _read_tagged_block(BinaryReader(entry[1]), pkg.name_map,
                                 pkg.file_version_ue5)
    entry = _find_prop(runtime, 'MaterialLayers', 'StructProperty')
    if entry is None:
        return []

    layers = _read_tagged_block(BinaryReader(entry[1]), pkg.name_map,
                                pkg.file_version_ue5)
    entry = _find_prop(layers, 'Layers', 'ArrayProperty')
    if entry is None:
        return []

    names = []
    for index in _iter_object_array(pkg, entry[1]):
        name = _resolve_package_index(pkg, index)
        if name is not None:
            names.append(name)
    return names


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
    graph = _MaterialGraph(uasset_index)
    overrides: Dict[ParameterKey, str] = {}
    samplers: List[Sampler] = []
    layers: List[str] = []
    visited = set()
    name: Optional[str] = material_name

    while name is not None and name not in visited and len(visited) <= 8:
        visited.add(name)
        pkg = graph.package(name)
        if pkg is None:
            break

        for key, tex_asset_name in _parse_texture_parameter_values(pkg).items():
            # The nearest instance in the chain wins.
            overrides.setdefault(key, tex_asset_name)
        if not layers:
            layers = _parse_material_layers(pkg)
        if not samplers:
            samplers = _get_base_color_samplers(pkg, graph)
        name = _find_parent_material_name(pkg, name)

    # A layered material builds its base colour from stacked material layer
    # functions rather than from the master's own graph.  Layer 0 is the base
    # everything else is blended over, so that is the one that carries the
    # material's base colour, and its parameters are the ones to match.
    association, layer_index = _GLOBAL_PARAMETER, 0
    if layers:
        layer_samplers = _get_layer_base_color_samplers(layers[0], graph)
        if layer_samplers:
            samplers = layer_samplers
            association = _LAYER_PARAMETER
            logger.debug(f"Material '{material_name}': base layer "
                         f"'{layers[0]}' of {len(layers)}")

    logger.debug(f"Material '{material_name}': overrides={overrides} "
                 f"base-color samplers={samplers}")

    # 1. A sampler the base colour reads from, overridden by the instance.
    for param_name, _default in samplers:
        override = _lookup_override(overrides, param_name, association,
                                    layer_index)
        mapped = _lookup_texture(tex_map, override) if override else None
        if mapped is not None:
            logger.debug(f"  '{material_name}': base colour parameter "
                         f"'{param_name}' → '{override}'")
            return mapped

    # 2. The texture the material graph samples by default.
    for _param_name, default in samplers:
        mapped = _lookup_texture(tex_map, default) if default else None
        if mapped is not None:
            logger.debug(f"  '{material_name}': base colour default "
                         f"texture '{default}'")
            return mapped

    # 3. No usable graph — fall back to a parameter named like a base-colour
    #    slot, which is all there is to go on when the master material lives
    #    outside the project.
    for (param_name, _assoc, _index), tex_asset_name in overrides.items():
        if param_name in _BASE_COLOR_PARAM_NAMES:
            mapped = _lookup_texture(tex_map, tex_asset_name)
            if mapped is not None:
                logger.debug(f"  '{material_name}': base color param "
                             f"'{param_name}' → texture '{tex_asset_name}'")
                return mapped

    # 4. Last resort: any texture parameter that maps into tex_map.
    for (param_name, _assoc, _index), tex_asset_name in overrides.items():
        mapped = _lookup_texture(tex_map, tex_asset_name)
        if mapped is not None:
            logger.debug(f"  '{material_name}': fallback to first available "
                         f"texture '{tex_asset_name}' (parameter '{param_name}')")
            return mapped

    return None


def _lookup_override(overrides: Dict[ParameterKey, str],
                     param_name: Optional[str], association: str,
                     layer_index: int) -> Optional[str]:
    """Find the instance override for one graph parameter.

    A layer parameter belongs to exactly one layer, so another layer's value
    for the same name is a different texture rather than a fallback.  Only an
    unlayered material accepts a name-only match, where the leniency covers
    parameters whose association was not recorded.
    """
    if param_name is None:
        return None
    exact = overrides.get((param_name, association, layer_index))
    if exact is not None:
        return exact
    if association != _GLOBAL_PARAMETER:
        return None
    for (name, _assoc, _index), texture in overrides.items():
        if name == param_name:
            return texture
    return None


def _get_layer_base_color_samplers(layer_name: str,
                                   graph: _MaterialGraph) -> List[Sampler]:
    """Samplers feeding the base colour of one material layer.

    A layer slot normally holds a layer *instance*, which contributes only
    parameter values — the graph lives in the layer function it derives from,
    so ``Parent`` is followed until one turns up.  The values collected on the
    way replace the function's own defaults, since that is what the layer
    actually samples.  The material instance can still override them again.
    """
    overrides: Dict[str, str] = {}
    name: Optional[str] = layer_name
    visited = set()

    while name is not None and name not in visited and len(visited) <= _MAX_FUNCTION_DEPTH:
        visited.add(name)
        pkg = graph.package(name)
        if pkg is None:
            return []

        for (param, _assoc, _index), texture in \
                _parse_texture_parameter_values(pkg).items():
            # The nearest layer instance in the chain wins.
            overrides.setdefault(param, texture)

        samplers = graph.function_samplers(pkg)
        if samplers:
            return [(param, overrides.get(param, default) if param else default)
                    for param, default in samplers]

        # No graph here — this is an instance of another layer function.
        name = _function_parent_name(pkg)

    return []


def _get_base_color_samplers(pkg: Package,
                             graph: _MaterialGraph) -> List[Sampler]:
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
        samplers = graph.samplers_from(pkg, _read_int32(entry[1]))
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
            texture = _texture_reference(pkg, index)

    parameter = None
    named = _find_prop(props, 'ParameterName', 'NameProperty')
    if named is not None:
        name_idx = _read_int32(named[1])
        if name_idx is not None and 0 <= name_idx < len(pkg.name_map):
            parameter = pkg.name_map[name_idx]

    if parameter is None and texture is None:
        return None
    return parameter, texture


class _MaterialGraph:
    """Breadth-first search for texture samplers across material packages.

    A base-colour input rarely points straight at a sampler — tint multiplies
    and blend lerps sit in between, a blend reads more than one texture, and
    the sampler itself often lives in a material function in another package.
    Searching breadth-first returns samplers ordered by distance from the
    material output.  Evaluating the graph is out of scope; this only locates
    textures.

    One instance per resolution, so packages opened along the way are read
    once however many times the walk revisits them.
    """

    def __init__(self, uasset_index: Dict[str, str]):
        self._index = uasset_index
        self._packages: Dict[str, Optional[Package]] = {}

    def package(self, asset_name: Optional[str]) -> Optional[Package]:
        """Open the package providing *asset_name*, or None if unavailable.

        Engine content is not part of the project, so its material functions
        simply resolve to None and that branch of the walk stops.
        """
        if asset_name is None:
            return None
        if asset_name not in self._packages:
            filepath = self._index.get(asset_name)
            package = None
            if filepath is not None:
                try:
                    package = Package(filepath)
                except Exception as e:
                    logger.debug(f"Failed to open '{asset_name}': {e}")
            self._packages[asset_name] = package
        return self._packages[asset_name]

    def samplers_from(self, pkg: Package, root: Optional[int]) -> List[Sampler]:
        """Collect the samplers reachable from one expression input."""
        return self._search(deque([(pkg, root, 0)]))

    def function_samplers(self, pkg: Package, depth: int = 0) -> List[Sampler]:
        """Collect the samplers a material function's base colour reads."""
        return self._search(deque(
            (pkg, root, depth) for root in _function_base_color_roots(pkg)))

    def _search(self, queue) -> List[Sampler]:
        visited = set()
        samplers: List[Sampler] = []

        while queue and len(visited) < _MAX_EXPRESSION_NODES:
            pkg, index, depth = queue.popleft()
            # Expressions are exports of their own package; a non-positive
            # index means the input is unconnected or refers elsewhere.
            if not index or index <= 0:
                continue
            step = (pkg.filepath, index)
            if step in visited:
                continue
            visited.add(step)

            exp_idx = index - 1
            if not 0 <= exp_idx < len(pkg.exports):
                continue

            props = _read_export_properties(pkg, exp_idx)

            sampler = _read_sampler(pkg, props)
            if sampler is not None:
                samplers.append(sampler)

            # A function call continues the search inside the function it
            # names, starting from whatever that function outputs.
            if depth < _MAX_FUNCTION_DEPTH:
                called = self._called_function(pkg, props)
                if called is not None:
                    for root in _function_base_color_roots(called):
                        queue.append((called, root, depth + 1))

            for expression in _expression_inputs(pkg, props):
                queue.append((pkg, expression, depth))

        return samplers

    def _called_function(self, pkg: Package,
                         props: List[TaggedProperty]) -> Optional[Package]:
        """The package of the material function this expression calls."""
        entry = _find_prop(props, 'MaterialFunction', 'ObjectProperty')
        if entry is None:
            return None
        return self.package(_resolve_package_index(pkg, _read_int32(entry[1])))


def _expression_inputs(pkg: Package,
                       props: List[TaggedProperty]) -> List[Optional[int]]:
    """Every expression an node's inputs point at.

    Inputs also hide inside arrays of structs — ``SetMaterialAttributes.Inputs``
    and ``MaterialFunctionCall.FunctionInputs`` both work that way — so those
    are unpacked rather than skipped.
    """
    inputs: List[Optional[int]] = []
    for tag, value in props:
        if tag.type_name == 'StructProperty':
            if tag.struct_name in _EXPRESSION_INPUT_STRUCTS:
                inputs.append(_read_int32(value))
        elif tag.type_name == 'ArrayProperty' and 'StructProperty' in tag.inner_types:
            for element in _iter_struct_array(pkg, value):
                for inner, inner_value in element:
                    if (inner.type_name == 'StructProperty'
                            and inner.struct_name in _EXPRESSION_INPUT_STRUCTS):
                        inputs.append(_read_int32(inner_value))
    return inputs


def _function_base_color_roots(pkg: Package) -> List[int]:
    """Where to start searching a material function for its base colour.

    A layer function assembles its result with MakeMaterialAttributes, whose
    BaseColor input names the base colour exactly.  Plain functions have no
    such node, so their output expressions are used instead and the search
    covers everything they return.
    """
    roots: List[int] = []
    outputs: List[int] = []

    for i in range(pkg.export_count):
        class_name = pkg.get_export_class_name(i)
        if class_name == 'MaterialExpressionMakeMaterialAttributes':
            entry = _find_prop(_read_export_properties(pkg, i),
                               'BaseColor', 'StructProperty')
            if entry is not None:
                index = _read_int32(entry[1])
                if index:
                    roots.append(index)
        elif class_name in _FUNCTION_OUTPUT_CLASSES:
            outputs.append(i + 1)

    return roots or outputs


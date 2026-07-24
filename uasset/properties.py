"""Property tag parser/skipper for UE5 .uasset files.

Handles both old format (UE4 style) and new format (UE5 >= PROPERTY_TAG_COMPLETE_TYPE_NAME).
Provides both skip_properties (for mesh/texture parsing) and read_properties (for umap parsing).
"""
from .reader import BinaryReader, resolve_fname
from .package import (
    UE5_PROPERTY_TAG_COMPLETE_TYPE_NAME,
    UE5_PROPERTY_TAG_EXTENSION,
)


# EPropertyTagFlags bits
TAG_HasArrayIndex = 0x01
TAG_HasPropertyGuid = 0x02
TAG_HasPropertyExtensions = 0x04
TAG_HasBinaryOrNativeSerialize = 0x08
TAG_BoolTrue = 0x10
TAG_SkippedSerialize = 0x20


def _read_property_type_name(r: BinaryReader, name_map):
    """Read FPropertyTypeName (tree of FName + InnerCount nodes)."""
    total_nodes = 1
    type_name = ""
    i = 0
    while i < total_nodes:
        idx = r.read_int32()
        num = r.read_int32()  # FName number
        inner_count = r.read_int32()
        name = resolve_fname(name_map, idx, num)
        if i == 0:
            type_name = name
        total_nodes += inner_count
        i += 1
    return type_name


def _read_property_type_tree(r: BinaryReader, name_map):
    """Read FPropertyTypeName tree and return (root_name, [inner_names]).

    Reads exactly the same bytes as _read_property_type_name but captures
    inner type names (e.g. struct type name for StructProperty).
    """
    total_nodes = 1
    names = []
    i = 0
    while i < total_nodes:
        idx = r.read_int32()
        num = r.read_int32()
        inner_count = r.read_int32()
        name = resolve_fname(name_map, idx, num)
        names.append(name)
        total_nodes += inner_count
        i += 1
    root = names[0] if names else ""
    inner = names[1:] if len(names) > 1 else []
    return root, inner


# ---------------------------------------------------------------------------
# FPropertyTag header reader (version-aware)
# ---------------------------------------------------------------------------

def has_serialization_control_byte(file_version_ue5: int) -> bool:
    """Whether export data starts with a serialization-control byte.

    UE5 writes this byte ahead of the tagged-property block from
    PROPERTY_TAG_EXTENSION onwards.  Older packages — including engine content
    such as StarterContent — begin directly with the first FPropertyTag.
    """
    return file_version_ue5 >= UE5_PROPERTY_TAG_EXTENSION


class PropertyTag:
    """One FPropertyTag header, parsed in either the UE4 or the UE5 layout."""

    __slots__ = ('name', 'type_name', 'inner_types', 'size', 'flags',
                 'array_index', 'bool_value', 'new_format')

    def __init__(self, name, type_name, inner_types, size, flags,
                 array_index, bool_value, new_format):
        self.name = name
        self.type_name = type_name
        self.inner_types = inner_types
        self.size = size
        self.flags = flags
        self.array_index = array_index
        self.bool_value = bool_value
        self.new_format = new_format

    @property
    def struct_name(self) -> str:
        """Inner struct/enum type name, or "" when the tag carries none."""
        return self.inner_types[0] if self.inner_types else ""


def read_property_tag(reader: BinaryReader, name_map,
                      file_version_ue5: int):
    """Read the next FPropertyTag header.

    Returns None at the ``None`` terminator or when the stream is exhausted.
    On success the reader is left at the first byte of the value data, so the
    caller can parse it or ``skip(tag.size)``.  Call :func:`end_property_tag`
    once the value has been consumed.

    Reads the same byte sequences as :func:`skip_properties`, but hands the tag
    back instead of discarding it.
    """
    if not reader.can_read(8):
        return None

    name_idx = reader.read_int32()
    name_num = reader.read_int32()
    name = resolve_fname(name_map, name_idx, name_num)
    if name == "None":
        return None

    if file_version_ue5 >= UE5_PROPERTY_TAG_COMPLETE_TYPE_NAME:
        # New format: FPropertyTypeName tree + Size + Flags byte
        type_name, inner_types = _read_property_type_tree(reader, name_map)
        size = reader.read_int32()
        flags = reader.read_uint8()
        array_index = reader.read_int32() if flags & TAG_HasArrayIndex else 0
        return PropertyTag(name, type_name, inner_types, size, flags,
                           array_index, bool(flags & TAG_BoolTrue), True)

    # Old format: FName type + Size + ArrayIndex + type-specific header, with
    # HasPropertyGuid ahead of the value data rather than after it.
    type_idx = reader.read_int32()
    type_num = reader.read_int32()
    type_name = resolve_fname(name_map, type_idx, type_num)

    size = reader.read_int32()
    array_index = reader.read_int32()

    inner_types = []
    bool_value = False
    if type_name == "StructProperty":
        struct_idx = reader.read_int32()
        struct_num = reader.read_int32()
        inner_types = [resolve_fname(name_map, struct_idx, struct_num)]
        reader.skip(16)  # StructGuid
    elif type_name == "BoolProperty":
        bool_value = reader.read_uint8() != 0
    elif type_name in ("ByteProperty", "EnumProperty"):
        enum_idx = reader.read_int32()
        enum_num = reader.read_int32()
        inner_types = [resolve_fname(name_map, enum_idx, enum_num)]
    elif type_name in ("ArrayProperty", "SetProperty"):
        reader.skip(8)   # InnerType FName
    elif type_name == "MapProperty":
        reader.skip(16)  # InnerType + ValueType FNames

    if reader.read_uint8():
        reader.skip(16)  # PropertyGuid

    return PropertyTag(name, type_name, inner_types, size,
                       TAG_BoolTrue if bool_value else 0,
                       array_index, bool_value, False)


def end_property_tag(reader: BinaryReader, tag: PropertyTag,
                     file_version_ue5: int) -> None:
    """Consume the tag bytes that trail the value data.

    Only the new format puts anything after the value; the old format's
    property GUID is already consumed by :func:`read_property_tag`.
    """
    if not tag.new_format:
        return

    if tag.flags & TAG_HasPropertyGuid:
        reader.skip(16)

    if (file_version_ue5 >= UE5_PROPERTY_TAG_EXTENSION
            and tag.flags & TAG_HasPropertyExtensions):
        ext = reader.read_uint8()
        if ext & 0x01:  # OverridableInformation
            reader.skip(1 + 4)  # OverrideOperation + bExperimentalOverridableLogic


# ---------------------------------------------------------------------------
# Struct value readers
# ---------------------------------------------------------------------------

_VECTOR_NAMES = frozenset({
    "Vector", "Vector3f", "Vector_NetQuantize",
    "Vector_NetQuantize10", "Vector_NetQuantize100", "Vector_NetQuantizeNormal",
})

_ROTATOR_NAMES = frozenset({"Rotator", "Rotator_NetQuantize"})

_TRANSFORM_NAMES = frozenset({"Transform", "Transform_NetQuantize"})


def _read_struct_value(reader: BinaryReader, struct_name: str, size: int):
    """Read struct value from Size bytes based on struct type name.

    Handles both float (32-bit) and double (64-bit) precision.
    UE5 Large World Coordinates uses doubles (Size=24 for Vector).

    Does NOT skip remaining bytes — caller handles alignment.
    """
    if struct_name in _VECTOR_NAMES:
        if size >= 24:
            # Double precision (LWC): FVector3d
            x = reader.read_double()
            y = reader.read_double()
            z = reader.read_double()
            return {"_type": "Vector", "x": x, "y": y, "z": z}
        elif size >= 12:
            # Single precision: FVector3f
            x = reader.read_float()
            y = reader.read_float()
            z = reader.read_float()
            return {"_type": "Vector", "x": x, "y": y, "z": z}

    elif struct_name in _ROTATOR_NAMES:
        if size >= 24:
            # Double precision (LWC)
            pitch = reader.read_double()
            yaw = reader.read_double()
            roll = reader.read_double()
            return {"_type": "Rotator", "pitch": pitch, "yaw": yaw, "roll": roll}
        elif size >= 12:
            # Single precision
            pitch = reader.read_float()
            yaw = reader.read_float()
            roll = reader.read_float()
            return {"_type": "Rotator", "pitch": pitch, "yaw": yaw, "roll": roll}

    elif struct_name in _TRANSFORM_NAMES:
        if size >= 80:
            # Double precision (LWC): FQuat4d(32) + FVector3d(24) + FVector3d(24)
            rx, ry, rz, rw = reader.read_double(), reader.read_double(), reader.read_double(), reader.read_double()
            tx, ty, tz = reader.read_double(), reader.read_double(), reader.read_double()
            sx, sy, sz = reader.read_double(), reader.read_double(), reader.read_double()
            return {
                "_type": "Transform",
                "rotation": (rx, ry, rz, rw),
                "translation": (tx, ty, tz),
                "scale3d": (sx, sy, sz),
            }
        elif size >= 40:
            # Single precision: FQuat(16) + FVector(12) + FVector(12)
            rx, ry, rz, rw = reader.read_float(), reader.read_float(), reader.read_float(), reader.read_float()
            tx, ty, tz = reader.read_float(), reader.read_float(), reader.read_float()
            sx, sy, sz = reader.read_float(), reader.read_float(), reader.read_float()
            return {
                "_type": "Transform",
                "rotation": (rx, ry, rz, rw),
                "translation": (tx, ty, tz),
                "scale3d": (sx, sy, sz),
            }

    elif struct_name == "Quat":
        if size >= 32:
            # Double precision
            x = reader.read_double()
            y = reader.read_double()
            z = reader.read_double()
            w = reader.read_double()
            return {"_type": "Quat", "x": x, "y": y, "z": z, "w": w}
        elif size >= 16:
            # Single precision
            x = reader.read_float()
            y = reader.read_float()
            z = reader.read_float()
            w = reader.read_float()
            return {"_type": "Quat", "x": x, "y": y, "z": z, "w": w}

    # Unknown struct or unexpected size — return raw bytes
    return reader.read_bytes(size)


# ---------------------------------------------------------------------------
# Typed value reader (for non-struct, non-bool properties)
# ---------------------------------------------------------------------------

def _read_typed_value(reader: BinaryReader, type_name: str, size: int, name_map):
    """Read a typed property value from Size bytes.

    Does NOT skip remaining bytes — caller handles alignment.
    """
    if type_name == "ObjectProperty" and size >= 4:
        return reader.read_int32()  # FPackageIndex

    elif type_name == "NameProperty" and size >= 8:
        idx = reader.read_int32()
        num = reader.read_int32()
        return resolve_fname(name_map, idx, num)

    elif type_name == "StrProperty" and size > 0:
        return reader.read_fstring()

    elif type_name == "FloatProperty" and size >= 4:
        return reader.read_float()

    elif type_name in ("IntProperty", "UInt32Property") and size >= 4:
        return reader.read_int32()

    elif type_name == "SoftObjectProperty" and size > 0:
        path = reader.read_fstring()
        subpath = reader.read_fstring()
        return (path, subpath)

    else:
        return reader.read_bytes(size)


# ---------------------------------------------------------------------------
# skip_properties — unchanged, used by mesh.py and texture.py
# ---------------------------------------------------------------------------

def skip_properties(reader: BinaryReader, name_map, file_version_ue5: int) -> int:
    """Skip all property tags until "None" is encountered.

    Returns the number of properties skipped.
    The reader should be positioned at the start of the export data.
    """
    use_new_format = file_version_ue5 >= UE5_PROPERTY_TAG_COMPLETE_TYPE_NAME
    use_extensions = file_version_ue5 >= UE5_PROPERTY_TAG_EXTENSION

    count = 0
    while True:
        if not reader.can_read(8):
            break

        # Read property name (FName: index + number)
        name_idx = reader.read_int32()
        name_num = reader.read_int32()
        name = resolve_fname(name_map, name_idx, name_num)

        if name == "None":
            break

        count += 1

        if use_new_format:
            # New format: FPropertyTypeName + Size + Flags byte
            type_name = _read_property_type_name(reader, name_map)
            size = reader.read_int32()
            flags = reader.read_uint8()

            # HasArrayIndex
            if flags & TAG_HasArrayIndex:
                reader.skip(4)

            # Skip value data
            if type_name == "BoolProperty":
                # BoolProperty has Size=0, value is in flags
                pass
            else:
                reader.skip(size)

            # HasPropertyGuid
            if flags & TAG_HasPropertyGuid:
                reader.skip(16)

            # HasPropertyExtensions
            if use_extensions and (flags & TAG_HasPropertyExtensions):
                ext = reader.read_uint8()
                if ext & 0x01:  # OverridableInformation
                    reader.skip(1 + 4)  # OverrideOperation + bExperimentalOverridableLogic
        else:
            # Old format: FName type + Size + ArrayIndex + type-specific header
            type_idx = reader.read_int32()
            type_num = reader.read_int32()
            type_name = resolve_fname(name_map, type_idx, type_num)

            size = reader.read_int32()
            _array_index = reader.read_int32()

            # Type-specific header data
            if type_name == "StructProperty":
                reader.skip(8)   # StructName FName
                reader.skip(16)  # StructGuid
            elif type_name == "BoolProperty":
                reader.skip(1)   # BoolVal byte
                has_guid = reader.read_uint8()
                if has_guid:
                    reader.skip(16)
                continue  # BoolProperty has Size=0
            elif type_name in ("ByteProperty", "EnumProperty"):
                reader.skip(8)   # EnumName FName
            elif type_name == "ArrayProperty":
                reader.skip(8)   # InnerType FName
            elif type_name == "SetProperty":
                reader.skip(8)   # InnerType FName
            elif type_name == "MapProperty":
                reader.skip(16)  # InnerType + ValueType FNames

            has_guid = reader.read_uint8()
            if has_guid:
                reader.skip(16)

            # Skip value data
            reader.skip(size)

    return count


# ---------------------------------------------------------------------------
# read_properties — reads property values into a dict
# ---------------------------------------------------------------------------

def read_properties(reader: BinaryReader, name_map, file_version_ue5: int) -> dict:
    """Read all properties and return as dict.

    Returns dict mapping property name -> value. Value types:
    - StructProperty with Vector: {"_type": "Vector", "x": float, "y": float, "z": float}
    - StructProperty with Rotator: {"_type": "Rotator", "pitch": float, "yaw": float, "roll": float}
    - StructProperty with Transform: {"_type": "Transform", "rotation": (x,y,z,w), "translation": (x,y,z), "scale3d": (x,y,z)}
    - StructProperty with Quat: {"_type": "Quat", "x": float, "y": float, "z": float, "w": float}
    - ObjectProperty: int (FPackageIndex value)
    - NameProperty: str (resolved name)
    - StrProperty: str
    - BoolProperty: bool
    - FloatProperty: float
    - IntProperty: int
    - SoftObjectProperty: (path_str, sub_path_str)
    - Other/Unknown: bytes (raw value data)
    """
    use_new_format = file_version_ue5 >= UE5_PROPERTY_TAG_COMPLETE_TYPE_NAME
    use_extensions = file_version_ue5 >= UE5_PROPERTY_TAG_EXTENSION

    props = {}

    while True:
        if not reader.can_read(8):
            break

        # Read property name (FName: index + number)
        name_idx = reader.read_int32()
        name_num = reader.read_int32()
        name = resolve_fname(name_map, name_idx, name_num)

        if name == "None":
            break

        try:
            if use_new_format:
                _read_property_new(reader, name_map, name, use_extensions, props)
            else:
                _read_property_old(reader, name_map, name, props)
        except Exception:
            # On any parse error, stop reading and return what we have
            break

    return props


def _read_property_new(reader: BinaryReader, name_map, name: str,
                       use_extensions: bool, props: dict):
    """Read a single property in new format (UE5 >= PROPERTY_TAG_COMPLETE_TYPE_NAME).

    Reads the same byte sequence as the new-format branch in skip_properties.
    """
    # Type name tree (captures root + inner names for struct type detection)
    type_name, inner_types = _read_property_type_tree(reader, name_map)
    size = reader.read_int32()
    flags = reader.read_uint8()

    # HasArrayIndex
    array_index = 0
    if flags & TAG_HasArrayIndex:
        array_index = reader.read_int32()

    # Read value
    if type_name == "BoolProperty":
        # BoolProperty has Size=0, value is in flags
        value = bool(flags & TAG_BoolTrue)
    else:
        value_start = reader.position()
        if type_name == "StructProperty":
            struct_name = inner_types[0] if inner_types else ""
            value = _read_struct_value(reader, struct_name, size)
        else:
            value = _read_typed_value(reader, type_name, size, name_map)
        # Skip any remaining bytes to align with Size
        remaining = size - (reader.position() - value_start)
        if remaining > 0:
            reader.skip(remaining)

    # HasPropertyGuid
    if flags & TAG_HasPropertyGuid:
        reader.skip(16)

    # HasPropertyExtensions
    if use_extensions and (flags & TAG_HasPropertyExtensions):
        ext = reader.read_uint8()
        if ext & 0x01:  # OverridableInformation
            reader.skip(1 + 4)  # OverrideOperation + bExperimentalOverridableLogic

    props[name] = value


def _read_property_old(reader: BinaryReader, name_map, name: str, props: dict):
    """Read a single property in old format (UE4 style).

    Reads the same byte sequence as the old-format branch in skip_properties.
    """
    # Type FName (8 bytes)
    type_idx = reader.read_int32()
    type_num = reader.read_int32()
    type_name = resolve_fname(name_map, type_idx, type_num)

    size = reader.read_int32()
    _array_index = reader.read_int32()

    # Type-specific header + value
    if type_name == "StructProperty":
        # StructName FName (8 bytes) + StructGuid (16 bytes)
        struct_idx = reader.read_int32()
        struct_num = reader.read_int32()
        struct_name = resolve_fname(name_map, struct_idx, struct_num)
        reader.skip(16)  # StructGuid (matches skip_properties)

        # HasPropertyGuid
        has_guid = reader.read_uint8()
        if has_guid:
            reader.skip(16)

        # Value data (Size bytes)
        value_start = reader.position()
        value = _read_struct_value(reader, struct_name, size)
        remaining = size - (reader.position() - value_start)
        if remaining > 0:
            reader.skip(remaining)

    elif type_name == "BoolProperty":
        # BoolVal byte + HasPropertyGuid
        bool_val = reader.read_uint8()
        has_guid = reader.read_uint8()
        if has_guid:
            reader.skip(16)
        # BoolProperty has Size=0, no value data
        props[name] = bool_val != 0
        return

    elif type_name in ("ByteProperty", "EnumProperty"):
        reader.skip(8)  # EnumName FName
        has_guid = reader.read_uint8()
        if has_guid:
            reader.skip(16)
        # Read value data
        value_start = reader.position()
        if type_name == "EnumProperty" and size >= 8:
            enum_idx = reader.read_int32()
            enum_num = reader.read_int32()
            value = resolve_fname(name_map, enum_idx, enum_num)
        else:
            value = reader.read_bytes(size)
        remaining = size - (reader.position() - value_start)
        if remaining > 0:
            reader.skip(remaining)

    elif type_name in ("ArrayProperty", "SetProperty"):
        reader.skip(8)  # InnerType FName
        has_guid = reader.read_uint8()
        if has_guid:
            reader.skip(16)
        value = reader.read_bytes(size)

    elif type_name == "MapProperty":
        reader.skip(16)  # InnerType + ValueType FNames
        has_guid = reader.read_uint8()
        if has_guid:
            reader.skip(16)
        value = reader.read_bytes(size)

    else:
        # ObjectProperty, NameProperty, StrProperty, FloatProperty,
        # IntProperty, SoftObjectProperty, and unknown types
        has_guid = reader.read_uint8()
        if has_guid:
            reader.skip(16)
        # Read typed value
        value_start = reader.position()
        value = _read_typed_value(reader, type_name, size, name_map)
        remaining = size - (reader.position() - value_start)
        if remaining > 0:
            reader.skip(remaining)

    props[name] = value

"""Static mesh parser and OBJ/GLB exporter for UE 5.5 .uasset files.

Parses FMeshDescription from FCompressedBuffer payload in the package trailer.
Pipeline: .uasset → Package → Trailer → FCompressedBuffer → Oodle decompress → FMeshDescription → OBJ/GLB
"""
import math
import os
import struct
from typing import Dict, List, NamedTuple, Tuple, Optional

import numpy as np
import ooz

from .reader import BinaryReader
from .package import Package

try:
    from pygltflib import (
        GLTF2,
        Scene as GLTFScene,
        Node as GLTFNode,
        Mesh as GLTFMesh,
        Primitive,
        Material,
        Camera,
        Perspective,
        PbrMetallicRoughness,
        TextureInfo,
        Texture as GLTFTexture,
        Sampler,
        Image as GLTFImage,
        BufferView,
        Accessor,
        Buffer,
    )
    _HAS_PYGLTFLIB = True
except ImportError:
    _HAS_PYGLTFLIB = False


# ---------------------------------------------------------------------------
# FCompressedBuffer decompression
# ---------------------------------------------------------------------------

def _be_uint32(data, offset):
    return struct.unpack_from('>I', data, offset)[0]


def _be_uint64(data, offset):
    return struct.unpack_from('>Q', data, offset)[0]


def decompress_compressed_buffer(data: bytes) -> Optional[bytes]:
    """Decompress an FCompressedBuffer (UE5 big-endian header + Oodle/LZ4 blocks).

    Returns the raw decompressed bytes, or None on failure.
    """
    if len(data) < 64:
        return None

    magic = _be_uint32(data, 0)
    if magic != 0xB7756362:
        return None

    method = data[8]
    block_size_exp = data[11]
    block_count = _be_uint32(data, 12)
    total_raw_size = _be_uint64(data, 16)
    block_size = 1 << block_size_exp if block_size_exp > 0 else 0

    # EMethod::None — the payload follows the 64-byte header verbatim and
    # there is no block-size table.  UE stores source art that is already an
    # encoded image (PNG/JPEG) this way, since re-compressing it is pointless.
    if method == 0:
        raw = data[64:64 + total_raw_size]
        return raw if len(raw) == total_raw_size else None

    # Parse block sizes (big-endian uint32 array after 64-byte header)
    block_sizes_offset = 64
    block_sizes = []
    for i in range(block_count):
        bs = _be_uint32(data, block_sizes_offset + i * 4)
        block_sizes.append(bs)

    # Calculate raw block sizes
    raw_block_sizes = []
    for i in range(block_count):
        if i < block_count - 1:
            raw_block_sizes.append(block_size)
        else:
            raw_block_sizes.append(total_raw_size - block_size * (block_count - 1))

    # Decompress blocks
    blocks_data_offset = block_sizes_offset + block_count * 4
    decompressed = bytearray()
    current_offset = blocks_data_offset

    for i in range(block_count):
        compressed_block_size = block_sizes[i]
        raw_block_size = raw_block_sizes[i]
        compressed_block = data[current_offset:current_offset + compressed_block_size]

        if compressed_block_size >= raw_block_size:
            # Block stored uncompressed
            decompressed.extend(compressed_block[:raw_block_size])
        elif method == 3:  # Oodle
            decompressed_block = ooz.decompress(compressed_block, raw_block_size)
            decompressed.extend(decompressed_block)
        else:
            return None

        current_offset += compressed_block_size

    if len(decompressed) != total_raw_size:
        return None

    return bytes(decompressed)


# ---------------------------------------------------------------------------
# Package trailer helpers
# ---------------------------------------------------------------------------

def extract_trailer_payload(file_data: bytes) -> Optional[bytes]:
    """Extract the FCompressedBuffer payload from the package trailer.

    Returns the compressed buffer bytes, or None on failure.
    """
    file_size = len(file_data)

    # Trailer footer is the last 20 bytes:
    #   FooterTag: uint64 (8 bytes)
    #   TrailerLength: uint64 (8 bytes)
    #   PackageTag: uint32 (4 bytes)
    if file_size < 20:
        return None

    trailer_length = struct.unpack_from('<Q', file_data, file_size - 12)[0]
    if trailer_length <= 0 or trailer_length > file_size:
        return None

    trailer_start = file_size - trailer_length

    # Trailer header (28 bytes):
    #   HeaderTag: uint64 (8 bytes)
    #   Version: int32 (4 bytes)
    #   HeaderLength: uint32 (4 bytes)
    #   PayloadsDataLength: uint64 (8 bytes)
    #   NumPayloads: int32 (4 bytes)
    if trailer_start + 28 > file_size:
        return None

    header_length = struct.unpack_from('<I', file_data, trailer_start + 12)[0]
    payloads_data_length = struct.unpack_from('<Q', file_data, trailer_start + 16)[0]

    payload_section_start = trailer_start + header_length
    if payload_section_start + payloads_data_length > file_size:
        return None

    return file_data[payload_section_start:payload_section_start + payloads_data_length]


# ---------------------------------------------------------------------------
# FMeshDescription binary parser
# ---------------------------------------------------------------------------

# Attribute type sizes for bulk-serializable types
_ATTR_TYPE_SIZES = {
    0: 16,  # FVector4f
    1: 12,  # FVector3f
    2: 8,   # FVector2f
    3: 4,   # float
    4: 4,   # int32
    5: 4,   # bool (serialized as int32)
}

_ATTR_TYPE_NAMES = {
    0: 'FVector4f',
    1: 'FVector3f',
    2: 'FVector2f',
    3: 'float',
    4: 'int32',
    5: 'bool',
    6: 'FName',
}


class _MeshDescReader:
    """Low-level reader for FMeshDescription binary data."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read_int32(self):
        val = struct.unpack_from('<i', self.data, self.pos)[0]
        self.pos += 4
        return val

    def read_uint32(self):
        val = struct.unpack_from('<I', self.data, self.pos)[0]
        self.pos += 4
        return val

    def read_float(self):
        val = struct.unpack_from('<f', self.data, self.pos)[0]
        self.pos += 4
        return val

    def read_bytes(self, n):
        val = self.data[self.pos:self.pos + n]
        self.pos += n
        return val

    def read_fstring(self):
        length = self.read_int32()
        if length > 0:
            s = self.data[self.pos:self.pos + length - 1].decode('latin-1', errors='replace')
            self.pos += length
            return s
        elif length < 0:
            char_count = -length
            s = self.data[self.pos:self.pos + char_count * 2].decode('utf-16-le', errors='replace')
            self.pos += char_count * 2
            return s
        return ""

    def read_tbit_array(self):
        num_bits = self.read_int32()
        num_words = (num_bits + 31) // 32
        words = []
        for _ in range(num_words):
            words.append(self.read_uint32())
        return num_bits, words

    @staticmethod
    def count_valid_elements(num_bits, words):
        count = 0
        for word in words:
            count += bin(word).count('1')
        return count

    def read_attribute_default_value(self, attr_type):
        if attr_type == 0:  # FVector4f
            vals = struct.unpack_from('<ffff', self.data, self.pos)
            self.pos += 16
            return vals
        elif attr_type == 1:  # FVector3f
            x, y, z = struct.unpack_from('<fff', self.data, self.pos)
            self.pos += 12
            return (x, y, z)
        elif attr_type == 2:  # FVector2f
            x, y = struct.unpack_from('<ff', self.data, self.pos)
            self.pos += 8
            return (x, y)
        elif attr_type == 3:  # float
            return self.read_float()
        elif attr_type == 4:  # int32
            return self.read_int32()
        elif attr_type == 5:  # bool
            return self.read_int32()
        elif attr_type == 6:  # FName -> FString
            return self.read_fstring()
        else:
            raise ValueError(f"Unknown attribute type: {attr_type}")

    def parse_attribute_array_base(self, attr_type, is_bulk):
        """Parse TMeshAttributeArrayBase: Extent(u32) + Container data."""
        extent = self.read_uint32()

        if is_bulk and attr_type in _ATTR_TYPE_SIZES:
            # BulkSerialize: ElementSize(i32) + Count(i32) + raw data
            serialized_elem_size = self.read_int32()
            count = self.read_int32()
            raw = self.read_bytes(count * serialized_elem_size)
            return {'extent': extent, 'count': count, 'data': raw}
        else:
            # Element-by-element: TArray serialization (Count + elements)
            count = self.read_int32()
            if attr_type == 6:  # FName -> FString
                strings = [self.read_fstring() for _ in range(count)]
                return {'extent': extent, 'count': count, 'data': strings}
            elif attr_type == 5:  # bool as int32
                bools = [self.read_int32() for _ in range(count)]
                return {'extent': extent, 'count': count, 'data': bools}
            else:
                raise ValueError(f"Unexpected non-bulk type {attr_type}")

    def parse_chunk(self, attr_type):
        """Parse FChunk from TAttributeArrayContainer."""
        if attr_type in _ATTR_TYPE_SIZES:
            serialized_elem_size = self.read_int32()
            data_count = self.read_int32()
            raw = self.read_bytes(data_count * serialized_elem_size)
        elif attr_type == 6:
            data_count = self.read_int32()
            raw = [self.read_fstring() for _ in range(data_count)]
        else:
            data_count = self.read_int32()
            raw = self.read_bytes(data_count * 4)

        chunk_num_elements = self.read_int32()
        start_indices = [self.read_int32() for _ in range(chunk_num_elements)]
        counts = [self.read_int32() for _ in range(chunk_num_elements)]
        max_counts = [self.read_int32() for _ in range(chunk_num_elements)]

        return {'data': raw, 'num_elements': chunk_num_elements,
                'start_indices': start_indices, 'counts': counts, 'max_counts': max_counts}

    def parse_unbounded_channel(self, attr_type):
        """Parse TAttributeArrayContainer."""
        num_chunks = self.read_int32()
        chunks = [self.parse_chunk(attr_type) for _ in range(num_chunks)]
        num_elements = self.read_int32()
        default = self.read_attribute_default_value(attr_type)
        return {'chunks': chunks, 'num_elements': num_elements, 'default': default}

    def parse_attribute_set_entry(self):
        """Parse FAttributesSetEntry."""
        attr_type = self.read_uint32()
        extent = self.read_uint32()
        is_bulk = attr_type in _ATTR_TYPE_SIZES

        num_elements = self.read_int32()
        num_channels = self.read_int32()

        if extent > 0:
            # Bounded: TMeshAttributeArraySet
            channels = [self.parse_attribute_array_base(attr_type, is_bulk) for _ in range(num_channels)]
        else:
            # Unbounded: TMeshUnboundedAttributeArraySet
            channels = [self.parse_unbounded_channel(attr_type) for _ in range(num_channels)]

        default = self.read_attribute_default_value(attr_type)
        flags = self.read_uint32()

        return {'type': attr_type, 'type_name': _ATTR_TYPE_NAMES.get(attr_type, f'?{attr_type}'),
                'extent': extent, 'num_elements': num_elements,
                'num_channels': num_channels, 'channels': channels,
                'default': default, 'flags': flags, 'bounded': extent > 0}

    def parse_attributes_set_base(self):
        """Parse FAttributesSetBase."""
        num_elements = self.read_int32()
        map_count = self.read_int32()
        attributes = {}
        for _ in range(map_count):
            key = self.read_fstring()
            entry = self.parse_attribute_set_entry()
            attributes[key] = entry
        return {'num_elements': num_elements, 'attributes': attributes}

    def parse_element_container(self):
        """Parse FMeshElementContainer."""
        num_bits, words = self.read_tbit_array()
        num_holes = self.read_int32()
        valid_count = self.count_valid_elements(num_bits, words)
        attributes = self.parse_attributes_set_base()
        return {'num_bits': num_bits, 'num_holes': num_holes,
                'valid_count': valid_count, 'words': words,
                'attributes': attributes}

    def parse_mesh_description(self):
        """Parse the full FMeshDescription."""
        num_entries = self.read_int32()
        elements = {}
        for i in range(num_entries):
            key = self.read_fstring()
            # FMeshElementChannels: TArray<FMeshElementContainer>
            num_channels = self.read_int32()
            channels = [self.parse_element_container() for _ in range(num_channels)]
            elements[key] = {'channels': channels}
        return elements


# ---------------------------------------------------------------------------
# Mesh data extraction
# ---------------------------------------------------------------------------

def _extract_attr_data(channel, attr_name, expected_type=None):
    """Extract raw attribute data from a channel's attributes."""
    attrs = channel['attributes']
    for name, attr in attrs['attributes'].items():
        if name.strip() == attr_name or name == attr_name:
            if expected_type is not None and attr['type'] != expected_type:
                continue
            if attr['bounded'] and isinstance(attr['channels'][0].get('data'), bytes):
                return attr['channels'][0]
    return None


def _extract_fname_attr(channel, attr_name):
    """Extract FName (string list) attribute data from a channel.

    Handles both bounded (TMeshAttributeArraySet) and unbounded
    (TMeshUnboundedAttributeArraySet) FName attributes.
    """
    attrs = channel['attributes']
    for name, attr in attrs['attributes'].items():
        if name.strip() == attr_name or name == attr_name:
            if attr['type'] != 6:  # FName
                continue
            if not attr['channels']:
                continue
            ch = attr['channels'][0]
            if attr['bounded']:
                data = ch.get('data')
                if isinstance(data, list):
                    return data
            else:
                # Unbounded: collect from chunks
                chunks = ch.get('chunks', [])
                result = []
                for chunk in chunks:
                    chunk_data = chunk.get('data')
                    if isinstance(chunk_data, list):
                        result.extend(chunk_data)
                if result:
                    return result
    return None


def _build_sparse_mapping(num_bits, words):
    """Build sparse-to-dense mapping from TBitArray validity mask.

    Returns dict mapping valid sparse element IDs to sequential dense indices.
    """
    sparse_to_dense = {}
    dense_idx = 0
    for bit_pos in range(num_bits):
        word_idx = bit_pos // 32
        bit_idx = bit_pos % 32
        if word_idx < len(words) and (words[word_idx] & (1 << bit_idx)):
            sparse_to_dense[bit_pos] = dense_idx
            dense_idx += 1
    return sparse_to_dense


def _maybe_expand_sparse(data_list, container_info, default=None):
    """Expand dense data to sparse array if the element container has holes.

    When an element container has holes (num_holes > 0), element IDs are
    not contiguous.  If the attribute data is stored densely (only valid
    entries), this function expands it to a sparse array indexed by the
    actual element ID, so that lookups by sparse ID work correctly.
    """
    num_bits = container_info['num_bits']
    num_holes = container_info['num_holes']
    words = container_info.get('words', [])

    # No holes or no words — data is already contiguous
    if num_holes <= 0 or not words:
        return data_list

    # Data already covers all sparse IDs — no expansion needed
    if len(data_list) >= num_bits:
        return data_list

    # Data is dense — expand to sparse array
    sparse_mapping = _build_sparse_mapping(num_bits, words)
    result = [default] * num_bits
    for sparse_id, dense_idx in sparse_mapping.items():
        if dense_idx < len(data_list):
            result[sparse_id] = data_list[dense_idx]
    return result


def extract_mesh_data(elements: dict) -> Optional[dict]:
    """Extract vertices, normals, UVs, and triangles from parsed FMeshDescription."""
    result = {
        'vertices': [],
        'vi_to_vertex': [],
        'normals': [],
        'uvs': [],       # List of UV channel arrays
        'triangles': [],
    }

    # --- Vertex positions ---
    if 'Vertices' not in elements:
        return None
    vert_ch = elements['Vertices']['channels'][0]
    pos_data = _extract_attr_data(vert_ch, 'Position', expected_type=1)
    if pos_data is None:
        return None
    raw = pos_data['data']
    count = pos_data['count']
    vertices = []
    for i in range(count):
        x, y, z = struct.unpack_from('<fff', raw, i * 12)
        vertices.append((x, y, z))
    result['vertices'] = _maybe_expand_sparse(vertices, vert_ch, default=(0.0, 0.0, 0.0))

    # --- Vertex instance → vertex mapping ---
    if 'VertexInstances' not in elements:
        return None
    vi_ch = elements['VertexInstances']['channels'][0]
    vi_data = _extract_attr_data(vi_ch, 'VertexIndex', expected_type=4)
    if vi_data is None:
        return None
    raw = vi_data['data']
    count = vi_data['count']
    vi_to_vertex = []
    for i in range(count):
        idx = struct.unpack_from('<i', raw, i * 4)[0]
        vi_to_vertex.append(idx)
    result['vi_to_vertex'] = _maybe_expand_sparse(vi_to_vertex, vi_ch, default=0)

    # --- Normals (per vertex instance) ---
    normal_data = _extract_attr_data(vi_ch, 'Normal', expected_type=1)
    if normal_data:
        raw = normal_data['data']
        count = normal_data['count']
        normals = []
        for i in range(count):
            x, y, z = struct.unpack_from('<fff', raw, i * 12)
            normals.append((x, y, z))
        result['normals'] = _maybe_expand_sparse(normals, vi_ch, default=(0.0, 0.0, 1.0))

    # --- UV coordinates (per vertex instance, channel 0 = texture UV) ---
    uv_channels = []
    vi_attrs = vi_ch['attributes']

    for name, attr in vi_attrs['attributes'].items():
        if (name.strip() == 'TextureCoordinate' or name == 'TextureCoordinate') and attr['type'] == 2:
            # Use only channel 0 (primary texture UV).
            # Channel 1 is lightmap UV and is skipped.
            if attr['channels']:
                channel = attr['channels'][0]
                dense_uvs = []
                if isinstance(channel.get('data'), bytes):
                    # Bounded attribute — direct data array
                    ch_raw = channel['data']
                    ch_count = channel['count']
                    for i in range(ch_count):
                        u, v = struct.unpack_from('<ff', ch_raw, i * 8)
                        dense_uvs.append((u, v))
                elif 'chunks' in channel:
                    # Unbounded attribute — collect data from chunks
                    for chunk in channel.get('chunks', []):
                        chunk_data = chunk.get('data')
                        if isinstance(chunk_data, bytes):
                            num_uvs = len(chunk_data) // 8
                            for i in range(num_uvs):
                                u, v = struct.unpack_from('<ff', chunk_data, i * 8)
                                dense_uvs.append((u, v))
                if dense_uvs:
                    uv_channels.append(_maybe_expand_sparse(dense_uvs, vi_ch, default=(0.0, 0.0)))
            break
    result['uvs'] = uv_channels

    # --- Triangle data ---
    if 'Triangles' not in elements:
        return None
    tri_ch = elements['Triangles']['channels'][0]
    tri_attrs = tri_ch['attributes']

    # VertexInstanceIndex (extent=3, int32)
    raw_tri = None
    tri_count = 0
    for name, attr in tri_attrs['attributes'].items():
        if name == 'VertexInstanceIndex' and attr['type'] == 4 and attr['extent'] == 3:
            raw_tri = attr['channels'][0]['data']
            tri_count = attr['channels'][0]['count']
            break
    if raw_tri is None:
        return result

    triangles_raw = []
    for i in range(tri_count // 3):
        v0, v1, v2 = struct.unpack_from('<iii', raw_tri, i * 12)
        triangles_raw.append((v0, v1, v2))

    # Material assignment — UE5 uses PolygonGroupIndex (one per triangle)
    # which maps directly to the material slot index.  Fall back to the
    # older MaterialIndex attribute if it exists.
    material_indices: List[int] = []
    mat_attr_name = None
    for name, attr in tri_attrs['attributes'].items():
        n = name.strip()
        if n == 'PolygonGroupIndex' and attr['type'] == 4:
            mat_attr_name = n
            break
        if n == 'MaterialIndex' and attr['type'] == 4:
            mat_attr_name = n
            # keep looking — prefer PolygonGroupIndex if it exists
    if mat_attr_name:
        for name, attr in tri_attrs['attributes'].items():
            n = name.strip()
            if n == mat_attr_name and attr['type'] == 4:
                ch_list = attr.get('channels', [])
                if ch_list:
                    ch = ch_list[0]
                    if isinstance(ch.get('data'), bytes):
                        raw_mi = ch['data']
                        count_mi = ch['count']
                        for i in range(count_mi):
                            mi = struct.unpack_from('<i', raw_mi, i * 4)[0]
                            material_indices.append(mi)
                break

    # Build triangles with material indices: (vi0, vi1, vi2, material_index)
    result['triangles'] = [
        (t[0], t[1], t[2], material_indices[i] if i < len(material_indices) else 0)
        for i, t in enumerate(triangles_raw)
    ]

    # --- Material slot names from PolygonGroups ---
    # ImportedMaterialSlotName maps each polygon group to a material slot
    # name.  This is needed because polygon group N does NOT necessarily
    # correspond to material import slot N — the slot names must be matched
    # against the ordered material import names to build the correct mapping.
    material_slot_names = None
    if 'PolygonGroups' in elements:
        pg_ch = elements['PolygonGroups']['channels'][0]
        slot_names = _extract_fname_attr(pg_ch, 'ImportedMaterialSlotName')
        if slot_names:
            slot_names = _maybe_expand_sparse(slot_names, pg_ch, default=None)
            material_slot_names = slot_names
    result['material_slot_names'] = material_slot_names

    return result


# ---------------------------------------------------------------------------
# StaticMaterials parser
# ---------------------------------------------------------------------------

def _parse_static_materials(pkg: Package) -> Optional[List[Tuple[str, str]]]:
    """Parse the StaticMaterials property from the StaticMesh export data.

    Reads the real material-slot-to-import mapping from the UStaticMesh
    export's serialized ``StaticMaterials`` array.  Each ``FStaticMaterial``
    element contains an ``ImportedMaterialSlotName`` and a
    ``MaterialInterface`` (FPackageIndex pointing to a material import).

    Returns:
        Ordered list of ``(ImportedMaterialSlotName, material_import_name)``
        tuples, or ``None`` if parsing fails.
    """
    from .properties import read_properties

    # Find the StaticMesh export
    sm_export_idx = None
    for i in range(pkg.export_count):
        if pkg.get_export_class_name(i) == 'StaticMesh':
            sm_export_idx = i
            break
    if sm_export_idx is None:
        return None

    reader = pkg.get_export_data(sm_export_idx)
    if reader is None:
        return None
    data = reader.data

    # Find the FName index for "StaticMaterials"
    sm_name_idx = None
    for ni, n in enumerate(pkg.name_map):
        if n == 'StaticMaterials':
            sm_name_idx = ni
            break
    if sm_name_idx is None:
        return None

    # Search for the FName (index + number=0) in the binary data
    target = struct.pack('<ii', sm_name_idx, 0)
    offset = 0
    while offset < len(data) - len(target):
        idx = data.find(target, offset)
        if idx == -1:
            return None
        try:
            result = _parse_static_materials_at(data, idx, pkg)
            if result is not None:
                return result
        except Exception:
            pass
        offset = idx + 1
    return None


def _parse_static_materials_at(data: bytes, offset: int,
                               pkg: Package) -> Optional[List[Tuple[str, str]]]:
    """Try to parse the StaticMaterials array property at *offset*."""
    from .properties import read_properties

    r = BinaryReader(data)
    r.seek(offset)

    # Skip Name FName (8 bytes)
    r.skip(8)

    # Skip type tree (FPropertyTypeName)
    total_nodes = 1
    i = 0
    while i < total_nodes:
        r.skip(8)  # FName (idx + number)
        inner_count = r.read_int32()
        total_nodes += inner_count
        i += 1

    # Size
    size = r.read_int32()
    if size <= 0 or size > len(data):
        return None

    # Flags
    flags = r.read_uint8()
    if flags & 0x01:  # HasArrayIndex
        r.skip(4)
    if flags & 0x02:  # HasPropertyGuid
        r.skip(16)
    if flags & 0x04:  # HasPropertyExtensions
        ext = r.read_uint8()
        if ext & 0x01:
            r.skip(1 + 4)

    # Array element count
    arr_count = r.read_int32()
    if arr_count < 0 or arr_count > 256:
        return None

    # Parse each FStaticMaterial struct (properties until "None")
    result: List[Tuple[str, str]] = []
    for _ in range(arr_count):
        elem_props = read_properties(r, pkg.name_map, pkg.file_version_ue5)
        imported_slot_name = elem_props.get('ImportedMaterialSlotName')
        material_interface = elem_props.get('MaterialInterface')
        if imported_slot_name is None or material_interface is None:
            continue
        # Resolve FPackageIndex to import name
        if isinstance(material_interface, int) and material_interface < 0:
            imp_idx = -material_interface - 1
            if 0 <= imp_idx < len(pkg.imports):
                result.append(
                    (imported_slot_name, pkg.imports[imp_idx].object_name))

    return result if result else None


def _parse_section_info_map(data: bytes, name_map: list,
                            num_sections: int) -> Optional[List[int]]:
    """Parse the SectionInfoMap to extract MaterialIndex overrides for LOD 0.

    UE5's ``FMeshSectionInfoMap`` stores a ``Map<UInt32, FMeshSectionInfo>``
    keyed by ``GetMeshMaterialKey(LOD, Section) = (LOD << 16) | Section``.
    For LOD 0 the keys are simply 0, 1, 2, … and the map is populated in
    insertion order (section 0 first).  Each ``FMeshSectionInfo`` contains a
    ``MaterialIndex`` *IntProperty* that overrides which entry in the
    ``StaticMaterials`` array the section should use.

    Returns a list of *MaterialIndex* values for the first *num_sections*
    sections of LOD 0, or ``None`` on failure.
    """
    if num_sections <= 0:
        return None

    # Locate required FName indices
    sim_idx = sm_idx = mi_idx = None
    for i, n in enumerate(name_map):
        if n == 'SectionInfoMap':
            sim_idx = i
        elif n == 'StaticMaterials':
            sm_idx = i
        elif n == 'MaterialIndex':
            mi_idx = i
    if sim_idx is None or mi_idx is None:
        return None

    # Find SectionInfoMap property start
    target = struct.pack('<ii', sim_idx, 0)
    sim_offset = data.find(target)
    if sim_offset < 0:
        return None

    # Determine end of SectionInfoMap range (StaticMaterials comes after)
    search_end = len(data)
    if sm_idx is not None:
        target = struct.pack('<ii', sm_idx, 0)
        sm_offset = data.find(target, sim_offset + 8)
        if sm_offset >= 0:
            search_end = sm_offset

    # Scan for MaterialIndex FName occurrences and extract IntProperty values
    mi_pattern = struct.pack('<ii', mi_idx, 0)
    material_indices: List[int] = []
    offset = sim_offset
    while offset < search_end and len(material_indices) < num_sections:
        idx = data.find(mi_pattern, offset, search_end)
        if idx == -1:
            break

        # After the MaterialIndex FName (8 bytes) comes the type tree
        pos = idx + 8
        if pos + 12 > len(data):
            break

        type_idx = struct.unpack_from('<i', data, pos)[0]
        type_name = name_map[type_idx] if 0 <= type_idx < len(name_map) else ''
        pos += 8  # type FName
        inner_count = struct.unpack_from('<i', data, pos)[0]
        pos += 4  # inner_count

        if type_name == 'IntProperty' and inner_count == 0:
            if pos + 5 > len(data):
                break
            size = struct.unpack_from('<i', data, pos)[0]
            pos += 4  # size
            flags = data[pos]
            pos += 1  # flags

            if flags & 0x01:  # HasArrayIndex
                pos += 4
            if flags & 0x02:  # HasPropertyGuid
                pos += 16
            if flags & 0x04:  # HasPropertyExtensions
                if pos >= len(data):
                    break
                ext = data[pos]
                pos += 1
                if ext & 0x01:
                    pos += 1 + 4

            if size >= 4 and pos + 4 <= len(data):
                value = struct.unpack_from('<i', data, pos)[0]
                material_indices.append(value)

        offset = idx + 1

    if len(material_indices) >= num_sections:
        return material_indices[:num_sections]
    return None


# ---------------------------------------------------------------------------
# StaticMesh class
# ---------------------------------------------------------------------------

class StaticMesh:
    def __init__(self):
        self.vertices: List[Tuple[float, float, float]] = []
        self.normals: List[Tuple[float, float, float]] = []
        self.uvs: List[List[Tuple[float, float]]] = []  # list of UV channels
        self.triangles: List[Tuple[int, int, int, int]] = []  # (vi0, vi1, vi2, material_index)
        self.vi_to_vertex: List[int] = []
        self.material_slot_names: Optional[List[Optional[str]]] = None
        # Ordered list of (ImportedMaterialSlotName, material_import_name)
        # tuples parsed from the StaticMaterials export property, indexed by
        # material slot index.
        self.material_slots: Optional[List[Tuple[str, str]]] = None
        # SectionInfoMap: maps polygon group index → material slot index.
        self.section_info_map: Optional[List[int]] = None

    @classmethod
    def from_package(cls, pkg: Package) -> Optional['StaticMesh']:
        """Parse static mesh from a Package using the FMeshDescription pipeline."""
        mesh = cls()

        # Read raw file data
        file_data = pkg.reader.data

        # Extract FCompressedBuffer from package trailer
        compressed_buffer = extract_trailer_payload(file_data)
        if compressed_buffer is None:
            return None

        # Decompress
        raw_data = decompress_compressed_buffer(compressed_buffer)
        if raw_data is None:
            return None

        # Parse FMeshDescription
        reader = _MeshDescReader(raw_data)
        try:
            elements = reader.parse_mesh_description()
        except Exception:
            return None

        # Extract mesh data
        mesh_data = extract_mesh_data(elements)
        if mesh_data is None:
            return None

        mesh.vertices = mesh_data['vertices']
        mesh.vi_to_vertex = mesh_data['vi_to_vertex']
        mesh.normals = mesh_data['normals']
        mesh.uvs = mesh_data['uvs']
        mesh.triangles = mesh_data['triangles']
        mesh.material_slot_names = mesh_data.get('material_slot_names')

        # Parse real material slot mapping from export data
        mesh.material_slots = _parse_static_materials(pkg)

        # Apply SectionInfoMap overrides — the map can remap which
        # StaticMaterials entry each polygon group (section) uses.
        if mesh.material_slots and mesh.material_slot_names:
            sm_export_idx = None
            for i in range(pkg.export_count):
                if pkg.get_export_class_name(i) == 'StaticMesh':
                    sm_export_idx = i
                    break
            if sm_export_idx is not None:
                exp_reader = pkg.get_export_data(sm_export_idx)
                if exp_reader is not None:
                    section_map = _parse_section_info_map(
                        exp_reader.data, pkg.name_map,
                        len(mesh.material_slot_names))
                    mesh.section_info_map = section_map
                    if section_map is not None:
                        # section_map[pg_idx] = material slot index into
                        # StaticMaterials; mesh.material_slots[slot_idx] =
                        # (slot_name, material_name).  Remap material_slot_names
                        # so each PG gets the ImportedMaterialSlotName from the
                        # correct StaticMaterials entry.
                        remapped: List[Optional[str]] = []
                        for pg_idx in range(len(mesh.material_slot_names)):
                            if pg_idx < len(section_map):
                                mat_idx = section_map[pg_idx]
                                if mat_idx < len(mesh.material_slots):
                                    remapped.append(
                                        mesh.material_slots[mat_idx][0])
                                    continue
                            remapped.append(
                                mesh.material_slot_names[pg_idx])
                        mesh.material_slot_names = remapped

        return mesh


# ---------------------------------------------------------------------------
# GLB export
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# UE → glTF basis change
#
# UE is left-handed (X forward, Y right, Z up); glTF is right-handed (X right,
# Y up, -Z forward).  The axis map  glTF = (UE_Y, UE_Z, -UE_X)  has determinant
# -1, so it flips handedness — and therefore face winding CW→CCW — without
# needing a negative node scale.
#
# UE measures in centimetres, glTF in metres, so distances are scaled by 0.01.
# A UE 500-unit wall becomes a 5-metre wall rather than a 500-metre one.  The
# full conversion is  C = 0.01 · axis-swap.
# ---------------------------------------------------------------------------
# Default distance scale: glTF is metres, UE is centimetres, so 1 UE unit
# becomes 0.01 glTF units.  A UE 500-unit wall becomes a 5-metre wall rather
# than a 500-metre one.  Callers can override it (``--scale``) for pipelines
# that expect a different unit.
_UE_TO_GLTF_SCALE = 0.01

_AXIS_SWAP = np.array([
    [0.0, 1.0, 0.0],
    [0.0, 0.0, 1.0],
    [-1.0, 0.0, 0.0],
])


def _basis(scale: float) -> Tuple[np.ndarray, np.ndarray]:
    """The UE→glTF conversion ``C = scale · axis-swap`` and its inverse.

    The axis swap is orthogonal, so its inverse is its transpose; the scale
    inverts to ``1/scale``.  ``C`` itself is not orthogonal once scaled, which
    is why the inverse is built rather than transposed.
    """
    c = np.eye(4)
    c[:3, :3] = scale * _AXIS_SWAP
    c_inv = np.eye(4)
    c_inv[:3, :3] = (1.0 / scale) * _AXIS_SWAP.T
    return c, c_inv


def ue_matrix_to_gltf(matrix: np.ndarray,
                      scale: float = _UE_TO_GLTF_SCALE) -> List[float]:
    """Convert a UE world transform into a glTF node matrix.

    Vertices are already stored in glTF axes and scaled, so a UE transform has
    to be re-expressed in that basis rather than merely applied: the result is
    ``C·M·C⁻¹``.  The uniform scale cancels out of the rotation/scale part and
    survives only in the translation, which is exactly what keeps a placed
    instance's own proportions intact while moving it to the scaled
    coordinates.  Returned column-major, as glTF requires.
    """
    c, c_inv = _basis(scale)
    converted = c @ np.asarray(matrix, dtype=float) @ c_inv
    return [float(v) for v in converted.T.flatten()]


class MaterialSpec(NamedTuple):
    """One material slot's appearance, as the GLB writer wants it.

    A plain ``(material_index, pixels, key)`` tuple still works wherever this
    is accepted — the extra fields default to an opaque, untinted material —
    so callers that only have a texture need not build one of these.
    """
    material_index: int
    pixels: object = None
    texture_key: Optional[str] = None
    factor: Tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    alpha_mode: str = 'OPAQUE'
    alpha_cutoff: Optional[float] = None


def _as_rgba(pixels) -> np.ndarray:
    """A base-colour array as RGBA, whatever channel count it arrives with."""
    array = np.asarray(pixels)
    if array.ndim == 2:                       # greyscale
        array = np.stack([array] * 3, axis=-1)
    if array.shape[2] == 4:
        return array
    if array.shape[2] == 3:
        alpha = np.full(array.shape[:2] + (1,), 255, dtype=array.dtype)
        return np.concatenate([array, alpha], axis=2)
    raise ValueError(f"unsupported base-colour channel count {array.shape[2]}")


def _check_import_contract(mat, base_color_image) -> None:
    """Assert the two invariants that fail *silently* in the importer.

    Both are multiplicative-identity traps on the consuming side: the asset
    loads, renders, and merely looks wrong, so nothing downstream can catch
    them and they have to be caught here.

    1. A non-opaque material's base-colour image must be RGBA.  The importer
       decodes at the file's native channel count and pads 3-channel data to
       RGBA with alpha 0xFF, so an RGB image on a ``BLEND`` material loads
       fully opaque.
    2. ``emissiveFactor`` must be non-zero wherever ``emissiveTexture`` is set.
       The shader computes ``emissive = factor.rgb; if (tex) emissive *= tex``,
       and glTF defaults the factor to ``[0, 0, 0]`` — which multiplies any
       emissive texture to nothing.
    """
    alpha_mode = getattr(mat, 'alphaMode', None) or 'OPAQUE'
    if alpha_mode != 'OPAQUE' and base_color_image is not None:
        if base_color_image.mode != 'RGBA':
            raise AssertionError(
                f"base-colour image for a {alpha_mode} material is "
                f"{base_color_image.mode}, not RGBA — it would import as fully "
                f"opaque")

    if getattr(mat, 'emissiveTexture', None) is not None:
        factor = getattr(mat, 'emissiveFactor', None)
        if not factor or not any(factor):
            raise AssertionError(
                "emissiveTexture is set but emissiveFactor is zero/default — "
                "the texture would multiply to zero and be invisible")


class _GLBBuilder:
    """Accumulates meshes, materials and textures into one glTF binary blob.

    Shared by the single-mesh and whole-level exporters so both produce
    identical geometry, and so a level stores each distinct mesh once however
    many times it is placed.
    """

    # glTF constants
    ARRAY_BUFFER = 34962
    ELEMENT_ARRAY_BUFFER = 34963
    COMP_FLOAT = 5126
    COMP_UNSIGNED_SHORT = 5123
    COMP_UNSIGNED_INT = 5125

    def __init__(self, scale: float = _UE_TO_GLTF_SCALE):
        self.scale = scale
        self.binary = bytearray()
        self.buffer_views: list = []
        self.accessors: list = []
        self.images: list = []
        self.textures: list = []
        self.samplers: list = []
        self.materials: list = []
        self.meshes: list = []
        self.cameras: list = []
        # KHR_lights_punctual light definitions, referenced by node extension.
        self.lights: list = []
        # Texture identity -> glTF texture index.  A level places dozens of
        # meshes that share one texture; embedding it per mesh would multiply
        # the file size by the reuse count.
        self._texture_index: Dict[object, int] = {}
        # glTF texture index -> 'color' | 'data', so the same image is never
        # asked to be both.  See _note_texture_slot.
        self._texture_slot_kind: Dict[int, str] = {}

    def _note_texture_slot(self, texture_index: int, kind: str) -> None:
        """Record which colour space a texture is being used in, and enforce it.

        An importer that dedupes by image and classifies colour space on first
        use decodes the loser wrong: base colour and emissive are sRGB, while
        normal, metallic-roughness and occlusion are linear.  Only base colour
        is written today, so this cannot fire yet — it exists so that adding a
        normal or MR map cannot quietly reuse a base-colour image.
        """
        previous = self._texture_slot_kind.setdefault(texture_index, kind)
        if previous != kind:
            raise AssertionError(
                f"texture {texture_index} is used as both a {previous} and a "
                f"{kind} map — one colour-space classification would win and "
                f"the other would decode wrong; emit separate images")

    # -- lights & cameras -------------------------------------------------

    def add_light(self, spec) -> int:
        """Add one ``KHR_lights_punctual`` light; returns its index.

        Written straight through in the spec's units — candela for point and
        spot, lux for directional, radians for cone angles.  Nothing here
        rescales to suit a particular renderer.
        """
        light = {
            'type': spec.type,
            'color': [float(c) for c in spec.color],
            'intensity': float(spec.intensity),
        }
        if spec.name:
            light['name'] = spec.name
        if spec.range:
            light['range'] = float(spec.range)
        if spec.type == 'spot':
            light['spot'] = {
                'innerConeAngle': float(spec.inner_cone_angle or 0.0),
                'outerConeAngle': float(spec.outer_cone_angle
                                        if spec.outer_cone_angle is not None
                                        else math.pi / 4.0),
            }
        self.lights.append(light)
        return len(self.lights) - 1

    def add_camera(self, spec) -> int:
        """Add one perspective camera; returns its index.

        ``zfar`` is always written.  glTF treats an absent zfar as an infinite
        projection, which is a thing a consumer then has to invent a number
        for — better that the number is chosen here, where it is visible.
        """
        camera = Camera()
        camera.type = 'perspective'
        camera.perspective = Perspective(
            aspectRatio=float(spec.aspect_ratio),
            yfov=float(spec.yfov),
            znear=float(spec.znear),
            zfar=float(spec.zfar),
        )
        if spec.name:
            camera.name = spec.name
        self.cameras.append(camera)
        return len(self.cameras) - 1

    # -- buffer plumbing --------------------------------------------------

    def _pad4(self):
        """Pad the binary blob to 4-byte alignment."""
        rem = len(self.binary) % 4
        if rem:
            self.binary.extend(b'\x00' * (4 - rem))

    def _add_buffer_view(self, data: bytes, target=None) -> int:
        self._pad4()
        offset = len(self.binary)
        self.binary.extend(data)
        bv = BufferView()
        bv.buffer = 0
        bv.byteOffset = offset
        bv.byteLength = len(data)
        if target is not None:
            bv.target = target
        self.buffer_views.append(bv)
        return len(self.buffer_views) - 1

    def _add_accessor(self, bv_idx: int, component_type: int, count: int,
                      acc_type: str, min_vals=None, max_vals=None) -> int:
        acc = Accessor()
        acc.bufferView = bv_idx
        acc.byteOffset = 0
        acc.componentType = component_type
        acc.count = count
        acc.type = acc_type
        if min_vals is not None:
            acc.min = list(min_vals)
        if max_vals is not None:
            acc.max = list(max_vals)
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def _sampler(self) -> int:
        """Index of the shared texture sampler, created on first use."""
        if not self.samplers:
            sampler = Sampler()
            sampler.magFilter = 9729   # LINEAR
            sampler.minFilter = 9987   # LINEAR_MIPMAP_LINEAR
            sampler.wrapS = 10497      # REPEAT
            sampler.wrapT = 10497      # REPEAT
            self.samplers.append(sampler)
        return 0

    def add_mesh(self, mesh: 'StaticMesh',
                 textures: Optional[List[Tuple[int, object]]] = None,
                 name: Optional[str] = None) -> Optional[int]:
        """Add one static mesh; returns its glTF mesh index, or None if empty."""
        from io import BytesIO
        from PIL import Image as PILImage

        # Primary UV channel only
        uvs = mesh.uvs[0] if mesh.uvs else []
        has_normals = bool(mesh.normals)
        has_uvs = bool(uvs)

        # Group triangles by material index
        material_groups: dict = {}
        for tri in mesh.triangles:
            vi0, vi1, vi2 = tri[0], tri[1], tri[2]
            mat_idx = tri[3] if len(tri) >= 4 else 0
            material_groups.setdefault(mat_idx, []).append((vi0, vi1, vi2))

        if not material_groups:
            return None

        # Build texture lookup  material_index -> (PIL.Image, identity)
        # An optional third tuple element identifies the texture, letting the
        # same pixels be embedded once and shared by every material using it.
        # A spec may carry no pixels at all: a glass material's colour is a
        # constant, and it still needs its factor and blend mode.
        texture_lookup: dict = {}
        spec_lookup: dict = {}
        if textures:
            for item in textures:
                spec = (item if isinstance(item, MaterialSpec)
                        else MaterialSpec(*item))
                mat_idx, tex_data = spec.material_index, spec.pixels
                spec_lookup[mat_idx] = spec
                identity = spec.texture_key
                if isinstance(tex_data, np.ndarray):
                    array = tex_data
                    # A non-opaque material must reach the importer as RGBA:
                    # 3-channel data is padded with alpha 0xFF downstream, so
                    # a BLEND material silently loads fully opaque.  Opacity is
                    # composited here because the contract has no separate slot.
                    if spec.alpha_mode != 'OPAQUE':
                        array = _as_rgba(array)
                        if spec.factor[3] < 1.0:
                            array = array.copy()
                            array[..., 3] = (array[..., 3].astype(np.float32)
                                             * spec.factor[3]).astype(array.dtype)
                            # These pixels are no longer the texture as stored,
                            # so they need their own identity — otherwise two
                            # materials sharing a texture at different opacities
                            # would dedupe onto whichever was written first.
                            if identity is not None:
                                identity = (identity, round(spec.factor[3], 4))
                        spec = spec._replace(
                            factor=spec.factor[:3] + (1.0,),
                            texture_key=identity)
                        spec_lookup[mat_idx] = spec
                    if array.ndim == 3 and array.shape[2] == 4:
                        image = PILImage.fromarray(array, 'RGBA')
                    else:
                        image = PILImage.fromarray(array)
                elif hasattr(tex_data, 'save'):  # PIL.Image already
                    image = tex_data
                    if spec.alpha_mode != 'OPAQUE' and image.mode != 'RGBA':
                        image = image.convert('RGBA')
                else:
                    continue
                texture_lookup[mat_idx] = (image, identity)

        gltf_primitives: list = []

        # -- materials & textures -----------------------------------------
        sorted_mat_indices = sorted(material_groups.keys())
        mat_idx_to_gltf_mat: dict = {}

        for mat_idx in sorted_mat_indices:
            spec = spec_lookup.get(mat_idx, MaterialSpec(mat_idx))
            mat = Material()
            mat.pbrMetallicRoughness = PbrMetallicRoughness()
            mat.pbrMetallicRoughness.baseColorFactor = list(spec.factor)
            mat.pbrMetallicRoughness.metallicFactor = 0.0
            mat.pbrMetallicRoughness.roughnessFactor = 1.0
            if spec.alpha_mode != 'OPAQUE':
                mat.alphaMode = spec.alpha_mode
                if spec.alpha_mode == 'MASK' and spec.alpha_cutoff is not None:
                    mat.alphaCutoff = spec.alpha_cutoff
                # Both sides of a translucent surface are visible in UE, and a
                # cutout leaf is meaningless backface-culled.
                mat.doubleSided = True

            if mat_idx in texture_lookup:
                pil_img, identity = texture_lookup[mat_idx]
                tex_index = self._texture_index.get(identity) if identity else None

                if tex_index is None:
                    buf = BytesIO()
                    pil_img.save(buf, format='PNG')
                    png_bytes = buf.getvalue()

                    bv_idx = self._add_buffer_view(png_bytes)

                    img = GLTFImage()
                    img.bufferView = bv_idx
                    img.mimeType = 'image/png'
                    self.images.append(img)

                    tex = GLTFTexture()
                    tex.source = len(self.images) - 1
                    tex.sampler = self._sampler()
                    self.textures.append(tex)

                    tex_index = len(self.textures) - 1
                    if identity is not None:
                        self._texture_index[identity] = tex_index

                tex_info = TextureInfo()
                tex_info.index = tex_index
                # texCoord 0 always: only TEXCOORD_0 is written, and a consumer
                # that reads attribute 0 regardless would silently sample the
                # wrong set if this ever said otherwise.
                tex_info.texCoord = 0
                mat.pbrMetallicRoughness.baseColorTexture = tex_info
                self._note_texture_slot(tex_index, 'color')

            _check_import_contract(mat, pil_img if mat_idx in texture_lookup
                                   else None)
            self.materials.append(mat)
            mat_idx_to_gltf_mat[mat_idx] = len(self.materials) - 1

        self._build_primitives(mesh, uvs, has_normals, has_uvs,
                               material_groups, sorted_mat_indices,
                               mat_idx_to_gltf_mat, gltf_primitives)

        if not gltf_primitives:
            return None

        gltf_mesh = GLTFMesh(primitives=gltf_primitives)
        if name:
            gltf_mesh.name = name
        self.meshes.append(gltf_mesh)
        return len(self.meshes) - 1

    def _build_primitives(self, mesh, uvs, has_normals, has_uvs,
                          material_groups, sorted_mat_indices,
                          mat_idx_to_gltf_mat, gltf_primitives):
        """One primitive per material group, in glTF axes."""
        ARRAY_BUFFER = self.ARRAY_BUFFER
        ELEMENT_ARRAY_BUFFER = self.ELEMENT_ARRAY_BUFFER
        COMP_FLOAT = self.COMP_FLOAT
        COMP_UNSIGNED_SHORT = self.COMP_UNSIGNED_SHORT
        COMP_UNSIGNED_INT = self.COMP_UNSIGNED_INT
        _add_buffer_view = self._add_buffer_view
        _add_accessor = self._add_accessor
        scale = self.scale

        for mat_idx in sorted_mat_indices:
            tris = material_groups[mat_idx]

            # Collect unique vertex instances for this primitive
            vi_to_local: dict = {}
            local_verts: list = []  # (px, py, pz, nx, ny, nz, u, v)
            indices: list = []

            for vi0, vi1, vi2 in tris:
                for vi in (vi0, vi1, vi2):
                    if vi not in vi_to_local:
                        # Position — resolve through vi_to_vertex
                        v_idx = mesh.vi_to_vertex[vi] if vi < len(mesh.vi_to_vertex) else vi
                        pos = mesh.vertices[v_idx] if v_idx < len(mesh.vertices) else (0.0, 0.0, 0.0)
                        # UE → glTF:  x=ue_y, y=ue_z, z=-ue_x, scaled to glTF units
                        px, py, pz = pos[1] * scale, pos[2] * scale, -pos[0] * scale

                        # Normal — same coordinate conversion
                        if has_normals and vi < len(mesh.normals):
                            n = mesh.normals[vi]
                            nx, ny, nz = n[1], n[2], -n[0]
                        else:
                            nx, ny, nz = 0.0, 1.0, 0.0

                        # UV — no V-flip needed: UE5 stores textures top-to-bottom
                        # (same as PNG/glTF), and the UV coordinate (0,0) already
                        # maps to the first pixel row in both engines.
                        if has_uvs and vi < len(uvs):
                            u, v = uvs[vi]
                        else:
                            u, v = 0.0, 0.0

                        local_verts.append((px, py, pz, nx, ny, nz, u, v))
                        vi_to_local[vi] = len(local_verts) - 1

                    indices.append(vi_to_local[vi])

            if not local_verts:
                continue

            num_verts = len(local_verts)

            # Numpy arrays
            pos_arr = np.array([(v[0], v[1], v[2]) for v in local_verts], dtype=np.float32)
            norm_arr = np.array([(v[3], v[4], v[5]) for v in local_verts], dtype=np.float32)
            uv_arr = np.array([(v[6], v[7]) for v in local_verts], dtype=np.float32)

            if num_verts <= 65535:
                idx_arr = np.array(indices, dtype=np.uint16)
                idx_comp = COMP_UNSIGNED_SHORT
            else:
                idx_arr = np.array(indices, dtype=np.uint32)
                idx_comp = COMP_UNSIGNED_INT

            # Position accessor
            pos_bv = _add_buffer_view(pos_arr.tobytes(), target=ARRAY_BUFFER)
            pos_acc = _add_accessor(
                pos_bv, COMP_FLOAT, num_verts, "VEC3",
                pos_arr.min(axis=0).tolist(), pos_arr.max(axis=0).tolist())

            # Normal accessor
            norm_acc = None
            if has_normals:
                norm_bv = _add_buffer_view(norm_arr.tobytes(), target=ARRAY_BUFFER)
                norm_acc = _add_accessor(norm_bv, COMP_FLOAT, num_verts, "VEC3")

            # UV accessor
            uv_acc = None
            if has_uvs:
                uv_bv = _add_buffer_view(uv_arr.tobytes(), target=ARRAY_BUFFER)
                uv_acc = _add_accessor(uv_bv, COMP_FLOAT, num_verts, "VEC2")

            # Index accessor
            idx_bv = _add_buffer_view(idx_arr.tobytes(), target=ELEMENT_ARRAY_BUFFER)
            idx_acc = _add_accessor(idx_bv, idx_comp, len(indices), "SCALAR")

            # Primitive
            prim = Primitive()
            prim.attributes.POSITION = pos_acc
            if norm_acc is not None:
                prim.attributes.NORMAL = norm_acc
            if uv_acc is not None:
                prim.attributes.TEXCOORD_0 = uv_acc
            prim.indices = idx_acc
            prim.material = mat_idx_to_gltf_mat[mat_idx]

            gltf_primitives.append(prim)

    # -- output -----------------------------------------------------------

    def save(self, filepath: str, nodes: list):
        """Write the accumulated data out as a .glb."""
        dirpath = os.path.dirname(filepath)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)

        gltf = GLTF2()
        gltf.scene = 0
        gltf.scenes = [GLTFScene(nodes=list(range(len(nodes))))]
        gltf.nodes = nodes
        gltf.meshes = self.meshes
        gltf.materials = self.materials
        if self.textures:
            gltf.textures = self.textures
        if self.samplers:
            gltf.samplers = self.samplers
        if self.images:
            gltf.images = self.images
        if self.cameras:
            gltf.cameras = self.cameras
        if self.lights:
            gltf.extensions = dict(gltf.extensions or {})
            gltf.extensions['KHR_lights_punctual'] = {'lights': self.lights}
            used = list(gltf.extensionsUsed or [])
            if 'KHR_lights_punctual' not in used:
                used.append('KHR_lights_punctual')
            gltf.extensionsUsed = used
        gltf.accessors = self.accessors
        gltf.bufferViews = self.buffer_views
        gltf.buffers = [Buffer(byteLength=len(self.binary))]

        gltf.set_binary_blob(bytes(self.binary))
        gltf.save(filepath)


def export_glb(mesh: StaticMesh, filepath: str,
               textures: Optional[List[Tuple[int, object]]] = None,
               scale: float = _UE_TO_GLTF_SCALE):
    """Export a StaticMesh as GLB (binary glTF 2.0) with embedded textures.

    Args:
        mesh: StaticMesh object with geometry data.
        filepath: Output ``.glb`` file path.
        textures: Optional list of ``(material_index, PIL.Image or numpy.ndarray)``
            tuples.  Each texture is embedded as PNG inside the GLB and assigned
            to the corresponding material slot.  If *None* or empty, a default
            grey material is used for every primitive.
        scale: UE-unit → glTF-unit factor; defaults to centimetres → metres.
    """
    if not _HAS_PYGLTFLIB:
        raise ImportError(
            "pygltflib is required for GLB export.  "
            "Install with: pip install pygltflib"
        )

    builder = _GLBBuilder(scale=scale)
    mesh_index = builder.add_mesh(mesh, textures)
    if mesh_index is None:
        return

    # No negative scale needed — the axis remap (UE_Y, UE_Z, -UE_X)
    # already flips handedness and is baked into the geometry.
    builder.save(filepath, [GLTFNode(mesh=mesh_index)])


def export_level_glb(meshes: Dict[str, Tuple['StaticMesh', object]],
                     placements: List[Tuple[str, str, np.ndarray]],
                     filepath: str,
                     scale: float = _UE_TO_GLTF_SCALE,
                     lights: Optional[List[Tuple[object, np.ndarray]]] = None,
                     cameras: Optional[List[Tuple[object, np.ndarray]]] = None
                     ) -> Tuple[int, int]:
    """Export a whole level as one GLB with every actor already positioned.

    Each distinct mesh is stored once and referenced by a node per placement,
    so a level that puts the same wall panel down eighty times costs one copy
    of its geometry rather than eighty.

    Args:
        meshes:     ``{key: (StaticMesh, textures)}`` for each distinct mesh,
                    where *textures* is what :func:`export_glb` accepts.
        placements: ``(node_name, mesh_key, ue_world_matrix)`` per actor.
                    Placements naming a key absent from *meshes* are skipped.
        filepath:   Output ``.glb`` file path.
        scale:      UE-unit → glTF-unit factor; defaults to centimetres →
                    metres.  Applied identically to geometry and placements,
                    so the assembled level stays self-consistent.
        lights:     ``(LightSpec, ue_world_matrix)`` per light.
        cameras:    ``(CameraSpec, ue_world_matrix)`` per camera.

    Returns:
        ``(meshes_written, nodes_written)``.  Light and camera nodes count
        towards the node total.
    """
    if not _HAS_PYGLTFLIB:
        raise ImportError(
            "pygltflib is required for GLB export.  "
            "Install with: pip install pygltflib"
        )

    builder = _GLBBuilder(scale=scale)

    mesh_indices: Dict[str, int] = {}
    for key, (mesh, textures) in meshes.items():
        index = builder.add_mesh(mesh, textures, name=key)
        if index is not None:
            mesh_indices[key] = index

    nodes = []
    for node_name, mesh_key, world_matrix in placements:
        index = mesh_indices.get(mesh_key)
        if index is None:
            continue
        node = GLTFNode(mesh=index)
        node.name = node_name
        node.matrix = ue_matrix_to_gltf(world_matrix, scale)
        nodes.append(node)

    # A light or camera needs no basis correction beyond the one the meshes
    # get: UE aims down +X with +Z up, and the axis remap sends +X to -Z and
    # +Z to +Y, which is exactly how glTF orients a light or a camera.
    for spec, world_matrix in (lights or ()):
        node = GLTFNode()
        node.name = spec.name
        node.matrix = ue_matrix_to_gltf(world_matrix, scale)
        node.extensions = {
            'KHR_lights_punctual': {'light': builder.add_light(spec)}
        }
        if spec.extras:
            node.extras = dict(spec.extras)
        nodes.append(node)

    for spec, world_matrix in (cameras or ()):
        node = GLTFNode(camera=builder.add_camera(spec))
        node.name = spec.name
        node.matrix = ue_matrix_to_gltf(world_matrix, scale)
        nodes.append(node)

    if not nodes:
        return 0, 0

    builder.save(filepath, nodes)
    return len(mesh_indices), len(nodes)

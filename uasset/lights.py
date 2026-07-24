"""UE light and camera components → glTF ``KHR_lights_punctual`` and cameras.

Everything here is a unit conversion, and every constant is taken from the
UE 5.5 source rather than guessed (project rule 2).  The rule the whole module
follows is that values go out in the units the glTF spec asks for — candela for
point and spot, lux for directional, radians for angles — with no scaling to
make a particular renderer look right.  A consumer that wants different units
converts them where that decision is visible.

UE stores intensity in one of four units *per light* (``ELightUnits``), and a
level usually mixes them, so the unit is read from each component rather than
assumed.  Where a property is absent it was left at its UE default and the
default is supplied here; ``Intensity`` alone defaults to 5000, so guessing
would be conspicuous.
"""
import math
import struct
from typing import Dict, List, NamedTuple, Optional, Tuple

from .package import Package
from .reader import resolve_fname


# ---------------------------------------------------------------------------
# Constants from the UE 5.5 source
# ---------------------------------------------------------------------------

# Engine/Source/Runtime/Engine/Classes/Engine/Scene.h — enum class ELightUnits
_LIGHT_UNITS = ('Unitless', 'Candelas', 'Lumens', 'EV')

# ULocalLightComponent::ULocalLightComponent — LocalLightComponent.cpp
_DEFAULT_INTENSITY = 5000.0
_DEFAULT_INTENSITY_UNITS = 'Unitless'
_DEFAULT_ATTENUATION_RADIUS = 1000.0        # cm

# USpotLightComponent::USpotLightComponent — SpotLightComponent.cpp
_DEFAULT_INNER_CONE_DEG = 0.0
_DEFAULT_OUTER_CONE_DEG = 44.0

# URectLightComponent::URectLightComponent — RectLightComponent.cpp
_DEFAULT_SOURCE_WIDTH = 64.0                # cm
_DEFAULT_SOURCE_HEIGHT = 64.0               # cm

# ULightComponent::ULightComponent — LightComponent.cpp
_DEFAULT_TEMPERATURE = 6500.0
_DEFAULT_LIGHT_COLOR = (255, 255, 255)      # FColor, sRGB

# UCameraComponent::UCameraComponent — CameraComponent.cpp
_DEFAULT_FOV_DEG = 90.0
_DEFAULT_ASPECT = 1.777778

# Engine/Config/BaseEngine.ini — NearClipPlane=10.0
_UE_NEAR_CLIP_CM = 10.0

# USpotLightComponent::GetHalfConeAngle clamps before taking the cosine.
_MAX_CONE_DEG = 89.0

# UE works in centimetres, so every ComputeLightBrightness() carries a cm²→m²
# factor of 100*100.  Dividing it back out is what yields candela.
_CM2_PER_M2 = 100.0 * 100.0

# UPointLightComponent::ComputeLightBrightness — "Legacy scale of 16".
_UNITLESS_SCALE = 16.0

# Light component class → the punctual type it maps to.  RectLight has no glTF
# equivalent and is encoded as a spot; see rect_light_extras.
_LIGHT_COMPONENT_TYPES = {
    'PointLightComponent': 'point',
    'SpotLightComponent': 'spot',
    'RectLightComponent': 'rect',
    'DirectionalLightComponent': 'directional',
}

CAMERA_COMPONENT_CLASSES = ('CineCameraComponent', 'CameraComponent')

LIGHT_COMPONENT_CLASSES = tuple(_LIGHT_COMPONENT_TYPES)


class LightSpec(NamedTuple):
    """One light, in the units ``KHR_lights_punctual`` defines.

    *intensity* is candela for point and spot, lux for directional.  *range* is
    in glTF units (metres at the default scale) or None where UE's attenuation
    radius should not be treated as a hard cutoff.  Cone angles are radians.
    """
    name: str
    type: str                                   # 'point' | 'spot' | 'directional'
    color: Tuple[float, float, float]           # linear, 0..1
    intensity: float
    range: Optional[float] = None
    inner_cone_angle: Optional[float] = None
    outer_cone_angle: Optional[float] = None
    extras: Optional[dict] = None               # node extras (rect lights)


class CameraSpec(NamedTuple):
    """One perspective camera, angles in radians and distances in glTF units."""
    name: str
    yfov: float
    aspect_ratio: float
    znear: float
    zfar: float
    orthographic: bool = False


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

def srgb_to_linear(channel: float) -> float:
    """One sRGB channel (0..1) to linear.

    ``FLinearColor(const FColor&)`` runs every channel through
    ``sRGBToLinearTable``, so a UE ``LightColor`` is sRGB and glTF wants linear.
    """
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


# CIE XYZ (D65) → linear sRGB.  UE builds this from the working colour space's
# primaries; the working space is sRGB/Rec709, so these are its coefficients.
_XYZ_TO_LINEAR_SRGB = (
    (3.2409699419, -1.5373831776, -0.4986107603),
    (-0.9692436363, 1.8759675015, 0.0415550574),
    (0.0556300797, -0.2039769589, 1.0569715142),
)


def color_temperature_to_linear(kelvin: float) -> Tuple[float, float, float]:
    """Blackbody colour for *kelvin*, as ``FColorSpace::MakeFromColorTemperature``.

    UE approximates the Planckian locus in CIE 1960 UCS, converts to XYZ at
    unit luminance and then to the working colour space, clamping the negatives
    the transform can produce.  Reproduced coefficient-for-coefficient so a
    light with bUseTemperature matches what the editor showed.
    """
    temp = min(max(kelvin, 1000.0), 15000.0)
    u = ((0.860117757 + 1.54118254e-4 * temp + 1.28641212e-7 * temp * temp)
         / (1.0 + 8.42420235e-4 * temp + 7.08145163e-7 * temp * temp))
    v = ((0.317398726 + 4.22806245e-5 * temp + 4.20481691e-8 * temp * temp)
         / (1.0 - 2.89741816e-5 * temp + 1.61456053e-7 * temp * temp))

    denominator = 2.0 * u - 8.0 * v + 4.0
    x = 3.0 * u / denominator
    y = 2.0 * v / denominator
    z = 1.0 - x - y

    xyz = (x / y, 1.0, z / y)
    return tuple(
        max(0.0, sum(row[i] * xyz[i] for i in range(3)))
        for row in _XYZ_TO_LINEAR_SRGB
    )


# ---------------------------------------------------------------------------
# Property helpers
#
# umap.py reads properties with properties.read_properties, which returns
# typed values for the common types and raw bytes for the rest.  An FColor and
# an enum both arrive as bytes, so they are decoded here.
# ---------------------------------------------------------------------------

def _float(props: dict, name: str, default: float) -> float:
    value = props.get(name)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (bytes, bytearray)) and len(value) >= 4:
        return struct.unpack_from('<f', value, 0)[0]
    return default


def _bool(props: dict, name: str, default: bool = False) -> bool:
    value = props.get(name)
    return bool(value) if isinstance(value, bool) else default


# Properties whose value is an FName into the package's name table — an enum
# case, or an object reference — and so cannot be read once the package is
# closed.  resolve_component_properties turns them into plain strings while the
# package is still open.
_ENUM_PROPERTIES = ('IntensityUnits', 'ProjectionMode', 'Mobility')
_OBJECT_PROPERTIES = ('SourceTexture', 'IESTexture', 'LightFunctionMaterial')


def resolve_component_properties(pkg: Package, props: dict) -> dict:
    """Resolve a component's name-table-dependent properties to strings.

    ``read_properties`` hands back raw bytes for enum values, because decoding
    one needs the package's name table.  A parsed level outlives the packages
    it came from, so the few properties that need it are resolved here, at
    parse time, rather than by keeping every package open.
    """
    resolved = dict(props)
    for name in _ENUM_PROPERTIES:
        value = props.get(name)
        if isinstance(value, (bytes, bytearray)) and len(value) >= 8:
            index, number = struct.unpack_from('<ii', value, 0)
            resolved[name] = resolve_fname(pkg.name_map, index, number)
    for name in _OBJECT_PROPERTIES:
        value = props.get(name)
        if isinstance(value, int) and value:
            referenced = _resolve_object_name(pkg, value)
            if referenced is not None:
                resolved[name] = referenced
    return resolved


def _enum(props: dict, name: str) -> Optional[str]:
    """An enum-valued property, rendered as UE writes it (``Type::Value``)."""
    value = props.get(name)
    return value if isinstance(value, str) else None


def _light_units(props: dict) -> str:
    """The light's ``ELightUnits``, read per light rather than assumed.

    UE serializes the property only when it differs from the default, and the
    default is Unitless — which is why an explicitly authored Candelas light
    always writes one.
    """
    raw = _enum(props, 'IntensityUnits')
    if not raw:
        return _DEFAULT_INTENSITY_UNITS
    name = raw.rsplit('::', 1)[-1]
    return name if name in _LIGHT_UNITS else _DEFAULT_INTENSITY_UNITS


def _light_color(props: dict) -> Tuple[float, float, float]:
    """``LightColor`` as linear RGB.  FColor is stored B, G, R, A."""
    value = props.get('LightColor')
    if isinstance(value, (bytes, bytearray)) and len(value) >= 3:
        b, g, r = value[0], value[1], value[2]
    else:
        r, g, b = _DEFAULT_LIGHT_COLOR
    return tuple(srgb_to_linear(c / 255.0) for c in (r, g, b))


def _normalize_color(color: Tuple[float, float, float], intensity: float
                     ) -> Tuple[Tuple[float, float, float], float]:
    """Move any colour component above 1 into the intensity.

    A blackbody tint carries unit *luminance*, not unit maximum, so
    ``MakeFromColorTemperature`` legitimately returns components over 1 — 1500 K
    is (3.27, 0.43, 0.00).  glTF wants a normalized colour with the magnitude in
    ``intensity``, and since a renderer only ever uses the product
    ``color * intensity`` this rescaling is exact rather than a fudge.
    """
    brightest = max(color)
    if brightest <= 1.0 or brightest <= 0.0:
        return color, intensity
    return tuple(c / brightest for c in color), intensity * brightest


def _cos_half_cone(props: dict) -> float:
    """``USpotLightComponent::GetCosHalfConeAngle``, clamps included."""
    inner = _float(props, 'InnerConeAngle', _DEFAULT_INNER_CONE_DEG)
    outer = _float(props, 'OuterConeAngle', _DEFAULT_OUTER_CONE_DEG)
    inner_rad = math.radians(min(max(inner, 0.0), _MAX_CONE_DEG))
    outer_rad = min(max(math.radians(outer), inner_rad + 0.001),
                    math.radians(_MAX_CONE_DEG) + 0.001)
    return math.cos(outer_rad)


# ---------------------------------------------------------------------------
# Intensity
# ---------------------------------------------------------------------------

def intensity_in_candela(kind: str, intensity: float, units: str,
                         cos_half_cone: float = -1.0) -> float:
    """Convert a local light's authored intensity to candela.

    Each branch mirrors that component's ``ComputeLightBrightness()``, divided
    by the cm²→m² factor UE carries because it works in centimetres.  What is
    left is luminous intensity in candela, which is what
    ``KHR_lights_punctual`` wants for point and spot.

    The rect case is the one worth spelling out.  A rect light is a Lambertian
    emitter, so its total flux is ``Φ = L·A·π`` and its on-axis intensity is
    ``Φ/π`` — UE's ``Lumens`` branch divides by exactly that π.  Treating it as
    a point source (``Φ/4π``) would under-light the forward direction 4×.
    """
    if units == 'Candelas':
        return intensity
    if units == 'Lumens':
        if kind == 'point':
            return intensity / (4.0 * math.pi)          # sphere
        if kind == 'spot':
            return intensity / (2.0 * math.pi * (1.0 - cos_half_cone))
        if kind == 'rect':
            return intensity / math.pi                  # cosine distribution
        return intensity
    if units == 'EV':
        # EV100ToLuminance(EV) = 2^EV with LuminanceMax 1 (RenderUtils.h).
        return 2.0 ** intensity
    # Unitless — UE's "legacy scale of 16", then cm² → m².
    return intensity * _UNITLESS_SCALE / _CM2_PER_M2


# ---------------------------------------------------------------------------
# Components → specs
# ---------------------------------------------------------------------------

def rect_light_extras(props: dict, scale: float,
                      units: str, raw_intensity: float) -> dict:
    """The rect light description that glTF has nowhere to put.

    There is no ratified area-light extension — ``KHR_lights_area`` was closed
    in 2023 — so the punctual spot this becomes is lossy by necessity.  The
    field names follow the draft proposal so migrating to a real extension
    later is mechanical, and the authored intensity is kept **with its unit
    string** so no one has to invert the conversion above to recover it.
    """
    width = _float(props, 'SourceWidth', _DEFAULT_SOURCE_WIDTH) * scale
    height = _float(props, 'SourceHeight', _DEFAULT_SOURCE_HEIGHT) * scale
    extras = {
        'shape': 'rect',
        'width': width,
        'height': height,
        'ue_intensity': raw_intensity,
        'ue_intensity_units': units,
    }
    if 'BarnDoorAngle' in props:
        extras['barnDoorAngle'] = math.radians(_float(props, 'BarnDoorAngle', 0.0))
    if 'BarnDoorLength' in props:
        extras['barnDoorLength'] = _float(props, 'BarnDoorLength', 0.0) * scale
    texture = props.get('SourceTexture')
    if isinstance(texture, str) and texture:
        extras['sourceTexture'] = texture
    return extras


def _resolve_object_name(pkg: Package, index: int) -> Optional[str]:
    if index > 0 and index - 1 < len(pkg.exports):
        return pkg.exports[index - 1].object_name
    if index < 0 and -index - 1 < len(pkg.imports):
        return pkg.imports[-index - 1].object_name
    return None


def light_from_component(props: dict, class_name: str,
                         name: str, scale: float) -> Optional[LightSpec]:
    """Build a :class:`LightSpec` from one light component's properties.

    *props* must have been through :func:`resolve_component_properties`.
    Returns None for a component class with no punctual equivalent at all.
    """
    kind = _LIGHT_COMPONENT_TYPES.get(class_name)
    if kind is None:
        return None

    units = _light_units(props)
    raw_intensity = _float(props, 'Intensity', _DEFAULT_INTENSITY)

    color = _light_color(props)
    if _bool(props, 'bUseTemperature'):
        temperature = _float(props, 'Temperature', _DEFAULT_TEMPERATURE)
        tint = color_temperature_to_linear(temperature)
        color = tuple(c * t for c, t in zip(color, tint))

    if kind == 'directional':
        # UE authors a directional light in lux, which is already glTF's unit.
        color, lux = _normalize_color(color, raw_intensity)
        return LightSpec(name, 'directional', color, lux)

    attenuation = _float(props, 'AttenuationRadius',
                         _DEFAULT_ATTENUATION_RADIUS) * scale

    if kind == 'spot':
        cos_half = _cos_half_cone(props)
        inner = math.radians(min(max(_float(props, 'InnerConeAngle',
                                            _DEFAULT_INNER_CONE_DEG),
                                     0.0), _MAX_CONE_DEG))
        outer = math.acos(max(-1.0, min(1.0, cos_half)))
        color, candela = _normalize_color(
            color, intensity_in_candela('spot', raw_intensity, units, cos_half))
        return LightSpec(name, 'spot', color, candela, attenuation, inner, outer)

    if kind == 'rect':
        # A rect light is one-sided, and spot is the only punctual type that
        # says so.  A hemisphere cone leaves the encoding as close to the real
        # emitter as the spec allows.
        color, candela = _normalize_color(
            color, intensity_in_candela('rect', raw_intensity, units))
        return LightSpec(
            name, 'spot', color, candela, attenuation, 0.0, math.pi / 2.0,
            rect_light_extras(props, scale, units, raw_intensity))

    color, candela = _normalize_color(
        color, intensity_in_candela('point', raw_intensity, units))
    return LightSpec(name, 'point', color, candela, attenuation)


def camera_from_component(props: dict, name: str, scale: float,
                          zfar: float) -> CameraSpec:
    """Build a :class:`CameraSpec` from a camera component's properties.

    glTF wants a *vertical* field of view in radians; UE stores a horizontal
    one in degrees, alongside the aspect ratio needed to relate them.  A
    CineCameraComponent keeps the value it computed from its filmback in
    ``CurrentHorizontalFOV``, so the filmback itself does not need unpacking.
    """
    projection = _enum(props, 'ProjectionMode') or ''
    orthographic = projection.rsplit('::', 1)[-1] == 'Orthographic'

    aspect = _float(props, 'AspectRatio', _DEFAULT_ASPECT)
    if aspect <= 0:
        aspect = _DEFAULT_ASPECT
    hfov_deg = _float(props, 'CurrentHorizontalFOV',
                      _float(props, 'FieldOfView', _DEFAULT_FOV_DEG))
    hfov = math.radians(min(max(hfov_deg, 1e-3), 179.0))
    yfov = 2.0 * math.atan(math.tan(hfov / 2.0) / aspect)

    return CameraSpec(name, yfov, aspect, _UE_NEAR_CLIP_CM * scale, zfar,
                      orthographic)

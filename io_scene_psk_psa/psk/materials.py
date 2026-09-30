"""
Automatic material & texture setup for imported PSK files.

The textures of each material are found using, in order of preference:

1. Material definition files that are exported alongside the meshes by the common extraction tools:
   * FModel / CUE4Parse material `.json` files (both the simplified "Textures"/"Parameters" format and the raw
     property dump format).
   * UE Viewer (umodel) `.mat` files and `.props.txt` files.
2. Matching the name of the material against the names of the texture files (e.g., `M_Body` -> `T_Body_D.png`).

The folder of the PSK file, its parent folder (and their sub-folders), the folders matching the original asset paths
(e.g., `/Game/Characters/...`) and an optional user-provided folder are searched.
"""
import json
import os
import re
from pathlib import Path

import bpy
from bpy.types import Material

TEXTURE_EXTENSIONS = ('.png', '.tga', '.dds', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.exr', '.hdr', '.webp')
MATERIAL_EXTENSIONS = ('.json', '.mat', '.props.txt')

# The maximum number of files that are indexed from each search root (to avoid scanning huge folders).
MAX_INDEXED_FILES = 20000

BASE_COLOR_NODE_NAME = 'PSK Base Color'
ALPHA_CANDIDATE_PROPERTY = 'psk_alpha_candidate'
# The opacity mask clip value used by Unreal Engine by default.
OPACITY_MASK_CLIP_VALUE = 1.0 / 3.0
MAX_SEARCH_DEPTH = 5
# How many folders above the PSK file are searched (e.g., `Model/<Id>/Mesh/<Mesh>/x.psk` -> `Model/` and beyond, so
# that materials & textures shared between models are found).
MAX_PARENT_LEVELS = 5
# Folders that are never searched from (too broad).
_STOP_FOLDER_NAMES = {'home', 'users', 'documents', 'desktop', 'downloads', 'onedrive'}
# The time budget for indexing the files (seconds).
MAX_INDEX_SECONDS = 5.0

# Unreal blend modes.
BLEND_OPAQUE = 0
BLEND_MASKED = 1
BLEND_TRANSLUCENT = 2
BLEND_ADDITIVE = 3
BLEND_MODULATE = 4

_BLEND_MODE_NAMES = {
    'BLEND_OPAQUE': BLEND_OPAQUE,
    'BLEND_MASKED': BLEND_MASKED,
    'BLEND_TRANSLUCENT': BLEND_TRANSLUCENT,
    'BLEND_ADDITIVE': BLEND_ADDITIVE,
    'BLEND_MODULATE': BLEND_MODULATE,
}

# Texture parameter names for each slot, in order of preference (lower-case, non-alphanumerics removed).
_SLOT_PARAMETERS = {
    'base_color': ('diffuse', 'basecolor', 'basecolortexture', 'basecolormap', 'albedo', 'albedotexture',
                   'pmdiffuse', 'diffusetexture', 'diffusemap', 'colortexture', 'color', 'maintex', 'maintexture',
                   'basetexture', 'basemap', 'colormap', 'd', 'bc'),
    'normal': ('normal', 'normals', 'normalmap', 'normaltexture', 'pmnormals', 'pmnormal', 'bump', 'bumpmap', 'n'),
    'orm': ('orm', 'occlusionroughnessmetallic', 'aorm', 'ormtexture', 'packedtexture', 'packed'),
    'mrao': ('mrao', 'metallicroughnessao', 'mra'),
    'ao': ('ao', 'textureao', 'aotexture', 'aomap', 'ambientocclusion', 'occlusion', 'occlusionmap', 'occlusiontexture'),
    'emissive': ('emissive', 'emission', 'emissivetexture', 'emissivemap', 'pmemissive', 'glow'),
    'opacity': ('opacity', 'opacitymask', 'alpha', 'alphamask', 'opacitytexture'),
}

# Texture name suffixes for each slot (lower-case).
_SLOT_SUFFIXES = {
    'base_color': ('_d', '_bc', '_basecolor', '_albedo', '_diffuse', '_diff', '_col', '_color', '_c', '_a', '_alb'),
    'normal': ('_n', '_normal', '_nrm', '_nm', '_norm', '_normals'),
    'orm': ('_orm', '_arm', '_rma'),
    'mrao': ('_mrao', '_mra'),
    'ao': ('_ao', '_occ', '_occlusion', '_ambientocclusion'),
    'emissive': ('_e', '_emissive', '_emission', '_em', '_glow'),
    'opacity': ('_o', '_opacity', '_alpha', '_op'),
}

# Words in parameter names that indicate that the texture is not the main texture for the slot.
_EXCLUDED_PARAMETER_WORDS = ('overwrite', 'override', 'scroll', 'decal', 'ice', 'freeze', 'detail', 'mask', 'noise',
                             'dissolve', 'kizu', 'debug', 'heatmap', 'default', 'edit', 'ramp', 'gradient', 'tint',
                             'shade', 'shadow', 'toon', 'matcap', 'rim', 'outline', 'cube', 'env', 'reflection', 'blend',
                             'layer', 'second', 'secondary', 'wet', 'dirt', 'damage', 'offset')


def _normalize(name: str) -> str:
    return re.sub(r'[^a-z0-9]', '', name.lower())


def _asset_name(path: str) -> str:
    """
    Returns the asset name from an Unreal object path, e.g.:
      `/Game/Characters/Tex/T_Body_D.T_Body_D` -> `T_Body_D`
      `Texture2D'/Game/Characters/Tex/T_Body_D.T_Body_D'` -> `T_Body_D`
      `Texture2D'T_Body_D'` -> `T_Body_D`
    """
    path = path.strip()
    match = re.match(r"^\w+'(.*)'$", path)
    if match:
        path = match.group(1)
    path = path.strip('"\'')
    name = path.replace('\\', '/').split('/')[-1]
    if '.' in name:
        name = name.split('.')[0]
    return name


def _asset_relative_path(path: str) -> str | None:
    """Returns the package path of an Unreal object path relative to the content root (e.g., `Characters/Tex/T_Body`)."""
    path = path.strip()
    match = re.match(r"^\w+'(.*)'$", path)
    if match:
        path = match.group(1)
    path = path.strip('"\'').replace('\\', '/')
    if '/' not in path:
        return None
    path = path.split('.')[0]
    parts = [p for p in path.split('/') if p]
    if parts and parts[0].lower() in ('game', 'content', 'engine'):
        parts = parts[1:]
    return '/'.join(parts) if parts else None


class MaterialDefinition:
    def __init__(self):
        # Parameter name -> texture asset path (or name).
        self.textures: dict[str, str] = {}
        self.blend_mode: int | None = None
        self.source: str = ''


def _parse_json_material(path: Path) -> MaterialDefinition | None:
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    definition = MaterialDefinition()
    definition.source = str(path)

    if isinstance(data, dict) and 'Textures' in data:
        # FModel's simplified material format.
        textures = data.get('Textures') or {}
        if isinstance(textures, dict):
            for key, value in textures.items():
                if isinstance(value, str):
                    definition.textures[key] = value
        parameters = data.get('Parameters') or {}
        if isinstance(parameters, dict):
            blend_mode = parameters.get('BlendMode')
            if isinstance(blend_mode, int):
                definition.blend_mode = blend_mode
            elif isinstance(blend_mode, str):
                definition.blend_mode = _BLEND_MODE_NAMES.get(blend_mode.upper())
        return definition

    # The raw property dump format (a list of exports).
    exports = data if isinstance(data, list) else [data]
    for export in exports:
        if not isinstance(export, dict):
            continue
        if 'Material' not in str(export.get('Type', '')):
            continue
        properties = export.get('Properties') or {}
        for item in properties.get('TextureParameterValues', []) or []:
            try:
                info = item.get('ParameterInfo') or {}
                name = info.get('Name') or item.get('ParameterName') or ''
                value = item.get('ParameterValue')
                if isinstance(value, dict):
                    value = value.get('ObjectPath') or value.get('ObjectName') or ''
                if name and isinstance(value, str) and value:
                    definition.textures[name] = value
            except AttributeError:
                continue
        blend_mode = properties.get('BlendMode')
        overrides = properties.get('BasePropertyOverrides') or {}
        if overrides.get('bOverride_BlendMode') or blend_mode is None:
            blend_mode = overrides.get('BlendMode', blend_mode)
        if isinstance(blend_mode, str):
            definition.blend_mode = _BLEND_MODE_NAMES.get(blend_mode.split('::')[-1].upper())
        elif isinstance(blend_mode, int):
            definition.blend_mode = blend_mode
        # Some exports also list the textures referenced by the material.
        for i, value in enumerate(properties.get('ReferencedTextures', []) or []):
            if isinstance(value, dict):
                value = value.get('ObjectPath') or value.get('ObjectName')
            if isinstance(value, str) and value:
                definition.textures.setdefault(f'ReferencedTexture{i}', value)
    return definition if definition.textures else None


def _parse_umodel_mat(path: Path) -> MaterialDefinition | None:
    definition = MaterialDefinition()
    definition.source = str(path)
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                if '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key, value = key.strip(), value.strip()
                if value:
                    definition.textures[key] = value
    except OSError:
        return None
    return definition if definition.textures else None


def _parse_props_txt(path: Path) -> MaterialDefinition | None:
    try:
        text = Path(path).read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return None
    definition = MaterialDefinition()
    definition.source = str(path)
    # e.g.
    #   ParameterInfo = { Name=Diffuse }  (or ParameterName = Diffuse)
    #   ParameterValue = Texture2D'/Game/Characters/Tex/T_Body_D.T_Body_D'
    pattern = re.compile(r"(?:ParameterName\s*=\s*|Name\s*=\s*)([\w .\-]+?)\s*[}\n].*?ParameterValue\s*=\s*(\w+'[^']*'|[^\s}]+)", re.S)
    for match in pattern.finditer(text):
        name, value = match.group(1).strip(), match.group(2).strip()
        if 'texture' in value.lower() or '/' in value:
            definition.textures.setdefault(name, value)
    match = re.search(r'BlendMode\s*=\s*(BLEND_\w+)', text)
    if match:
        definition.blend_mode = _BLEND_MODE_NAMES.get(match.group(1).upper())
    return definition if definition.textures else None


class MaterialTextureFinder:
    def __init__(self, psk_filepath: str, extra_directory: str = ''):
        self.psk_path = Path(psk_filepath).resolve() if psk_filepath else None
        self.roots: list[Path] = []
        if self.psk_path is not None:
            # Search the folder of the PSK first, then gradually the folders above it (nearer files take precedence).
            folder = self.psk_path.parent
            for _ in range(MAX_PARENT_LEVELS + 1):
                if folder.name.lower() in _STOP_FOLDER_NAMES or folder.parent == folder or folder == Path.home():
                    break
                self.roots.append(folder)
                if folder.name.lower() in ('game', 'content', 'exports'):
                    # The root of an exported content hierarchy.
                    break
                folder = folder.parent
        if extra_directory:
            extra = Path(bpy.path.abspath(extra_directory))
            if extra.is_dir():
                self.roots.insert(0, extra)
        self.textures: dict[str, list[Path]] = {}
        self.materials: dict[str, list[Path]] = {}
        self._images: dict[str, bpy.types.Image] = {}
        self._placeholders: dict[str, bool] = {}
        self._definitions: dict[str, MaterialDefinition | None] = {}
        self._build_index()

    def _build_index(self):
        import time
        visited: set[Path] = set()
        deadline = time.monotonic() + MAX_INDEX_SECONDS
        for root in self.roots:
            if time.monotonic() > deadline:
                break
            if not root.is_dir():
                continue
            count = 0
            root_depth = len(root.parts)
            for directory, subdirectories, files in os.walk(root):
                directory_path = Path(directory)
                if directory_path in visited:
                    subdirectories[:] = []
                    continue
                visited.add(directory_path)
                if len(directory_path.parts) - root_depth >= MAX_SEARCH_DEPTH:
                    subdirectories[:] = []
                # Skip hidden folders.
                subdirectories[:] = [d for d in subdirectories if not d.startswith('.')]
                for file in files:
                    count += 1
                    lower = file.lower()
                    path = directory_path / file
                    if lower.endswith(TEXTURE_EXTENSIONS):
                        self.textures.setdefault(os.path.splitext(lower)[0], []).append(path)
                    elif lower.endswith('.props.txt'):
                        self.materials.setdefault(lower[:-len('.props.txt')], []).append(path)
                    elif lower.endswith(('.json', '.mat')):
                        self.materials.setdefault(os.path.splitext(lower)[0], []).append(path)
                if count > MAX_INDEXED_FILES or time.monotonic() > deadline:
                    break

    def _content_roots(self) -> list[Path]:
        """Folders that could be the root of an exported content hierarchy (e.g., `.../Game/`)."""
        roots = []
        for root in self.roots + ([self.psk_path] if self.psk_path else []):
            for ancestor in [root] + list(root.parents):
                if ancestor.name.lower() in ('game', 'content'):
                    roots.append(ancestor)
        return roots

    def find_texture(self, reference: str) -> Path | None:
        """Finds the texture file for an Unreal object path or texture name."""
        relative = _asset_relative_path(reference)
        if relative:
            for content_root in self._content_roots():
                for extension in TEXTURE_EXTENSIONS:
                    candidate = content_root / (relative + extension)
                    if candidate.is_file():
                        return candidate
        name = _asset_name(reference).lower()
        candidates = self.textures.get(name, [])
        if not candidates:
            return None
        if len(candidates) > 1 and relative:
            # Prefer the file whose folder structure matches the asset path the best.
            parts = [p.lower() for p in relative.split('/')[:-1]]

            def score(path: Path) -> int:
                folders = [p.lower() for p in path.parent.parts]
                return sum(1 for p in parts if p in folders)
            candidates = sorted(candidates, key=score, reverse=True)
        # The candidates are ordered by how close they are to the PSK file. Prefer lossless formats among equals.
        return candidates[0]

    def is_placeholder(self, path: Path) -> bool:
        """
        Returns True if the texture is a solid color (e.g., the black/white/gray placeholder textures that are assigned
        to unused material slots).
        """
        key = str(path)
        if key not in self._placeholders:
            result = False
            image = self.get_image(path)
            if image is not None:
                try:
                    import numpy as np
                    pixels = np.empty(len(image.pixels), dtype=np.float32)
                    image.pixels.foreach_get(pixels)
                    rgb = pixels.reshape(-1, image.channels)[:, :3]
                    result = float((rgb.max(axis=0) - rgb.min(axis=0)).max()) < 0.02
                except Exception:
                    result = False
            self._placeholders[key] = result
        return self._placeholders[key]

    def _parse_definition(self, path: Path) -> MaterialDefinition | None:
        key = str(path)
        if key not in self._definitions:
            lower = path.name.lower()
            if lower.endswith('.json'):
                definition = _parse_json_material(path)
            elif lower.endswith('.props.txt'):
                definition = _parse_props_txt(path)
            else:
                definition = _parse_umodel_mat(path)
            self._definitions[key] = definition
        return self._definitions[key]

    def find_material_definition(self, material_name: str) -> MaterialDefinition | None:
        for path in self.materials.get(material_name.lower(), []):
            definition = self._parse_definition(path)
            if definition is not None:
                return definition
        return self._find_variant_material_definition(material_name)

    def _find_variant_material_definition(self, material_name: str) -> MaterialDefinition | None:
        """
        Finds the material definition of a material variant for which there is no definition file. For example, the
        costume variant `MII_Pl13100_body_CV` uses the definition of `MII_Pl13101_body` (same part, most similar id).
        """
        tokens = _tokens(material_name)
        if len(tokens) < 2:
            return None
        prefix = tokens[0] if tokens[0] in _MATERIAL_PREFIXES else None
        body = tokens[1:] if prefix else tokens
        parts = [t for t in body if t not in _VARIANT_TOKENS and not t.isdigit()]
        if len(parts) < 2:
            return None
        # e.g. [pl13100, body]: the id is the first token, the part is the rest.
        material_id, part = parts[0], parts[1:]
        best = None
        for name, paths in self.materials.items():
            candidate_tokens = _tokens(name)
            if prefix and (not candidate_tokens or candidate_tokens[0] != prefix):
                continue
            candidate_body = candidate_tokens[1:] if prefix else candidate_tokens
            candidate_parts = [t for t in candidate_body if t not in _VARIANT_TOKENS and not t.isdigit()]
            if len(candidate_parts) < 2 or candidate_parts[1:] != part:
                continue
            common = len(os.path.commonprefix([material_id, candidate_parts[0]]))
            # The ids must at least share the character (e.g., `pl13`).
            if common < min(4, len(material_id)):
                continue
            if best is None or common > best[0]:
                best = (common, paths)
        if best is None:
            return None
        for path in best[1]:
            definition = self._parse_definition(path)
            if definition is not None:
                return definition
        return None

    def get_image(self, path: Path) -> bpy.types.Image | None:
        key = str(path)
        if key in self._images:
            return self._images[key]
        image = None
        for existing in bpy.data.images:
            if existing.filepath and bpy.path.abspath(existing.filepath) == key:
                image = existing
                break
        if image is None:
            try:
                image = bpy.data.images.load(key, check_existing=True)
            except RuntimeError:
                image = None
        self._images[key] = image
        return image


def _choose_slot_texture(finder: MaterialTextureFinder, definition: MaterialDefinition, slot: str) -> Path | None:
    preferred = _SLOT_PARAMETERS[slot]
    scored = []
    for parameter, reference in definition.textures.items():
        normalized = _normalize(parameter)
        score = None
        if normalized in preferred:
            score = preferred.index(normalized)
        elif not any(word in normalized for word in _EXCLUDED_PARAMETER_WORDS):
            for i, keyword in enumerate(preferred):
                if len(keyword) >= 4 and keyword in normalized:
                    score = 100 + i
                    break
        if score is None:
            continue
        path = finder.find_texture(reference)
        if path is not None and not finder.is_placeholder(path):
            scored.append((score, path))
    if scored:
        return sorted(scored, key=lambda x: x[0])[0][1]
    # Look at the texture names instead (e.g., for UE Viewer `.mat` files with generic `Other[0]` entries). FModel's
    # `.json` files have meaningful parameter names, so this is not done for them.
    if definition.source.lower().endswith('.json'):
        return None
    for parameter, reference in definition.textures.items():
        name = _asset_name(reference).lower()
        if name.endswith(_SLOT_SUFFIXES[slot]):
            path = finder.find_texture(reference)
            if path is not None and not finder.is_placeholder(path):
                return path
    return None


_MATERIAL_PREFIXES = ('m', 'mi', 'mii', 'mic', 'mat', 'mt', 'material', 'mtl')
# Tokens that mark variants of a material (e.g., costume variants, visibility groups).
_VARIANT_TOKENS = {'cv', 'hidea', 'hideb', 'hidec', 'hided', 'shidea', 'shideb', 'shidec', 'shided', 'gpa', 'gpb',
                   'gpc', 'gpd', 'inst', 'instance', 'lod', 'lod0', 'lod1', 'd'}
_IGNORED_TOKENS = {'cv', 'inst', 'instance', 'mat', 'material', 'lod', 'lod0', 't', 'tex', 'texture'}


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r'[^a-z0-9]+', name.lower()) if t]


def _find_textures_by_name(finder: MaterialTextureFinder, material_name: str) -> dict[str, Path]:
    """
    Finds the textures of a material by matching the name of the material against the texture names, e.g.:
      `MI_Hero_Body` -> `T_Hero_Body_D`, `T_Hero_Body_N`
    """
    tokens = _tokens(material_name)
    if tokens and tokens[0] in _MATERIAL_PREFIXES:
        tokens = tokens[1:]
    tokens = [t for t in tokens if t not in _IGNORED_TOKENS]
    if not tokens:
        return {}
    result = {}
    for slot, suffixes in _SLOT_SUFFIXES.items():
        best = None
        for name, paths in finder.textures.items():
            if not name.endswith(suffixes):
                continue
            # Strip the suffix & the texture prefix.
            base = name
            for suffix in sorted(suffixes, key=len, reverse=True):
                if base.endswith(suffix):
                    base = base[:-len(suffix)]
                    break
            texture_tokens = [t for t in _tokens(base) if t not in ('t', 'tex', 'tx')]
            if not texture_tokens:
                continue
            common = set(tokens) & set(texture_tokens)
            if not common:
                continue
            # All of the distinctive tokens of the texture must be in the material name (e.g., `body` for `T_Body_D`)
            # and at least one of the material's tokens must be matched.
            missing = [t for t in texture_tokens if t not in tokens]
            score = len(common) * 10 - len(missing) * 3
            if missing and len(common) < 2:
                continue
            if best is None or score > best[0]:
                best = (score, paths[0], False)
            elif score == best[0] and paths[0] != best[1]:
                best = (score, best[1], True)  # Ambiguous.
        if best is not None and not best[2]:
            result[slot] = best[1]
    return result


def _is_same_texture_family(a: Path, b: Path) -> bool:
    """
    Returns True if the two textures belong to the same asset (e.g., `T_Hero_body_AO` & `T_Hero_body_D`), i.e.,
    they share their name up to the last token, or at least their first two tokens (e.g., `T_Hero_00_hair_AO` &
    `T_Hero_00_body_D`).
    """
    ta = [t for t in _tokens(a.stem) if t not in ('t', 'tex', 'tx')]
    tb = [t for t in _tokens(b.stem) if t not in ('t', 'tex', 'tx')]
    if ta[:-1] and ta[:-1] == tb[:-1]:
        return True
    return len(ta) >= 3 and len(tb) >= 3 and ta[:2] == tb[:2]


def _image_has_alpha(image: bpy.types.Image) -> bool:
    if image is None or image.channels < 4 or image.depth not in (32, 64, 128):
        return False
    try:
        import numpy as np
        pixels = np.empty(len(image.pixels), dtype=np.float32)
        image.pixels.foreach_get(pixels)
        return float(pixels[3::4].min()) < 0.99
    except Exception:
        return False


def setup_material(material: Material, finder: MaterialTextureFinder) -> bool:
    """
    Sets up the node tree of the material with the textures that could be found.
    Returns True if any textures were found.
    """
    definition = finder.find_material_definition(material.name)
    textures: dict[str, Path] = {}
    blend_mode = None
    if definition is not None:
        blend_mode = definition.blend_mode
        for slot in _SLOT_PARAMETERS:
            path = _choose_slot_texture(finder, definition, slot)
            if path is not None:
                textures[slot] = path
    if 'base_color' not in textures:
        for slot, path in _find_textures_by_name(finder, material.name).items():
            if not finder.is_placeholder(path):
                textures.setdefault(slot, path)
    # Secondary textures (normal maps, packed masks, opacity) must either belong to the same asset family as the base
    # color texture, or be named for their purpose (e.g., `_N` for normal maps). Materials often have placeholder
    # textures (e.g., test patterns) assigned to their unused slots.
    for slot in ('normal', 'orm', 'mrao', 'opacity'):
        if slot not in textures:
            continue
        path = textures[slot]
        if not path.stem.lower().endswith(_SLOT_SUFFIXES[slot]) and \
                ('base_color' not in textures or not _is_same_texture_family(path, textures['base_color'])):
            del textures[slot]
    # Emissive & AO textures are only used if they belong to the same asset family as the base color texture (e.g.
    # `T_Hero_body_AO` for `T_Hero_body_D`). Materials often reference shared effect textures (e.g., glows that are
    # only enabled at runtime) that would make the whole material glow.
    for slot in ('emissive', 'ao'):
        if slot in textures and ('base_color' not in textures or
                                 not _is_same_texture_family(textures[slot], textures['base_color'])):
            del textures[slot]
    if not textures:
        return False

    material.use_nodes = True
    node_tree = material.node_tree
    nodes = node_tree.nodes
    links = node_tree.links
    nodes.clear()

    output = nodes.new('ShaderNodeOutputMaterial')
    output.location = (400, 0)
    bsdf = nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.location = (0, 0)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

    y = 300

    def image_node(path: Path, non_color: bool):
        nonlocal y
        image = finder.get_image(path)
        if image is None:
            return None, None
        if non_color:
            try:
                image.colorspace_settings.name = 'Non-Color'
            except TypeError:
                pass
        node = nodes.new('ShaderNodeTexImage')
        node.image = image
        node.location = (-500, y)
        y -= 300
        return node, image

    uses_alpha = False
    if 'base_color' in textures:
        node, image = image_node(textures['base_color'], False)
        if node is not None:
            node.name = BASE_COLOR_NODE_NAME
            links.new(node.outputs['Color'], bsdf.inputs['Base Color'])
            if blend_mode in (BLEND_MASKED, BLEND_TRANSLUCENT) and 'opacity' not in textures and _image_has_alpha(image):
                # The alpha channel of the base color texture is often used as the opacity (mask), but it's also
                # often used for other things (e.g., masks for a custom shader). Whether it is used as the opacity is
                # decided once the mesh is available (see `finalize_material_alpha`).
                material[ALPHA_CANDIDATE_PROPERTY] = blend_mode
            # Show the texture in the viewport's solid mode as well.
            nodes.active = node

    if 'ao' in textures and 'base_color' in textures:
        base_node = nodes.get(BASE_COLOR_NODE_NAME)
        node, _ = image_node(textures['ao'], True)
        if node is not None and base_node is not None:
            # Multiply the base color by the ambient occlusion.
            multiply = nodes.new('ShaderNodeMix')
            multiply.data_type = 'RGBA'
            multiply.blend_type = 'MULTIPLY'
            multiply.name = 'PSK Ambient Occlusion'
            multiply.label = 'Ambient Occlusion'
            multiply.inputs['Factor'].default_value = 1.0
            multiply.location = (-200, base_node.location.y)
            # NOTE: The color sockets of the mix node are A (6), B (7) and Result (2).
            links.new(base_node.outputs['Color'], multiply.inputs[6])
            links.new(node.outputs['Color'], multiply.inputs[7])
            links.new(multiply.outputs[2], bsdf.inputs['Base Color'])

    if 'opacity' in textures:
        node, _ = image_node(textures['opacity'], True)
        if node is not None:
            links.new(node.outputs['Color'], bsdf.inputs['Alpha'])
            uses_alpha = True

    if 'normal' in textures:
        node, _ = image_node(textures['normal'], True)
        if node is not None:
            normal_map = nodes.new('ShaderNodeNormalMap')
            normal_map.location = (-200, node.location.y)
            links.new(node.outputs['Color'], normal_map.inputs['Color'])
            links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])

    for slot, channels in (('orm', {'Roughness': 1, 'Metallic': 2}), ('mrao', {'Metallic': 0, 'Roughness': 1})):
        if slot not in textures:
            continue
        node, _ = image_node(textures[slot], True)
        if node is None:
            continue
        separate = nodes.new('ShaderNodeSeparateColor')
        separate.location = (-200, node.location.y)
        links.new(node.outputs['Color'], separate.inputs['Color'])
        for socket, channel in channels.items():
            links.new(separate.outputs[channel], bsdf.inputs[socket])
        break

    if 'emissive' in textures:
        node, _ = image_node(textures['emissive'], False)
        if node is not None:
            emission_input = bsdf.inputs.get('Emission Color') or bsdf.inputs.get('Emission')
            if emission_input is not None:
                links.new(node.outputs['Color'], emission_input)
                strength = bsdf.inputs.get('Emission Strength')
                if strength is not None:
                    strength.default_value = 1.0

    if blend_mode in (BLEND_TRANSLUCENT, BLEND_ADDITIVE, BLEND_MODULATE):
        if hasattr(material, 'surface_render_method'):
            material.surface_render_method = 'BLENDED'
        if hasattr(material, 'blend_method'):
            material.blend_method = 'BLEND'
    elif uses_alpha:
        if hasattr(material, 'surface_render_method'):
            material.surface_render_method = 'DITHERED'
        if hasattr(material, 'blend_method'):
            material.blend_method = 'HASHED'

    return True


def finalize_material_alpha(material: Material, mesh_data, material_index: int):
    """
    Decides whether the alpha channel of the base color texture is used as the opacity of the material.

    For masked materials, the alpha is only used if it doesn't cut away a significant part of the faces that use the
    material (in which case the alpha channel is almost certainly used for something else).
    """
    blend_mode = material.get(ALPHA_CANDIDATE_PROPERTY)
    if blend_mode is None:
        return
    del material[ALPHA_CANDIDATE_PROPERTY]
    if not material.use_nodes or material.node_tree is None:
        return
    nodes = material.node_tree.nodes
    node = nodes.get(BASE_COLOR_NODE_NAME)
    bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if node is None or bsdf is None or node.image is None:
        return
    image = node.image

    if blend_mode == BLEND_MASKED:
        import numpy as np
        width, height = image.size
        if width == 0 or height == 0 or mesh_data is None or not mesh_data.uv_layers:
            return
        pixels = np.empty(width * height * image.channels, dtype=np.float32)
        image.pixels.foreach_get(pixels)
        alpha = pixels[3::image.channels].reshape(height, width)
        # Sample the alpha at the center of each face that uses the material.
        polygon_count = len(mesh_data.polygons)
        material_indices = np.empty(polygon_count, dtype=np.int32)
        mesh_data.polygons.foreach_get('material_index', material_indices)
        loop_starts = np.empty(polygon_count, dtype=np.int32)
        loop_totals = np.empty(polygon_count, dtype=np.int32)
        mesh_data.polygons.foreach_get('loop_start', loop_starts)
        mesh_data.polygons.foreach_get('loop_total', loop_totals)
        uvs = np.empty(len(mesh_data.loops) * 2, dtype=np.float32)
        mesh_data.uv_layers[0].data.foreach_get('uv', uvs)
        uvs = uvs.reshape(-1, 2)
        mask = material_indices == material_index
        if not mask.any():
            return
        centers = np.array([uvs[start:start + total].mean(axis=0) for start, total in zip(loop_starts[mask], loop_totals[mask])])
        x = np.clip((np.mod(centers[:, 0], 1.0) * width).astype(np.int64), 0, width - 1)
        y = np.clip((np.mod(centers[:, 1], 1.0) * height).astype(np.int64), 0, height - 1)
        cut_fraction = float((alpha[y, x] < OPACITY_MASK_CLIP_VALUE).mean())
        if cut_fraction > 0.05:
            return
        # Masked: the opacity is either fully on or off (like in Unreal Engine).
        threshold = nodes.new('ShaderNodeMath')
        threshold.operation = 'GREATER_THAN'
        threshold.inputs[1].default_value = OPACITY_MASK_CLIP_VALUE
        threshold.location = (node.location.x + 300, node.location.y - 250)
        material.node_tree.links.new(node.outputs['Alpha'], threshold.inputs[0])
        material.node_tree.links.new(threshold.outputs['Value'], bsdf.inputs['Alpha'])
        if hasattr(material, 'surface_render_method'):
            material.surface_render_method = 'DITHERED'
        if hasattr(material, 'blend_method'):
            material.blend_method = 'CLIP' if hasattr(material, 'alpha_threshold') else 'HASHED'
    else:
        material.node_tree.links.new(node.outputs['Alpha'], bsdf.inputs['Alpha'])
        if hasattr(material, 'surface_render_method'):
            material.surface_render_method = 'BLENDED'
        if hasattr(material, 'blend_method'):
            material.blend_method = 'BLEND'

import sys

from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from bpy.types import PropertyGroup

mesh_triangle_types_items = (
    ('NORMAL', 'Normal', 'Normal one-sided', 0),
    ('NORMAL_TWO_SIDED', 'Normal Two-Sided', 'Normal but two-sided', 1),
    ('TRANSLUCENT', 'Translucent', 'Translucent two-sided', 2),
    ('MASKED', 'Masked', 'Masked two-sided', 3),
    ('MODULATE', 'Modulate', 'Modulation blended two-sided', 4),
    ('PLACEHOLDER', 'Placeholder', 'Placeholder triangle for positioning weapon. Invisible', 8),
)

mesh_triangle_bit_flags_items = (
    ('UNLIT', 'Unlit', 'Full brightness, no lighting', 16),
    ('FLAT', 'Flat', 'Flat surface, don\'t do bMeshCurvy thing', 32),
    ('ENVIRONMENT', 'Environment', 'Environment mapped', 64),
    ('NO_SMOOTH', 'No Smooth', 'No bilinear filtering on this poly\'s texture', 128),
)

class PSX_PG_material(PropertyGroup):
    mesh_triangle_type: EnumProperty(
        name='Triangle Type',
        items=mesh_triangle_types_items
        )
    mesh_triangle_bit_flags: EnumProperty(
        name='Triangle Bit Flags',
        items=mesh_triangle_bit_flags_items,
        options={'ENUM_FLAG'}
        )

mesh_triangle_types_items_dict = {item[0]: item[3] for item in mesh_triangle_types_items}
mesh_triangle_bit_flags_items_dict = {item[0]: item[3] for item in mesh_triangle_bit_flags_items}


def triangle_type_and_bit_flags_to_poly_flags(mesh_triangle_type: str, mesh_triangle_bit_flags: set[str]) -> int:
    poly_flags = 0
    poly_flags |= mesh_triangle_types_items_dict.get(mesh_triangle_type, 0)
    for flag in mesh_triangle_bit_flags:
        poly_flags |= mesh_triangle_bit_flags_items_dict.get(flag, 0)
    return poly_flags


def poly_flags_to_triangle_type_and_bit_flags(poly_flags: int) -> tuple[str, set[str]]:
    try:
        triangle_type = next(item[0] for item in mesh_triangle_types_items if item[3] == (poly_flags & 15))
    except StopIteration:
        triangle_type = 'NORMAL'
    triangle_bit_flags = {item[0] for item in mesh_triangle_bit_flags_items if item[3] & poly_flags}
    return triangle_type, triangle_bit_flags


def should_import_mesh_get(self):
    return self.components in {'ALL', 'MESH'}


def should_import_skleton_get(self):
    return self.components in {'ALL', 'ARMATURE'}


vertex_color_space_items = (
    ('LINEAR', 'Linear', ''),
    ('SRGBA', 'sRGBA', ''),
)

psk_import_components_items = (
    ('ALL', 'Mesh & Armature', 'Import mesh and armature'),
    ('MESH', 'Mesh Only', 'Import mesh only'),
    ('ARMATURE', 'Armature Only', 'Import armature only'),
)

class PskImportMixin:
    should_import_vertex_colors: BoolProperty(
        default=True,
        options=set(),
        name='Import Vertex Colors',
        description='Import vertex colors, if available'
    )
    vertex_color_space: EnumProperty(
        name='Vertex Color Space',
        options=set(),
        description='The source vertex color space',
        default='SRGBA',
        items=vertex_color_space_items
    )
    should_import_vertex_normals: BoolProperty(
        default=True,
        name='Import Vertex Normals',
        options=set(),
        description='Import vertex normals, if available.\n\nThis is only supported for PSKX files'
    )
    should_import_extra_uvs: BoolProperty(
        default=True,
        name='Import Extra UVs',
        options=set(),
        description='Import extra UV maps, if available'
    )
    components: EnumProperty(
        name='Components',
        options=set(),
        description='Which components to import',
        items=psk_import_components_items,
        default='ALL'
    )
    should_import_mesh: BoolProperty(
        name='Import Mesh',
        get=should_import_mesh_get,
    )
    should_import_materials: BoolProperty(
        default=True,
        name='Import Materials',
        options=set(),
    )
    should_import_armature: BoolProperty(
        name='Import Skeleton',
        get=should_import_skleton_get,
    )
    bone_length: FloatProperty(
        default=1.0,
        min=sys.float_info.epsilon,
        step=100,
        soft_min=1.0,
        name='Bone Length',
        options=set(),
        subtype='DISTANCE',
        description='Length of the bones'
    )
    should_import_shape_keys: BoolProperty(
        default=True,
        name='Import Shape Keys',
        options=set(),
        description='Import shape keys, if available.\n\nThis is only supported for PSKX files'
    )
    scale: FloatProperty(
        name='Scale',
        default=1.0,
        soft_min=0.0,
    )
    should_fix_bone_orientation: BoolProperty(
        default=True,
        name='Fix Bone Tails',
        options=set(),
        description='Re-orient the bones so that the bone tails point to the next joint (e.g., the upper arm points '
                    'to the elbow), instead of all of the bones pointing in the same direction.\n\n'
                    'The original bone orientations are remembered, so PSA imports and PSK/PSA exports are unaffected'
    )
    should_make_animate_ready: BoolProperty(
        default=False,
        name='Animate Ready',
        options=set(),
        description='Set up a control rig for animating: IK arms & legs (with IK/FK switching, pole targets and '
                    'twist controls), foot roll, finger curl & spread controls, eye aim targets and facial controls.\n\n'
                    'The controls are never exported to PSK/PSA files, and PSA animations imported onto the armature '
                    'are automatically baked onto the controls'
    )
    rig_use_arm_ik: BoolProperty(default=True, name='Arm IK', options=set(), description='Add IK controls to the arms')
    rig_use_leg_ik: BoolProperty(default=True, name='Leg IK', options=set(), description='Add IK controls & a foot roll to the legs')
    rig_use_fingers: BoolProperty(default=True, name='Fingers', options=set(), description='Add hand controls to curl & spread the fingers')
    rig_use_eyes: BoolProperty(default=True, name='Eyes', options=set(), description='Add eye aim target controls')
    rig_use_face: BoolProperty(default=True, name='Face', options=set(),
                               description='Add facial controls (blink, brows, jaw, mouth corners, lips & cheeks)')
    should_setup_material_textures: BoolProperty(
        default=True,
        name='Auto Setup Textures',
        options=set(),
        description='Automatically find the textures for the materials and set up the material node trees.\n\n'
                    'Material definition files exported by FModel (.json) or UE Viewer (.mat, .props.txt) are used '
                    'when available, otherwise the textures are matched by name. The folder of the PSK file and the '
                    'folders around it are searched'
    )
    texture_search_directory: StringProperty(
        name='Texture Folder',
        default='',
        subtype='DIR_PATH',
        options=set(),
        description='An additional folder to search for textures & material files (optional)'
    )
    bdk_repository_id: StringProperty(
        name='BDK Repository ID',
        default='',
        options=set(),
        description='The ID of the BDK repository to use for loading materials'
    )


_classes = (
    PSX_PG_material,
)

from bpy.utils import register_classes_factory
register, unregister = register_classes_factory(_classes)


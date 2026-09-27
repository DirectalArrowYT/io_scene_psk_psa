class PSX_PG_material:
    mesh_triangle_type: str
    mesh_triangle_bit_flags: set[str]


class PskImportMixin:
    should_import_vertex_colors: bool
    vertex_color_space: str
    should_import_vertex_normals: bool
    should_import_extra_uvs: bool
    components: str
    should_import_mesh: bool
    should_import_materials: bool
    should_import_armature: bool
    bone_length: float
    should_import_shape_keys: bool
    scale: float
    should_fix_bone_orientation: bool
    should_make_animate_ready: bool
    rig_use_arm_ik: bool
    rig_use_leg_ik: bool
    rig_use_fingers: bool
    rig_use_eyes: bool
    rig_use_face: bool
    should_setup_material_textures: bool
    texture_search_directory: str
    bdk_repository_id: str


def triangle_type_and_bit_flags_to_poly_flags(mesh_triangle_type: str, mesh_triangle_bit_flags: set[str]) -> int: ...

def poly_flags_to_triangle_type_and_bit_flags(poly_flags: int) -> tuple[str, set[str]]: ...

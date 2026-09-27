import os
from pathlib import Path

from bpy.props import CollectionProperty, StringProperty
from bpy.types import Context, FileHandler, Operator, OperatorFileListElement, UILayout
from bpy_extras.io_utils import ImportHelper

from ..importer import PskImportOptions, import_psk
from ..properties import PskImportMixin
from psk_psa_py.psk.reader import read_psk_from_file


def get_psk_import_options_from_properties(property_group: PskImportMixin):
    options = PskImportOptions()
    options.should_import_mesh = property_group.should_import_mesh
    options.should_import_extra_uvs = property_group.should_import_extra_uvs
    options.should_import_vertex_colors = property_group.should_import_vertex_colors
    options.should_import_vertex_normals = property_group.should_import_vertex_normals
    options.vertex_color_space = property_group.vertex_color_space
    options.should_import_armature = property_group.should_import_armature
    options.bone_length = property_group.bone_length
    options.should_import_materials = property_group.should_import_materials
    options.should_import_shape_keys = property_group.should_import_shape_keys
    options.scale = property_group.scale

    options.should_fix_bone_orientation = property_group.should_fix_bone_orientation
    options.should_make_animate_ready = property_group.should_make_animate_ready
    options.rig_options.use_arm_ik = property_group.rig_use_arm_ik
    options.rig_options.use_leg_ik = property_group.rig_use_leg_ik
    options.rig_options.use_fingers = property_group.rig_use_fingers
    options.rig_options.use_eyes = property_group.rig_use_eyes
    options.rig_options.use_face = property_group.rig_use_face
    options.rig_options.eye_style = property_group.rig_eye_style
    options.should_setup_material_textures = property_group.should_setup_material_textures
    options.texture_search_directory = property_group.texture_search_directory

    if property_group.bdk_repository_id:
        options.bdk_repository_id = property_group.bdk_repository_id

    return options


def psk_import_draw(layout: UILayout, props: PskImportMixin):
    row = layout.row()

    col = row.column()
    col.use_property_split = True
    col.use_property_decorate = False
    col.prop(props, 'components')

    if props.should_import_mesh:
        mesh_header, mesh_panel = layout.panel('mesh_panel_id', default_closed=False)
        mesh_header.label(text='Mesh', icon='MESH_DATA')

        if mesh_panel:
            row = mesh_panel.row()
            col = row.column()
            col.use_property_split = True
            col.use_property_decorate = False
            col.prop(props, 'should_import_extra_uvs', text='Extra UVs')
            col.prop(props, 'should_import_materials', text='Materials')
            if props.should_import_materials:
                col.prop(props, 'should_setup_material_textures')
                if props.should_setup_material_textures:
                    col.prop(props, 'texture_search_directory')
            col.prop(props, 'should_import_vertex_colors', text='Vertex Colors')
            if props.should_import_vertex_colors:
                col.prop(props, 'vertex_color_space')
            col.separator()
            col.prop(props, 'should_import_vertex_normals', text='Vertex Normals')
            col.prop(props, 'should_import_shape_keys', text='Shape Keys')

    if props.should_import_armature:
        armature_header, armature_panel = layout.panel('armature_panel_id', default_closed=False)
        armature_header.label(text='Armature', icon='OUTLINER_DATA_ARMATURE')

        if armature_panel:
            row = armature_panel.row()
            col = row.column()
            col.use_property_split = True
            col.use_property_decorate = False
            col.prop(props, 'should_fix_bone_orientation')
            if not props.should_fix_bone_orientation:
                col.prop(props, 'bone_length')

    if props.should_import_armature:
        rig_header, rig_panel = layout.panel('rig_panel_id', default_closed=False)
        rig_header.use_property_split = False
        rig_header.prop(props, 'should_make_animate_ready', text='')
        rig_header.label(text='Animate Ready', icon='POSE_HLT')
        if rig_panel:
            col = rig_panel.column(heading='Controls')
            col.use_property_split = True
            col.use_property_decorate = False
            col.enabled = props.should_make_animate_ready
            col.prop(props, 'rig_use_arm_ik')
            col.prop(props, 'rig_use_leg_ik')
            col.prop(props, 'rig_use_fingers')
            col.prop(props, 'rig_use_eyes')
            if props.rig_use_eyes:
                col.prop(props, 'rig_eye_style')
            col.prop(props, 'rig_use_face')

    transform_header, transform_panel = layout.panel('transform_panel_id', default_closed=False)
    transform_header.label(text='Transform')
    if transform_panel:
        row = transform_panel.row()
        col = row.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(props, 'scale')


class PSK_OT_import(Operator, ImportHelper, PskImportMixin):
    bl_idname = 'psk.import_file'
    bl_label = 'Import'
    bl_options = {'INTERNAL', 'UNDO', 'PRESET'}
    bl_description = 'Import a PSK file'
    filename_ext = '.psk'
    filter_glob: StringProperty(default='*.psk;*.pskx', options={'HIDDEN'})
    filepath: StringProperty(
        name='File Path',
        description='File path used for exporting the PSK file',
        maxlen=1024,
        default='')

    def execute(self, context):
        try:
            psk = read_psk_from_file(self.filepath)
        except OSError as e:
            self.report({'ERROR'}, f'Failed to read "{self.filepath}". The file may be corrupted or not a valid PSK file: {e}')
            return {'CANCELLED'}

        name = os.path.splitext(os.path.basename(self.filepath))[0]
        options = get_psk_import_options_from_properties(self)
        options.filepath = self.filepath
        result = import_psk(psk, context, name, options)

        if len(result.warnings):
            message = f'PSK imported as "{result.root_object.name}" with {len(result.warnings)} warning(s)\n'
            message += '\n'.join(result.warnings)
            self.report({'WARNING'}, message)
        else:
            self.report({'INFO'}, f'PSK imported as "{result.root_object.name}"')

        return {'FINISHED'}

    def draw(self, context):
        assert self.layout
        psk_import_draw(self.layout, self)


class PSK_OT_import_drag_and_drop(Operator, PskImportMixin):
    bl_idname = 'psk.import_drag_and_drop'
    bl_label = 'Import PSK'
    bl_options = {'INTERNAL', 'UNDO', 'PRESET'}
    bl_description = 'Import PSK files by dragging and dropping them onto the 3D view'

    directory: StringProperty(subtype='FILE_PATH', options={'SKIP_SAVE', 'HIDDEN'})
    files: CollectionProperty(type=OperatorFileListElement, options={'SKIP_SAVE', 'HIDDEN'})

    @classmethod
    def poll(cls, context) -> bool:
        return context.area is not None and context.area.type == 'VIEW_3D'

    def draw(self, context):
        assert self.layout
        psk_import_draw(self.layout, self)

    def invoke(self, context, event):
        assert context.window_manager
        context.window_manager.invoke_props_dialog(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        warning_count = 0

        options = get_psk_import_options_from_properties(self)

        for file in self.files:
            filepath = Path(self.directory) / file.name
            try:
                psk = read_psk_from_file(filepath)
            except OSError as e:
                self.report({'ERROR'}, f'Failed to read "{filepath}". The file may be corrupted or not a valid PSK file: {e}')
                return {'CANCELLED'}

            name = os.path.splitext(file.name)[0]
            options.filepath = str(filepath)
            result = import_psk(psk, context, name, options)
            if result.warnings:
                warning_count += len(result.warnings)

        if warning_count > 0:
            self.report({'WARNING'}, f'Imported {len(self.files)} PSK file(s) with {warning_count} warning(s)')
        else:
            self.report({'INFO'}, f'Imported {len(self.files)} PSK file(s)')

        return {'FINISHED'}


# TODO: move to another file
class PSK_FH_file_handler(FileHandler):
    bl_idname = 'PSK_FH_import'
    bl_label = 'Unreal PSK'
    bl_import_operator = PSK_OT_import_drag_and_drop.bl_idname
    bl_export_operator = 'psk.export_collection'
    bl_file_extensions = '.psk;.pskx'

    @classmethod
    def poll_drop(cls, context: Context) -> bool:
        return context.area is not None and context.area.type == 'VIEW_3D'


_classes = (
    PSK_OT_import,
    PSK_OT_import_drag_and_drop,
    PSK_FH_file_handler,
)

from bpy.utils import register_classes_factory
register, unregister = register_classes_factory(_classes)

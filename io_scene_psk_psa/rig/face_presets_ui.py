import os

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Operator, OperatorFileListElement, Panel, UIList

from .face_presets import (
    FACE_PRESET_PREFIX, FACE_PRESET_PROPERTY, apply_face_preset, face_preset_from_pose, get_face_presets,
    import_face_presets, reset_face,
)
from .operators import _get_armature


def _get_active_preset(context):
    presets = bpy.data.actions
    index = context.scene.psk_face_preset_index
    if 0 <= index < len(presets) and presets[index].get(FACE_PRESET_PROPERTY):
        return presets[index]
    return None


class PSK_UL_face_presets(UIList):
    bl_idname = 'PSK_UL_face_presets'

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        name = item.name[len(FACE_PRESET_PREFIX):] if item.name.startswith(FACE_PRESET_PREFIX) else item.name
        layout.label(text=name, icon='SHAPEKEY_DATA')

    def filter_items(self, context, data, propname):
        actions = getattr(data, propname)
        flags = []
        filter_name = self.filter_name.lower()
        for action in actions:
            visible = bool(action.get(FACE_PRESET_PROPERTY)) and (not filter_name or filter_name in action.name.lower())
            flags.append(self.bitflag_filter_item if visible else 0)
        order = bpy.types.UI_UL_list.sort_items_by_name(actions, 'name') if self.use_filter_sort_alpha else []
        return flags, order


class PSK_OT_face_presets_import(Operator):
    bl_idname = 'psk_face.import_presets'
    bl_label = 'Import Face Presets'
    bl_description = 'Create face presets from PSA facial animations (one preset for each sequence)'
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(default='*.psa', options={'HIDDEN'})
    directory: StringProperty(subtype='DIR_PATH', options={'HIDDEN', 'SKIP_SAVE'})
    files: CollectionProperty(type=OperatorFileListElement, options={'HIDDEN', 'SKIP_SAVE'})
    frame_mode: EnumProperty(name='Frame', default='PEAK', items=(
        ('PEAK', 'Most Expressive', 'Use the frame where the face differs the most from its rest pose'),
        ('FIRST', 'First', 'Use the first frame of each sequence'),
        ('MIDDLE', 'Middle', 'Use the middle frame of each sequence'),
        ('LAST', 'Last', 'Use the last frame of each sequence'),
    ))

    @classmethod
    def poll(cls, context):
        return _get_armature(context) is not None

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        armature_object = _get_armature(context)
        filepaths = [os.path.join(self.directory, f.name) for f in self.files if f.name.lower().endswith('.psa')]
        if not filepaths:
            # Nothing selected: use every PSA in the folder.
            filepaths = [os.path.join(self.directory, f) for f in sorted(os.listdir(self.directory))
                         if f.lower().endswith('.psa')] if os.path.isdir(self.directory) else []
        if not filepaths:
            self.report({'ERROR'}, 'No PSA files selected')
            return {'CANCELLED'}
        try:
            presets = import_face_presets(context, armature_object, filepaths, self.frame_mode)
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, f'Created {len(presets)} face preset(s)')
        return {'FINISHED'}


class PSK_OT_face_preset_apply(Operator):
    bl_idname = 'psk_face.apply_preset'
    bl_label = 'Apply Face Preset'
    bl_description = 'Blend the face toward the selected preset (by the blend amount)'
    bl_options = {'REGISTER', 'UNDO'}

    insert_keys: BoolProperty(name='Insert Keyframes', default=False)
    factor: FloatProperty(name='Blend', default=1.0, min=0.0, max=1.0, subtype='FACTOR')

    @classmethod
    def poll(cls, context):
        return _get_armature(context) is not None and _get_active_preset(context) is not None

    def execute(self, context):
        armature_object = _get_armature(context)
        preset = _get_active_preset(context)
        count = apply_face_preset(armature_object, preset, self.factor, self.insert_keys)
        self.report({'INFO'}, f'Applied "{preset.name}" to {count} bone(s)')
        return {'FINISHED'}


class PSK_OT_face_reset(Operator):
    bl_idname = 'psk_face.reset'
    bl_label = 'Reset Face'
    bl_description = 'Reset the facial bones to their rest pose'
    bl_options = {'REGISTER', 'UNDO'}

    insert_keys: BoolProperty(name='Insert Keyframes', default=False)

    @classmethod
    def poll(cls, context):
        return _get_armature(context) is not None

    def execute(self, context):
        reset_face(_get_armature(context), self.insert_keys)
        return {'FINISHED'}


class PSK_OT_face_preset_add(Operator):
    bl_idname = 'psk_face.add_preset'
    bl_label = 'Save Face as Preset'
    bl_description = 'Save the current pose of the face as a new preset'
    bl_options = {'REGISTER', 'UNDO'}

    name: StringProperty(name='Name', default='New Expression')

    @classmethod
    def poll(cls, context):
        return _get_armature(context) is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        action = face_preset_from_pose(_get_armature(context), self.name)
        context.scene.psk_face_preset_index = list(bpy.data.actions).index(action)
        return {'FINISHED'}


class PSK_OT_face_preset_remove(Operator):
    bl_idname = 'psk_face.remove_preset'
    bl_label = 'Remove Face Preset'
    bl_description = 'Delete the selected face preset'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _get_active_preset(context) is not None

    def execute(self, context):
        bpy.data.actions.remove(_get_active_preset(context))
        return {'FINISHED'}


class PSK_PT_face_presets(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'PSK Rig'
    bl_label = 'Face Presets'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _get_armature(context) is not None

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        row = layout.row()
        row.template_list(PSK_UL_face_presets.bl_idname, '', bpy.data, 'actions', scene, 'psk_face_preset_index', rows=6)
        col = row.column(align=True)
        col.operator(PSK_OT_face_presets_import.bl_idname, text='', icon='IMPORT')
        col.operator(PSK_OT_face_preset_add.bl_idname, text='', icon='ADD')
        col.operator(PSK_OT_face_preset_remove.bl_idname, text='', icon='REMOVE')
        if not get_face_presets():
            layout.label(text='Import facial PSA animations to', icon='INFO')
            layout.label(text='create presets (e.g., smile, blink).')
        layout.prop(scene, 'psk_face_preset_factor', slider=True)
        row = layout.row(align=True)
        op = row.operator(PSK_OT_face_preset_apply.bl_idname, text='Apply', icon='CHECKMARK')
        op.factor = scene.psk_face_preset_factor
        op.insert_keys = False
        op = row.operator(PSK_OT_face_preset_apply.bl_idname, text='Apply & Key', icon='KEY_HLT')
        op.factor = scene.psk_face_preset_factor
        op.insert_keys = True
        row = layout.row(align=True)
        op = row.operator(PSK_OT_face_reset.bl_idname, text='Reset Face', icon='LOOP_BACK')
        op.insert_keys = False
        op = row.operator(PSK_OT_face_reset.bl_idname, text='Reset & Key', icon='KEY_HLT')
        op.insert_keys = True


_classes = (
    PSK_UL_face_presets,
    PSK_OT_face_presets_import,
    PSK_OT_face_preset_apply,
    PSK_OT_face_reset,
    PSK_OT_face_preset_add,
    PSK_OT_face_preset_remove,
    PSK_PT_face_presets,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.psk_face_preset_index = IntProperty(name='Face Preset', default=0, options={'HIDDEN'})
    bpy.types.Scene.psk_face_preset_factor = FloatProperty(
        name='Blend', default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        description='How much of the preset is applied to the face')


def unregister():
    del bpy.types.Scene.psk_face_preset_factor
    del bpy.types.Scene.psk_face_preset_index
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

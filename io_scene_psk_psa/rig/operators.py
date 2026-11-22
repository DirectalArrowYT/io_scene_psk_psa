from typing import cast as typing_cast

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty
from bpy.types import Armature, Context, Object, Operator
from mathutils import Quaternion, Vector

from ..shared.helpers import get_bone_rest_correction
from .bake import (
    bake_fk_to_ik,
    bake_ik_to_fk,
    get_action_frame_range,
    get_rig_limbs,
    snap_fk_to_ik,
    snap_ik_to_fk,
    _reset_pose_bones,
)
from .builder import RigOptions, build_rig, get_rig_info, has_rig, is_rig_outdated, remove_rig, upgrade_rig
from .orientation import reorient_armature_bones
from ..psk.properties import eye_style_items


def _get_armature(context: Context) -> Object | None:
    obj = context.active_object
    if obj is not None and obj.type == 'ARMATURE':
        return obj
    if obj is not None and obj.parent is not None and obj.parent.type == 'ARMATURE':
        return obj.parent
    return None


def _convert_actions_for_new_orientation(armature_object: Object, old_corrections: dict[str, Quaternion | None]):
    """
    Converts the pose-bone animation of all the actions that animate the armature after the bones have been
    re-oriented, so that the animations look the same as before.
    """
    armature_data = typing_cast(Armature, armature_object.data)
    changes: dict[str, Quaternion] = {}
    for bone in armature_data.bones:
        new = get_bone_rest_correction(bone) or Quaternion()
        old = old_corrections.get(bone.name) or Quaternion()
        # old_local @ old = new_local @ new  =>  new_local = old_local @ (old @ new^-1)
        change = old @ new.inverted()
        if abs(abs(change.w) - 1.0) > 1e-9:
            changes[bone.name] = change
    if not changes:
        return 0
    converted = 0
    for action in bpy.data.actions:
        for layer in action.layers:
            for strip in layer.strips:
                for channelbag in strip.channelbags:
                    for name, change in changes.items():
                        prefix = f'pose.bones["{name}"].'
                        curves = {}
                        for fcurve in channelbag.fcurves:
                            if fcurve.data_path.startswith(prefix):
                                curves[(fcurve.data_path[len(prefix):], fcurve.array_index)] = fcurve
                        if not curves:
                            continue
                        times = sorted({k.co.x for f in curves.values() for k in f.keyframe_points})
                        c = change
                        ci = change.inverted()
                        m = ci.to_matrix()
                        new_values: dict[tuple[str, int], list[float]] = {key: [] for key in curves}
                        for t in times:
                            def value(prop, index, default):
                                f = curves.get((prop, index))
                                return f.evaluate(t) if f is not None else default
                            q = Quaternion([value('rotation_quaternion', i, d) for i, d in enumerate((1.0, 0.0, 0.0, 0.0))])
                            loc = Vector([value('location', i, 0.0) for i in range(3)])
                            scale = Vector([value('scale', i, 1.0) for i in range(3)])
                            q = ci @ q @ c
                            loc = ci @ loc
                            scale = Vector(sum(m[i][j] ** 2 * scale[j] for j in range(3)) for i in range(3))
                            for (prop, index) in curves:
                                match prop:
                                    case 'rotation_quaternion':
                                        new_values[(prop, index)].append(q[index])
                                    case 'location':
                                        new_values[(prop, index)].append(loc[index])
                                    case 'scale':
                                        new_values[(prop, index)].append(scale[index])
                        for key, fcurve in curves.items():
                            if key[0] not in ('rotation_quaternion', 'location', 'scale'):
                                continue
                            # Ensure that every channel has a key at each of the times.
                            fcurve.keyframe_points.clear()
                            fcurve.keyframe_points.add(len(times))
                            for i, (t, v) in enumerate(zip(times, new_values[key])):
                                fcurve.keyframe_points[i].co = (t, v)
                                fcurve.keyframe_points[i].interpolation = 'LINEAR'
                            fcurve.update()
                        converted += 1
    return converted


def make_armature_animate_ready(context: Context, armature_object: Object, options: RigOptions, fix_orientation: bool = True) -> list[str]:
    warnings = []
    if fix_orientation:
        armature_data = typing_cast(Armature, armature_object.data)
        old = {b.name: get_bone_rest_correction(b) for b in armature_data.bones}
        if has_rig(armature_object):
            remove_rig(context, armature_object)
        reorient_armature_bones(context, armature_object)
        _convert_actions_for_new_orientation(armature_object, old)
    result = build_rig(context, armature_object, options)
    warnings.extend(result.warnings)
    return warnings


class RigOptionsMixin:
    use_arm_ik: BoolProperty(name='Arm IK', default=True, description='Add IK controls to the arms')
    use_leg_ik: BoolProperty(name='Leg IK', default=True, description='Add IK controls & a foot roll to the legs')
    use_fingers: BoolProperty(name='Finger Controls', default=True,
                              description='Add hand controls to curl & spread the fingers')
    use_eyes: BoolProperty(name='Eye Controls', default=True, description='Add eye aim target controls')
    eye_style: EnumProperty(name='Eye Style', items=eye_style_items, default='AUTO')
    use_face: BoolProperty(name='Face Controls', default=True,
                           description='Add facial controls (blink, brows, jaw, mouth corners, lips & cheeks)')

    def get_rig_options(self) -> RigOptions:
        options = RigOptions()
        options.use_arm_ik = self.use_arm_ik
        options.use_leg_ik = self.use_leg_ik
        options.use_fingers = self.use_fingers
        options.use_eyes = self.use_eyes
        options.use_face = self.use_face
        options.eye_style = self.eye_style
        return options

    def draw_rig_options(self, layout):
        col = layout.column(heading='Controls')
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(self, 'use_arm_ik')
        col.prop(self, 'use_leg_ik')
        col.prop(self, 'use_fingers')
        col.prop(self, 'use_eyes')
        if self.use_eyes:
            col.prop(self, 'eye_style')
        col.prop(self, 'use_face')


class PSK_OT_rig_make_animate_ready(Operator, RigOptionsMixin):
    bl_idname = 'psk_rig.make_animate_ready'
    bl_label = 'Make Animate Ready'
    bl_description = 'Fix the bone orientations (so the bone tails point to the next joint) and add a control rig ' \
                     '(IK, foot roll, fingers, eyes & face controls). The PSK/PSA exports are not affected by the rig'
    bl_options = {'REGISTER', 'UNDO'}

    fix_orientation: BoolProperty(name='Fix Bone Orientation', default=True,
                                  description='Re-orient the bones so that the bone tails point to the next joint. '
                                              'Required for IK. Existing actions are converted automatically')

    @classmethod
    def poll(cls, context):
        return _get_armature(context) is not None

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(self, 'fix_orientation')
        self.draw_rig_options(layout)

    def execute(self, context):
        armature_object = _get_armature(context)
        assert armature_object is not None
        previous_mode = context.mode
        if previous_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        warnings = make_armature_animate_ready(context, armature_object, self.get_rig_options(), self.fix_orientation)
        for warning in warnings:
            self.report({'WARNING'}, warning)
        info = get_rig_info(armature_object) or {}
        self.report({'INFO'}, f'Rig created: {len(info.get("arms", {}))} arm(s), {len(info.get("legs", {}))} leg(s)')
        return {'FINISHED'}


class PSK_OT_rig_remove(Operator):
    bl_idname = 'psk_rig.remove'
    bl_label = 'Remove Rig'
    bl_description = 'Remove the control rig (IK, finger, eye & face controls), leaving only the deform bones. ' \
                     'Use "Bake IK → FK" first to keep the animation made with the controls'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = _get_armature(context)
        return obj is not None and has_rig(obj)

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        armature_object = _get_armature(context)
        assert armature_object is not None
        remove_rig(context, armature_object)
        self.report({'INFO'}, 'Rig removed')
        return {'FINISHED'}


limb_items = (
    ('ALL', 'All', 'All arms & legs'),
    ('ARMS', 'Arms', 'Both arms'),
    ('LEGS', 'Legs', 'Both legs'),
    ('arm_L', 'Left Arm', ''),
    ('arm_R', 'Right Arm', ''),
    ('leg_L', 'Left Leg', ''),
    ('leg_R', 'Right Leg', ''),
)


def _limbs_from_item(info: dict, item: str):
    match item:
        case 'ALL':
            return get_rig_limbs(info)
        case 'ARMS':
            return get_rig_limbs(info, kinds=('arm',))
        case 'LEGS':
            return get_rig_limbs(info, kinds=('leg',))
        case _:
            kind, side = item.split('_')
            return get_rig_limbs(info, sides=(side,), kinds=(kind,))


frame_range_items = (
    ('ACTION', 'Action', 'The frame range of the active action'),
    ('SCENE', 'Scene', 'The frame range of the scene'),
)


class _BakeOperatorMixin:
    limbs: EnumProperty(name='Limbs', items=limb_items, default='ALL')
    frame_range_source: EnumProperty(name='Frame Range', items=frame_range_items, default='ACTION')
    include_eyes: BoolProperty(name='Eyes', default=True)

    @classmethod
    def poll(cls, context):
        obj = _get_armature(context)
        if obj is None or not has_rig(obj):
            return False
        if obj.animation_data is None or obj.animation_data.action is None:
            cls.poll_message_set('The armature has no active action')
            return False
        return True

    def get_frame_range(self, context, armature_object: Object) -> tuple[int, int]:
        if self.frame_range_source == 'SCENE':
            return context.scene.frame_start, context.scene.frame_end
        return get_action_frame_range(armature_object.animation_data.action)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)


class PSK_OT_rig_bake_fk_to_ik(Operator, _BakeOperatorMixin):
    bl_idname = 'psk_rig.bake_fk_to_ik'
    bl_label = 'Bake FK → IK'
    bl_description = 'Key the IK & eye controls on every frame so that they match the FK animation, then switch to IK'
    bl_options = {'REGISTER', 'UNDO'}

    reset_additive_controls: BoolProperty(name='Reset Finger & Face Controls', default=True,
                                          description='Key the finger & face controls at rest')

    def execute(self, context):
        armature_object = _get_armature(context)
        assert armature_object is not None
        info = get_rig_info(armature_object)
        start, end = self.get_frame_range(context, armature_object)
        try:
            count = bake_fk_to_ik(context, armature_object, start, end, _limbs_from_item(info, self.limbs),
                                  self.include_eyes, self.reset_additive_controls)
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, f'Baked {count} frame(s) to IK')
        return {'FINISHED'}


class PSK_OT_rig_bake_ik_to_fk(Operator, _BakeOperatorMixin):
    bl_idname = 'psk_rig.bake_ik_to_fk'
    bl_label = 'Bake IK → FK'
    bl_description = 'Key the deform (FK) bones on every frame with the result of the IK, eye, finger & face ' \
                     'controls, then switch to FK and remove the keys of the controls'
    bl_options = {'REGISTER', 'UNDO'}

    include_additive: BoolProperty(name='Finger & Face Controls', default=True,
                                   description='Also bake the finger & face controls into the FK animation')
    clear_controls: BoolProperty(name='Clear Control Keys', default=True,
                                 description='Remove the keys of the controls that were baked')

    def execute(self, context):
        armature_object = _get_armature(context)
        assert armature_object is not None
        info = get_rig_info(armature_object)
        start, end = self.get_frame_range(context, armature_object)
        try:
            count = bake_ik_to_fk(context, armature_object, start, end, _limbs_from_item(info, self.limbs),
                                  self.include_eyes, self.include_additive, self.clear_controls)
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, f'Baked {count} frame(s) to FK')
        return {'FINISHED'}


class PSK_OT_rig_snap(Operator):
    bl_idname = 'psk_rig.snap'
    bl_label = 'Snap'
    bl_description = 'Match the IK and FK poses on the current frame'
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(name='Direction', items=(
        ('FK_TO_IK', 'FK → IK', 'Move the IK controls to the FK pose and switch to IK'),
        ('IK_TO_FK', 'IK → FK', 'Pose the FK bones like the IK pose and switch to FK'),
    ))
    limbs: EnumProperty(name='Limbs', items=limb_items, default='ALL')

    @classmethod
    def poll(cls, context):
        obj = _get_armature(context)
        return obj is not None and has_rig(obj)

    def execute(self, context):
        armature_object = _get_armature(context)
        assert armature_object is not None
        info = get_rig_info(armature_object)
        limbs = _limbs_from_item(info, self.limbs)
        if self.direction == 'FK_TO_IK':
            snap_fk_to_ik(context, armature_object, limbs)
        else:
            snap_ik_to_fk(context, armature_object, limbs)
        return {'FINISHED'}


class PSK_OT_rig_reset_controls(Operator):
    bl_idname = 'psk_rig.reset_controls'
    bl_label = 'Reset Controls'
    bl_description = 'Reset the pose of the finger & face controls to rest'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = _get_armature(context)
        return obj is not None and has_rig(obj)

    def execute(self, context):
        armature_object = _get_armature(context)
        assert armature_object is not None
        info = get_rig_info(armature_object) or {}
        _reset_pose_bones(armature_object, info.get('additive_controls', []))
        return {'FINISHED'}


class PSK_OT_rig_update(Operator):
    bl_idname = 'psk_rig.update'
    bl_label = 'Update Rig'
    bl_description = 'Update a rig made with an older version of the add-on (the controls & their animation are ' \
                     'kept): the IK follows the scale of the limb bones, the fingers get sliders & a thumb pad, and ' \
                     'the feet get a foot roll control'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = _get_armature(context)
        return obj is not None and is_rig_outdated(obj)

    def execute(self, context):
        armature_object = _get_armature(context)
        assert armature_object is not None
        try:
            upgrade_rig(armature_object, context)
        except RuntimeError as e:
            self.report({'ERROR'}, f'Could not update the rig: {e}')
            return {'CANCELLED'}
        self.report({'INFO'}, 'Rig updated')
        return {'FINISHED'}


@persistent
def _upgrade_rigs_on_load(*_):
    """Rigs made with an older version of the add-on are updated when a file is opened."""
    for obj in bpy.data.objects:
        if obj.type != 'ARMATURE' or obj.library is not None or obj.override_library is not None:
            continue
        try:
            if upgrade_rig(obj):
                print(f'PSK Rig: updated the rig of "{obj.name}"')
        except Exception as e:
            print(f'PSK Rig: could not update the rig of "{obj.name}": {e}')


_classes = (
    PSK_OT_rig_make_animate_ready,
    PSK_OT_rig_update,
    PSK_OT_rig_remove,
    PSK_OT_rig_bake_fk_to_ik,
    PSK_OT_rig_bake_ik_to_fk,
    PSK_OT_rig_snap,
    PSK_OT_rig_reset_controls,
)

from bpy.utils import register_classes_factory
_register_classes, _unregister_classes = register_classes_factory(_classes)


def register():
    _register_classes()
    if _upgrade_rigs_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_upgrade_rigs_on_load)


def unregister():
    if _upgrade_rigs_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_upgrade_rigs_on_load)
    _unregister_classes()

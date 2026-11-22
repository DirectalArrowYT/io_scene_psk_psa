from bpy.types import Panel

from .builder import (
    COLLECTION_BODY, COLLECTION_FACE, COLLECTION_FACE_CONTROLS, COLLECTION_FINGERS, COLLECTION_HAND, COLLECTION_IK,
    COLLECTION_MECHANISM, get_rig_info, is_rig_outdated,
)
from .operators import (
    PSK_OT_rig_bake_fk_to_ik, PSK_OT_rig_bake_ik_to_fk, PSK_OT_rig_make_animate_ready, PSK_OT_rig_remove,
    PSK_OT_rig_reset_controls, PSK_OT_rig_snap, PSK_OT_rig_update, _get_armature,
)

SIDE_LABELS = {'L': 'Left', 'R': 'Right'}


class PSK_PT_rig(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'PSK Rig'
    bl_label = 'Animate Ready Rig'

    @classmethod
    def poll(cls, context):
        return _get_armature(context) is not None

    def draw(self, context):
        layout = self.layout
        armature_object = _get_armature(context)
        info = get_rig_info(armature_object)
        if info is None:
            layout.label(text='No control rig', icon='INFO')
            layout.operator(PSK_OT_rig_make_animate_ready.bl_idname, icon='ARMATURE_DATA')
            return

        if is_rig_outdated(armature_object):
            box = layout.box()
            box.label(text='Made with an older version', icon='ERROR')
            box.operator(PSK_OT_rig_update.bl_idname, icon='FILE_REFRESH')

        # IK/FK switches.
        box = layout.box()
        box.label(text='IK / FK', icon='CON_KINEMATIC')
        for kind, key in (('arm', 'arms'), ('leg', 'legs')):
            for side, limb in sorted(info.get(key, {}).items()):
                prop = limb['prop']
                if prop not in armature_object:
                    continue
                row = box.row(align=True)
                row.prop(armature_object, f'["{prop}"]', text=f'{SIDE_LABELS.get(side, side)} {kind.title()}', slider=True)
                op = row.operator(PSK_OT_rig_snap.bl_idname, text='', icon='CON_KINEMATIC')
                op.direction = 'FK_TO_IK'
                op.limbs = f'{kind}_{side}'
                op = row.operator(PSK_OT_rig_snap.bl_idname, text='', icon='BONE_DATA')
                op.direction = 'IK_TO_FK'
                op.limbs = f'{kind}_{side}'
        row = box.row(align=True)
        op = row.operator(PSK_OT_rig_snap.bl_idname, text='All FK → IK')
        op.direction = 'FK_TO_IK'
        op.limbs = 'ALL'
        op = row.operator(PSK_OT_rig_snap.bl_idname, text='All IK → FK')
        op.direction = 'IK_TO_FK'
        op.limbs = 'ALL'

        # Foot roll.
        legs = info.get('legs', {})
        if legs:
            box = layout.box()
            box.label(text='Foot Roll', icon='MOD_DYNAMICPAINT')
            for side, limb in sorted(legs.items()):
                if limb['roll_prop'] in armature_object:
                    box.prop(armature_object, f'["{limb["roll_prop"]}"]', text=f'{SIDE_LABELS.get(side, side)} Foot', slider=True)

        # Eyes.
        eyes = info.get('eyes')
        if eyes and eyes['prop'] in armature_object:
            box = layout.box()
            box.label(text='Eyes', icon='HIDE_OFF')
            box.prop(armature_object, f'["{eyes["prop"]}"]', text='Aim at Target', slider=True)

        # Bone collections.
        armature_data = armature_object.data
        box = layout.box()
        box.label(text='Visibility', icon='GROUP_BONE')
        grid = box.grid_flow(columns=2, even_columns=True, align=True)
        for name in (COLLECTION_IK, COLLECTION_HAND, COLLECTION_FACE_CONTROLS, COLLECTION_BODY, COLLECTION_FINGERS,
                     COLLECTION_FACE, COLLECTION_MECHANISM):
            collection = armature_data.collections.get(name)
            if collection is not None:
                grid.prop(collection, 'is_visible', text=name, toggle=True)

        # Baking.
        box = layout.box()
        box.label(text='Bake (Active Action)', icon='ACTION')
        col = box.column(align=True)
        col.operator(PSK_OT_rig_bake_fk_to_ik.bl_idname, icon='CON_KINEMATIC')
        col.operator(PSK_OT_rig_bake_ik_to_fk.bl_idname, icon='BONE_DATA')
        box.operator(PSK_OT_rig_reset_controls.bl_idname, icon='LOOP_BACK')

        box = layout.box()
        col = box.column(align=True)
        col.operator(PSK_OT_rig_make_animate_ready.bl_idname, text='Rebuild Rig', icon='FILE_REFRESH')
        col.operator(PSK_OT_rig_remove.bl_idname, icon='X')


class PSK_PT_rig_help(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'PSK Rig'
    bl_label = 'Controls Help'
    bl_parent_id = 'PSK_PT_rig'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        col = self.layout.column(align=True)
        for line in (
            'Hands: slide the Curl & Spread sliders over',
            '  the back of the hand (toward the fingertips',
            '  curls), or a finger\'s own slider. Move the',
            '  thumb pad to curl & swing the thumb.',
            'Arms & legs: move the IK controls & poles.',
            '  Rotate the twist rings to twist the limb.',
            'Feet: rotate the arrow behind the heel (or',
            '  use Foot Roll) to roll on the ball of the',
            '  foot (the toes stay on the floor).',
            'Eyes: move the eye target controls.',
            'Face: move the blink, brow, mouth corner,',
            '  lip, cheek & jaw controls in front of',
            '  the face. Rotate the brow controls to tilt.',
            'The controls are never exported.',
        ):
            col.label(text=line)


_classes = (
    PSK_PT_rig,
    PSK_PT_rig_help,
)

from bpy.utils import register_classes_factory
register, unregister = register_classes_factory(_classes)

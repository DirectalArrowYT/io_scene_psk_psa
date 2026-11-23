"""
The "Animate Ready" control rig.

The control rig is built directly on top of the imported (deform) skeleton:

* The deform bones keep their names & hierarchy, so PSA animations can be imported & exported as usual. The deform
  bones themselves are the FK controls.
* IK is added to the deform chains of the arms & legs with constraints, the influence of which is controlled by an
  IK/FK switch property on the armature object. The IK controls, pole targets and the foot roll mechanism are extra
  bones that are flagged as rig bones, so they are never exported.
* Finger, eye & face controls are *additive* (i.e., they are layered on top of the FK animation), so imported
  animations look exactly the same as long as those controls are at rest.

All rig constraints are prefixed with `CONSTRAINT_PREFIX`, and all rig bones have the `RIG_BONE_PROPERTY` custom
property, so that the rig can be cleanly removed again.
"""
import json
import math
import re
from typing import cast as typing_cast

import bpy
from bpy.types import Armature, Context, Object, PoseBone, Constraint
from mathutils import Matrix, Vector

from ..shared.helpers import RIG_BONE_PROPERTY, is_rig_bone
from .orientation import _orientation_matrix, BoneOrientation, get_palm_side_vector, _limb_bend_vector, skeleton_from_bones
from .skeleton import Skeleton, SkeletonMap, analyze_skeleton, _is_helper
from .widgets import get_widget

RIG_INFO_PROPERTY = 'psk_rig_info'
# Version 2: the IK chains follow the scale of the limb bones & their parents.
# Version 3: finger sliders & a thumb pad on the hands, and a foot roll control on the feet.
# Version 4: the rotation helper bones at the limb joints (e.g., `L_elbow_EX`) follow the joints.
RIG_VERSION = 4
CONSTRAINT_PREFIX = 'PSKRig'
CTRL = 'CTRL-'
MCH = 'MCH-'

COLLECTION_IK = 'IK Controls'
COLLECTION_HAND = 'Hand Controls'
COLLECTION_FACE_CONTROLS = 'Face Controls'
COLLECTION_MECHANISM = 'Mechanism'
COLLECTION_BODY = 'Body'
COLLECTION_FINGERS = 'Fingers'
COLLECTION_FACE = 'Face'
COLLECTION_HELPERS = 'Joint Helpers'

RIG_COLLECTIONS = (COLLECTION_IK, COLLECTION_HAND, COLLECTION_FACE_CONTROLS, COLLECTION_MECHANISM)
DEFORM_COLLECTIONS = (COLLECTION_BODY, COLLECTION_FINGERS, COLLECTION_FACE, COLLECTION_HELPERS)

SIDE_COLORS = {'L': 'THEME04', 'R': 'THEME01', 'C': 'THEME09'}
FACE_COLOR = 'THEME06'
FACE_DETAIL_COLOR = 'THEME07'
MECHANISM_COLOR = 'THEME10'

# Default ranges for the control mappings.
FINGER_CURL_ANGLE = math.radians(85.0)
THUMB_CURL_ANGLE = math.radians(55.0)
THUMB_CURL_WEIGHTS = (0.35, 0.8, 1.0)
FINGER_SPREAD_ANGLE = math.radians(15.0)
# The spread of the outermost finger with the spread slider at the end of its track.
FINGER_SPREAD_MAX_ANGLE = math.radians(30.0)
# The sideways swing of the base of the thumb with the thumb pad at its edge. The tip of the thumb doesn't swing.
THUMB_SIDE_ANGLE = math.radians(45.0)
THUMB_SIDE_WEIGHTS = (0.75, 0.3, 0.0)
# The foot roll at the end of the foot roll slider (in the panel), and the limit of the foot roll control.
FOOT_ROLL_ANGLE = math.radians(60.0)
FOOT_ROLL_MAX_ANGLE = math.radians(90.0)
JAW_OPEN_ANGLE = math.radians(30.0)
JAW_SIDE_ANGLE = math.radians(10.0)


class RigOptions:
    def __init__(self):
        self.use_arm_ik = True
        self.use_leg_ik = True
        self.use_fingers = True
        self.use_eyes = True
        self.use_face = True
        # One of 'AUTO', 'SLIDE' (flat irises that slide across the face, common in anime-style games) or 'AIM'
        # (spherical eyeballs that rotate to look at a target).
        self.eye_style = 'AUTO'


class RigBuildResult:
    def __init__(self):
        self.warnings: list[str] = []
        self.info: dict = {}


def side_suffix(side: str) -> str:
    return f'.{side}' if side in ('L', 'R') else ''


def get_rig_info(armature_object: Object) -> dict | None:
    if armature_object is None or armature_object.type != 'ARMATURE':
        return None
    value = armature_object.data.get(RIG_INFO_PROPERTY)
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def has_rig(armature_object: Object) -> bool:
    return get_rig_info(armature_object) is not None


class _BoneSpec:
    def __init__(self, name: str, matrix: Matrix, length: float, parent: str | None, kind: str, collection: str,
                 color: str, widget: str | None = None, widget_scale: float | Vector = 1.0,
                 widget_translation: Vector | None = None, rotation_mode: str = 'QUATERNION',
                 lock_location: tuple = (False, False, False), lock_rotation: tuple = (False, False, False),
                 lock_rotation_w: bool = False, lock_scale: tuple = (True, True, True), hide: bool = False,
                 location_limits: tuple | None = None, hide_select: bool = False):
        self.name = name
        self.matrix = matrix
        self.length = length
        self.parent = parent
        self.kind = kind
        self.collection = collection
        self.color = color
        self.widget = widget
        self.widget_scale = widget_scale
        self.widget_translation = widget_translation
        self.rotation_mode = rotation_mode
        self.lock_location = lock_location
        self.lock_rotation = lock_rotation
        self.lock_rotation_w = lock_rotation_w
        self.lock_scale = lock_scale
        self.hide = hide
        # The (min, max) of the local location on each axis (for slider & pad controls).
        self.location_limits = location_limits
        self.hide_select = hide_select


def _matrix(head: Vector, y: Vector, z_hint: Vector | None = None, x_hint: Vector | None = None) -> Matrix:
    m = _orientation_matrix(BoneOrientation(y, 1.0, x_axis_hint=x_hint, z_axis_hint=z_hint)).to_4x4()
    m.translation = head
    return m


def _add_driver(target, data_path: str, armature_object: Object, prop: str, index: int = -1, expression: str | None = None):
    fcurve = target.driver_add(data_path, index) if index >= 0 else target.driver_add(data_path)
    driver = fcurve.driver
    driver.type = 'SCRIPTED' if expression else 'AVERAGE'
    for v in list(driver.variables):
        driver.variables.remove(v)
    var = driver.variables.new()
    var.name = 'v'
    var.type = 'SINGLE_PROP'
    var.targets[0].id_type = 'OBJECT'
    var.targets[0].id = armature_object
    var.targets[0].data_path = f'["{prop}"]'
    if expression:
        driver.expression = expression
    # Remove the default modifiers so the driver value is used as-is.
    for modifier in list(fcurve.modifiers):
        fcurve.modifiers.remove(modifier)
    return fcurve


def set_rig_property(armature_object: Object, name: str, value: float):
    """
    Sets a rig property (e.g., an IK/FK switch). Setting an ID property from Python does not tag the object for an
    update, so the drivers that use the property would not be re-evaluated.
    """
    armature_object[name] = value
    armature_object.update_tag()


def _ensure_float_property(armature_object: Object, name: str, default: float, min_value: float, max_value: float,
                           description: str, soft_min: float | None = None, soft_max: float | None = None):
    if name not in armature_object:
        armature_object[name] = default
    ui = armature_object.id_properties_ui(name)
    ui.update(min=min_value, max=max_value,
              soft_min=soft_min if soft_min is not None else min_value,
              soft_max=soft_max if soft_max is not None else max_value,
              description=description, default=default)
    armature_object.property_overridable_library_set(f'["{name}"]', True)


def _new_constraint(pose_bone: PoseBone, type_: str, name: str) -> Constraint:
    constraint = pose_bone.constraints.new(type_)
    constraint.name = f'{CONSTRAINT_PREFIX} {name}'
    return constraint


def _add_transform_constraint(armature_object: Object, owner: PoseBone, control: str, name: str,
                              map_from: str, from_axis: str, from_range: tuple[float, float],
                              map_to: str, to_values_min: Vector, to_values_max: Vector | None = None):
    """
    Adds an additive Transformation constraint that maps a single axis of the control's local transform to the
    owner's local location or rotation.
    """
    if to_values_max is None:
        to_values_max = Vector((0.0, 0.0, 0.0))
    c = typing_cast(bpy.types.TransformConstraint, _new_constraint(owner, 'TRANSFORM', name))
    c.target = armature_object
    c.subtarget = control
    c.owner_space = 'LOCAL'
    c.target_space = 'LOCAL'
    c.use_motion_extrapolate = True
    c.map_from = map_from
    lo, hi = from_range
    suffix = {'LOCATION': '', 'ROTATION': '_rot', 'SCALE': '_scale'}[map_from]
    for axis in 'xyz':
        # Unused axes get a non-degenerate range that maps to zero.
        default_lo, default_hi = (0.0, 1.0)
        setattr(c, f'from_min_{axis}{suffix}', lo if axis == from_axis.lower() else default_lo)
        setattr(c, f'from_max_{axis}{suffix}', hi if axis == from_axis.lower() else default_hi)
    c.map_to = map_to
    c.map_to_x_from = from_axis
    c.map_to_y_from = from_axis
    c.map_to_z_from = from_axis
    to_suffix = {'LOCATION': '', 'ROTATION': '_rot', 'SCALE': '_scale'}[map_to]
    for i, axis in enumerate('xyz'):
        setattr(c, f'to_min_{axis}{to_suffix}', to_values_min[i])
        setattr(c, f'to_max_{axis}{to_suffix}', to_values_max[i])
    if map_to == 'LOCATION':
        c.mix_mode = 'ADD'
    elif map_to == 'ROTATION':
        c.mix_mode_rot = 'AFTER'
    return c


def _to_local(bone_matrix: Matrix, vector: Vector) -> Vector:
    """Converts an armature-space direction/offset into the local (rest) space of a bone."""
    return bone_matrix.to_3x3().normalized().inverted() @ vector


def _create_spec_edit_bones(armature_data: Armature, specs: list[_BoneSpec], collections: dict):
    """Creates the edit bones of the rig bone specs. The armature must be in edit mode."""
    edit_bones = armature_data.edit_bones
    for spec in specs:
        edit_bone = edit_bones.new(spec.name)
        edit_bone.head = spec.matrix.translation
        edit_bone.tail = spec.matrix.translation + Vector((0.0, max(spec.length, 1e-4), 0.0))
        edit_bone.matrix = spec.matrix
        edit_bone.length = max(spec.length, 1e-4)
        edit_bone.use_deform = False
        edit_bone[RIG_BONE_PROPERTY] = spec.kind
        if spec.parent is not None:
            edit_bone.parent = edit_bones[spec.parent]
        collections[spec.collection].assign(edit_bone)


def _apply_spec_pose_settings(armature_object: Object, specs: list[_BoneSpec]):
    """Applies the pose bone settings (locks, limits, colors & custom shapes) of the rig bone specs."""
    assert armature_object.pose
    pose = armature_object.pose.bones
    for spec in specs:
        pose_bone = pose[spec.name]
        bone = pose_bone.bone
        pose_bone.rotation_mode = spec.rotation_mode
        pose_bone.lock_location = spec.lock_location
        pose_bone.lock_rotation = spec.lock_rotation
        pose_bone.lock_rotation_w = spec.lock_rotation_w
        pose_bone.lock_scale = spec.lock_scale
        bone.color.palette = spec.color
        pose_bone.color.palette = spec.color
        bone.hide = spec.hide
        bone.hide_select = spec.hide_select
        if spec.location_limits is not None:
            limit = typing_cast(bpy.types.LimitLocationConstraint, _new_constraint(pose_bone, 'LIMIT_LOCATION', 'Range'))
            limit.owner_space = 'LOCAL'
            limit.use_transform_limit = True
            for axis, (low, high) in zip('xyz', spec.location_limits):
                setattr(limit, f'use_min_{axis}', True)
                setattr(limit, f'use_max_{axis}', True)
                setattr(limit, f'min_{axis}', low)
                setattr(limit, f'max_{axis}', high)
        if spec.widget:
            pose_bone.custom_shape = get_widget(spec.widget)
            pose_bone.use_custom_shape_bone_size = False
            scale = spec.widget_scale
            pose_bone.custom_shape_scale_xyz = scale if isinstance(scale, Vector) else (scale, scale, scale)
            if spec.widget_translation is not None:
                pose_bone.custom_shape_translation = spec.widget_translation
            bone.show_wire = True


def _floor_height(skeleton: Skeleton, smap: SkeletonMap) -> float:
    """The height of the floor (used for the foot controls)."""
    floor_height = skeleton.head(smap.root).dot(smap.up)
    feet_heights = [skeleton.head(leg.toe or leg.foot).dot(smap.up) for leg in smap.legs.values()]
    if feet_heights and floor_height > min(feet_heights):
        floor_height = min(feet_heights)
    return floor_height


def _foot_geometry(skeleton: Skeleton, smap: SkeletonMap, leg, floor_height: float) -> dict:
    """Where the foot is & which way it points (flat on the floor), for the foot controls."""
    up, forward = smap.up, smap.forward
    ankle = skeleton.head(leg.foot)
    upper_length = (skeleton.head(leg.lower) - skeleton.head(leg.upper)).length
    toe_position = skeleton.head(leg.toe) if leg.toe else ankle + forward * upper_length * 0.25
    foot_forward = toe_position - ankle
    foot_forward = foot_forward - up * foot_forward.dot(up)
    if foot_forward.length < skeleton.epsilon:
        foot_forward = forward.copy()
    foot_forward.normalize()
    foot_length = max((toe_position - ankle).length, upper_length * 0.1)
    ankle_height = ankle.dot(up) - floor_height
    heel_position = ankle - up * ankle_height - foot_forward * foot_length * 0.25
    return {'ankle': ankle, 'toe': toe_position, 'forward': foot_forward, 'length': foot_length,
            'ankle_height': ankle_height, 'heel': heel_position}


def _add_foot_roll_control(add_spec, side: str, geometry: dict, up: Vector, ik_name: str) -> str:
    """
    Adds the foot roll control: an arrow behind the heel that follows the foot IK control. Rotating it forward rolls
    the foot onto the ball (the heel lifts while the toes stay on the floor), rotating it back lifts the toes.
    """
    name = f'{CTRL}foot_roll{side_suffix(side)}'
    foot_forward, foot_length = geometry['forward'], geometry['length']
    position = geometry['heel'] - foot_forward * foot_length * 0.9
    add_spec(name=name, matrix=_matrix(position, foot_forward, z_hint=up), length=foot_length * 0.3, parent=ik_name,
             kind='CTRL', collection=COLLECTION_IK, color=SIDE_COLORS[side], widget='bone_arrow',
             widget_scale=foot_length * 0.6, rotation_mode='XYZ', lock_location=(True, True, True),
             lock_rotation=(False, True, True))
    return name


def _add_foot_roll_drivers(armature_object: Object, limb: dict):
    """
    Drives the heel & ball pivots of a foot with the foot roll slider (the property) plus the foot roll control
    (replacing any existing drivers). The pivot bones point forward with Z up, so their X axis points to the
    character's right & a positive rotation around X lifts the front of the foot.
    """
    assert armature_object.pose
    pose = armature_object.pose.bones
    roll_ctrl = limb.get('roll_ctrl')
    roll = f'(v*{FOOT_ROLL_ANGLE:.6f}+r)' if roll_ctrl else f'(v*{FOOT_ROLL_ANGLE:.6f})'
    for name, expression in ((limb['ball'], f'-min(max({roll},0),{FOOT_ROLL_MAX_ANGLE:.6f})'),
                             (limb['heel'], f'-max(min({roll},0),-{FOOT_ROLL_MAX_ANGLE:.6f})')):
        pose[name].driver_remove('rotation_euler', 0)
        fcurve = _add_driver(pose[name], 'rotation_euler', armature_object, limb['roll_prop'], index=0,
                             expression=expression)
        if roll_ctrl:
            var = fcurve.driver.variables.new()
            var.name = 'r'
            var.type = 'TRANSFORMS'
            var.targets[0].id = armature_object
            var.targets[0].bone_target = roll_ctrl
            var.targets[0].transform_type = 'ROT_X'
            var.targets[0].transform_space = 'LOCAL_SPACE'


def _build_fingers(skeleton: Skeleton, smap: SkeletonMap, rest: dict, lengths: dict, add_spec, constraint_jobs: list,
                   info: dict, armature_object: Object):
    """
    Adds the finger controls: a panel of sliders that floats over the back of each hand.

    * Curl & Spread (near the wrist) curl & spread all the fingers.
    * One slider per finger (over the knuckles, in the order of the fingers) curls that finger on top of Curl.
    * The thumb has a 2D pad on its side of the hand: along the hand curls it, across the hand swings it in & out.

    Sliding toward the fingertips curls the fingers, sliding toward the wrist straightens them (and bends them back
    past the rest pose). All of it is layered on top of the FK animation of the finger bones.
    """
    up = smap.up
    hands = info.setdefault('hands', {})
    for side, arm in smap.arms.items():
        if not arm.fingers:
            continue
        s = side_suffix(side)
        palm = get_palm_side_vector(skeleton, smap, side) or -up
        wrist = skeleton.head(arm.hand)
        along = rest[arm.hand].col[1].xyz.normalized()
        back = -palm - along * (-palm).dot(along)
        back = back.normalized() if back.length > 1e-6 else up.copy()
        knuckles = {kind: skeleton.head(bones[0]) for kind, bones in arm.fingers.items()}
        first = next((knuckles[k] for k in ('index', 'middle') if k in knuckles), None)
        last = next((knuckles[k] for k in ('pinky', 'ring') if k in knuckles), None)
        # Across the hand, from the index finger to the pinky.
        across = back.cross(along) if side == 'L' else along.cross(back)
        span = 0.0
        if first is not None and last is not None:
            knuckle_line = last - first
            knuckle_line = knuckle_line - along * knuckle_line.dot(along) - back * knuckle_line.dot(back)
            if knuckle_line.length > skeleton.epsilon:
                across, span = knuckle_line.normalized(), knuckle_line.length
        across.normalize()

        # The size of the panel follows the hand, and it floats just clear of the hand IK control's cube.
        box = max(lengths[arm.hand] * 0.9, span * 1.6)
        travel = box * 0.18
        half_width = box * 0.42
        top = wrist + back * max(box * 0.58, lengths[arm.hand] * 0.55)
        slider_settings = dict(parent=arm.hand, kind='CTRL', collection=COLLECTION_HAND, widget='slider',
                               widget_scale=box * 0.22, length=box * 0.08, rotation_mode='XYZ',
                               lock_location=(True, False, True), lock_rotation=(True, True, True),
                               lock_rotation_w=True, location_limits=((0.0, 0.0), (-travel, travel), (0.0, 0.0)))

        def slider_matrix(position: Vector) -> Matrix:
            return _matrix(position, along, z_hint=back)

        curl_name, spread_name = f'{CTRL}fingers_curl{s}', f'{CTRL}fingers_spread{s}'
        add_spec(name=curl_name, color='THEME09',
                 matrix=slider_matrix(top - along * (box * 0.32) - across * (half_width * 0.55)), **slider_settings)
        add_spec(name=spread_name, color='THEME03',
                 matrix=slider_matrix(top - along * (box * 0.32) + across * (half_width * 0.55)), **slider_settings)
        info['additive_controls'].extend([curl_name, spread_name])

        # The finger sliders are evenly spaced, in the order of the fingers across the hand.
        finger_controls = {}
        kinds = sorted((k for k in arm.fingers if k != 'thumb'), key=lambda k: (knuckles[k] - wrist).dot(across))
        for index, kind in enumerate(kinds):
            x = (index / (len(kinds) - 1) - 0.5) * 2.0 * half_width if len(kinds) > 1 else 0.0
            name = f'{CTRL}finger_{kind}{s}'
            add_spec(name=name, color=SIDE_COLORS[side], matrix=slider_matrix(top + along * (box * 0.4) + across * x),
                     **slider_settings)
            finger_controls[kind] = name
            info['additive_controls'].append(name)

        thumb_name, pad_name, thumb_side_sign = None, None, 1.0
        if 'thumb' in arm.fingers:
            thumb_name, pad_name = f'{CTRL}thumb{s}', f'{CTRL}thumb_pad{s}'
            # The pad sits on the thumb's side of the hand (the index finger's side).
            origin = first if first is not None else wrist
            thumb_side = 1.0 if (knuckles['thumb'] - origin).dot(across) > 0.0 else -1.0
            position = wrist + back * (box * 0.12) + along * (box * 0.22) + across * (thumb_side * box * 0.58)
            knob_scale = travel * 0.32
            add_spec(name=pad_name, matrix=slider_matrix(position), length=box * 0.08, parent=arm.hand, kind='CTRL',
                     collection=COLLECTION_HAND, color='THEME02', widget='square_flat', widget_scale=travel + knob_scale,
                     rotation_mode='XYZ', lock_location=(True, True, True), lock_rotation=(True, True, True),
                     lock_rotation_w=True, hide_select=True)
            add_spec(name=thumb_name, matrix=slider_matrix(position), length=box * 0.08, parent=pad_name, kind='CTRL',
                     collection=COLLECTION_HAND, color='THEME02', widget='square_flat', widget_scale=knob_scale,
                     rotation_mode='XYZ', lock_location=(False, False, True), lock_rotation=(True, True, True),
                     lock_rotation_w=True, location_limits=((-travel, travel), (-travel, travel), (0.0, 0.0)))
            finger_controls['thumb'] = thumb_name
            info['additive_controls'].append(thumb_name)
            # Sliding the pad toward the rest of the hand swings the thumb in (toward the fingers & the palm).
            inward = -across * thumb_side
            pad_x = slider_matrix(position).col[0].xyz.normalized()
            thumb_x = rest[arm.fingers['thumb'][0]].col[0].xyz.normalized()
            # A positive rotation around the local Z axis moves the tip of the bone toward its -X axis.
            swing = (-thumb_x).dot(inward)
            if abs(swing) < 0.2:
                swing = (-thumb_x).dot(palm)
            thumb_side_sign = (1.0 if swing >= 0.0 else -1.0) * (1.0 if pad_x.dot(inward) >= 0.0 else -1.0)

        hands[side] = {'curl': curl_name, 'spread': spread_name, 'thumb': thumb_name, 'thumb_pad': pad_name,
                       'fingers': finger_controls, 'travel': travel}

        # Spread factors: how far each finger is from the middle of the hand, along the hand's width.
        reference = arm.fingers.get('middle', next(iter(arm.fingers.values())))[0]
        reference_position = skeleton.head(reference)

        def fingers_job(pose, arm=arm, curl_name=curl_name, spread_name=spread_name, finger_controls=finger_controls,
                        reference_position=reference_position, travel=travel, thumb_side_sign=thumb_side_sign):
            zero = Vector((0.0, 0.0, 0.0))
            offsets = {}
            for kind, bones in arm.fingers.items():
                x_axis = rest[bones[0]].col[0].xyz.normalized()
                offsets[kind] = (skeleton.head(bones[0]) - reference_position).dot(x_axis)
            max_offset = max((abs(v) for v in offsets.values()), default=0.0)
            for kind, bones in arm.fingers.items():
                for index, bone in enumerate(bones):
                    curl = FINGER_CURL_ANGLE
                    if kind == 'thumb':
                        # The thumb's base joint moves much less than its tip.
                        curl = THUMB_CURL_ANGLE * THUMB_CURL_WEIGHTS[min(index, len(THUMB_CURL_WEIGHTS) - 1)]
                    # Curl (around the local X axis, toward the palm).
                    for control, label in ((curl_name, 'Fingers Curl'), (finger_controls[kind], 'Finger Curl')):
                        _add_transform_constraint(armature_object, pose[bone], control, label,
                                                  map_from='LOCATION', from_axis='Y', from_range=(0.0, travel),
                                                  map_to='ROTATION', to_values_min=zero,
                                                  to_values_max=Vector((curl, 0.0, 0.0)))
                    if kind == 'thumb':
                        # Swing (around the local Z axis), from the pad's other axis.
                        weight = THUMB_SIDE_WEIGHTS[min(index, len(THUMB_SIDE_WEIGHTS) - 1)]
                        if weight > 0.0:
                            angle = thumb_side_sign * THUMB_SIDE_ANGLE * weight
                            _add_transform_constraint(armature_object, pose[bone], finger_controls[kind],
                                                      'Finger Side', map_from='LOCATION', from_axis='X',
                                                      from_range=(0.0, travel), map_to='ROTATION', to_values_min=zero,
                                                      to_values_max=Vector((0.0, 0.0, angle)))
                # Spread (around the local Z axis of the first bone).
                if max_offset > skeleton.epsilon:
                    factor = offsets[kind] / max_offset
                    if kind == 'thumb':
                        factor *= 1.5
                    # A negative rotation around Z moves the tip of the bone toward +X.
                    angle = -factor * FINGER_SPREAD_MAX_ANGLE
                    _add_transform_constraint(armature_object, pose[bones[0]], spread_name, 'Fingers Spread',
                                              map_from='LOCATION', from_axis='Y', from_range=(0.0, travel),
                                              map_to='ROTATION', to_values_min=zero,
                                              to_values_max=Vector((0.0, 0.0, angle)))
            return None
        constraint_jobs.append(('additive', fingers_job))


def _dominant_axis(vector: Vector) -> int:
    """The index of the largest component of the vector (Y for a zero vector)."""
    if vector.length < 1e-9:
        return 1
    return max(range(3), key=lambda i: abs(vector[i]))


# How the rotation helper bones at the limb joints follow the joint (in the frame of the joint bone, with Y along the
# bone). Helper bones (e.g., `L_elbow_EX` in Unreal Engine skeletons) are deform bones that share the parent & the
# position of a joint and keep the skin around the joint from collapsing. Games drive them at runtime, so animations
# usually don't key them. The rules were measured from game animations that do key them (Unreal Engine `_EX` bones):
#   * 'swing': the helper follows the joint without its twist (shoulders & hips).
#   * 'bend': a fraction of the bend of the joint, without its twist (elbows & knees).
#   * 'twist': a fraction of the rotation of the joint about the Y axis of the helper (wrists & ankles).
JOINT_HELPER_RULES = {
    'upper': ('swing', None),
    'lower': ('bend', 0.5),
    'hand': ('twist', 0.5),
    'foot': ('twist', 0.5),
}


def _find_joint_helpers(skeleton: Skeleton, joint: str) -> list[str]:
    """The helper bones of a joint: leaf helper bones that share the parent & the head of the joint."""
    parent = skeleton.parent(joint)
    if parent is None:
        return []
    return [name for name in skeleton.children(parent)
            if name != joint and _is_helper(name) and not skeleton.children(name)
            and skeleton.distance(name, joint) <= skeleton.epsilon]


def _build_joint_helpers(skeleton: Skeleton, smap: SkeletonMap, rest: dict, lengths: dict, add_spec,
                         constraint_jobs: list, info: dict, armature_object: Object):
    """
    Makes the rotation helper bones of the limb joints follow the joints. Each helper gets a mechanism bone that
    applies the rule of the joint (`JOINT_HELPER_RULES`), and the helper copies its rotation in its own orientation
    (the rest orientations of the helpers differ from the joint's).
    """
    joints = []
    for arm in smap.arms.values():
        joints += [(arm.upper, 'upper', None, None), (arm.lower, 'lower', arm.upper, arm.hand),
                   (arm.hand, 'hand', None, None)]
    for leg in smap.legs.values():
        joints += [(leg.upper, 'upper', None, None), (leg.lower, 'lower', leg.upper, leg.foot),
                   (leg.foot, 'foot', None, None)]
    items = []
    for joint, role, upper, end in joints:
        rule, value = JOINT_HELPER_RULES[role]
        bend_axis = 0
        if rule == 'bend' and upper is not None and end is not None:
            # The bend axis is the normal of the plane of the limb (X if the limb is straight).
            normal = (rest[joint].translation - rest[upper].translation).cross(
                rest[end].translation - rest[joint].translation)
            if normal.length > 1e-6 * max(lengths[joint], 1e-6) ** 2:
                axis = _dominant_axis(_to_local(rest[joint], normal))
                bend_axis = axis if axis != 1 else 0
        for helper in _find_joint_helpers(skeleton, joint):
            # The mechanism bone has the rest pose of the joint, or of the helper for a twist about the helper's axis.
            mch = f'{MCH}{helper}'
            matrix = (rest[helper] if rule == 'twist' else rest[joint]).copy()
            add_spec(name=mch, matrix=matrix, length=lengths[joint], parent=skeleton.parent(joint), kind='MCH',
                     collection=COLLECTION_MECHANISM, color=MECHANISM_COLOR, hide=True)
            items.append({'joint': joint, 'mch': mch, 'helpers': [helper], 'rule': rule, 'value': value,
                          'bend_axis': bend_axis})
    info['joint_helpers'] = items

    def joint_helpers_job(pose):
        for item in items:
            mch_bone = pose[item['mch']]
            rule, value = item['rule'], item['value']
            if rule == 'swing':
                track = typing_cast(bpy.types.DampedTrackConstraint,
                                    _new_constraint(mch_bone, 'DAMPED_TRACK', 'Joint Swing'))
                track.target = armature_object
                track.subtarget = item['joint']
                track.head_tail = 1.0
                track.track_axis = 'TRACK_Y'
            elif rule in ('bend', 'twist'):
                axis = 'XYZ'[item['bend_axis']] if rule == 'bend' else 'Y'
                copy = typing_cast(bpy.types.CopyRotationConstraint,
                                   _new_constraint(mch_bone, 'COPY_ROTATION', f'Joint {rule.title()}'))
                copy.target = armature_object
                copy.subtarget = item['joint']
                copy.owner_space = 'LOCAL'
                copy.target_space = 'LOCAL_OWNER_ORIENT' if rule == 'twist' else 'LOCAL'
                # The twist comes first so that the bend doesn't pick it up.
                copy.euler_order = {'X': 'YXZ', 'Z': 'YZX', 'Y': 'YZX'}[axis]
                copy.use_x, copy.use_y, copy.use_z = (a == axis for a in 'XYZ')
                copy.influence = value
            for helper in item['helpers']:
                copy = typing_cast(bpy.types.CopyRotationConstraint,
                                   _new_constraint(pose[helper], 'COPY_ROTATION', 'Joint Helper'))
                copy.target = armature_object
                copy.subtarget = item['mch']
                copy.owner_space = 'LOCAL'
                copy.target_space = 'LOCAL_OWNER_ORIENT'
                copy.mix_mode = 'REPLACE'
        return None
    constraint_jobs.append(('joint_helpers', joint_helpers_job))


def _assign_joint_helper_collection(armature_data: Armature, info: dict, collections: dict):
    """Moves the joint helper bones into their own (hidden) collection. The armature must be in edit mode."""
    helper_collection = collections[COLLECTION_HELPERS]
    helper_collection.is_visible = False
    for item in info.get('joint_helpers', []):
        for name in item['helpers']:
            edit_bone = armature_data.edit_bones.get(name)
            if edit_bone is None:
                continue
            for collection in list(edit_bone.collections):
                collection.unassign(edit_bone)
            helper_collection.assign(edit_bone)


def _upgrade_joint_helpers(context: Context, armature_object: Object, info: dict):
    """Version 4: the rotation helper bones of the limb joints follow the joints."""
    assert context.view_layer and armature_object.pose
    armature_data = typing_cast(Armature, armature_object.data)
    skeleton = skeleton_from_bones([b for b in armature_data.bones if not is_rig_bone(b)])
    smap = analyze_skeleton(skeleton)
    if smap.root is None:
        return
    rest = {b.name: b.matrix_local.copy() for b in armature_data.bones}
    lengths = {b.name: b.length for b in armature_data.bones}
    specs: list[_BoneSpec] = []
    jobs: list = []

    def add_spec(**kwargs) -> _BoneSpec:
        spec = _BoneSpec(**kwargs)
        specs.append(spec)
        return spec

    _build_joint_helpers(skeleton, smap, rest, lengths, add_spec, jobs, info, armature_object)
    previous_active = context.view_layer.objects.active
    previous_mode = armature_object.mode
    context.view_layer.objects.active = armature_object
    bpy.ops.object.mode_set(mode='EDIT')
    collections = {name: armature_data.collections.get(name) or armature_data.collections.new(name)
                   for name in RIG_COLLECTIONS + (COLLECTION_HELPERS,)}
    _create_spec_edit_bones(armature_data, specs, collections)
    _assign_joint_helper_collection(armature_data, info, collections)
    bpy.ops.object.mode_set(mode='POSE')
    _apply_spec_pose_settings(armature_object, specs)
    pose = armature_object.pose.bones
    for _, job in jobs:
        job(pose)
    bpy.ops.object.mode_set(mode='OBJECT' if previous_mode == 'EDIT' else previous_mode)
    context.view_layer.objects.active = previous_active


def _ik_scale_axes(rest: dict[str, Matrix], upper: str, lower: str, end: str) -> list[int]:
    """
    The local scale axes that the lengths of an IK chain follow: the axis of the upper bone that its own length lies
    on, the axis of the upper bone that the lower bone lies on, and the axis of the lower bone that its own length
    lies on. Any axis is exact for a uniform scale.
    """
    upper_inverse = rest[upper].inverted()
    lower_offset = upper_inverse @ rest[lower].translation
    end_offset = rest[lower].inverted() @ rest[end].translation
    end_offset_in_upper = (upper_inverse @ rest[lower]).to_3x3() @ end_offset
    return [_dominant_axis(lower_offset), _dominant_axis(end_offset_in_upper), _dominant_axis(end_offset)]


def _add_ik_solver_drivers(armature_object: Object, limb: dict, target: str):
    """
    Adds the drivers of an analytic two bone IK chain (replacing any existing ones):

    * The bend angle of `mch_upper` (law of cosines). The lengths of the chain are the `length_a` & `length_b`
      properties (the lengths of the unscaled bones) multiplied by the scale of the limb bones & their parents, so
      that the hand/foot stays on the IK target when the limb bones (or the bones above them) are scaled.
    * The scale of `mch_upper`, which follows the upper deform bone so that `mch_lower` aims at the IK target from
      where the elbow/knee really is.

    Only the local scale properties of the limb bones are read (not their final transforms, which depend on the IK),
    so there are no dependency cycles.
    """
    assert armature_object.pose
    pose = armature_object.pose.bones
    mch_upper = pose[limb['mch_upper']]
    upper, lower = pose[limb['upper']], pose[limb['lower']]
    upper_axis, upper_lower_axis, lower_axis = limb['scale_axes']

    mch_upper.driver_remove('rotation_euler', 0)
    fcurve = mch_upper.driver_add('rotation_euler', 0)
    for modifier in list(fcurve.modifiers):
        fcurve.modifiers.remove(modifier)
    driver = fcurve.driver
    driver.type = 'SCRIPTED'

    def add_property_variable(name: str, data_path: str):
        var = driver.variables.new()
        var.name = name
        var.type = 'SINGLE_PROP'
        var.targets[0].id_type = 'OBJECT'
        var.targets[0].id = armature_object
        var.targets[0].data_path = data_path

    add_property_variable('a', f'["{limb["length_a"]}"]')
    add_property_variable('b', f'["{limb["length_b"]}"]')
    add_property_variable('u', f'{upper.path_from_id("scale")}[{upper_axis}]')
    add_property_variable('l', f'{lower.path_from_id("scale")}[{lower_axis}]')
    lower_parent_scale = 'u'
    if upper_lower_axis != upper_axis:
        add_property_variable('v', f'{upper.path_from_id("scale")}[{upper_lower_axis}]')
        lower_parent_scale = 'v'
    var = driver.variables.new()
    var.name = 'd'
    var.type = 'LOC_DIFF'
    var.targets[0].id = armature_object
    var.targets[0].bone_target = limb['mch_root']
    var.targets[1].id = armature_object
    var.targets[1].bone_target = target
    # The scale of everything above the chain (the parent of the upper bone, or the armature object), in world space
    # like the distance.
    var = driver.variables.new()
    var.name = 's'
    var.type = 'TRANSFORMS'
    var.targets[0].id = armature_object
    var.targets[0].bone_target = limb.get('scale_parent') or ''
    var.targets[0].transform_type = 'SCALE_AVG'
    var.targets[0].transform_space = 'WORLD_SPACE'
    # NOTE: This must stay a "simple expression" (no Python) & under 256 characters.
    distance = '(d/max(abs(s),1e-9))'
    length_a, length_b = '(a*u)', f'(b*{lower_parent_scale}*l)'
    driver.expression = (f'acos(min(max(({length_a}*{length_a}+{distance}*{distance}-{length_b}*{length_b})/'
                         f'max(2*abs{length_a}*{distance},1e-9),-1),1))-{limb["rest_angle"]:.9f}')

    for index in range(3):
        mch_upper.driver_remove('scale', index)
        fcurve = mch_upper.driver_add('scale', index)
        for modifier in list(fcurve.modifiers):
            fcurve.modifiers.remove(modifier)
        driver = fcurve.driver
        driver.type = 'SUM'
        add_property_variable('v', f'{upper.path_from_id("scale")}[{index}]')


def is_rig_outdated(armature_object: Object) -> bool:
    """Whether the rig was made with an older version of the add-on (see `upgrade_rig`)."""
    info = get_rig_info(armature_object)
    return info is not None and info.get('version', 1) < RIG_VERSION


def _upgrade_ik_scale(armature_object: Object, info: dict):
    """Version 2: the IK chains follow the scale of the limb bones & their parents (drivers only)."""
    bones = typing_cast(Armature, armature_object.data).bones
    rest = {b.name: b.matrix_local for b in bones}
    for key, end_key, target_key in (('arms', 'hand', 'ik'), ('legs', 'foot', 'target')):
        for limb in info.get(key, {}).values():
            names = [limb.get(k) for k in ('upper', 'lower', end_key, 'mch_root', 'mch_upper', target_key)]
            if any(name is None or name not in bones for name in names):
                continue
            parent = bones[limb['upper']].parent
            limb['scale_parent'] = parent.name if parent is not None else None
            limb['scale_axes'] = _ik_scale_axes(rest, limb['upper'], limb['lower'], limb[end_key])
            _add_ik_solver_drivers(armature_object, limb, limb[target_key])


_OLD_FINGER_CONTROL = re.compile(rf'^{re.escape(CTRL)}(fingers|finger_([a-z]+))(\..+)?$')


def _convert_finger_control_keys(conversions: dict[tuple[str, str, int], tuple[str, float, float]], old_controls: set[str]):
    """
    Moves the keys of the old finger controls (scale to curl, rotate to spread) onto the finger sliders, in all the
    actions. `conversions` maps (old bone, property, index) to (slider bone, factor, offset) for its Y location; all
    the other keys of the old controls are removed.
    """
    pattern = re.compile(r'^pose\.bones\["(.+)"\]\.(\w+)$')
    for action in bpy.data.actions:
        for layer in action.layers:
            for strip in layer.strips:
                for channelbag in strip.channelbags:
                    to_convert = []
                    for fcurve in list(channelbag.fcurves):
                        match = pattern.match(fcurve.data_path)
                        if match is None or match.group(1) not in old_controls:
                            continue
                        conversion = conversions.get((match.group(1), match.group(2), fcurve.array_index))
                        if conversion is None:
                            channelbag.fcurves.remove(fcurve)
                        else:
                            to_convert.append((fcurve, conversion))
                    for fcurve, (name, factor, offset) in to_convert:
                        fcurve.data_path = f'pose.bones["{name}"].location'
                        fcurve.array_index = 1
                        for keyframe in fcurve.keyframe_points:
                            keyframe.co.y = keyframe.co.y * factor + offset
                            keyframe.handle_left.y = keyframe.handle_left.y * factor + offset
                            keyframe.handle_right.y = keyframe.handle_right.y * factor + offset
                        fcurve.update()
                        try:
                            fcurve.group = channelbag.groups.get(name) or channelbag.groups.new(name)
                        except (AttributeError, RuntimeError, TypeError):
                            pass


def _upgrade_hand_and_foot_controls(context: Context, armature_object: Object, info: dict):
    """
    Version 3: the finger controls become sliders (with a thumb pad) and the feet get a foot roll control. The keys
    & the pose of the old finger controls are carried over to the sliders.
    """
    assert context.view_layer and armature_object.pose
    armature_data = typing_cast(Armature, armature_object.data)
    skeleton = skeleton_from_bones(armature_data.bones)
    smap = analyze_skeleton(skeleton)
    if smap.root is None:
        return
    rest = {b.name: b.matrix_local.copy() for b in armature_data.bones}
    lengths = {b.name: b.length for b in armature_data.bones}
    pose = armature_object.pose.bones
    specs: list[_BoneSpec] = []
    jobs: list = []

    def add_spec(**kwargs) -> _BoneSpec:
        spec = _BoneSpec(**kwargs)
        specs.append(spec)
        return spec

    # Fingers (only if the rig has finger controls).
    old_controls = [n for n in info.get('additive_controls', []) if _OLD_FINGER_CONTROL.match(n)]
    old_pose = {n: (pose[n].scale[1], pose[n].rotation_euler[2]) for n in old_controls if n in pose}
    if old_controls:
        for pose_bone in pose:
            for constraint in list(pose_bone.constraints):
                if constraint.name.startswith(f'{CONSTRAINT_PREFIX} Finger'):
                    pose_bone.constraints.remove(constraint)
        info['additive_controls'] = [n for n in info['additive_controls'] if n not in old_controls]
        _build_fingers(skeleton, smap, rest, lengths, add_spec, jobs, info, armature_object)

    # Foot roll controls.
    floor_height = _floor_height(skeleton, smap)
    for side, limb in info.get('legs', {}).items():
        leg = smap.legs.get(side)
        if leg is None or leg.foot != limb.get('foot') or limb.get('roll_ctrl') or \
                any(limb.get(k) not in pose for k in ('ik', 'heel', 'ball')):
            continue
        geometry = _foot_geometry(skeleton, smap, leg, floor_height)
        limb['roll_ctrl'] = _add_foot_roll_control(add_spec, side, geometry, smap.up, limb['ik'])
        jobs.append(('ik', lambda pose, limb=limb: _add_foot_roll_drivers(armature_object, limb)))

    previous_active = context.view_layer.objects.active
    previous_mode = armature_object.mode
    context.view_layer.objects.active = armature_object
    bpy.ops.object.mode_set(mode='EDIT')
    edit_bones = armature_data.edit_bones
    for name in old_controls:
        edit_bone = edit_bones.get(name)
        if edit_bone is not None:
            edit_bones.remove(edit_bone)
    collections = {name: armature_data.collections.get(name) or armature_data.collections.new(name)
                   for name in RIG_COLLECTIONS}
    _create_spec_edit_bones(armature_data, specs, collections)
    bpy.ops.object.mode_set(mode='POSE')
    _apply_spec_pose_settings(armature_object, specs)
    pose = armature_object.pose.bones
    for _, job in jobs:
        job(pose)
    # The foot controls show which way the foot points.
    for limb in info.get('legs', {}).values():
        foot_ik = pose.get(limb.get('ik') or '')
        if foot_ik is not None and foot_ik.custom_shape is not None and foot_ik.custom_shape.name == 'WGT-psk_foot':
            foot_ik.custom_shape = get_widget('foot_arrow')

    # Carry the keys & the pose of the old finger controls over to the sliders.
    conversions: dict[tuple[str, str, int], tuple[str, float, float]] = {}
    for name in old_controls:
        match = _OLD_FINGER_CONTROL.match(name)
        assert match is not None
        suffix = match.group(3) or ''
        hand = next((h for side, h in info.get('hands', {}).items() if side_suffix(side) == suffix), None)
        if hand is None:
            continue
        travel = hand['travel']
        if match.group(1) == 'fingers':
            # Scale 1 was the rest pose & scale 0 a fist. The rotation was the spread of the outermost finger.
            conversions[(name, 'scale', 1)] = (hand['curl'], -travel, travel)
            conversions[(name, 'rotation_euler', 2)] = (hand['spread'], travel / FINGER_SPREAD_MAX_ANGLE, 0.0)
        elif hand['fingers'].get(match.group(2)):
            conversions[(name, 'scale', 1)] = (hand['fingers'][match.group(2)], -travel, travel)
    _convert_finger_control_keys(conversions, set(old_controls))
    for (old_name, prop, _), (new_name, factor, offset) in conversions.items():
        if old_name in old_pose and new_name in pose:
            value = old_pose[old_name][0 if prop == 'scale' else 1]
            pose[new_name].location[1] = value * factor + offset

    bpy.ops.object.mode_set(mode='OBJECT' if previous_mode == 'EDIT' else previous_mode)
    context.view_layer.objects.active = previous_active


def upgrade_rig(armature_object: Object, context: Context | None = None) -> bool:
    """
    Updates a rig that was made with an older version of the add-on in place (the controls & their animation are
    kept). Returns True if the rig was changed.

    Without a context only the updates that don't add bones are made (so that it's safe to call when a file is
    loaded); the rig then stays outdated until it's updated with a context (the Update Rig operator).
    """
    info = get_rig_info(armature_object)
    if info is None or not is_rig_outdated(armature_object) or armature_object.pose is None:
        return False
    changed = False
    if info.get('version', 1) < 2:
        _upgrade_ik_scale(armature_object, info)
        info['version'] = 2
        changed = True
    if info.get('version', 1) < 3 and context is not None:
        _upgrade_hand_and_foot_controls(context, armature_object, info)
        info['version'] = 3
        changed = True
    if info.get('version', 1) < 4 and context is not None:
        _upgrade_joint_helpers(context, armature_object, info)
        info['version'] = 4
        changed = True
    if changed:
        armature_object.data[RIG_INFO_PROPERTY] = json.dumps(info)
        armature_object.update_tag()
    return changed


def remove_rig(context: Context, armature_object: Object):
    """
    Removes the control rig (control & mechanism bones, constraints, drivers, custom shapes and properties) from the
    armature, leaving only the deform bones.
    """
    armature_data = typing_cast(Armature, armature_object.data)
    info = get_rig_info(armature_object) or {}

    # Constraints & custom shapes on the deform bones.
    assert armature_object.pose
    for pose_bone in armature_object.pose.bones:
        for constraint in list(pose_bone.constraints):
            if constraint.name.startswith(CONSTRAINT_PREFIX):
                pose_bone.constraints.remove(constraint)
        if pose_bone.custom_shape is not None and pose_bone.custom_shape.name.startswith('WGT-psk_'):
            pose_bone.custom_shape = None

    rig_bone_names = {b.name for b in armature_data.bones if is_rig_bone(b)}

    # Drivers.
    if armature_object.animation_data is not None:
        for fcurve in list(armature_object.animation_data.drivers):
            path = fcurve.data_path
            if CONSTRAINT_PREFIX in path or any(f'"{n}"' in path for n in rig_bone_names):
                armature_object.animation_data.drivers.remove(fcurve)

    # F-Curves of the rig bones & properties in all the actions that animate this armature.
    rig_properties = set(info.get('properties', []))
    for action in bpy.data.actions:
        for layer in action.layers:
            for strip in layer.strips:
                for channelbag in strip.channelbags:
                    for fcurve in list(channelbag.fcurves):
                        path = fcurve.data_path
                        if any(f'pose.bones["{n}"]' in path for n in rig_bone_names) or \
                                any(path == f'["{p}"]' for p in rig_properties):
                            channelbag.fcurves.remove(fcurve)

    # Bones.
    if rig_bone_names:
        previous_active = context.view_layer.objects.active
        previous_mode = armature_object.mode
        context.view_layer.objects.active = armature_object
        bpy.ops.object.mode_set(mode='EDIT')
        for name in rig_bone_names:
            edit_bone = armature_data.edit_bones.get(name)
            if edit_bone is not None:
                armature_data.edit_bones.remove(edit_bone)
        bpy.ops.object.mode_set(mode='OBJECT' if previous_mode == 'EDIT' else previous_mode)
        context.view_layer.objects.active = previous_active

    for name in RIG_COLLECTIONS:
        collection = armature_data.collections.get(name)
        if collection is not None:
            armature_data.collections.remove(collection)
    # The joint helpers stop following the joints, so show them again.
    helper_collection = armature_data.collections.get(COLLECTION_HELPERS)
    if helper_collection is not None:
        helper_collection.is_visible = True

    for prop in rig_properties:
        if prop in armature_object:
            del armature_object[prop]

    if RIG_INFO_PROPERTY in armature_data:
        del armature_data[RIG_INFO_PROPERTY]


def build_rig(context: Context, armature_object: Object, options: RigOptions) -> RigBuildResult:
    result = RigBuildResult()
    assert context.view_layer
    armature_data = typing_cast(Armature, armature_object.data)

    if has_rig(armature_object):
        remove_rig(context, armature_object)

    skeleton = skeleton_from_bones(armature_data.bones)
    smap = analyze_skeleton(skeleton)
    up, left, forward = smap.up, smap.left, smap.forward
    rest = {b.name: b.matrix_local.copy() for b in armature_data.bones}
    lengths = {b.name: b.length for b in armature_data.bones}

    if smap.root is None:
        result.warnings.append('The armature has no bones')
        return result

    root = smap.root
    specs: list[_BoneSpec] = []
    info: dict = {'version': RIG_VERSION, 'root': root, 'arms': {}, 'legs': {}, 'eyes': None, 'additive_controls': [],
                  'properties': []}
    constraint_jobs = []  # Callables executed in pose mode after the bones have been created.

    face_unit = smap.height * 0.01
    if len(smap.eyes) == 2:
        face_unit = skeleton.distance(smap.eyes['L'], smap.eyes['R']) * 0.25

    floor_height = _floor_height(skeleton, smap)

    def add_spec(**kwargs) -> _BoneSpec:
        spec = _BoneSpec(**kwargs)
        specs.append(spec)
        return spec

    def add_ik_chain(kind: str, side: str, upper: str, lower: str, end: str, pole_position: Vector,
                     twist_name: str, lower_twist_name: str) -> dict:
        """
        Adds the mechanism bones for an analytic two bone IK chain:

        * `MCH-<upper>_ik_root` sits at the head of the upper bone, aims at the IK target (Damped Track) and rolls
          toward the pole (Locked Track).
        * `MCH-<upper>_ik` is rotated away from the aim direction by the angle given by the law of cosines (a driver).
        * `MCH-<lower>_ik` aims at the IK target (Damped Track).

        Unlike Blender's IK solver, this is exact (even for straight limbs), deterministic, and does not depend on the
        FK animation. The deform bones copy the rotations of the twist controls (which are children of the mechanism
        bones), so the positions of the joints come from the deform bone hierarchy.
        """
        mch_root, mch_upper, mch_lower = f'{MCH}{upper}_ik_root', f'{MCH}{upper}_ik', f'{MCH}{lower}_ik'
        shoulder, elbow, wrist = skeleton.head(upper), skeleton.head(lower), skeleton.head(end)
        root_matrix = _matrix(shoulder, wrist - shoulder, z_hint=pole_position - shoulder)
        add_spec(name=mch_root, matrix=root_matrix, length=(wrist - shoulder).length * 0.25, parent=skeleton.parent(upper),
                 kind='MCH', collection=COLLECTION_MECHANISM, color=MECHANISM_COLOR, hide=True)
        add_spec(name=mch_upper, matrix=rest[upper], length=lengths[upper], parent=mch_root, kind='MCH',
                 collection=COLLECTION_MECHANISM, color=MECHANISM_COLOR, rotation_mode='XYZ', hide=True)
        add_spec(name=mch_lower, matrix=rest[lower], length=lengths[lower], parent=mch_upper, kind='MCH',
                 collection=COLLECTION_MECHANISM, color=MECHANISM_COLOR, hide=True)
        # The twist control rotates the upper bone of the chain around its own axis (which IK cannot do). This is
        # also used to exactly reproduce FK animations that twist the upper arm/leg when baking FK to IK.
        add_spec(name=twist_name, matrix=rest[upper], length=lengths[upper], parent=mch_upper, kind='CTRL',
                 collection=COLLECTION_IK, color=SIDE_COLORS[side], widget='circle',
                 widget_scale=lengths[upper] * 0.12, widget_translation=Vector((0.0, lengths[upper] * 0.35, 0.0)),
                 rotation_mode='YXZ', lock_location=(True, True, True), lock_rotation=(True, False, True))
        add_spec(name=lower_twist_name, matrix=rest[lower], length=lengths[lower], parent=mch_lower, kind='CTRL',
                 collection=COLLECTION_IK, color=SIDE_COLORS[side], widget='circle',
                 widget_scale=lengths[lower] * 0.1, widget_translation=Vector((0.0, lengths[lower] * 0.6, 0.0)),
                 rotation_mode='YXZ', lock_location=(True, True, True), lock_rotation=(True, False, True))
        # The rest angle between the aim direction and the upper bone (around the X axis of the root, which is the
        # normal of the plane of the limb).
        root_y, root_z = root_matrix.col[1].xyz.normalized(), root_matrix.col[2].xyz.normalized()
        upper_y = (elbow - shoulder).normalized()
        rest_angle = math.atan2(upper_y.dot(root_z), upper_y.dot(root_y))
        length_a, length_b = f'ik_{kind}_{side}_length_a', f'ik_{kind}_{side}_length_b'
        info['properties'].extend([length_a, length_b])
        return {'mch_root': mch_root, 'mch_upper': mch_upper, 'mch_lower': mch_lower, 'twist': twist_name,
                'lower_twist': lower_twist_name, 'rest_angle': rest_angle, 'length_a': length_a,
                'length_b': length_b, 'rest_length_a': (elbow - shoulder).length, 'rest_length_b': (wrist - elbow).length,
                'scale_parent': skeleton.parent(upper), 'scale_axes': _ik_scale_axes(rest, upper, lower, end)}

    def setup_ik_chain(pose, upper: str, lower: str, chain: dict, target: str, pole: str, prop: str):
        _ensure_float_property(armature_object, chain['length_a'], chain['rest_length_a'], 0.0, 1e9,
                               'The length of the upper part of the IK chain (keyed when baking FK to IK)')
        _ensure_float_property(armature_object, chain['length_b'], chain['rest_length_b'], 0.0, 1e9,
                               'The length of the lower part of the IK chain (keyed when baking FK to IK)')
        root = pose[chain['mch_root']]
        c = typing_cast(bpy.types.DampedTrackConstraint, _new_constraint(root, 'DAMPED_TRACK', 'IK Aim'))
        c.target = armature_object
        c.subtarget = target
        c = typing_cast(bpy.types.LockedTrackConstraint, _new_constraint(root, 'LOCKED_TRACK', 'IK Pole'))
        c.target = armature_object
        c.subtarget = pole
        c.lock_axis = 'LOCK_Y'
        c.track_axis = 'TRACK_Z'
        # The bend angle of the upper bone (law of cosines) & the scale of the chain.
        _add_ik_solver_drivers(armature_object, {'upper': upper, 'lower': lower, **chain}, target)
        c = typing_cast(bpy.types.DampedTrackConstraint, _new_constraint(pose[chain['mch_lower']], 'DAMPED_TRACK', 'IK Aim'))
        c.target = armature_object
        c.subtarget = target
        # NOTE: Only the rotation is copied, so the positions of the joints come from the deform bone hierarchy.
        for deform, source in ((upper, chain['twist']), (lower, chain['lower_twist'])):
            copy = _new_constraint(pose[deform], 'COPY_ROTATION', 'IK')
            copy.target = armature_object
            copy.subtarget = source
            _add_driver(copy, 'influence', armature_object, prop)

    # ------------------------------------------------------------------------------------------------------------
    # Arms
    # ------------------------------------------------------------------------------------------------------------
    if options.use_arm_ik:
        for side, arm in smap.arms.items():
            s = side_suffix(side)
            prop = f'ik_arm_{side}'
            shoulder, elbow, wrist = skeleton.head(arm.upper), skeleton.head(arm.lower), skeleton.head(arm.hand)
            upper_length = (elbow - shoulder).length
            bend = _limb_bend_vector(shoulder, elbow, wrist, -forward)
            ik_name = f'{CTRL}hand_ik{s}'
            pole_name = f'{CTRL}elbow_pole{s}'
            add_spec(name=ik_name, matrix=rest[arm.hand], length=lengths[arm.hand], parent=root, kind='CTRL',
                     collection=COLLECTION_IK, color=SIDE_COLORS[side], widget='cube',
                     widget_scale=Vector((lengths[arm.hand] * 0.5, lengths[arm.hand] * 0.5, lengths[arm.hand] * 0.5)),
                     widget_translation=Vector((0.0, lengths[arm.hand] * 0.5, 0.0)), lock_scale=(True, True, True))
            pole_position = elbow + bend * upper_length
            add_spec(name=pole_name, matrix=_matrix(pole_position, bend, z_hint=up), length=upper_length * 0.1,
                     parent=root, kind='CTRL', collection=COLLECTION_IK, color=SIDE_COLORS[side], widget='sphere',
                     widget_scale=upper_length * 0.06, lock_rotation=(True, True, True), lock_rotation_w=True)
            chain = add_ik_chain('arm', side, arm.upper, arm.lower, arm.hand, pole_position,
                                 f'{CTRL}arm_twist{s}', f'{CTRL}forearm_twist{s}')
            info['arms'][side] = {'upper': arm.upper, 'lower': arm.lower, 'hand': arm.hand, 'ik': ik_name,
                                  'pole': pole_name, 'prop': prop, 'pole_distance': upper_length, **chain}
            info['properties'].append(prop)

            def arm_job(pose, arm=arm, ik_name=ik_name, pole_name=pole_name, prop=prop, side=side, chain=chain):
                _ensure_float_property(armature_object, prop, 1.0, 0.0, 1.0, f'IK/FK switch for the {side} arm (1 = IK, 0 = FK)')
                setup_ik_chain(pose, arm.upper, arm.lower, chain, ik_name, pole_name, prop)
                copy = typing_cast(bpy.types.CopyRotationConstraint, _new_constraint(pose[arm.hand], 'COPY_ROTATION', 'IK Rotation'))
                copy.target = armature_object
                copy.subtarget = ik_name
                _add_driver(copy, 'influence', armature_object, prop)
                return None
            constraint_jobs.append(('ik', arm_job))

    # ------------------------------------------------------------------------------------------------------------
    # Legs
    # ------------------------------------------------------------------------------------------------------------
    if options.use_leg_ik:
        for side, leg in smap.legs.items():
            s = side_suffix(side)
            prop = f'ik_leg_{side}'
            roll_prop = f'foot_roll_{side}'
            hip, knee, ankle = skeleton.head(leg.upper), skeleton.head(leg.lower), skeleton.head(leg.foot)
            upper_length = (knee - hip).length
            bend = _limb_bend_vector(hip, knee, ankle, forward)
            geometry = _foot_geometry(skeleton, smap, leg, floor_height)
            toe_position, foot_forward, foot_length = geometry['toe'], geometry['forward'], geometry['length']
            ankle_height, heel_position = geometry['ankle_height'], geometry['heel']
            ball_position = toe_position.copy()

            ik_name = f'{CTRL}foot_ik{s}'
            pole_name = f'{CTRL}knee_pole{s}'
            heel_name = f'{MCH}heel_pivot{s}'
            ball_name = f'{MCH}ball_pivot{s}'
            target_name = f'{MCH}foot_ik_target{s}'
            toe_ctrl_name = f'{CTRL}toe{s}' if leg.toe else None

            # The foot control is flat on the ground, pointing forward, with its head at the ankle.
            foot_shape_length = foot_length * 1.5
            add_spec(name=ik_name, matrix=_matrix(ankle, foot_forward, z_hint=up), length=foot_length * 0.5,
                     parent=root, kind='CTRL', collection=COLLECTION_IK, color=SIDE_COLORS[side], widget='foot_arrow',
                     widget_scale=Vector((foot_length * 0.4, foot_shape_length * 0.5, 1.0)),
                     widget_translation=Vector((0.0, foot_shape_length * 0.5 - foot_length * 0.4, -ankle_height)))
            add_spec(name=heel_name, matrix=_matrix(heel_position, foot_forward, z_hint=up), length=foot_length * 0.2,
                     parent=ik_name, kind='MCH', collection=COLLECTION_MECHANISM, color=MECHANISM_COLOR,
                     rotation_mode='XYZ', hide=True)
            add_spec(name=ball_name, matrix=_matrix(ball_position, foot_forward, z_hint=up), length=foot_length * 0.2,
                     parent=heel_name, kind='MCH', collection=COLLECTION_MECHANISM, color=MECHANISM_COLOR,
                     rotation_mode='XYZ', hide=True)
            add_spec(name=target_name, matrix=rest[leg.foot], length=lengths[leg.foot], parent=ball_name, kind='MCH',
                     collection=COLLECTION_MECHANISM, color=MECHANISM_COLOR, hide=True)
            if leg.toe and toe_ctrl_name:
                toe_length = lengths[leg.toe]
                add_spec(name=toe_ctrl_name, matrix=rest[leg.toe], length=toe_length, parent=heel_name, kind='CTRL',
                         collection=COLLECTION_IK, color=SIDE_COLORS[side], widget='circle',
                         widget_scale=foot_length * 0.3, widget_translation=Vector((0.0, toe_length * 0.5, 0.0)))
            roll_ctrl_name = _add_foot_roll_control(add_spec, side, geometry, up, ik_name)
            pole_position = knee + bend * upper_length
            add_spec(name=pole_name, matrix=_matrix(pole_position, bend, z_hint=up), length=upper_length * 0.1,
                     parent=root, kind='CTRL', collection=COLLECTION_IK, color=SIDE_COLORS[side], widget='sphere',
                     widget_scale=upper_length * 0.06, lock_rotation=(True, True, True), lock_rotation_w=True)
            chain = add_ik_chain('leg', side, leg.upper, leg.lower, leg.foot, pole_position,
                                 f'{CTRL}leg_twist{s}', f'{CTRL}shin_twist{s}')
            info['legs'][side] = {'upper': leg.upper, 'lower': leg.lower, 'foot': leg.foot, 'toe': leg.toe,
                                  'ik': ik_name, 'pole': pole_name, 'heel': heel_name, 'ball': ball_name,
                                  'target': target_name, 'toe_ctrl': toe_ctrl_name, 'prop': prop,
                                  'roll_prop': roll_prop, 'roll_ctrl': roll_ctrl_name,
                                  'pole_distance': upper_length, **chain}
            info['properties'].extend([prop, roll_prop])

            def leg_job(pose, leg=leg, ik_name=ik_name, pole_name=pole_name, heel_name=heel_name, ball_name=ball_name,
                        target_name=target_name, toe_ctrl_name=toe_ctrl_name, prop=prop, roll_prop=roll_prop, side=side,
                        chain=chain, roll_ctrl_name=roll_ctrl_name):
                _ensure_float_property(armature_object, prop, 1.0, 0.0, 1.0, f'IK/FK switch for the {side} leg (1 = IK, 0 = FK)')
                _ensure_float_property(armature_object, roll_prop, 0.0, -1.0, 1.0,
                                       f'Foot roll for the {side} foot. Positive values lift the heel (rolling on the ball '
                                       f'of the foot while the toes stay on the floor), negative values lift the toes',
                                       soft_min=-1.0, soft_max=1.0)
                setup_ik_chain(pose, leg.upper, leg.lower, chain, target_name, pole_name, prop)
                copy = typing_cast(bpy.types.CopyRotationConstraint, _new_constraint(pose[leg.foot], 'COPY_ROTATION', 'IK Rotation'))
                copy.target = armature_object
                copy.subtarget = target_name
                _add_driver(copy, 'influence', armature_object, prop)
                if leg.toe and toe_ctrl_name:
                    copy = typing_cast(bpy.types.CopyRotationConstraint, _new_constraint(pose[leg.toe], 'COPY_ROTATION', 'IK Rotation'))
                    copy.target = armature_object
                    copy.subtarget = toe_ctrl_name
                    _add_driver(copy, 'influence', armature_object, prop)
                _add_foot_roll_drivers(armature_object, {'ball': ball_name, 'heel': heel_name, 'roll_prop': roll_prop,
                                                         'roll_ctrl': roll_ctrl_name})
                return None
            constraint_jobs.append(('ik', leg_job))

    # ------------------------------------------------------------------------------------------------------------
    # Fingers
    # ------------------------------------------------------------------------------------------------------------
    if options.use_fingers:
        _build_fingers(skeleton, smap, rest, lengths, add_spec, constraint_jobs, info, armature_object)

    # ------------------------------------------------------------------------------------------------------------
    # Eyes
    # ------------------------------------------------------------------------------------------------------------
    if options.use_eyes and smap.eyes and smap.head is not None:
        eye_positions = {side: skeleton.head(name) for side, name in smap.eyes.items()}
        center = sum(eye_positions.values(), Vector()) / len(eye_positions)
        eye_separation = (eye_positions['L'] - eye_positions['R']).length if len(eye_positions) == 2 else face_unit * 4
        eye_style = options.eye_style
        if eye_style == 'AUTO':
            eye_style = detect_eye_style(armature_object, list(smap.eyes.values()))
        info['eye_style'] = eye_style
        parent_name = f'{CTRL}eyes'
        if eye_style == 'SLIDE':
            _build_sliding_eyes(smap, rest, eye_positions, center, eye_separation, add_spec, constraint_jobs, info,
                                armature_object, parent_name)
        else:
            prop = 'eyes_aim'
            distance = max(eye_separation * 4.0, smap.height * 0.1)
            add_spec(name=parent_name, matrix=_matrix(center + forward * distance, forward, z_hint=up),
                     length=eye_separation * 0.5, parent=smap.head, kind='CTRL', collection=COLLECTION_FACE_CONTROLS,
                     color=SIDE_COLORS['C'], widget='eyes', widget_scale=Vector((eye_separation * 0.55, 1.0, eye_separation * 0.55)),
                     lock_scale=(True, True, True))
            items = []
            for side, eye in smap.eyes.items():
                ctrl_name = f'{CTRL}eye{side_suffix(side)}'
                add_spec(name=ctrl_name, matrix=_matrix(eye_positions[side] + forward * distance, forward, z_hint=up),
                         length=eye_separation * 0.25, parent=parent_name, kind='CTRL', collection=COLLECTION_FACE_CONTROLS,
                         color=SIDE_COLORS[side], widget='circle', widget_scale=eye_separation * 0.2,
                         lock_rotation=(True, True, True), lock_rotation_w=True)
                items.append({'bone': eye, 'ctrl': ctrl_name})
            info['eyes'] = {'parent': parent_name, 'items': items, 'prop': prop, 'distance': distance}
            info['properties'].append(prop)

            def eyes_job(pose, items=items, prop=prop):
                _ensure_float_property(armature_object, prop, 1.0, 0.0, 1.0, 'Aim the eyes at the eye target controls (1 = aim, 0 = FK)')
                for item in items:
                    c = typing_cast(bpy.types.DampedTrackConstraint, _new_constraint(pose[item['bone']], 'DAMPED_TRACK', 'Eye Aim'))
                    c.target = armature_object
                    c.subtarget = item['ctrl']
                    c.track_axis = 'TRACK_Y'
                    _add_driver(c, 'influence', armature_object, prop)
                return None
            constraint_jobs.append(('eyes', eyes_job))

    # ------------------------------------------------------------------------------------------------------------
    # Face
    # ------------------------------------------------------------------------------------------------------------
    if options.use_face and smap.head is not None:
        _build_face(skeleton, smap, rest, lengths, face_unit, add_spec, constraint_jobs, info, armature_object)

    # ------------------------------------------------------------------------------------------------------------
    # Joint helpers
    # ------------------------------------------------------------------------------------------------------------
    _build_joint_helpers(skeleton, smap, rest, lengths, add_spec, constraint_jobs, info, armature_object)

    # ------------------------------------------------------------------------------------------------------------
    # Create the bones.
    # ------------------------------------------------------------------------------------------------------------
    previous_active = context.view_layer.objects.active
    previous_mode = armature_object.mode
    context.view_layer.objects.active = armature_object
    bpy.ops.object.mode_set(mode='EDIT')

    collections = {}
    for name in DEFORM_COLLECTIONS + RIG_COLLECTIONS:
        collection = armature_data.collections.get(name)
        if collection is None:
            collection = armature_data.collections.new(name)
        collections[name] = collection
    collections[COLLECTION_MECHANISM].is_visible = False

    _create_spec_edit_bones(armature_data, specs, collections)
    edit_bones = armature_data.edit_bones

    # Organize the deform bones into collections.
    finger_bones = {b for arm in smap.arms.values() for bones in arm.fingers.values() for b in bones}
    face_bones = set(smap.face.all_bones)
    if smap.jaw:
        face_bones.add(smap.jaw)
    face_bones.update(smap.eyes.values())
    if smap.head is not None:
        for name in skeleton.descendants(smap.head):
            if name in face_bones:
                face_bones.update(skeleton.descendants(name))
    for edit_bone in edit_bones:
        if is_rig_bone(edit_bone):
            continue
        if edit_bone.name in finger_bones:
            collections[COLLECTION_FINGERS].assign(edit_bone)
        elif edit_bone.name in face_bones:
            collections[COLLECTION_FACE].assign(edit_bone)
        else:
            collections[COLLECTION_BODY].assign(edit_bone)
    _assign_joint_helper_collection(armature_data, info, collections)

    bpy.ops.object.mode_set(mode='POSE')

    # ------------------------------------------------------------------------------------------------------------
    # Pose bone settings.
    # ------------------------------------------------------------------------------------------------------------
    _apply_spec_pose_settings(armature_object, specs)
    pose = armature_object.pose.bones

    # Deform bone display: fingers & face bones get small shapes so that they are easy to pick.
    for name in finger_bones:
        pose_bone = pose.get(name)
        if pose_bone is not None:
            side = 'L' if skeleton.head(name).dot(left) > 0 else 'R'
            pose_bone.bone.color.palette = SIDE_COLORS[side]
    for name in face_bones:
        pose_bone = pose.get(name)
        if pose_bone is None or name in smap.eyes.values() or name == smap.jaw:
            continue
        pose_bone.custom_shape = get_widget('sphere')
        pose_bone.use_custom_shape_bone_size = False
        pose_bone.custom_shape_scale_xyz = (face_unit * 0.12,) * 3
        pose_bone.bone.show_wire = True
        pose_bone.bone.color.palette = FACE_DETAIL_COLOR
        pose_bone.color.palette = FACE_DETAIL_COLOR

    for kind, job in constraint_jobs:
        job(pose)

    armature_data.display_type = 'OCTAHEDRAL'
    armature_data[RIG_INFO_PROPERTY] = json.dumps(info)
    result.info = info

    bpy.ops.object.mode_set(mode='OBJECT' if previous_mode == 'EDIT' else previous_mode)
    context.view_layer.objects.active = previous_active
    return result


def detect_eye_style(armature_object: Object, eye_bones: list[str]) -> str:
    """
    Determines how the eyes of a character work from the shape of the mesh that is weighted to the eye bones:
    flat irises (discs) slide across the face ('SLIDE'), while eyeballs (spheres) rotate ('AIM').
    """
    import numpy as np
    ratios = []
    for obj in bpy.data.objects:
        if obj.type != 'MESH' or not any(m.type == 'ARMATURE' and m.object == armature_object for m in obj.modifiers):
            continue
        group_indices = {obj.vertex_groups[name].index for name in eye_bones if name in obj.vertex_groups}
        if not group_indices:
            continue
        for index in group_indices:
            points = [tuple(v.co) for v in obj.data.vertices
                      if any(g.group == index and g.weight > 0.5 for g in v.groups)]
            if len(points) < 8:
                continue
            extents = np.sqrt(np.maximum(np.linalg.eigvalsh(np.cov(np.array(points).T)), 0.0))
            if extents[2] > 0.0:
                ratios.append(extents[0] / extents[2])
    if not ratios:
        return 'AIM'
    # A sphere has a ratio of ~1, a flat disc has a ratio of ~0.
    return 'SLIDE' if min(ratios) < 0.35 else 'AIM'


def _build_sliding_eyes(smap: SkeletonMap, rest: dict, eye_positions: dict, center: Vector, eye_separation: float,
                        add_spec, constraint_jobs: list, info: dict, armature_object: Object, parent_name: str):
    """
    Eye controls for flat irises that slide across the face (instead of rotating). Moving the controls moves the
    irises and scaling them scales the irises (e.g., small pupils when shocked). Moving/scaling `CTRL-eyes` moves or
    scales both irises. These controls are additive (layered on top of the FK animation of the eye bones).
    """
    up, forward = smap.up, smap.forward
    front = max((position - center).dot(forward) for position in eye_positions.values()) + eye_separation * 0.35
    add_spec(name=parent_name, matrix=_matrix(center + forward * front, forward, z_hint=up),
             length=eye_separation * 0.3, parent=smap.head, kind='CTRL', collection=COLLECTION_FACE_CONTROLS,
             color=SIDE_COLORS['C'], widget='eyes', widget_scale=Vector((eye_separation * 0.45, 1.0, eye_separation * 0.3)),
             rotation_mode='XYZ', lock_location=(False, True, False), lock_rotation=(True, True, True),
             lock_scale=(False, False, False))
    info['additive_controls'].append(parent_name)
    control_x = _matrix(Vector(), forward, z_hint=up).col[0].xyz.normalized()
    items = []
    for side, eye in smap.eyes.items():
        ctrl_name = f'{CTRL}eye{side_suffix(side)}'
        position = eye_positions[side] + forward * (front - (eye_positions[side] - center).dot(forward))
        add_spec(name=ctrl_name, matrix=_matrix(position, forward, z_hint=up), length=eye_separation * 0.15,
                 parent=parent_name, kind='CTRL', collection=COLLECTION_FACE_CONTROLS, color=SIDE_COLORS[side],
                 widget='circle', widget_scale=eye_separation * 0.12, rotation_mode='XYZ',
                 lock_location=(False, True, False), lock_rotation=(True, True, True), lock_scale=(False, False, False))
        info['additive_controls'].append(ctrl_name)
        items.append((eye, ctrl_name))

    def job(pose):
        for eye, ctrl_name in items:
            for control in (parent_name, ctrl_name):
                # Translation (in the plane of the face).
                for axis, direction in (('X', control_x), ('Z', up)):
                    _add_transform_constraint(armature_object, pose[eye], control, 'Eye Slide', map_from='LOCATION',
                                              from_axis=axis, from_range=(0.0, 1.0), map_to='LOCATION',
                                              to_values_min=Vector((0.0, 0.0, 0.0)),
                                              to_values_max=_to_local(rest[eye], direction))
                # Scale (the size of the iris).
                c = typing_cast(bpy.types.TransformConstraint, _new_constraint(pose[eye], 'TRANSFORM', 'Eye Scale'))
                c.target = armature_object
                c.subtarget = control
                c.owner_space = 'LOCAL'
                c.target_space = 'LOCAL'
                c.use_motion_extrapolate = True
                c.map_from = 'SCALE'
                c.map_to = 'SCALE'
                for axis in 'xyz':
                    setattr(c, f'from_min_{axis}_scale', 0.0)
                    setattr(c, f'from_max_{axis}_scale', 1.0)
                    setattr(c, f'to_min_{axis}_scale', 0.0)
                    setattr(c, f'to_max_{axis}_scale', 1.0)
                c.map_to_x_from, c.map_to_y_from, c.map_to_z_from = 'X', 'Y', 'Z'
                c.mix_mode_scale = 'MULTIPLY'
        return None
    constraint_jobs.append(('additive', job))


def _build_face(skeleton: Skeleton, smap: SkeletonMap, rest: dict, lengths: dict, face_unit: float, add_spec,
                constraint_jobs: list, info: dict, armature_object: Object):
    up, left, forward = smap.up, smap.left, smap.forward
    head = smap.head
    assert head is not None
    face = smap.face
    head_position = skeleton.head(head)

    def lateral(p: Vector) -> float:
        return (p - head_position).dot(left)

    def height(p: Vector) -> float:
        return p.dot(up)

    all_face = face.all_bones + list(smap.eyes.values())
    if not all_face:
        return
    # How far in front of the face the controls are placed.
    front = max((skeleton.head(n) - head_position).dot(forward) for n in all_face) + face_unit * 1.5

    def control_position(p: Vector) -> Vector:
        return p - forward * (p - head_position).dot(forward) + forward * front

    def add_face_control(name: str, position: Vector, widget: str, size: float, color: str,
                         lock_location=(False, True, False), lock_rotation=(True, True, True), parent=None):
        add_spec(name=name, matrix=_matrix(position, forward, z_hint=up), length=size, parent=parent or head,
                 kind='CTRL', collection=COLLECTION_FACE_CONTROLS, color=color, widget=widget,
                 widget_scale=size, rotation_mode='XYZ', lock_location=lock_location, lock_rotation=lock_rotation,
                 lock_scale=(True, True, True))
        info['additive_controls'].append(name)

    # The control's local axes (Y forward, Z up), in armature space. X points to the character's right.
    control_x = _matrix(Vector(), forward, z_hint=up).col[0].xyz.normalized()

    def translation_jobs(pose, control: str, weights: dict[str, float], axis: str, direction: Vector,
                         label: str, scale: float = 1.0):
        """Maps the control's local translation along `axis` onto the bones (1 unit -> `direction` * weight)."""
        for bone, weight in weights.items():
            if abs(weight) < 1e-4:
                continue
            offset = _to_local(rest[bone], direction * weight * scale)
            _add_transform_constraint(armature_object, pose[bone], control, label, map_from='LOCATION',
                                      from_axis=axis, from_range=(0.0, 1.0), map_to='LOCATION',
                                      to_values_min=Vector((0.0, 0.0, 0.0)), to_values_max=offset)

    # Blink (per side).
    for side in ('L', 'R'):
        upper = face.upper_lids.get(side, [])
        lower = face.lower_lids.get(side, [])
        if not upper:
            continue
        s = side_suffix(side)
        name = f'{CTRL}blink{s}'
        lid_positions = [skeleton.head(n) for n in upper + lower]
        center = sum(lid_positions, Vector()) / len(lid_positions)
        gap = (max(height(skeleton.head(n)) for n in upper) - min(height(skeleton.head(n)) for n in lower)) if lower else face_unit
        gap = max(gap, face_unit * 0.2)
        add_face_control(name, control_position(center) + up * gap * 2.0, 'arrow_updown', face_unit * 0.6, SIDE_COLORS[side],
                         lock_location=(True, True, False))

        def lower_height_at(x: float) -> float:
            if not lower:
                return min(height(skeleton.head(n)) for n in upper) - gap
            points = sorted((lateral(skeleton.head(n)), height(skeleton.head(n))) for n in lower)
            if x <= points[0][0]:
                return points[0][1]
            if x >= points[-1][0]:
                return points[-1][1]
            for (x0, h0), (x1, h1) in zip(points, points[1:]):
                if x0 <= x <= x1:
                    t = (x - x0) / (x1 - x0) if x1 > x0 else 0.0
                    return h0 + (h1 - h0) * t
            return points[-1][1]

        def upper_height_at(x: float) -> float:
            points = sorted((lateral(skeleton.head(n)), height(skeleton.head(n))) for n in upper)
            if x <= points[0][0]:
                return points[0][1]
            if x >= points[-1][0]:
                return points[-1][1]
            for (x0, h0), (x1, h1) in zip(points, points[1:]):
                if x0 <= x <= x1:
                    t = (x - x0) / (x1 - x0) if x1 > x0 else 0.0
                    return h0 + (h1 - h0) * t
            return points[-1][1]

        # The lids meet at 35% of the way from the lower to the upper lid. When the control is moved down by `gap`
        # the eye is fully closed. Moving it up opens the eye wider.
        weights = {}
        for n in upper:
            x = lateral(skeleton.head(n))
            meet = lower_height_at(x) + (height(skeleton.head(n)) - lower_height_at(x)) * 0.35
            weights[n] = (height(skeleton.head(n)) - meet) / gap
        for n in lower:
            x = lateral(skeleton.head(n))
            meet = height(skeleton.head(n)) + (upper_height_at(x) - height(skeleton.head(n))) * 0.35
            weights[n] = (height(skeleton.head(n)) - meet) / gap

        def blink_job(pose, name=name, weights=weights):
            # Moving the control up by 1 unit moves each lid by `weight` units (i.e., down by `gap` closes the eye).
            translation_jobs(pose, name, weights, 'Z', up, 'Blink')
        constraint_jobs.append(('additive', blink_job))

    # Brows (per side): translation moves the brows, rotation around the forward axis tilts them.
    for side in ('L', 'R'):
        brows = face.brows.get(side, [])
        if not brows:
            continue
        s = side_suffix(side)
        name = f'{CTRL}brow{s}'
        positions = [skeleton.head(n) for n in brows]
        center = sum(positions, Vector()) / len(positions)
        add_face_control(name, control_position(center) + up * face_unit * 1.2, 'rounded_square', face_unit * 0.6,
                         SIDE_COLORS[side], lock_location=(False, True, False), lock_rotation=(True, False, True))

        def brow_job(pose, name=name, brows=brows, center=center):
            weights = {n: 1.0 for n in brows}
            translation_jobs(pose, name, weights, 'Z', up, 'Brow')
            translation_jobs(pose, name, weights, 'X', control_x, 'Brow', scale=0.5)
            # Tilt: rotating the control around its Y axis (which points out of the face) by +angle moves the bones on
            # the +X side down, like rotating the brow line.
            angle = math.radians(30.0)
            for n in brows:
                p = (skeleton.head(n) - center).dot(control_x)
                offset = _to_local(rest[n], up * (-p * math.tan(angle)))
                _add_transform_constraint(armature_object, pose[n], name, 'Brow Tilt', map_from='ROTATION',
                                          from_axis='Y', from_range=(0.0, angle), map_to='LOCATION',
                                          to_values_min=Vector((0.0, 0.0, 0.0)), to_values_max=offset)
        constraint_jobs.append(('additive', brow_job))

    # Mouth.
    mouth = face.upper_mouth + face.lower_mouth
    if mouth:
        mouth_positions = {n: skeleton.head(n) for n in mouth}
        mouth_center = sum(mouth_positions.values(), Vector()) / len(mouth_positions)
        half_width = max(abs(lateral(p) - lateral(mouth_center)) for p in mouth_positions.values())
        half_width = max(half_width, face_unit * 0.5)

        # Mouth corners (per side): smile, frown, wide, narrow.
        for side, sign in (('L', 1.0), ('R', -1.0)):
            corner_bones = {n: p for n, p in mouth_positions.items() if (lateral(p) - lateral(mouth_center)) * sign > 0.0}
            if not corner_bones:
                continue
            corner = max(corner_bones.values(), key=lambda p: abs(lateral(p) - lateral(mouth_center)))
            s = side_suffix(side)
            name = f'{CTRL}mouth_corner{s}'
            add_face_control(name, control_position(corner) + left * sign * face_unit * 0.8, 'circle', face_unit * 0.5,
                             SIDE_COLORS[side], lock_location=(False, True, False))
            weights = {n: (abs(lateral(p) - lateral(mouth_center)) / half_width) ** 2 for n, p in corner_bones.items()}

            def corner_job(pose, name=name, weights=weights):
                translation_jobs(pose, name, weights, 'Z', up, 'Mouth Corner')
                translation_jobs(pose, name, weights, 'X', control_x, 'Mouth Corner')
            constraint_jobs.append(('additive', corner_job))

        # Upper & lower lips.
        for label, bones, direction in (('upper', face.upper_mouth, 1.0), ('lower', face.lower_mouth, -1.0)):
            if not bones:
                continue
            name = f'{CTRL}lip_{label}'
            positions = [mouth_positions[n] for n in bones]
            center = sum(positions, Vector()) / len(positions)
            parent = smap.jaw if (label == 'lower' and smap.jaw and all(skeleton.is_ancestor(smap.jaw, n) for n in bones)) else None
            add_face_control(name, control_position(center) + up * direction * face_unit * 0.8, 'rounded_square',
                             face_unit * 0.5, SIDE_COLORS['C'], lock_location=(True, False, False), parent=parent)
            weights = {n: max(0.0, 1.0 - (abs(lateral(mouth_positions[n]) - lateral(mouth_center)) / half_width) ** 2) * 0.8 + 0.2
                       for n in bones}

            def lip_job(pose, name=name, weights=weights):
                translation_jobs(pose, name, weights, 'Z', up, 'Lip')
                # Moving the control forward (local Y) pushes the lips forward (pout).
                translation_jobs(pose, name, weights, 'Y', forward, 'Lip')
            constraint_jobs.append(('additive', lip_job))

    # Jaw.
    if smap.jaw is not None and smap.jaw in rest:
        jaw = smap.jaw
        jaw_rest = rest[jaw]
        jaw_length = lengths[jaw]
        jaw_tail = jaw_rest.translation + jaw_rest.col[1].xyz.normalized() * jaw_length
        name = f'{CTRL}jaw'
        add_face_control(name, control_position(jaw_tail) - up * face_unit * 1.5, 'rounded_square', face_unit * 0.8,
                         SIDE_COLORS['C'], lock_location=(False, True, False))
        # Determine the direction of the rotations from the bone's rest orientation.
        jaw_x = jaw_rest.col[0].xyz.normalized()
        jaw_z = jaw_rest.col[2].xyz.normalized()
        # Rotating around +X moves the tail toward +Z. Opening the jaw moves the tail down.
        open_sign = 1.0 if jaw_z.dot(up) < 0.0 else -1.0
        # Rotating around +Z moves the tail toward -X. Moving the control to the right (+X of the control) should
        # move the chin to the right.
        side_sign = 1.0 if (-jaw_x).dot(control_x) > 0.0 else -1.0
        travel = max(jaw_length * 0.5, face_unit)

        def jaw_job(pose, name=name, jaw=jaw, open_sign=open_sign, side_sign=side_sign, travel=travel):
            _add_transform_constraint(armature_object, pose[jaw], name, 'Jaw Open', map_from='LOCATION', from_axis='Z',
                                      from_range=(-travel, 0.0), map_to='ROTATION',
                                      to_values_min=Vector((open_sign * JAW_OPEN_ANGLE, 0.0, 0.0)))
            _add_transform_constraint(armature_object, pose[jaw], name, 'Jaw Side', map_from='LOCATION', from_axis='X',
                                      from_range=(0.0, travel), map_to='ROTATION',
                                      to_values_min=Vector((0.0, 0.0, 0.0)),
                                      to_values_max=Vector((0.0, 0.0, side_sign * JAW_SIDE_ANGLE)))
        constraint_jobs.append(('additive', jaw_job))

    # Cheeks (per side): translation moves the cheek bones, forward pushes them out (puff).
    for side, sign in (('L', 1.0), ('R', -1.0)):
        cheeks = [n for n in face.other if 'cheek' in n.lower() and lateral(skeleton.head(n)) * sign > 0.0]
        if not cheeks:
            continue
        s = side_suffix(side)
        name = f'{CTRL}cheek{s}'
        positions = [skeleton.head(n) for n in cheeks]
        center = sum(positions, Vector()) / len(positions)
        add_face_control(name, control_position(center) + left * sign * face_unit * 1.2, 'circle', face_unit * 0.45,
                         SIDE_COLORS[side], lock_location=(False, False, False))

        def cheek_job(pose, name=name, cheeks=cheeks):
            weights = {n: 1.0 for n in cheeks}
            translation_jobs(pose, name, weights, 'Z', up, 'Cheek')
            translation_jobs(pose, name, weights, 'X', control_x, 'Cheek')
            translation_jobs(pose, name, weights, 'Y', forward, 'Cheek')
        constraint_jobs.append(('additive', cheek_job))

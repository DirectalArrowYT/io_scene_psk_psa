"""
Snapping & baking between the FK (deform bone) animation and the IK/eye controls of the "Animate Ready" rig.
"""
import math
from typing import cast as typing_cast, Iterable

import bpy
from bpy.types import Action, Armature, Context, Object
from mathutils import Matrix, Quaternion

from ..shared.compat import action_ensure_channelbag_for_slot, action_ensure_slot, channelbag_ensure_fcurve
from .builder import CONSTRAINT_PREFIX, get_rig_info, set_rig_property, upgrade_rig
from .orientation import _limb_bend_vector


# ----------------------------------------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------------------------------------

def _rest(armature_object: Object, name: str) -> Matrix:
    return typing_cast(Armature, armature_object.data).bones[name].matrix_local


def _basis_from_pose(armature_object: Object, name: str, pose_matrix: Matrix, parent_pose_matrix: Matrix | None) -> Matrix:
    """
    Calculates the local (basis) transform of a pose bone that results in the given armature-space pose matrix, given
    the armature-space pose matrix of its parent.
    """
    bone = typing_cast(Armature, armature_object.data).bones[name]
    if bone.parent is None or parent_pose_matrix is None:
        return bone.matrix_local.inverted() @ pose_matrix
    local_rest = bone.parent.matrix_local.inverted() @ bone.matrix_local
    return local_rest.inverted() @ parent_pose_matrix.inverted() @ pose_matrix


def _rigid_follow(pose_matrix: Matrix, rest_matrix: Matrix, other_rest_matrix: Matrix) -> Matrix:
    """
    Returns the pose matrix of a bone that rigidly follows another bone (i.e., as though it were parented to it).
    """
    return pose_matrix @ rest_matrix.inverted() @ other_rest_matrix


def _orthonormal(matrix: Matrix) -> Matrix:
    location, rotation, scale = matrix.decompose()
    return Matrix.LocRotScale(location, rotation, scale)


def _set_basis(pose_bone, basis: Matrix):
    location, rotation, scale = basis.decompose()
    pose_bone.location = location
    if pose_bone.rotation_mode == 'QUATERNION':
        pose_bone.rotation_quaternion = rotation
    elif pose_bone.rotation_mode == 'AXIS_ANGLE':
        axis, angle = rotation.to_axis_angle()
        pose_bone.rotation_axis_angle = (angle, axis.x, axis.y, axis.z)
    else:
        pose_bone.rotation_euler = rotation.to_euler(pose_bone.rotation_mode)
    pose_bone.scale = scale


def _unscaled_chain_lengths(armature_object: Object, limb: dict, end: str) -> tuple[float, float]:
    """
    The lengths of the two bones of an IK chain in the current pose with the scale of the limb bones & their parents
    taken out. The IK solver multiplies the lengths by that scale again (see `_add_ik_solver_drivers` in the builder),
    so that the chain keeps following the bones when they are scaled afterwards.
    """
    pose = armature_object.pose.bones
    upper, lower = pose[limb['upper']], pose[limb['lower']]
    world = armature_object.matrix_world
    elbow = world @ lower.head
    length_a = (elbow - world @ upper.head).length
    length_b = (world @ pose[end].head - elbow).length
    parent = pose.get(limb.get('scale_parent') or '')
    matrix = world @ parent.matrix if parent is not None else world
    scale = abs(matrix.to_3x3().determinant()) ** (1.0 / 3.0)
    upper_axis, upper_lower_axis, lower_axis = limb.get('scale_axes', (1, 1, 1))
    scale_a = scale * abs(upper.scale[upper_axis])
    scale_b = scale * abs(upper.scale[upper_lower_axis]) * abs(lower.scale[lower_axis])
    return (length_a / scale_a if scale_a > 1e-9 else limb['rest_length_a'],
            length_b / scale_b if scale_b > 1e-9 else limb['rest_length_b'])


def _upgraded_limbs(armature_object: Object, limbs: list[tuple[str, str, dict]] | None) -> list[tuple[str, str, dict]] | None:
    """Updates a rig made with an older version of the add-on, and returns the limbs with their up-to-date info."""
    if not upgrade_rig(armature_object) or limbs is None:
        return limbs
    info = get_rig_info(armature_object) or {}
    return [(kind, side, info['arms' if kind == 'arm' else 'legs'][side]) for kind, side, _ in limbs]


def get_rig_limbs(info: dict, sides: Iterable[str] | None = None, kinds: Iterable[str] = ('arm', 'leg')) -> list[tuple[str, str, dict]]:
    limbs = []
    for kind, key in (('arm', 'arms'), ('leg', 'legs')):
        if kind not in kinds:
            continue
        for side, limb in info.get(key, {}).items():
            if sides is None or side in sides:
                limbs.append((kind, side, limb))
    return limbs


def _rig_follow_constraints(armature_object: Object):
    """The (non-additive) rig constraints, i.e., the ones that override the FK animation."""
    for pose_bone in armature_object.pose.bones:
        for constraint in pose_bone.constraints:
            if constraint.name.startswith(CONSTRAINT_PREFIX) and constraint.type in {'IK', 'COPY_ROTATION', 'COPY_TRANSFORMS', 'DAMPED_TRACK'}:
                yield constraint


class _MutedRigConstraints:
    """Context manager that temporarily mutes the IK & aim constraints of the rig, exposing the FK pose."""
    def __init__(self, armature_object: Object):
        self.armature_object = armature_object
        self.saved = []

    def __enter__(self):
        for constraint in _rig_follow_constraints(self.armature_object):
            self.saved.append((constraint, constraint.mute))
            constraint.mute = True
        return self

    def __exit__(self, *args):
        for constraint, mute in self.saved:
            constraint.mute = mute


class _DisabledArmatureModifiers:
    """Context manager that disables the armature modifiers of the meshes deformed by the armature, to speed up
    frame changes while baking."""
    def __init__(self, armature_object: Object):
        self.armature_object = armature_object
        self.saved = []

    def __enter__(self):
        for obj in bpy.data.objects:
            if obj.type != 'MESH':
                continue
            for modifier in obj.modifiers:
                if modifier.type == 'ARMATURE' and getattr(modifier, 'object', None) == self.armature_object:
                    self.saved.append((modifier, modifier.show_viewport))
                    modifier.show_viewport = False
        return self

    def __exit__(self, *args):
        for modifier, show_viewport in self.saved:
            modifier.show_viewport = show_viewport


# ----------------------------------------------------------------------------------------------------------------
# FK -> IK
# ----------------------------------------------------------------------------------------------------------------

def compute_ik_controls_from_fk(armature_object: Object, info: dict, limbs: list[tuple[str, str, dict]],
                                include_eyes: bool) -> tuple[dict[str, Matrix], dict[str, float]]:
    """
    Calculates the local (basis) transforms of the IK (and eye) controls so that they match the current (FK) pose of
    the deform bones, and the lengths of the IK chains. The rig constraints must be muted when this is called.
    """
    pose = armature_object.pose.bones
    bases: dict[str, Matrix] = {}
    lengths: dict[str, float] = {}

    def parent_pose(name: str, computed: dict[str, Matrix]) -> Matrix | None:
        bone = typing_cast(Armature, armature_object.data).bones[name]
        if bone.parent is None:
            return None
        return computed.get(bone.parent.name, pose[bone.parent.name].matrix)

    computed: dict[str, Matrix] = {}

    def solve(name: str, pose_matrix: Matrix):
        computed[name] = pose_matrix
        bases[name] = _orthonormal(_basis_from_pose(armature_object, name, pose_matrix, parent_pose(name, computed)))

    def solve_pole(limb: dict, upper: str, lower: str, end: str):
        pole = limb['pole']
        a, b, c = pose[upper].head, pose[lower].head, pose[end].head
        # Fallback bend direction (e.g., when the limb is straight): the rest bend direction, relative to the upper bone.
        rest_upper = _rest(armature_object, upper)
        rest_bend = _rest(armature_object, pole).translation - _rest(armature_object, lower).translation
        fallback = (pose[upper].matrix.to_3x3() @ rest_upper.to_3x3().inverted() @ rest_bend)
        bend = _limb_bend_vector(a, b, c, fallback)
        distance = limb.get('pole_distance', (b - a).length)
        position = b + bend * distance
        # The pole's rotation is locked, so only its location changes.
        parent_matrix = parent_pose(pole, computed)
        matrix = _rigid_follow(parent_matrix, _rest(armature_object, typing_cast(Armature, armature_object.data).bones[pole].parent.name), _rest(armature_object, pole)) \
            if parent_matrix is not None else _rest(armature_object, pole).copy()
        matrix.translation = position
        solve(pole, matrix)

    for kind, side, limb in limbs:
        # Match the offsets & lengths of the IK chain to the FK animation (which may differ slightly from the rest
        # pose), so that the IK reproduces the FK animation exactly.
        if limb.get('mch_root'):
            end = limb['hand'] if kind == 'arm' else limb['foot']
            upper_pose, lower_pose = pose[limb['upper']], pose[limb['lower']]
            upper_rest, root_rest = _rest(armature_object, limb['upper']), _rest(armature_object, limb['mch_root'])
            offset = upper_rest.to_3x3() @ upper_pose.location
            bases[limb['mch_root']] = Matrix.Translation(root_rest.to_3x3().inverted() @ offset)
            bases[limb['mch_lower']] = Matrix.Translation(lower_pose.location)
            lengths[limb['length_a']], lengths[limb['length_b']] = _unscaled_chain_lengths(armature_object, limb, end)
        if kind == 'arm':
            solve(limb['ik'], _rigid_follow(pose[limb['hand']].matrix, _rest(armature_object, limb['hand']), _rest(armature_object, limb['ik'])))
            solve_pole(limb, limb['upper'], limb['lower'], limb['hand'])
        else:
            foot_matrix = pose[limb['foot']].matrix
            foot_rest = _rest(armature_object, limb['foot'])
            ik_matrix = _rigid_follow(foot_matrix, foot_rest, _rest(armature_object, limb['ik']))
            solve(limb['ik'], ik_matrix)
            # The foot roll mechanism is at rest (the foot roll slider & control are reset).
            if limb.get('roll_ctrl'):
                bases[limb['roll_ctrl']] = Matrix.Identity(4)
            heel_matrix = _rigid_follow(ik_matrix, _rest(armature_object, limb['ik']), _rest(armature_object, limb['heel']))
            computed[limb['heel']] = heel_matrix
            ball_matrix = _rigid_follow(heel_matrix, _rest(armature_object, limb['heel']), _rest(armature_object, limb['ball']))
            computed[limb['ball']] = ball_matrix
            if limb.get('toe') and limb.get('toe_ctrl'):
                toe_matrix = pose[limb['toe']].matrix
                solve(limb['toe_ctrl'], _rigid_follow(toe_matrix, _rest(armature_object, limb['toe']), _rest(armature_object, limb['toe_ctrl'])))
            solve_pole(limb, limb['upper'], limb['lower'], limb['foot'])

    eyes = info.get('eyes')
    if include_eyes and eyes:
        parent = eyes['parent']
        parent_bone = typing_cast(Armature, armature_object.data).bones[parent]
        parent_matrix = pose[parent_bone.parent.name].matrix if parent_bone.parent else None
        eyes_matrix = _rigid_follow(parent_matrix, parent_bone.parent.matrix_local, parent_bone.matrix_local) \
            if parent_matrix is not None else parent_bone.matrix_local.copy()
        # Keep the eye target parent at its rest location relative to the head.
        solve(parent, eyes_matrix)
        distance = eyes.get('distance', 1.0)
        for item in eyes['items']:
            eye = pose[item['bone']]
            direction = eye.matrix.col[1].xyz.normalized()
            target = eye.head + direction * distance
            matrix = _rigid_follow(eyes_matrix, _rest(armature_object, parent), _rest(armature_object, item['ctrl']))
            matrix.translation = target
            solve(item['ctrl'], matrix)

    return bases, lengths


def compute_twists(armature_object: Object, fk_upper_matrices: dict[str, Matrix], limbs: list[tuple[str, str, dict]]) -> dict[str, Matrix]:
    """
    Calculates the local (basis) transforms of the twist controls so that the upper bones of the IK chains match the
    twist of the FK upper bones. The IK must be evaluated (with the twist controls at rest) when this is called.
    """
    bases = {}
    for kind, side, limb in limbs:
        for twist_key, mch_key, deform_key in (('twist', 'mch_upper', 'upper'), ('lower_twist', 'mch_lower', 'lower')):
            twist = limb.get(twist_key)
            if not twist or limb[deform_key] not in fk_upper_matrices:
                continue
            ik_rotation = armature_object.pose.bones[limb[mch_key]].matrix.to_quaternion()
            fk_rotation = fk_upper_matrices[limb[deform_key]].to_quaternion()
            q = ik_rotation.inverted() @ fk_rotation
            # Swing-twist decomposition around the Y axis (the axis of the bone).
            angle = 2.0 * math.atan2(q.y, q.w)
            angle = math.atan2(math.sin(angle), math.cos(angle))
            bases[twist] = Matrix.Rotation(angle, 4, 'Y')
    return bases


def _get_action_and_slot(armature_object: Object):
    animation_data = armature_object.animation_data
    if animation_data is None or animation_data.action is None:
        return None, None
    action = animation_data.action
    slot = animation_data.action_slot
    if slot is None:
        slot = action_ensure_slot(action, 'OBJECT', armature_object.name)
        animation_data.action_slot = slot
    return action, slot


def _write_bone_keys(action: Action, slot, armature_object: Object, samples: dict[str, list[tuple[float, Matrix]]]):
    """
    Writes the (frame, basis) samples as keyframes for the given pose bones, replacing any existing keyframes of the
    location, rotation and scale channels.
    """
    channelbag = action_ensure_channelbag_for_slot(action, slot)
    for name, frames in samples.items():
        if not frames:
            continue
        pose_bone = armature_object.pose.bones[name]
        rotation_mode = pose_bone.rotation_mode
        channels: list[tuple[str, int, list[float]]] = []
        locations, rotations, scales = [], [], []
        previous: Quaternion | None = None
        for frame, basis in frames:
            location, rotation, scale = basis.decompose()
            if previous is not None and previous.dot(rotation) < 0.0:
                rotation.negate()
            previous = rotation
            locations.append(location)
            rotations.append(rotation)
            scales.append(scale)
        times = [f for f, _ in frames]
        for i in range(3):
            channels.append(('location', i, [v[i] for v in locations]))
            channels.append(('scale', i, [v[i] for v in scales]))
        if rotation_mode == 'QUATERNION':
            for i in range(4):
                channels.append(('rotation_quaternion', i, [q[i] for q in rotations]))
        elif rotation_mode == 'AXIS_ANGLE':
            values = []
            for q in rotations:
                axis, angle = q.to_axis_angle()
                values.append((angle, axis.x, axis.y, axis.z))
            for i in range(4):
                channels.append(('rotation_axis_angle', i, [v[i] for v in values]))
        else:
            eulers = []
            previous_euler = None
            for q in rotations:
                e = q.to_euler(rotation_mode, previous_euler) if previous_euler is not None else q.to_euler(rotation_mode)
                eulers.append(e)
                previous_euler = e
            for i in range(3):
                channels.append(('rotation_euler', i, [e[i] for e in eulers]))
        for prop, index, values in channels:
            data_path = pose_bone.path_from_id(prop)
            fcurve = channelbag_ensure_fcurve(channelbag, data_path, index=index, group_name=name)
            # Remove the keyframes in the baked range.
            frame_min, frame_max = min(times), max(times)
            for i in reversed(range(len(fcurve.keyframe_points))):
                if frame_min - 1e-3 <= fcurve.keyframe_points[i].co.x <= frame_max + 1e-3:
                    fcurve.keyframe_points.remove(fcurve.keyframe_points[i], fast=True)
            existing = len(fcurve.keyframe_points)
            fcurve.keyframe_points.add(len(times))
            for i, (t, v) in enumerate(zip(times, values)):
                keyframe = fcurve.keyframe_points[existing + i]
                keyframe.co = (t, v)
                keyframe.interpolation = 'LINEAR'
            fcurve.update()


def _write_property_keys(action: Action, slot, armature_object: Object, samples: dict[str, list[tuple[float, float]]]):
    """Writes the (frame, value) samples as keyframes for the given custom properties of the armature object."""
    channelbag = action_ensure_channelbag_for_slot(action, slot)
    for prop, frames in samples.items():
        if not frames:
            continue
        fcurve = channelbag_ensure_fcurve(channelbag, f'["{prop}"]', index=0, group_name='Rig')
        frame_min, frame_max = min(f for f, _ in frames), max(f for f, _ in frames)
        for i in reversed(range(len(fcurve.keyframe_points))):
            if frame_min - 1e-3 <= fcurve.keyframe_points[i].co.x <= frame_max + 1e-3:
                fcurve.keyframe_points.remove(fcurve.keyframe_points[i], fast=True)
        existing = len(fcurve.keyframe_points)
        fcurve.keyframe_points.add(len(frames))
        for i, (t, v) in enumerate(frames):
            keyframe = fcurve.keyframe_points[existing + i]
            keyframe.co = (t, v)
            keyframe.interpolation = 'LINEAR'
        fcurve.update()
        set_rig_property(armature_object, prop, frames[-1][1])


def _key_property(action: Action, slot, armature_object: Object, prop: str, frame: float, value: float):
    channelbag = action_ensure_channelbag_for_slot(action, slot)
    fcurve = channelbag_ensure_fcurve(channelbag, f'["{prop}"]', index=0, group_name='Rig')
    fcurve.keyframe_points.insert(frame, value, options={'FAST'})
    fcurve.update()


def _clear_bone_fcurves(action: Action, slot, armature_object: Object, names: Iterable[str]):
    channelbag = action_ensure_channelbag_for_slot(action, slot)
    names = set(names)
    for fcurve in list(channelbag.fcurves):
        for name in names:
            if fcurve.data_path.startswith(f'pose.bones["{name}"].'):
                channelbag.fcurves.remove(fcurve)
                break


def _reset_pose_bones(armature_object: Object, names: Iterable[str]):
    for name in names:
        pose_bone = armature_object.pose.bones.get(name)
        if pose_bone is None:
            continue
        pose_bone.location = (0.0, 0.0, 0.0)
        pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pose_bone.rotation_euler = (0.0, 0.0, 0.0)
        pose_bone.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
        pose_bone.scale = (1.0, 1.0, 1.0)


def get_action_frame_range(action: Action) -> tuple[int, int]:
    start, end = action.frame_range
    return int(round(start)), int(round(end))


def bake_fk_to_ik(context: Context, armature_object: Object, frame_start: int, frame_end: int,
                  limbs: list[tuple[str, str, dict]] | None = None, include_eyes: bool = True,
                  reset_additive_controls: bool = True) -> int:
    """
    Keys the IK (and eye) controls so that they match the FK animation of the active action over the frame range,
    and switches the limbs to IK. Returns the number of frames baked.
    """
    limbs = _upgraded_limbs(armature_object, limbs)
    info = get_rig_info(armature_object)
    if info is None:
        raise RuntimeError('The armature does not have an Animate Ready rig')
    action, slot = _get_action_and_slot(armature_object)
    if action is None:
        raise RuntimeError('The armature does not have an active action')
    assert context.scene
    if limbs is None:
        limbs = get_rig_limbs(info)

    scene = context.scene
    saved_frame = scene.frame_current, scene.frame_subframe
    samples: dict[str, list[tuple[float, Matrix]]] = {}
    roll_props = [limb['roll_prop'] for kind, side, limb in limbs if kind == 'leg']
    for p in roll_props:
        set_rig_property(armature_object, p, 0.0)

    twist_names = [limb[k] for _, _, limb in limbs for k in ('twist', 'lower_twist') if limb.get(k)]
    length_samples: dict[str, list[tuple[float, float]]] = {}
    fk_upper_matrices: dict[int, dict[str, Matrix]] = {}
    with _DisabledArmatureModifiers(armature_object):
        # Pass 1: Match the IK controls to the FK pose.
        with _MutedRigConstraints(armature_object):
            for frame in range(frame_start, frame_end + 1):
                scene.frame_set(frame)
                bases, lengths = compute_ik_controls_from_fk(armature_object, info, limbs, include_eyes)
                for name, basis in bases.items():
                    samples.setdefault(name, []).append((float(frame), basis))
                for name, value in lengths.items():
                    length_samples.setdefault(name, []).append((float(frame), value))
                fk_upper_matrices[frame] = {limb[k]: armature_object.pose.bones[limb[k]].matrix.copy()
                                            for _, _, limb in limbs for k in ('upper', 'lower')}
        _write_property_keys(action, slot, armature_object, length_samples)
        # The twist controls are keyed at rest for the first pass.
        for name in twist_names:
            samples[name] = [(float(frame), Matrix.Identity(4)) for frame in range(frame_start, frame_end + 1)]
        _write_bone_keys(action, slot, armature_object, samples)
        for kind, side, limb in limbs:
            set_rig_property(armature_object, limb['prop'], 1.0)

        # Pass 2: Match the twist of the IK chains to the FK animation.
        if twist_names:
            twist_samples: dict[str, list[tuple[float, Matrix]]] = {name: [] for name in twist_names}
            for frame in range(frame_start, frame_end + 1):
                scene.frame_set(frame)
                for name, basis in compute_twists(armature_object, fk_upper_matrices[frame], limbs).items():
                    twist_samples[name].append((float(frame), basis))
            _write_bone_keys(action, slot, armature_object, twist_samples)

    scene.frame_set(saved_frame[0], subframe=saved_frame[1])

    # The foot roll is reset & keyed to 0 so that the IK controls are an exact match.
    for p in roll_props:
        _key_property(action, slot, armature_object, p, float(frame_start), 0.0)
        set_rig_property(armature_object, p, 0.0)

    if reset_additive_controls:
        additive = info.get('additive_controls', [])
        _clear_bone_fcurves(action, slot, armature_object, additive)
        _reset_pose_bones(armature_object, additive)
        # Key the additive controls at rest so that this action always plays back the same, regardless of what the
        # pose of the controls was before.
        rest_samples = {name: [(float(frame_start), Matrix.Identity(4))] for name in additive
                        if name in armature_object.pose.bones}
        _write_bone_keys(action, slot, armature_object, rest_samples)

    for kind, side, limb in limbs:
        set_rig_property(armature_object, limb['prop'], 1.0)
    eyes = info.get('eyes')
    if include_eyes and eyes:
        set_rig_property(armature_object, eyes['prop'], 1.0)

    scene.frame_set(saved_frame[0], subframe=saved_frame[1])
    return frame_end - frame_start + 1


# ----------------------------------------------------------------------------------------------------------------
# IK -> FK
# ----------------------------------------------------------------------------------------------------------------

def get_rig_driven_bones(armature_object: Object, info: dict, limbs: list[tuple[str, str, dict]] | None,
                         include_eyes: bool, include_additive: bool) -> list[str]:
    """
    Returns the deform bones whose (visual) pose is affected by the rig, in hierarchical order.
    """
    names = set()
    if limbs is None:
        limbs = get_rig_limbs(info)
    for kind, side, limb in limbs:
        names.update([limb['upper'], limb['lower']])
        names.add(limb['hand'] if kind == 'arm' else limb['foot'])
        if kind == 'leg' and limb.get('toe'):
            names.add(limb['toe'])
    eyes = info.get('eyes')
    if include_eyes and eyes:
        names.update(item['bone'] for item in eyes['items'])
    if include_additive:
        for pose_bone in armature_object.pose.bones:
            if any(c.name.startswith(CONSTRAINT_PREFIX) and c.type == 'TRANSFORM' for c in pose_bone.constraints):
                names.add(pose_bone.name)
    # Hierarchical order.
    return [b.name for b in typing_cast(Armature, armature_object.data).bones if b.name in names]


def compute_fk_from_visual(armature_object: Object, names: list[str]) -> dict[str, Matrix]:
    """
    Calculates the local (basis) transforms of the given bones so that they match their current visual pose
    (i.e., including the effect of the constraints).
    """
    pose = armature_object.pose.bones
    bases = {}
    for name in names:
        pose_bone = pose[name]
        parent_matrix = pose_bone.parent.matrix if pose_bone.parent else None
        bases[name] = _orthonormal(_basis_from_pose(armature_object, name, pose_bone.matrix, parent_matrix))
    return bases


def bake_ik_to_fk(context: Context, armature_object: Object, frame_start: int, frame_end: int,
                  limbs: list[tuple[str, str, dict]] | None = None, include_eyes: bool = True,
                  include_additive: bool = True, clear_controls: bool = True) -> int:
    """
    Keys the deform (FK) bones with the visual result of the rig (IK, eye aim, finger & face controls) over the
    frame range, then switches the rig to FK and removes the control keys that were baked.
    """
    info = get_rig_info(armature_object)
    if info is None:
        raise RuntimeError('The armature does not have an Animate Ready rig')
    action, slot = _get_action_and_slot(armature_object)
    if action is None:
        raise RuntimeError('The armature does not have an active action')
    assert context.scene
    if limbs is None:
        limbs = get_rig_limbs(info)
    names = get_rig_driven_bones(armature_object, info, limbs, include_eyes, include_additive)

    scene = context.scene
    saved_frame = scene.frame_current, scene.frame_subframe
    samples: dict[str, list[tuple[float, Matrix]]] = {name: [] for name in names}
    with _DisabledArmatureModifiers(armature_object):
        for frame in range(frame_start, frame_end + 1):
            scene.frame_set(frame)
            for name, basis in compute_fk_from_visual(armature_object, names).items():
                samples[name].append((float(frame), basis))

    _write_bone_keys(action, slot, armature_object, samples)

    # Switch to FK.
    for kind, side, limb in limbs:
        set_rig_property(armature_object, limb['prop'], 0.0)
        if kind == 'leg':
            set_rig_property(armature_object, limb['roll_prop'], 0.0)
    eyes = info.get('eyes')
    if include_eyes and eyes:
        set_rig_property(armature_object, eyes['prop'], 0.0)

    if clear_controls:
        controls = []
        for kind, side, limb in limbs:
            controls.extend([limb['ik'], limb['pole']])
            controls.extend(limb[k] for k in ('twist', 'lower_twist', 'mch_root', 'mch_lower') if limb.get(k))
            if kind == 'leg' and limb.get('toe_ctrl'):
                controls.append(limb['toe_ctrl'])
            if kind == 'leg' and limb.get('roll_ctrl'):
                controls.append(limb['roll_ctrl'])
        if include_eyes and eyes:
            controls.append(eyes['parent'])
            controls.extend(item['ctrl'] for item in eyes['items'])
        if include_additive:
            controls.extend(info.get('additive_controls', []))
            # The additive controls have been baked into the FK animation, so they must be reset to avoid them being
            # applied twice.
            _reset_pose_bones(armature_object, info.get('additive_controls', []))
        _clear_bone_fcurves(action, slot, armature_object, controls)
        # Remove the keys of the properties that were switched.
        channelbag = action_ensure_channelbag_for_slot(action, slot)
        props = {limb['prop'] for _, _, limb in limbs} | {limb['roll_prop'] for kind, _, limb in limbs if kind == 'leg'}
        for _, _, limb in limbs:
            for key in ('length_a', 'length_b'):
                if limb.get(key):
                    props.add(limb[key])
                    set_rig_property(armature_object, limb[key], limb[f'rest_{key}'])
        _reset_pose_bones(armature_object, [limb[k] for _, _, limb in limbs for k in ('mch_root', 'mch_lower', 'roll_ctrl') if limb.get(k)])
        if include_eyes and eyes:
            props.add(eyes['prop'])
        for fcurve in list(channelbag.fcurves):
            if any(fcurve.data_path == f'["{p}"]' for p in props):
                channelbag.fcurves.remove(fcurve)

    scene.frame_set(saved_frame[0], subframe=saved_frame[1])
    return frame_end - frame_start + 1


# ----------------------------------------------------------------------------------------------------------------
# Snapping (current frame only)
# ----------------------------------------------------------------------------------------------------------------

def _autokey(context: Context, armature_object: Object, names: Iterable[str]):
    if not context.scene or not context.scene.tool_settings.use_keyframe_insert_auto:
        return
    for name in names:
        pose_bone = armature_object.pose.bones[name]
        pose_bone.keyframe_insert('location', group=name)
        match pose_bone.rotation_mode:
            case 'QUATERNION':
                pose_bone.keyframe_insert('rotation_quaternion', group=name)
            case 'AXIS_ANGLE':
                pose_bone.keyframe_insert('rotation_axis_angle', group=name)
            case _:
                pose_bone.keyframe_insert('rotation_euler', group=name)
        pose_bone.keyframe_insert('scale', group=name)


def snap_fk_to_ik(context: Context, armature_object: Object, limbs: list[tuple[str, str, dict]], include_eyes: bool = False):
    """Moves the IK controls to match the current FK pose and switches the limbs to IK."""
    limbs = _upgraded_limbs(armature_object, limbs)
    info = get_rig_info(armature_object)
    assert info is not None and context.view_layer
    for kind, side, limb in limbs:
        if kind == 'leg':
            set_rig_property(armature_object, limb['roll_prop'], 0.0)
    with _MutedRigConstraints(armature_object):
        context.view_layer.update()
        bases, lengths = compute_ik_controls_from_fk(armature_object, info, limbs, include_eyes)
        fk_upper_matrices = {limb[k]: armature_object.pose.bones[limb[k]].matrix.copy() for _, _, limb in limbs for k in ('upper', 'lower')}
    for kind, side, limb in limbs:
        for k in ('twist', 'lower_twist'):
            if limb.get(k):
                bases[limb[k]] = Matrix.Identity(4)
    for name, basis in bases.items():
        _set_basis(armature_object.pose.bones[name], basis)
    for name, value in lengths.items():
        set_rig_property(armature_object, name, value)
    for kind, side, limb in limbs:
        set_rig_property(armature_object, limb['prop'], 1.0)
    if include_eyes and info.get('eyes'):
        set_rig_property(armature_object, info['eyes']['prop'], 1.0)
    context.view_layer.update()
    twists = compute_twists(armature_object, fk_upper_matrices, limbs)
    for name, basis in twists.items():
        _set_basis(armature_object.pose.bones[name], basis)
        bases[name] = basis
    context.view_layer.update()
    _autokey(context, armature_object, bases.keys())


def snap_ik_to_fk(context: Context, armature_object: Object, limbs: list[tuple[str, str, dict]], include_eyes: bool = False):
    """Sets the FK (deform) bones to match the current IK pose and switches the limbs to FK."""
    info = get_rig_info(armature_object)
    assert info is not None and context.view_layer
    context.view_layer.update()
    names = get_rig_driven_bones(armature_object, info, limbs, include_eyes, include_additive=False)
    bases = compute_fk_from_visual(armature_object, names)
    for name in names:
        _set_basis(armature_object.pose.bones[name], bases[name])
    for kind, side, limb in limbs:
        set_rig_property(armature_object, limb['prop'], 0.0)
    if include_eyes and info.get('eyes'):
        set_rig_property(armature_object, info['eyes']['prop'], 0.0)
    context.view_layer.update()
    _autokey(context, armature_object, names)

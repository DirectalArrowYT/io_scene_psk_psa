"""
Bone re-orientation ("fix bone tails").

PSK files only store the position & orientation of each bone's joint; there is no concept of a bone "tail". Many games
(e.g., the ones using Y-up skeletons where every bone has an identity rotation) end up with all of the bones pointing in
the same direction when imported, which makes the armature very difficult to read and animate, and makes IK impossible.

This module re-orients the bones so that the tails point toward the next joint in the chain (e.g., the upper arm
points toward the elbow), with sensible, symmetric bone rolls. The rotation that maps the new orientation back to the
original one is stored on each bone (see `BONE_REST_CORRECTION_PROPERTY`), and is used by the PSA importer and the
PSK/PSA exporters, so that the files that are exported are identical to the originals.
"""
from typing import cast as typing_cast

import bpy
from bpy.types import Armature, EditBone, Object
from mathutils import Matrix, Quaternion, Vector

from ..shared.helpers import BONE_REST_CORRECTION_PROPERTY, get_bone_rest_correction, is_rig_bone
from .skeleton import BoneInfo, Skeleton, SkeletonMap, analyze_skeleton, _is_helper


class BoneOrientation:
    def __init__(self, direction: Vector, length: float, x_axis_hint: Vector | None = None, z_axis_hint: Vector | None = None):
        self.direction = direction.normalized()
        self.length = length
        self.x_axis_hint = x_axis_hint
        self.z_axis_hint = z_axis_hint


def skeleton_from_edit_bones(edit_bones) -> Skeleton:
    return Skeleton([
        BoneInfo(name=eb.name, parent=eb.parent.name if eb.parent else None, head=eb.head.copy())
        for eb in edit_bones if not is_rig_bone(eb)
    ])


def skeleton_from_bones(bones) -> Skeleton:
    return Skeleton([
        BoneInfo(name=b.name, parent=b.parent.name if b.parent else None, head=b.head_local.copy())
        for b in bones if not is_rig_bone(b)
    ])


def _perpendicular(v: Vector, axis: Vector) -> Vector:
    return v - axis * v.dot(axis)


def get_palm_side_vector(skeleton: Skeleton, smap: SkeletonMap, side: str) -> Vector | None:
    """
    Returns a unit vector that points from the back of the hand toward the palm (i.e., the direction in which the
    fingers curl).
    """
    arm = smap.arms.get(side)
    if arm is None:
        return None
    hand = skeleton.head(arm.hand)
    first = next((arm.fingers[k][0] for k in ('index', 'middle') if k in arm.fingers), None)
    last = next((arm.fingers[k][0] for k in ('pinky', 'ring') if k in arm.fingers), None)
    if first is not None and last is not None and first != last:
        n = (skeleton.head(first) - hand).cross(skeleton.head(last) - hand)
        if n.length > skeleton.epsilon ** 2:
            # The chirality of the hand is the same regardless of the pose, so we can determine which side of the
            # hand the palm is on from the side of the body.
            n.normalize()
            return -n if side == 'L' else n
    # Fall back to assuming a T-pose with the palms facing down.
    return -smap.up


def _limb_bend_vector(a: Vector, b: Vector, c: Vector, fallback: Vector) -> Vector:
    """
    Returns a unit vector pointing from the line `a`->`c` toward the joint `b` (i.e., the direction in which the
    joint "points").
    """
    line = c - a
    if line.length < 1e-9:
        return fallback.normalized()
    t = (b - a).dot(line) / line.length_squared
    bend = b - (a + line * t)
    if bend.length < line.length * 1e-3:
        return fallback.normalized()
    return bend.normalized()


def compute_bone_orientations(skeleton: Skeleton, smap: SkeletonMap, default_length: float = 1.0) -> dict[str, BoneOrientation]:
    orientations: dict[str, BoneOrientation] = {}
    up, left, forward = smap.up, smap.left, smap.forward
    extent = skeleton.extent
    if extent <= 1e-4:
        # e.g., a skeleton with a single bone, or with all the bones at the same position.
        extent = default_length * 20.0
    min_length = extent * 0.005
    face_bones = set(smap.face.all_bones)
    if smap.head is not None:
        face_bones.update(n for n in skeleton.descendants(smap.head)
                          if smap.head is not None and n not in skeleton.children(smap.head))

    def default_x_hint(direction: Vector) -> Vector:
        for candidate in (left, forward, up):
            if abs(direction.dot(candidate)) < 0.8:
                return candidate
        return left

    # Role overrides.
    for side, arm in smap.arms.items():
        shoulder, elbow, wrist = skeleton.head(arm.upper), skeleton.head(arm.lower), skeleton.head(arm.hand)
        bend = _limb_bend_vector(shoulder, elbow, wrist, -forward)
        orientations[arm.upper] = BoneOrientation(elbow - shoulder, (elbow - shoulder).length, z_axis_hint=bend)
        orientations[arm.lower] = BoneOrientation(wrist - elbow, (wrist - elbow).length, z_axis_hint=bend)
        palm = get_palm_side_vector(skeleton, smap, side)
        finger_bases = [skeleton.head(bones[0]) for kind, bones in arm.fingers.items() if kind != 'thumb']
        if 'middle' in arm.fingers:
            target = skeleton.head(arm.fingers['middle'][0])
        elif finger_bases:
            target = sum(finger_bases, Vector()) / len(finger_bases)
        else:
            target = wrist + (wrist - elbow) * 0.3
        if (target - wrist).length <= skeleton.epsilon:
            target = wrist + (wrist - elbow) * 0.3
        orientations[arm.hand] = BoneOrientation(target - wrist, (target - wrist).length, z_axis_hint=palm)
        for kind, bones in arm.fingers.items():
            for i, bone in enumerate(bones):
                head = skeleton.head(bone)
                if i + 1 < len(bones) and skeleton.distance(bones[i + 1], bone) > skeleton.epsilon:
                    direction = skeleton.head(bones[i + 1]) - head
                    length = direction.length
                elif i > 0:
                    direction = head - skeleton.head(bones[i - 1])
                    length = direction.length * 0.75
                else:
                    direction = head - wrist
                    length = direction.length * 0.5
                if direction.length <= skeleton.epsilon:
                    continue
                orientations[bone] = BoneOrientation(direction, max(length, min_length), z_axis_hint=palm)

    for side, leg in smap.legs.items():
        hip, knee, ankle = skeleton.head(leg.upper), skeleton.head(leg.lower), skeleton.head(leg.foot)
        bend = _limb_bend_vector(hip, knee, ankle, forward)
        orientations[leg.upper] = BoneOrientation(knee - hip, (knee - hip).length, z_axis_hint=bend)
        orientations[leg.lower] = BoneOrientation(ankle - knee, (ankle - knee).length, z_axis_hint=bend)
        if leg.toe is not None:
            toe = skeleton.head(leg.toe)
            orientations[leg.foot] = BoneOrientation(toe - ankle, (toe - ankle).length, x_axis_hint=left)
            flat = _perpendicular(toe - ankle, up)
            if flat.length <= skeleton.epsilon:
                flat = forward.copy()
            toe_length = max((toe - ankle).length * 0.4, min_length)
            orientations[leg.toe] = BoneOrientation(flat, toe_length, x_axis_hint=left)
        else:
            flat = forward * (knee - ankle).length * 0.3 - up * (knee - ankle).length * 0.1
            orientations[leg.foot] = BoneOrientation(flat, flat.length, x_axis_hint=left)

    if smap.head is not None:
        head_position = skeleton.head(smap.head)
        direction = up.copy()
        length = extent * 0.1
        if smap.neck is not None and skeleton.distance(smap.neck, smap.head) > skeleton.epsilon:
            direction = head_position - skeleton.head(smap.neck)
            length = direction.length * 1.5
        # The head bone should at least reach the top of the facial bones.
        face_heights = [(skeleton.head(n) - head_position).dot(direction.normalized()) for n in face_bones]
        if face_heights:
            length = max(length, max(face_heights) * 1.25)
        orientations[smap.head] = BoneOrientation(direction, length, x_axis_hint=left)

    eye_length = extent * 0.01
    if len(smap.eyes) == 2:
        eye_length = skeleton.distance(smap.eyes['L'], smap.eyes['R']) * 0.25
    for eye in smap.eyes.values():
        orientations[eye] = BoneOrientation(forward, eye_length, x_axis_hint=left)

    if smap.jaw is not None:
        targets = [skeleton.head(n) for n in smap.face.lower_mouth] or \
                  [skeleton.head(n) for n in skeleton.distinct_descendants(smap.jaw)]
        jaw_head = skeleton.head(smap.jaw)
        if targets:
            target = sum(targets, Vector()) / len(targets)
            if (target - jaw_head).length > skeleton.epsilon:
                orientations[smap.jaw] = BoneOrientation(target - jaw_head, (target - jaw_head).length, x_axis_hint=left)

    # The root bone of a character lies flat on the ground, pointing forward.
    if smap.root is not None and smap.head is not None and smap.legs:
        orientations[smap.root] = BoneOrientation(forward, smap.height * 0.15, x_axis_hint=left)

    # Generic bones, in hierarchical order so that parents are always resolved before their children.
    face_leaf_length = extent * 0.008
    for name in skeleton.order:
        if name in orientations:
            continue
        head = skeleton.head(name)
        candidates = skeleton.distinct_descendants(name)
        non_helpers = [c for c in candidates if not _is_helper(c)]
        if non_helpers:
            candidates = non_helpers
        target: Vector | None = None
        if len(candidates) == 1:
            target = skeleton.head(candidates[0])
        elif len(candidates) > 1:
            counts = sorted(((skeleton.descendant_count(c), c) for c in candidates), reverse=True)
            if counts[0][0] >= 2 * max(counts[1][0], 1):
                target = skeleton.head(counts[0][1])
            else:
                average = sum((skeleton.head(c) for c in candidates), Vector()) / len(candidates)
                if (average - head).length > skeleton.epsilon * 10:
                    target = average
                else:
                    target = skeleton.head(counts[0][1])
        if target is not None and (target - head).length > skeleton.epsilon:
            direction = target - head
            orientations[name] = BoneOrientation(direction, direction.length, x_axis_hint=default_x_hint(direction.normalized()))
            continue
        # Leaf bones (or bones where all the descendants are at the same position).
        if name in face_bones:
            orientations[name] = BoneOrientation(forward, face_leaf_length, x_axis_hint=left)
            continue
        parent = skeleton.parent(name)
        parent_orientation = orientations.get(parent) if parent is not None else None
        if parent_orientation is not None:
            direction = parent_orientation.direction
            length = min(max(parent_orientation.length * 0.5, min_length), extent * 0.05)
        else:
            direction = up
            length = extent * 0.05
        orientations[name] = BoneOrientation(direction, length, x_axis_hint=default_x_hint(direction))

    return orientations


def _orientation_matrix(orientation: BoneOrientation) -> Matrix:
    y = orientation.direction.normalized()
    if orientation.z_axis_hint is not None:
        z = _perpendicular(orientation.z_axis_hint, y)
        if z.length < 1e-6:
            z = _perpendicular(Vector((0.0, 0.0, 1.0)) if abs(y.z) < 0.9 else Vector((1.0, 0.0, 0.0)), y)
        z.normalize()
        x = y.cross(z)
    else:
        hint = orientation.x_axis_hint if orientation.x_axis_hint is not None else Vector((1.0, 0.0, 0.0))
        x = _perpendicular(hint, y)
        if x.length < 1e-6:
            x = _perpendicular(Vector((1.0, 0.0, 0.0)) if abs(y.x) < 0.9 else Vector((0.0, 0.0, 1.0)), y)
        x.normalize()
        z = x.cross(y)
    return Matrix((x, y, z)).transposed()


def reorient_edit_bones(edit_bones, orientations: dict[str, BoneOrientation]):
    """
    Re-orients the edit bones and stores the rest orientation corrections on the bones.
    Must be called while the armature is in edit mode.
    """
    for name, orientation in orientations.items():
        edit_bone: EditBone = edit_bones[name]
        # The original orientation (taking into account any existing correction).
        original = edit_bone.matrix.to_3x3().normalized()
        existing_correction = get_bone_rest_correction(edit_bone)
        if existing_correction is not None:
            original = original @ existing_correction.to_matrix()
        head = edit_bone.head.copy()
        matrix = _orientation_matrix(orientation).to_4x4()
        matrix.translation = head
        edit_bone.length = max(orientation.length, 1e-5)
        edit_bone.matrix = matrix
        new = edit_bone.matrix.to_3x3().normalized()
        correction = (new.inverted() @ original).to_quaternion().normalized()
        if abs(correction.w) > 1.0 - 1e-9:
            # No correction needed.
            if BONE_REST_CORRECTION_PROPERTY in edit_bone:
                del edit_bone[BONE_REST_CORRECTION_PROPERTY]
        else:
            edit_bone[BONE_REST_CORRECTION_PROPERTY] = tuple(correction)


def reorient_armature_bones(context, armature_object: Object, default_length: float = 1.0) -> SkeletonMap:
    """
    Re-orients the bones of the armature so that the tails point toward the child joints.
    """
    assert context.view_layer
    armature_data = typing_cast(Armature, armature_object.data)
    previous_active = context.view_layer.objects.active
    previous_mode = armature_object.mode
    context.view_layer.objects.active = armature_object
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        skeleton = skeleton_from_edit_bones(armature_data.edit_bones)
        smap = analyze_skeleton(skeleton)
        orientations = compute_bone_orientations(skeleton, smap, default_length)
        reorient_edit_bones(armature_data.edit_bones, orientations)
    finally:
        bpy.ops.object.mode_set(mode='OBJECT' if previous_mode == 'EDIT' else previous_mode)
        context.view_layer.objects.active = previous_active
    return smap


def has_rest_corrections(armature_object: Object) -> bool:
    armature_data = typing_cast(Armature, armature_object.data)
    return any(get_bone_rest_correction(b) is not None for b in armature_data.bones)


def get_rest_correction_quaternion(bone) -> Quaternion:
    correction = get_bone_rest_correction(bone)
    return correction if correction is not None else Quaternion()

"""
Face presets: a library of facial expressions (e.g., smile, angry, blink, mouth shapes) that can be applied, blended
and keyed on the face of a character.

Presets are created from PSA animations (e.g., the facial animations of a game), where the most expressive frame of
each sequence is used, or from the current pose of the face. Each preset is stored as a single-frame action that only
contains the facial bones. The actions are also marked as pose assets, so they can be used with Blender's Pose
Library as well.
"""
import math
import re
from pathlib import Path
from typing import cast as typing_cast

import bpy
from bpy.types import Action, Armature, Context, Object
from mathutils import Quaternion, Vector

from ..shared.compat import action_ensure_channelbag_for_slot, action_ensure_slot, channelbag_ensure_fcurve
from ..shared.helpers import is_rig_bone
from .orientation import skeleton_from_bones
from .skeleton import analyze_skeleton

FACE_PRESET_PROPERTY = 'psk_face_preset'
FACE_PRESET_PREFIX = 'Face: '
_TEMPORARY_ACTION_PREFIX = '__psk_face_preset_tmp__'


def get_face_bones(armature_object: Object) -> list[str]:
    """Returns the names of the facial deform bones (brows, lids, eyes, mouth, jaw, tongue, etc.)."""
    armature_data = typing_cast(Armature, armature_object.data)
    skeleton = skeleton_from_bones(armature_data.bones)
    smap = analyze_skeleton(skeleton)
    names = set(smap.face.all_bones) | set(smap.eyes.values())
    if smap.jaw:
        names.add(smap.jaw)
    for name in list(names):
        names.update(skeleton.descendants(name))
    # Hierarchical order.
    return [b.name for b in armature_data.bones if b.name in names and not is_rig_bone(b)]


def get_face_presets() -> list[Action]:
    return [a for a in bpy.data.actions if a.get(FACE_PRESET_PROPERTY)]


def _read_bone_channels(action: Action, bone_names: list[str]):
    """Returns {bone: {(property, index): fcurve}} for the pose bone channels in the action."""
    result: dict[str, dict[tuple[str, int], bpy.types.FCurve]] = {}
    names = set(bone_names)
    for layer in action.layers:
        for strip in layer.strips:
            for channelbag in strip.channelbags:
                for fcurve in channelbag.fcurves:
                    match = re.match(r'pose\.bones\["(.+)"\]\.(location|rotation_quaternion|scale)$', fcurve.data_path)
                    if match and match.group(1) in names:
                        result.setdefault(match.group(1), {})[(match.group(2), fcurve.array_index)] = fcurve
    return result


def _evaluate(channels: dict, frame: float) -> tuple[Vector, Quaternion, Vector]:
    def value(prop, index, default):
        fcurve = channels.get((prop, index))
        return fcurve.evaluate(frame) if fcurve is not None else default
    location = Vector([value('location', i, 0.0) for i in range(3)])
    rotation = Quaternion([value('rotation_quaternion', i, d) for i, d in enumerate((1.0, 0.0, 0.0, 0.0))])
    scale = Vector([value('scale', i, 1.0) for i in range(3)])
    return location, rotation.normalized(), scale


def _find_peak_frame(channels: dict, start: int, end: int, face_unit: float) -> int:
    """Returns the frame where the face differs the most from its rest pose."""
    import numpy as np
    frames = np.arange(start, end + 1, dtype=np.float64)
    total = np.zeros(len(frames))

    def sample(bone_channels, prop, index, default):
        fcurve = bone_channels.get((prop, index))
        if fcurve is None or len(fcurve.keyframe_points) == 0:
            return np.full(len(frames), default)
        co = np.empty(len(fcurve.keyframe_points) * 2)
        fcurve.keyframe_points.foreach_get('co', co)
        return np.interp(frames, co[0::2], co[1::2])

    for bone_channels in channels.values():
        location = np.stack([sample(bone_channels, 'location', i, 0.0) for i in range(3)])
        total += np.linalg.norm(location, axis=0) / face_unit
        q = np.stack([sample(bone_channels, 'rotation_quaternion', i, d) for i, d in enumerate((1.0, 0.0, 0.0, 0.0))])
        w = np.abs(q[0]) / np.maximum(np.linalg.norm(q, axis=0), 1e-12)
        total += 2.0 * np.arccos(np.clip(w, 0.0, 1.0))
        total += sum(np.abs(sample(bone_channels, 'scale', i, 1.0) - 1.0) for i in range(3))
    return int(frames[int(np.argmax(total))])


def _pose_deviation(location: Vector, rotation: Quaternion, scale: Vector, face_unit: float) -> float:
    angle = rotation.angle if rotation.angle <= math.pi else 2.0 * math.pi - rotation.angle
    return location.length / face_unit + angle + sum(abs(s - 1.0) for s in scale)


def write_face_preset(armature_object: Object, name: str, pose: dict[str, tuple[Vector, Quaternion, Vector]]) -> Action:
    """Creates (or replaces) a face preset action from the given local bone transforms."""
    action_name = FACE_PRESET_PREFIX + name
    action = bpy.data.actions.get(action_name)
    if action is not None:
        for layer in list(action.layers):
            action.layers.remove(layer)
    else:
        action = bpy.data.actions.new(action_name)
    slot = action_ensure_slot(action, 'OBJECT', armature_object.name)
    channelbag = action_ensure_channelbag_for_slot(action, slot)
    for bone, (location, rotation, scale) in pose.items():
        for prop, values in (('location', location), ('rotation_quaternion', rotation), ('scale', scale)):
            for index, value in enumerate(values):
                fcurve = channelbag_ensure_fcurve(channelbag, f'pose.bones["{bone}"].{prop}', index=index, group_name=bone)
                fcurve.keyframe_points.insert(0.0, value, options={'FAST'})
    action[FACE_PRESET_PROPERTY] = True
    action.use_fake_user = True
    try:
        action.asset_mark()
        action.asset_data.tags.new('Face', skip_if_exists=True)
    except (AttributeError, RuntimeError):
        pass
    return action


def import_face_presets(context: Context, armature_object: Object, filepaths: list[str], frame_mode: str = 'PEAK') -> list[Action]:
    """
    Creates a face preset for each sequence in the PSA files. `frame_mode` is one of 'PEAK' (the most expressive
    frame), 'FIRST', 'MIDDLE' or 'LAST'.
    """
    from psk_psa_py.psa.reader import PsaReader
    from ..psa.importer import PsaImportOptions, import_psa

    face_bones = get_face_bones(armature_object)
    if not face_bones:
        raise RuntimeError('No facial bones were found in the armature')
    armature_data = typing_cast(Armature, armature_object.data)
    heads = [armature_data.bones[n].head_local for n in face_bones]
    face_unit = max(max((h - heads[0]).length for h in heads) * 0.1, 1e-6)

    presets = []
    for filepath in filepaths:
        with PsaReader.from_path(filepath) as reader:
            sequence_names = list(reader.sequences.keys())
            options = PsaImportOptions(sequence_names=sequence_names, should_bake_to_rig=False,
                                       should_use_fake_user=False, should_write_metadata=False,
                                       action_name_prefix=_TEMPORARY_ACTION_PREFIX)
            import_psa(context, reader, armature_object, options)
        for sequence_name in sequence_names:
            action = bpy.data.actions.get(_TEMPORARY_ACTION_PREFIX + sequence_name)
            if action is None:
                continue
            try:
                channels = _read_bone_channels(action, face_bones)
                if not channels:
                    continue
                start, end = (int(round(x)) for x in action.frame_range)
                match frame_mode:
                    case 'FIRST':
                        frame = start
                    case 'LAST':
                        frame = end
                    case 'MIDDLE':
                        frame = (start + end) // 2
                    case _:
                        frame = _find_peak_frame(channels, start, end, face_unit)
                pose = {bone: _evaluate(channels[bone], frame) for bone in face_bones if bone in channels}
                # Files with a single sequence are named after the file (sequence names are not always unique).
                name = Path(filepath).stem if len(sequence_names) == 1 else sequence_name
                presets.append(write_face_preset(armature_object, name, pose))
            finally:
                bpy.data.actions.remove(action)
    return presets


def read_face_preset(action: Action, armature_object: Object) -> dict[str, tuple[Vector, Quaternion, Vector]]:
    pose_bones = armature_object.pose.bones
    channels = _read_bone_channels(action, [pb.name for pb in pose_bones])
    return {bone: _evaluate(c, 0.0) for bone, c in channels.items()}


def _key_bones(armature_object: Object, names):
    for name in names:
        pose_bone = armature_object.pose.bones[name]
        pose_bone.keyframe_insert('location', group=name)
        pose_bone.keyframe_insert('rotation_quaternion', group=name)
        pose_bone.keyframe_insert('scale', group=name)


def apply_face_preset(armature_object: Object, action: Action, factor: float = 1.0, insert_keys: bool = False) -> int:
    """Blends the face toward the preset by `factor`. Returns the number of bones affected."""
    pose = read_face_preset(action, armature_object)
    for name, (location, rotation, scale) in pose.items():
        pose_bone = armature_object.pose.bones.get(name)
        if pose_bone is None:
            continue
        pose_bone.rotation_mode = 'QUATERNION'
        pose_bone.location = pose_bone.location.lerp(location, factor)
        current = pose_bone.rotation_quaternion.normalized()
        if current.dot(rotation) < 0.0:
            rotation = -rotation
        pose_bone.rotation_quaternion = current.slerp(rotation, factor)
        pose_bone.scale = pose_bone.scale.lerp(scale, factor)
    if insert_keys:
        _key_bones(armature_object, [n for n in pose if n in armature_object.pose.bones])
    return len(pose)


def reset_face(armature_object: Object, insert_keys: bool = False):
    names = [n for n in get_face_bones(armature_object) if n in armature_object.pose.bones]
    for name in names:
        pose_bone = armature_object.pose.bones[name]
        pose_bone.location = (0.0, 0.0, 0.0)
        pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pose_bone.scale = (1.0, 1.0, 1.0)
    if insert_keys:
        _key_bones(armature_object, names)


def face_preset_from_pose(armature_object: Object, name: str) -> Action:
    pose = {}
    for bone in get_face_bones(armature_object):
        pose_bone = armature_object.pose.bones[bone]
        pose[bone] = (pose_bone.location.copy(), pose_bone.rotation_quaternion.normalized(), pose_bone.scale.copy())
    return write_face_preset(armature_object, name, pose)

from typing import Sequence, Iterable, cast as typing_cast

import bpy
import numpy as np
import re
from bpy.types import Armature, Context, FCurve, Object, Bone, PoseBone
from mathutils import Vector, Quaternion

from ..shared.types import PSX_PG_action_export
from ..shared.compat import action_ensure_channelbag_for_slot, action_ensure_slot, channelbag_ensure_fcurve
from ..shared.helpers import get_bone_rest_correction, get_bone_original_matrix_local, is_rig_bone

from ..rig.builder import get_rig_info, has_rig, set_rig_property
from ..rig.bake import bake_fk_to_ik, get_action_frame_range
from .config import PsaConfig, REMOVE_TRACK_LOCATION, REMOVE_TRACK_ROTATION
from psk_psa_py.psa.reader import PsaReader
from psk_psa_py.shared.data import PsxBone

class BoneMapping:
    def __init__(self,
                 is_case_sensitive: bool = False,
                 should_ignore_trailing_whitespace: bool = True
                 ):
        self.is_case_sensitive = is_case_sensitive
        # Ancient PSK and PSA exporters would, for some reason, pad the bone names with spaces
        # instead of just writing null bytes, probably because the programmers were lazy.
        # By default, we will ignore trailing whitespace when doing comparisons.
        self.should_ignore_trailing_whitespace = should_ignore_trailing_whitespace


class PsaImportOptions:
    def __init__(self,
                 action_name_prefix: str = '',
                 bone_mapping: BoneMapping = BoneMapping(),
                 fps_custom: float = 30.0,
                 fps_source: str = 'SEQUENCE',
                 psa_config: PsaConfig = PsaConfig(),
                 sequence_names: list[str] | None = None,
                 should_convert_to_samples: bool = False,
                 should_overwrite: bool = False,
                 should_stash: bool = False,
                 should_use_config_file: bool = True,
                 should_use_fake_user: bool = False,
                 should_write_keyframes: bool = True,
                 should_write_metadata: bool = True,
                 should_write_scale_keys: bool = True,
                 translation_scale: float = 1.0,
                 should_bake_to_rig: bool = True,
                 ):
        self.action_name_prefix = action_name_prefix
        self.bone_mapping = bone_mapping
        self.fps_custom = fps_custom
        self.fps_source = fps_source
        self.psa_config = psa_config
        self.sequence_names = sequence_names if sequence_names is not None else []
        self.should_convert_to_samples = should_convert_to_samples
        self.should_overwrite = should_overwrite
        self.should_stash = should_stash
        self.should_use_config_file = should_use_config_file
        self.should_use_fake_user = should_use_fake_user
        self.should_write_keyframes = should_write_keyframes
        self.should_write_metadata = should_write_metadata
        self.should_write_scale_keys = should_write_scale_keys
        self.translation_scale = translation_scale
        # If the armature has an "Animate Ready" control rig, key the IK & eye controls to match the imported
        # animation.
        self.should_bake_to_rig = should_bake_to_rig


class ImportBone:
    def __init__(self, psa_bone: PsxBone):
        self.psa_bone: PsxBone = psa_bone
        self.parent: ImportBone | None = None
        self.armature_bone: Bone | None = None
        self.pose_bone: PoseBone | None = None
        self.original_location: Vector = Vector()
        self.original_rotation: Quaternion = Quaternion()
        self.post_rotation: Quaternion = Quaternion()
        # Rest orientation correction of the armature bone (see `get_bone_rest_correction`), or None.
        self.rest_correction: Quaternion | None = None
        self.fcurves: list[FCurve] = []


def _calculate_fcurve_data(import_bone: ImportBone, key_data: Sequence[float]):
    # Convert world-space transforms to local-space transforms.
    key_rotation = Quaternion(key_data[0:4])
    key_location = Vector(key_data[4:7])
    q = import_bone.post_rotation.copy()
    q.rotate(import_bone.original_rotation)
    rotation = q
    q = import_bone.post_rotation.copy()
    if import_bone.parent is None:
        q.rotate(key_rotation.conjugated())
    else:
        q.rotate(key_rotation)
    rotation.rotate(q.conjugated())
    location = key_location - import_bone.original_location
    location.rotate(import_bone.post_rotation.conjugated())
    scale = Vector(key_data[7:10])
    if import_bone.rest_correction is not None:
        # The armature bone has been re-oriented (e.g., to fix the bone tails) relative to the orientation stored in
        # the file. Convert the pose-space transform from the original bone basis to the re-oriented bone basis.
        c = import_bone.rest_correction
        rotation = c @ rotation @ c.conjugated()
        location = c @ location
        m = c.to_matrix()
        scale = Vector(sum(m[i][j] ** 2 * scale[j] for j in range(3)) for i in range(3))
    return rotation.w, rotation.x, rotation.y, rotation.z, location.x, location.y, location.z, scale.x, scale.y, scale.z


class PsaImportResult:
    def __init__(self):
        self.warnings: list[str] = []


def _get_armature_bone_index_for_psa_bone(psa_bone_name: str, armature_bone_names: list[str], bone_mapping: BoneMapping) -> int | None:
    """
    @param psa_bone_name: The name of the PSA bone.
    @param armature_bone_names: The names of the bones in the armature.
    @param bone_mapping: Bone mapping information.
    @return: The index of the armature bone that corresponds to the given PSA bone, or None if no such bone exists.
    """
    # Use regular expressions for bone name matching.
    pattern = psa_bone_name
    flags = 0

    if bone_mapping.should_ignore_trailing_whitespace:
        psa_bone_name = psa_bone_name.rstrip()
        pattern += r'\s*'
    
    if not bone_mapping.is_case_sensitive:
        flags = re.IGNORECASE
    
    pattern = re.compile(pattern, flags)

    for armature_bone_index, armature_bone_name in enumerate(armature_bone_names):
        if re.fullmatch(pattern, armature_bone_name):
            return armature_bone_index

    return None


def _resample_sequence_data_matrix(sequence_data_matrix: np.ndarray, frame_step: float = 1.0) -> np.ndarray:
    """
    Resamples the sequence data matrix to the target frame count.

    @param sequence_data_matrix: FxBx7 matrix where F is the number of frames, B is the number of bones, and X is the
    number of data elements per bone.
    @param frame_step: The step between frames in the resampled sequence.
    @return: The resampled sequence data matrix, or sequence_data_matrix if no resampling is necessary.
    """

    def _get_sample_frame_times(source_frame_count: int, frame_step: float) -> Iterable[float]:
        # TODO: for correctness, we should also emit the target frame time as well (because the last frame can be a
        #  fractional frame).
        assert frame_step > 0.0, 'Frame step must be greater than 0'
        time = 0.0
        while time < source_frame_count - 1:
            yield time
            time += frame_step
        yield source_frame_count - 1

    if frame_step == 1.0:
        # No resampling is necessary.
        return sequence_data_matrix

    source_frame_count, bone_count = sequence_data_matrix.shape[:2]
    sample_frame_times = list(_get_sample_frame_times(source_frame_count, frame_step))
    target_frame_count = len(sample_frame_times)
    resampled_sequence_data_matrix = np.zeros((target_frame_count, bone_count, 10), dtype=float)

    for sample_frame_index, sample_frame_time in enumerate(sample_frame_times):
        frame_index = int(sample_frame_time)
        if sample_frame_time % 1.0 == 0.0:
            # Sample time has no fractional part, so just copy the frame.
            resampled_sequence_data_matrix[sample_frame_index, :, :] = sequence_data_matrix[frame_index, :, :]
        else:
            # Sample time has a fractional part, so interpolate between two frames.
            next_frame_index = frame_index + 1
            for bone_index in range(bone_count):
                source_frame_1_data = sequence_data_matrix[frame_index, bone_index, :]
                source_frame_2_data = sequence_data_matrix[next_frame_index, bone_index, :]
                factor = sample_frame_time - frame_index
                q = Quaternion((source_frame_1_data[:4])).slerp(Quaternion((source_frame_2_data[:4])), factor)
                q.normalize()
                l = Vector(source_frame_1_data[4:7]).lerp(Vector(source_frame_2_data[4:7]), factor)
                s = Vector(source_frame_1_data[7:]).lerp(Vector(source_frame_2_data[4:7]), factor)
                resampled_sequence_data_matrix[sample_frame_index, bone_index, :] = q.w, q.x, q.y, q.z, l.x, l.y, l.z, s.x, s.y, s.z

    return resampled_sequence_data_matrix


def _read_sequence_data_matrix(psa_reader: PsaReader, sequence_name: str) -> np.ndarray:
    """
    Reads and returns the data matrix for the given sequence.
    The order of the data in the third axis is Qw, Qx, Qy, Qz, Lx, Ly, Lz, Sx, Sy, Sz
    
    @param sequence_name: The name of the sequence.
    @return: An FxBx10 matrix where F is the number of frames, B is the number of bones.
    """
    sequence = psa_reader.sequences[sequence_name]
    keys = psa_reader.read_sequence_keys(sequence_name)
    bone_count = len(psa_reader.bones)
    matrix_size = sequence.frame_count, bone_count, 10
    matrix = np.ones(matrix_size)
    keys_iter = iter(keys)
    # Populate rotation and location data.
    for frame_index in range(sequence.frame_count):
        for bone_index in range(bone_count):
            matrix[frame_index, bone_index, :7] = list(next(keys_iter).data)
    # Populate scale data, if it exists.
    scale_keys = psa_reader.read_sequence_scale_keys(sequence_name)
    if len(scale_keys) > 0:
        scale_keys_iter = iter(scale_keys)
        for frame_index in range(sequence.frame_count):
            for bone_index in range(bone_count):
                matrix[frame_index, bone_index, 7:] = list(next(scale_keys_iter).data)
    return matrix


def import_psa(context: Context, psa_reader: PsaReader, armature_object: Object, options: PsaImportOptions) -> PsaImportResult:

    assert context.window_manager

    result = PsaImportResult()
    sequences = [psa_reader.sequences[x] for x in options.sequence_names]
    armature_data = typing_cast(Armature, armature_object.data)

    # Create an index mapping from bones in the PSA to bones in the target armature.
    psa_to_armature_bone_indices = {}
    armature_to_psa_bone_indices = {}
    # Rig control & mechanism bones (see the "Animate Ready" import option) are never mapped to PSA bones.
    armature_bone_names = [x.name if not is_rig_bone(x) else '' for x in armature_data.bones]
    psa_bone_names = []
    duplicate_mappings = []

    for psa_bone_index, psa_bone in enumerate(psa_reader.bones):
        psa_bone_name: str = psa_bone.name.decode('windows-1252')
        armature_bone_index = _get_armature_bone_index_for_psa_bone(psa_bone_name, armature_bone_names, options.bone_mapping)
        if armature_bone_index is not None:
            # Ensure that no other PSA bone has been mapped to this armature bone yet.
            if armature_bone_index not in armature_to_psa_bone_indices:
                psa_to_armature_bone_indices[psa_bone_index] = armature_bone_index
                armature_to_psa_bone_indices[armature_bone_index] = psa_bone_index
            else:
                # This armature bone has already been mapped to a PSA bone.
                duplicate_mappings.append((psa_bone_index, armature_bone_index, armature_to_psa_bone_indices[armature_bone_index]))
            psa_bone_names.append(armature_bone_names[armature_bone_index])
        else:
            psa_bone_names.append(psa_bone_name)

    # Warn about duplicate bone mappings.
    if len(duplicate_mappings) > 0:
        for (psa_bone_index, armature_bone_index, mapped_psa_bone_index) in duplicate_mappings:
            psa_bone_name = psa_bone_names[psa_bone_index]
            armature_bone_name = armature_bone_names[armature_bone_index]
            mapped_psa_bone_name = psa_bone_names[mapped_psa_bone_index]
            result.warnings.append(f'PSA bone {psa_bone_index} ({psa_bone_name}) could not be mapped to armature bone {armature_bone_index} ({armature_bone_name}) because the armature bone is already mapped to PSA bone {mapped_psa_bone_index} ({mapped_psa_bone_name})')

    # Report if there are missing bones in the target armature.
    missing_bone_names = set(psa_bone_names).difference(set(armature_bone_names))
    missing_bone_names.discard('')
    if len(missing_bone_names) > 0:
        result.warnings.append(
            f'The armature \'{armature_object.name}\' is missing {len(missing_bone_names)} bones that exist in '
            'the PSA:\n' +
            str(list(sorted(missing_bone_names)))
        )
    del armature_bone_names

    assert armature_object.pose

    # Create intermediate bone data for import operations.
    import_bones = []
    psa_bone_names_to_import_bones = dict()

    for (psa_bone_index, psa_bone), psa_bone_name in zip(enumerate(psa_reader.bones), psa_bone_names):
        if psa_bone_index not in psa_to_armature_bone_indices:
            # PSA bone does not map to armature bone, skip it and leave an empty bone in its place.
            import_bones.append(None)
            continue
        import_bone = ImportBone(psa_bone)
        import_bone.armature_bone = armature_data.bones[psa_bone_name]
        import_bone.pose_bone = armature_object.pose.bones[psa_bone_name]
        psa_bone_names_to_import_bones[psa_bone_name] = import_bone
        import_bones.append(import_bone)

    bones_with_missing_parents = []

    for import_bone in filter(lambda x: x is not None, import_bones):
        armature_bone = import_bone.armature_bone
        has_parent = armature_bone.parent is not None
        if has_parent:
            if armature_bone.parent.name in psa_bone_names:
                import_bone.parent = psa_bone_names_to_import_bones[armature_bone.parent.name]
            else:
                # Add a warning if the parent bone is not in the PSA.
                bones_with_missing_parents.append(armature_bone)
        # Calculate the original location & rotation of each bone (in world-space maybe?)
        # NOTE: The *original* rest matrices are used here (i.e., the orientation of the bones as they were in the
        # PSK), so that re-oriented bones are handled correctly.
        matrix_local = get_bone_original_matrix_local(armature_bone)
        if has_parent:
            parent_matrix_local = get_bone_original_matrix_local(armature_bone.parent)
            import_bone.original_location = matrix_local.translation - parent_matrix_local.translation
            import_bone.original_location.rotate(parent_matrix_local.to_quaternion().conjugated())
            import_bone.original_rotation = matrix_local.to_quaternion()
            import_bone.original_rotation.rotate(parent_matrix_local.to_quaternion().conjugated())
            import_bone.original_rotation.conjugate()
        else:
            import_bone.original_location = matrix_local.translation.copy()
            import_bone.original_rotation = matrix_local.to_quaternion().conjugated()

        import_bone.rest_correction = get_bone_rest_correction(armature_bone)

        import_bone.post_rotation = import_bone.original_rotation.conjugated()

    # Warn about bones with missing parents.
    if len(bones_with_missing_parents) > 0:
        count = len(bones_with_missing_parents)
        message = f'{count} bone(s) have parents that are not present in the PSA:\n' + str([x.name for x in bones_with_missing_parents])
        result.warnings.append(message)

    context.window_manager.progress_begin(0, len(sequences))

    # Create and populate the data for new sequences.
    actions = []
    for sequence_index, sequence in enumerate(sequences):
        # Add the action.
        sequence_name = sequence.name.decode('windows-1252')
        action_name = options.action_name_prefix + sequence_name

        # Get the bone track flags for this sequence, or an empty dictionary if none exist.
        sequence_bone_track_flags = dict()
        if sequence_name in options.psa_config.sequence_bone_flags.keys():
            sequence_bone_track_flags = options.psa_config.sequence_bone_flags[sequence_name]

        if options.should_overwrite and action_name in bpy.data.actions:
            action = bpy.data.actions[action_name]
        else:
            action = bpy.data.actions.new(name=action_name)

        action_slot = action_ensure_slot(action, 'OBJECT', armature_object.name)

        assert action_slot

        # Calculate the target FPS.
        match options.fps_source:
            case 'CUSTOM':
                target_fps = options.fps_custom
            case 'SCENE':
                assert context.scene
                target_fps = context.scene.render.fps
            case 'SEQUENCE':
                target_fps = sequence.fps
            case _:
                assert False, f'Invalid FPS source: {options.fps_source}'

        if options.should_write_keyframes:            
            channelbag = action_ensure_channelbag_for_slot(action, action_slot)

            # Create f-curves for the rotation and location of each bone.
            for psa_bone_index, armature_bone_index in psa_to_armature_bone_indices.items():
                bone_track_flags = sequence_bone_track_flags.get(psa_bone_index, 0)
                import_bone = import_bones[psa_bone_index]
                pose_bone = import_bone.pose_bone
                rotation_data_path = pose_bone.path_from_id('rotation_quaternion')
                location_data_path = pose_bone.path_from_id('location')
                scale_data_path = pose_bone.path_from_id('scale')
                add_rotation_fcurves = (bone_track_flags & REMOVE_TRACK_ROTATION) == 0
                add_location_fcurves = (bone_track_flags & REMOVE_TRACK_LOCATION) == 0
                add_scale_fcurves = psa_reader.has_scale_keys and options.should_write_scale_keys
                import_bone.fcurves = [
                    channelbag_ensure_fcurve(channelbag, rotation_data_path, index=0, group_name=pose_bone.name) if add_rotation_fcurves else None,  # Qw
                    channelbag_ensure_fcurve(channelbag, rotation_data_path, index=1, group_name=pose_bone.name) if add_rotation_fcurves else None,  # Qx
                    channelbag_ensure_fcurve(channelbag, rotation_data_path, index=2, group_name=pose_bone.name) if add_rotation_fcurves else None,  # Qy
                    channelbag_ensure_fcurve(channelbag, rotation_data_path, index=3, group_name=pose_bone.name) if add_rotation_fcurves else None,  # Qz
                    channelbag_ensure_fcurve(channelbag, location_data_path, index=0, group_name=pose_bone.name) if add_location_fcurves else None,  # Lx
                    channelbag_ensure_fcurve(channelbag, location_data_path, index=1, group_name=pose_bone.name) if add_location_fcurves else None,  # Ly
                    channelbag_ensure_fcurve(channelbag, location_data_path, index=2, group_name=pose_bone.name) if add_location_fcurves else None,  # Lz
                    channelbag_ensure_fcurve(channelbag, scale_data_path, index=0, group_name=pose_bone.name) if add_scale_fcurves    else None,  # Sx
                    channelbag_ensure_fcurve(channelbag, scale_data_path, index=1, group_name=pose_bone.name) if add_scale_fcurves    else None,  # Sy
                    channelbag_ensure_fcurve(channelbag, scale_data_path, index=2, group_name=pose_bone.name) if add_scale_fcurves    else None,  # Sz
                ]

            # Read the sequence data matrix from the PSA.
            sequence_data_matrix = _read_sequence_data_matrix(psa_reader, sequence_name)

            if options.translation_scale != 1.0:
                # Scale the translation data.
                sequence_data_matrix[:, :, 4:7] *= options.translation_scale

            # Convert the sequence's data from world-space to local-space.
            for bone_index, import_bone in enumerate(import_bones):
                if import_bone is None:
                    continue
                for frame_index in range(sequence.frame_count):
                    # This bone has writeable keyframes for this frame.
                    key_data = sequence_data_matrix[frame_index, bone_index]
                    # Calculate the local-space key data for the bone.
                    sequence_data_matrix[frame_index, bone_index] = _calculate_fcurve_data(import_bone, key_data)

            # Resample the sequence data to the target FPS.
            # If the target frame count is the same as the source frame count, this will be a no-op.
            resampled_sequence_data_matrix = _resample_sequence_data_matrix(sequence_data_matrix,
                                                                            frame_step=sequence.fps / target_fps)

            # Write the keyframes out.
            # Note that the f-curve data consists of alternating time and value data.
            target_frame_count = resampled_sequence_data_matrix.shape[0]
            fcurve_data = np.zeros(2 * target_frame_count, dtype=float)
            fcurve_data[0::2] = range(0, target_frame_count)

            for bone_index, import_bone in enumerate(import_bones):
                if import_bone is None:
                    continue
                for fcurve_index, fcurve in enumerate(import_bone.fcurves):
                    if fcurve is None:
                        continue
                    fcurve.keyframe_points.clear()
                    fcurve_data[1::2] = resampled_sequence_data_matrix[:, bone_index, fcurve_index]
                    fcurve.keyframe_points.add(target_frame_count)
                    fcurve.keyframe_points.foreach_set('co', fcurve_data)
                    for fcurve_keyframe in fcurve.keyframe_points:
                        fcurve_keyframe.interpolation = 'LINEAR'

            if options.should_convert_to_samples:
                # Bake the curve to samples.
                for fcurve in channelbag.fcurves:
                    fcurve.convert_to_samples(start=0, end=sequence.frame_count)

        # Write meta-data.
        if options.should_write_metadata:
            pg = typing_cast(PSX_PG_action_export, getattr(action, 'psa_export'))
            pg.fps = target_fps
            pg.group = sequence.group.decode('windows-1252', errors='ignore')

        action.use_fake_user = options.should_use_fake_user

        actions.append(action)

        context.window_manager.progress_update(sequence_index)

    # If the armature has an "Animate Ready" control rig, bake the imported (FK) animation onto the IK & eye controls
    # so that the animation looks exactly the same with the controls enabled.
    if options.should_bake_to_rig and options.should_write_keyframes and has_rig(armature_object) and actions:
        animation_data = armature_object.animation_data or armature_object.animation_data_create()
        saved_action = animation_data.action
        saved_slot = animation_data.action_slot if saved_action is not None else None
        # Keep the current state of the IK/FK switches (the controls are baked regardless, so that switching to IK
        # later on also works).
        rig_info = get_rig_info(armature_object) or {}
        saved_switches = {p: armature_object[p] for p in rig_info.get('properties', [])
                          if p in armature_object and not p.startswith('foot_roll_')}
        try:
            for action in actions:
                animation_data.action = action
                animation_data.action_slot = action_ensure_slot(action, 'OBJECT', armature_object.name)
                frame_start, frame_end = get_action_frame_range(action)
                bake_fk_to_ik(context, armature_object, frame_start, frame_end)
        except RuntimeError as e:
            result.warnings.append(f'Failed to bake the animation to the rig controls: {e}')
        finally:
            animation_data.action = saved_action
            if saved_slot is not None:
                animation_data.action_slot = saved_slot
            for prop, value in saved_switches.items():
                set_rig_property(armature_object, prop, value)

    # If the user specifies, store the new animations as strips on a non-contributing NLA track.
    if options.should_stash:
        animation_data = armature_object.animation_data
        if animation_data is None:
            animation_data = armature_object.animation_data_create()
        for action in actions:
            nla_track = animation_data.nla_tracks.new()
            nla_track.name = action.name
            nla_track.mute = True
            nla_track.strips.new(name=action.name, start=0, action=action)

    context.window_manager.progress_end()

    return result

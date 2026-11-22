"""
Tests for the "Fix Bone Tails" (bone re-orientation) and "Animate Ready" (control rig) import options, and for the
automatic material setup.
"""
import json
import os
from pathlib import Path

import bpy
import pytest
from mathutils import Matrix, Vector

from psk_psa_py.psk.reader import read_psk_from_file
from psk_psa_py.psa.reader import PsaReader

SHREK_PSK_FILEPATH = 'tests/data/Shrek.psk'
SHREK_PSA_FILEPATH = 'tests/data/Shrek.psa'
SARGE_FILEPATH = 'tests/data/CS_Sarge_S0_Skelmesh.pskx'
UE_MANNEQUIN_FILEPATH = 'tests/data/SK_1033_1033001.pskx'
SWORD_FILEPATH = 'tests/data/WEP_BroadSword_SKEL.psk'
TEST_MODEL_DIRECTORY = 'Player/Pl13'
TEST_MODEL_PSK = os.path.join(TEST_MODEL_DIRECTORY, 'Model', 'Pl13_101', 'Mesh', 'SK_pl13_101', 'SK_Pl13_101.psk')
TEST_MODEL_ANIMATIONS = os.path.join(TEST_MODEL_DIRECTORY, 'Animation')
UR_MODEL_PSK = 'Player/Ch013/Model/Default/Mesh/SK_Ch013_Default_00.psk'


@pytest.fixture(autouse=True)
def run_before_and_after_tests(tmpdir):
    bpy.ops.wm.read_homefile(app_template='')
    yield


def _import_psk(filepath: str, **kwargs):
    before = set(bpy.data.objects)
    assert bpy.ops.psk.import_file(filepath=filepath, **kwargs) == {'FINISHED'}
    new_objects = set(bpy.data.objects) - before
    armature_object = next(o for o in new_objects if o.type == 'ARMATURE')
    return armature_object


def _import_sequence(armature_object, psa_filepath: str, sequence_name: str, should_bake_to_rig: bool = True):
    from io_scene_psk_psa.psa.importer import PsaImportOptions, import_psa
    options = PsaImportOptions(sequence_names=[sequence_name], should_bake_to_rig=should_bake_to_rig)
    options.action_name_prefix = f'{armature_object.name}_'
    with PsaReader.from_path(psa_filepath) as psa_reader:
        import_psa(bpy.context, psa_reader, armature_object, options)
    action = bpy.data.actions[options.action_name_prefix + sequence_name]
    animation_data = armature_object.animation_data or armature_object.animation_data_create()
    animation_data.action = action
    animation_data.action_slot = action.slots[0]
    return action


def _first_sequence_name(psa_filepath: str) -> str:
    with PsaReader.from_path(psa_filepath) as psa_reader:
        return next(iter(psa_reader.sequences.keys()))


def _deform_bones(armature_object):
    return [b.name for b in armature_object.data.bones if not b.get('psk_rig')]


def _skinning_matrices(armature_object) -> dict[str, Matrix]:
    return {pb.name: pb.matrix @ pb.bone.matrix_local.inverted()
            for pb in armature_object.pose.bones if not pb.bone.get('psk_rig')}


def _max_difference(a: dict[str, Matrix], b: dict[str, Matrix]) -> float:
    return max(max(abs(a[n][i][j] - b[n][i][j]) for i in range(4) for j in range(4)) for n in a)


def _export_psk_bones(armature_object):
    from io_scene_psk_psa.psk.builder import PskBuildOptions, build_psk
    from io_scene_psk_psa.shared.helpers import get_psk_input_objects_for_context
    bpy.ops.object.select_all(action='DESELECT')
    mesh_object = next(o for o in armature_object.children if o.type == 'MESH')
    mesh_object.select_set(True)
    bpy.context.view_layer.objects.active = mesh_object
    return build_psk(bpy.context, get_psk_input_objects_for_context(bpy.context), PskBuildOptions()).psk


def _quaternion_difference(q1, q2) -> float:
    q1, q2 = list(q1), list(q2)
    if sum(a * b for a, b in zip(q1, q2)) < 0.0:
        q2 = [-x for x in q2]
    return max(abs(a - b) for a, b in zip(q1, q2))


def test_fix_bone_tails_points_to_children():
    armature_object = _import_psk(SARGE_FILEPATH, should_fix_bone_orientation=True)
    bones = armature_object.data.bones
    # The tail of the upper arm should be at the elbow, etc.
    for parent, child in (('L_Shoulder', 'L_Elbow'), ('L_Elbow', 'L_Hand'), ('L_Hip', 'L_Knee'), ('L_Knee', 'L_Foot')):
        assert (bones[parent].tail_local - bones[child].head_local).length < 1e-3, f'{parent} should point to {child}'


def test_fix_bone_tails_psk_export_round_trip():
    """Exporting a PSK from an armature with re-oriented bones results in the original bone transforms."""
    armature_object = _import_psk(SHREK_PSK_FILEPATH, should_fix_bone_orientation=True)
    assert any(b.get('psk_rest_correction') for b in armature_object.data.bones)
    original = {b.name: b for b in read_psk_from_file(SHREK_PSK_FILEPATH).bones}
    exported = _export_psk_bones(armature_object)
    for bone in exported.bones:
        assert bone.name in original
        o = original[bone.name]
        assert max(abs(a - b) for a, b in zip(bone.location, o.location)) < 1e-3, bone.name
        assert _quaternion_difference(bone.rotation, o.rotation) < 1e-4, bone.name


def test_fix_bone_tails_psa_import_matches():
    """PSA animations deform the mesh identically whether the bones have been re-oriented or not."""
    sequence_name = _first_sequence_name(SHREK_PSA_FILEPATH)
    original = _import_psk(SHREK_PSK_FILEPATH, should_fix_bone_orientation=False)
    fixed = _import_psk(SHREK_PSK_FILEPATH, should_fix_bone_orientation=True)
    for armature_object in (original, fixed):
        _import_sequence(armature_object, SHREK_PSA_FILEPATH, sequence_name)
    for frame in (0, 5, 10):
        bpy.context.scene.frame_set(frame)
        assert _max_difference(_skinning_matrices(original), _skinning_matrices(fixed)) < 1e-2


def test_fix_bone_tails_psa_export_matches():
    """PSA exports are identical whether the bones have been re-oriented or not."""
    from io_scene_psk_psa.psa.builder import PsaBuildOptions, PsaBuildSequence, build_psa
    sequence_name = _first_sequence_name(SHREK_PSA_FILEPATH)
    results = []
    for fix in (False, True):
        armature_object = _import_psk(SHREK_PSK_FILEPATH, should_fix_bone_orientation=fix)
        action = _import_sequence(armature_object, SHREK_PSA_FILEPATH, sequence_name)
        options = PsaBuildOptions()
        options.armature_objects = [armature_object]
        options.animation_data = armature_object.animation_data
        sequence = PsaBuildSequence(armature_object, armature_object.animation_data)
        sequence.name = sequence_name
        sequence.nla_state.action = action
        sequence.nla_state.frame_start, sequence.nla_state.frame_end = (int(x) for x in action.frame_range)
        options.sequences = [sequence]
        results.append(build_psa(bpy.context, options))
    a, b = results
    assert len(a.keys) == len(b.keys)
    for k1, k2 in zip(a.keys, b.keys):
        assert _quaternion_difference(k1.rotation, k2.rotation) < 1e-3
        assert max(abs(x - y) for x, y in zip(k1.location, k2.location)) < 1e-2


def _build_psa(armature_object, action, sequence_name: str):
    from io_scene_psk_psa.psa.builder import PsaBuildOptions, PsaBuildSequence, build_psa
    options = PsaBuildOptions()
    options.armature_objects = [armature_object]
    options.animation_data = armature_object.animation_data
    sequence = PsaBuildSequence(armature_object, armature_object.animation_data)
    sequence.name = sequence_name
    sequence.nla_state.action = action
    sequence.nla_state.frame_start, sequence.nla_state.frame_end = (int(x) for x in action.frame_range)
    options.sequences = [sequence]
    return build_psa(bpy.context, options)


@pytest.mark.parametrize('should_fix_bone_orientation', [False, True])
def test_psa_export_scale_keys_round_trip(tmp_path, should_fix_bone_orientation):
    """Scaled bones are exported as scale keys, and importing the PSA again gives the same animation."""
    from psk_psa_py.psa.writer import write_psa_to_file
    sequence_name = _first_sequence_name(SHREK_PSA_FILEPATH)
    armature_object = _import_psk(SHREK_PSK_FILEPATH, should_fix_bone_orientation=should_fix_bone_orientation)
    action = _import_sequence(armature_object, SHREK_PSA_FILEPATH, sequence_name)
    frame_start, frame_end = (int(x) for x in action.frame_range)

    # Without scaled bones, no scale keys are written.
    assert len(_build_psa(armature_object, action, sequence_name).scale_keys) == 0

    # Scale an arm (animated) & the bone above it.
    pose = armature_object.pose.bones
    upper = pose['body_l_shoulder_joint']
    for pose_bone, frame, scale in ((upper, frame_start, 1.0), (upper, frame_end, 1.6),
                                    (upper.children[0], frame_start, 0.7), (upper.parent, frame_start, 1.25)):
        pose_bone.scale = (scale, scale, scale)
        pose_bone.keyframe_insert('scale', frame=frame)
    psa = _build_psa(armature_object, action, sequence_name)
    assert len(psa.scale_keys) == len(psa.keys)
    index = [b.name.decode('windows-1252') for b in psa.bones].index(upper.name)
    last_frame_scale = list(psa.scale_keys[len(psa.keys) - len(psa.bones) + index].data)
    assert max(abs(x - 1.6) for x in last_frame_scale) < 1e-3

    filepath = str(tmp_path / 'scaled.psa')
    write_psa_to_file(psa, filepath, write_scale_keys=True)
    reimported_object = _import_psk(SHREK_PSK_FILEPATH, should_fix_bone_orientation=should_fix_bone_orientation)
    _import_sequence(reimported_object, filepath, sequence_name)
    for frame in range(frame_start, frame_end + 1, max(1, (frame_end - frame_start) // 4)):
        bpy.context.scene.frame_set(frame)
        assert _max_difference(_skinning_matrices(armature_object), _skinning_matrices(reimported_object)) < 1e-2, f'Frame {frame}'


@pytest.mark.parametrize('filepath', [SHREK_PSK_FILEPATH, SARGE_FILEPATH, UE_MANNEQUIN_FILEPATH])
def test_animate_ready_rig(filepath):
    armature_object = _import_psk(filepath, should_make_animate_ready=True)
    from io_scene_psk_psa.rig.builder import get_rig_info
    info = get_rig_info(armature_object)
    assert info is not None
    assert set(info['arms'].keys()) == {'L', 'R'}
    assert set(info['legs'].keys()) == {'L', 'R'}
    rig_bones = [b for b in armature_object.data.bones if b.get('psk_rig')]
    assert len(rig_bones) > 0
    assert all(not b.use_deform for b in rig_bones)

    # The rig does not change the rest pose.
    bpy.context.view_layer.update()
    for pose_bone in armature_object.pose.bones:
        if pose_bone.bone.get('psk_rig'):
            continue
        difference = pose_bone.matrix - pose_bone.bone.matrix_local
        assert max(abs(difference[i][j]) for i in range(4) for j in range(4)) < 5e-2, pose_bone.name

    # The rig bones are never exported.
    exported = _export_psk_bones(armature_object)
    exported_names = {b.name.decode('windows-1252') for b in exported.bones}
    assert exported_names == set(_deform_bones(armature_object))


def test_animate_ready_no_limbs():
    armature_object = _import_psk(SWORD_FILEPATH, should_make_animate_ready=True)
    assert armature_object is not None


def test_animate_ready_psa_import_bakes_ik():
    """Importing a PSA onto an armature with a rig keys the IK controls so that the animation looks the same."""
    from io_scene_psk_psa.rig.bake import _MutedRigConstraints
    sequence_name = _first_sequence_name(SHREK_PSA_FILEPATH)
    armature_object = _import_psk(SHREK_PSK_FILEPATH, should_make_animate_ready=True)
    action = _import_sequence(armature_object, SHREK_PSA_FILEPATH, sequence_name)
    assert any('CTRL-' in fcurve.data_path for layer in action.layers for strip in layer.strips
               for channelbag in strip.channelbags for fcurve in channelbag.fcurves)
    assert armature_object['ik_arm_L'] == 1.0
    frame_start, frame_end = (int(x) for x in action.frame_range)
    for frame in range(frame_start, frame_end + 1, max(1, (frame_end - frame_start) // 4)):
        bpy.context.scene.frame_set(frame)
        ik_pose = _skinning_matrices(armature_object)
        with _MutedRigConstraints(armature_object):
            bpy.context.view_layer.update()
            fk_pose = _skinning_matrices(armature_object)
        bpy.context.view_layer.update()
        assert _max_difference(ik_pose, fk_pose) < 0.05, f'Frame {frame}'


def test_animate_ready_bake_ik_to_fk_and_remove():
    sequence_name = _first_sequence_name(SHREK_PSA_FILEPATH)
    armature_object = _import_psk(SHREK_PSK_FILEPATH, should_make_animate_ready=True)
    _import_sequence(armature_object, SHREK_PSA_FILEPATH, sequence_name)
    bpy.context.view_layer.objects.active = armature_object

    # Move the left hand IK control, then bake it to FK.
    bpy.context.scene.frame_set(0)
    armature_object.pose.bones['CTRL-hand_ik.L'].location.z += 0.1
    armature_object.pose.bones['CTRL-hand_ik.L'].keyframe_insert('location')
    bpy.context.view_layer.update()
    ik_pose = _skinning_matrices(armature_object)
    assert bpy.ops.psk_rig.bake_ik_to_fk() == {'FINISHED'}
    assert armature_object['ik_arm_L'] == 0.0
    bpy.context.scene.frame_set(0)
    assert _max_difference(ik_pose, _skinning_matrices(armature_object)) < 1e-2

    # Removing the rig leaves the (baked) animation intact.
    assert bpy.ops.psk_rig.remove() == {'FINISHED'}
    assert not any(b.get('psk_rig') for b in armature_object.data.bones)
    assert not any(c.name.startswith('PSKRig') for pb in armature_object.pose.bones for c in pb.constraints)
    bpy.context.scene.frame_set(0)
    assert _max_difference(ik_pose, _skinning_matrices(armature_object)) < 1e-2


def _ik_limbs(info: dict):
    for limb in info['arms'].values():
        yield limb, limb['hand'], limb['ik'], limb['ik']
    for limb in info['legs'].values():
        yield limb, limb['foot'], limb['target'], limb['ik']


def _bend_ik_limbs(armature_object, info: dict):
    """Moves the IK controls toward the shoulders/hips so that the IK targets stay within reach when scaling."""
    pose = armature_object.pose.bones
    bpy.context.view_layer.update()
    for limb, end, target, control in _ik_limbs(info):
        matrix = pose[control].matrix.copy()
        matrix.translation += (pose[limb['upper']].head - pose[end].head) * 0.4
        pose[control].matrix = matrix
    bpy.context.view_layer.update()


def _assert_ik_follows_scale(armature_object, info: dict):
    pose = armature_object.pose.bones
    bpy.context.view_layer.update()
    for limb, end, target, control in _ik_limbs(info):
        upper, lower = pose[limb['upper']], pose[limb['lower']]
        chain_length = limb['rest_length_a'] + limb['rest_length_b']
        assert (pose[end].head - pose[target].head).length < chain_length * 1e-3, limb['upper']
        for pose_bone, scale in ((upper, 1.5), (lower, 0.6), (upper.parent, 1.3)):
            pose_bone.scale = (scale, scale, scale)
            bpy.context.view_layer.update()
            # The limb is scaled, and its end is still on the IK target.
            assert abs((pose[end].head - lower.head).length + (lower.head - upper.head).length - chain_length) > chain_length * 0.05
            assert (pose[end].head - pose[target].head).length < chain_length * 1e-3, (limb['upper'], pose_bone.name)
            pose_bone.scale = (1.0, 1.0, 1.0)


@pytest.mark.parametrize('filepath', [SHREK_PSK_FILEPATH, UE_MANNEQUIN_FILEPATH])
def test_animate_ready_ik_follows_bone_scale(filepath):
    """The hands & feet stay on the IK targets when the limb bones (or the bones above them) are scaled."""
    from io_scene_psk_psa.rig.builder import get_rig_info
    armature_object = _import_psk(filepath, should_make_animate_ready=True)
    info = get_rig_info(armature_object)
    assert info['arms'] and info['legs']
    _bend_ik_limbs(armature_object, info)
    _assert_ik_follows_scale(armature_object, info)


def test_animate_ready_upgrade_rig():
    """A rig made before the IK followed the bone scale is updated in place."""
    from io_scene_psk_psa.rig.builder import RIG_INFO_PROPERTY, get_rig_info, is_rig_outdated, upgrade_rig
    armature_object = _import_psk(UE_MANNEQUIN_FILEPATH, should_make_animate_ready=True)
    info = get_rig_info(armature_object)
    assert not is_rig_outdated(armature_object) and not upgrade_rig(armature_object)
    # Turn the rig into an old one: no scale info & the old (fixed length) IK driver.
    info['version'] = 1
    pose = armature_object.pose.bones
    for limb, _, target, _ in _ik_limbs(info):
        del limb['scale_parent'], limb['scale_axes']
        mch_upper = pose[limb['mch_upper']]
        for index in range(3):
            mch_upper.driver_remove('scale', index)
        mch_upper.scale = (1.0, 1.0, 1.0)
        driver = mch_upper.driver_add('rotation_euler', 0).driver
        for name in 'ul':
            driver.variables.remove(driver.variables[name])
        s = driver.variables['s']
        s.targets[0].bone_target = ''
        s.targets[0].transform_type = 'SCALE_X'
        driver.expression = ('acos(min(max((a * a + (d / s) * (d / s) - b * b) / max(2.0 * a * (d / s), 1e-9), -1.0), 1.0))'
                             f' - {limb["rest_angle"]:.9f}')
    armature_object.data[RIG_INFO_PROPERTY] = json.dumps(info)
    _bend_ik_limbs(armature_object, info)
    with pytest.raises(AssertionError):
        _assert_ik_follows_scale(armature_object, info)
    for pose_bone in pose:
        pose_bone.scale = (1.0, 1.0, 1.0)

    # Without a context, only the updates that don't add bones are made.
    assert is_rig_outdated(armature_object) and upgrade_rig(armature_object)
    assert get_rig_info(armature_object)['version'] == 2
    _assert_ik_follows_scale(armature_object, get_rig_info(armature_object))


def test_animate_ready_bake_fk_to_ik_with_scale():
    """An FK animation with scaled limb bones looks the same on the IK controls, and the IK holds when rescaling."""
    from io_scene_psk_psa.rig.bake import _MutedRigConstraints
    from io_scene_psk_psa.rig.builder import get_rig_info
    sequence_name = _first_sequence_name(SHREK_PSA_FILEPATH)
    armature_object = _import_psk(SHREK_PSK_FILEPATH, should_make_animate_ready=True)
    action = _import_sequence(armature_object, SHREK_PSA_FILEPATH, sequence_name, should_bake_to_rig=False)
    bpy.context.view_layer.objects.active = armature_object
    info = get_rig_info(armature_object)
    pose = armature_object.pose.bones
    frame_start, frame_end = (int(x) for x in action.frame_range)
    arm = info['arms']['L']
    for name, scale in ((arm['upper'], 1.4), (arm['lower'], 0.8), (pose[arm['upper']].parent.name, 1.2)):
        pose[name].scale = (scale, scale, scale)
        pose[name].keyframe_insert('scale', frame=frame_start)
    assert bpy.ops.psk_rig.bake_fk_to_ik(limbs='ALL') == {'FINISHED'}
    assert armature_object['ik_arm_L'] == 1.0
    # The keyed lengths are those of the unscaled bones.
    assert abs(armature_object[arm['length_a']] - arm['rest_length_a']) < arm['rest_length_a'] * 0.05
    for frame in range(frame_start, frame_end + 1, max(1, (frame_end - frame_start) // 4)):
        bpy.context.scene.frame_set(frame)
        ik_pose = _skinning_matrices(armature_object)
        with _MutedRigConstraints(armature_object):
            bpy.context.view_layer.update()
            fk_pose = _skinning_matrices(armature_object)
        bpy.context.view_layer.update()
        assert _max_difference(ik_pose, fk_pose) < 0.05, f'Frame {frame}'

    # Growing the upper arm afterwards keeps the hand on the IK control (the arm bends more).
    bpy.context.scene.frame_set(frame_start)
    pose[arm['upper']].scale = (1.8, 1.8, 1.8)
    bpy.context.view_layer.update()
    assert (pose[arm['hand']].head - pose[arm['ik']].head).length < (arm['rest_length_a'] + arm['rest_length_b']) * 1e-3


def _finger_chains(armature_object, hand: dict) -> dict[str, list[str]]:
    """The finger bones driven by each finger control of a hand, from the base of the finger to its tip."""
    chains = {}
    for kind, control in hand['fingers'].items():
        chains[kind] = [pb.name for pb in armature_object.pose.bones
                        if any(c.name.startswith('PSKRig Finger Curl') and c.subtarget == control for c in pb.constraints)]
    return chains


def test_animate_ready_finger_sliders():
    """The fingers are posed with sliders over the back of the hand, and the thumb with a 2D pad."""
    from io_scene_psk_psa.rig.builder import get_rig_info
    armature_object = _import_psk(UE_MANNEQUIN_FILEPATH, should_make_animate_ready=True)
    info = get_rig_info(armature_object)
    pose = armature_object.pose.bones
    assert set(info['hands'].keys()) == {'L', 'R'}
    for side, hand in info['hands'].items():
        travel = hand['travel']
        chains = _finger_chains(armature_object, hand)
        assert {'thumb', 'index', 'middle', 'ring', 'pinky'} <= set(chains.keys())
        wrist = pose[info['arms'][side]['hand']]

        def tips() -> dict[str, Vector]:
            bpy.context.view_layer.update()
            return {kind: pose[bones[-1]].tail.copy() for kind, bones in chains.items()}

        def reach(positions: dict[str, Vector], kind: str) -> float:
            return (positions[kind] - wrist.head).length

        rest = tips()
        curl, spread = pose[hand['curl']], pose[hand['spread']]

        # Curl: toward the fingertips makes a fist, toward the wrist bends the fingers back. The sliders stop at the
        # ends of their tracks.
        curl.location.y = travel
        fist = tips()
        curl.location.y = travel * 10.0
        assert max((fist[k] - v).length for k, v in tips().items()) < 1e-4
        curl.location.y = travel * 0.2
        curled = tips()
        curl.location.y = -travel * 0.2
        opened = tips()
        curl.location.y = 0.0
        for kind in chains:
            assert reach(fist, kind) < reach(rest, kind) * 0.9, kind
            # Sliding the other way bends the finger the other way.
            assert (opened[kind] - rest[kind]).dot(curled[kind] - rest[kind]) < 0.0, kind

        # A finger's own slider only curls that finger.
        pose[hand['fingers']['index']].location.y = travel
        index_curled = tips()
        pose[hand['fingers']['index']].location.y = 0.0
        assert reach(index_curled, 'index') < reach(rest, 'index') * 0.9
        assert all((index_curled[k] - rest[k]).length < 1e-4 for k in chains if k != 'index')

        # Spread.
        spread.location.y = travel
        spread_out = tips()
        spread.location.y = -travel
        spread_in = tips()
        spread.location.y = 0.0
        gap = lambda positions: (positions['index'] - positions['pinky']).length
        assert gap(spread_out) > gap(rest) > gap(spread_in)

        # The thumb pad: along the hand curls the thumb, across the hand swings it in (toward the fingers) & out.
        thumb = pose[hand['thumb']]
        assert pose[hand['thumb_pad']].bone.hide_select
        thumb.location.y = travel
        assert reach(tips(), 'thumb') < reach(rest, 'thumb')
        thumb.location.y = 0.0
        knuckle = pose[chains['middle'][0]].head
        inward = 1.0 if thumb.bone.matrix_local.col[0].xyz.dot(knuckle - thumb.bone.head_local) > 0.0 else -1.0
        thumb.location.x = travel * inward
        swung_in = tips()
        thumb.location.x = -travel * inward
        swung_out = tips()
        thumb.location.x = 0.0
        assert (swung_in['thumb'] - knuckle).length < (rest['thumb'] - knuckle).length < (swung_out['thumb'] - knuckle).length
        assert all((swung_in[k] - rest[k]).length < 1e-4 for k in chains if k != 'thumb')


def test_animate_ready_foot_roll_control():
    """Rotating the foot roll control forward lifts the heel (the toes stay down), rotating it back lifts the toes."""
    from io_scene_psk_psa.rig.builder import get_rig_info
    armature_object = _import_psk(UE_MANNEQUIN_FILEPATH, should_make_animate_ready=True)
    info = get_rig_info(armature_object)
    pose = armature_object.pose.bones
    for side, leg in info['legs'].items():
        assert leg['toe'] is not None
        roll, foot, toe = pose[leg['roll_ctrl']], pose[leg['foot']], pose[leg['toe']]
        bpy.context.view_layer.update()
        ankle, ball = foot.head.copy(), toe.head.copy()
        length = (ball - ankle).length
        roll.rotation_euler.x = 0.6
        bpy.context.view_layer.update()
        assert foot.head.z > ankle.z + length * 0.1 and (toe.head - ball).length < length * 0.02
        # The foot roll slider in the panel adds to the control (the ankle rolls further over the ball).
        rolled = (foot.head - ankle).length
        armature_object[leg['roll_prop']] = 0.25
        armature_object.update_tag()
        bpy.context.view_layer.update()
        assert (foot.head - ankle).length > rolled + length * 0.1 and (toe.head - ball).length < length * 0.02
        armature_object[leg['roll_prop']] = 0.0
        armature_object.update_tag()
        roll.rotation_euler.x = -0.6
        bpy.context.view_layer.update()
        assert toe.head.z > ball.z + length * 0.1
        roll.rotation_euler.x = 0.0


def test_animate_ready_update_rig_adds_foot_roll():
    """Updating an older rig adds the foot roll controls, and keeps the other controls & their keys."""
    from io_scene_psk_psa.rig.builder import (RIG_INFO_PROPERTY, RigOptions, _add_foot_roll_drivers, build_rig,
                                              get_rig_info, is_rig_outdated)
    armature_object = _import_psk(UE_MANNEQUIN_FILEPATH)
    options = RigOptions()
    options.use_fingers = False
    build_rig(bpy.context, armature_object, options)
    bpy.context.view_layer.objects.active = armature_object

    # Turn the rig into an older one: no foot roll controls.
    info = get_rig_info(armature_object)
    info['version'] = 2
    removed = []
    for leg in info['legs'].values():
        removed.append(leg.pop('roll_ctrl'))
        _add_foot_roll_drivers(armature_object, leg)
    bpy.ops.object.mode_set(mode='EDIT')
    for name in removed:
        armature_object.data.edit_bones.remove(armature_object.data.edit_bones[name])
    bpy.ops.object.mode_set(mode='POSE')
    armature_object.data[RIG_INFO_PROPERTY] = json.dumps(info)

    hand_ik = armature_object.pose.bones['CTRL-hand_ik.L']
    hand_ik.location.z += 5.0
    hand_ik.keyframe_insert('location', frame=1)
    bpy.context.view_layer.update()
    before = _skinning_matrices(armature_object)

    assert is_rig_outdated(armature_object)
    assert bpy.ops.psk_rig.update() == {'FINISHED'}
    assert not is_rig_outdated(armature_object)
    info = get_rig_info(armature_object)
    pose = armature_object.pose.bones
    assert all(leg['roll_ctrl'] in pose for leg in info['legs'].values())
    assert not info.get('hands')
    bpy.context.view_layer.update()
    assert _max_difference(before, _skinning_matrices(armature_object)) < 1e-4
    action = armature_object.animation_data.action
    assert any('CTRL-hand_ik.L' in fcurve.data_path for layer in action.layers for strip in layer.strips
               for channelbag in strip.channelbags for fcurve in channelbag.fcurves)
    leg = info['legs']['L']
    ankle = pose[leg['foot']].head.copy()
    pose[leg['roll_ctrl']].rotation_euler.x = 0.6
    bpy.context.view_layer.update()
    assert pose[leg['foot']].head.z > ankle.z


def test_animate_ready_update_rig_converts_finger_keys():
    """The keys of the old finger controls (scale to curl, rotate to spread) are moved onto the finger sliders."""
    from io_scene_psk_psa.rig.builder import FINGER_SPREAD_MAX_ANGLE, _convert_finger_control_keys
    from io_scene_psk_psa.shared.compat import action_ensure_channelbag_for_slot, action_ensure_slot, channelbag_ensure_fcurve
    action = bpy.data.actions.new('fingers')
    channelbag = action_ensure_channelbag_for_slot(action, action_ensure_slot(action, 'OBJECT', 'Armature'))
    for path, index, values in (('pose.bones["CTRL-fingers.L"].scale', 1, (1.0, 0.0)),
                                ('pose.bones["CTRL-fingers.L"].scale', 0, (1.0, 1.0)),
                                ('pose.bones["CTRL-fingers.L"].rotation_euler', 2, (0.0, FINGER_SPREAD_MAX_ANGLE / 2)),
                                ('pose.bones["CTRL-finger_index.L"].scale', 1, (1.0, 2.0)),
                                ('pose.bones["CTRL-finger_index.L"].location', 1, (0.0, 0.0)),
                                ('pose.bones["CTRL-hand_ik.L"].location', 1, (0.0, 3.0))):
        fcurve = channelbag_ensure_fcurve(channelbag, path, index=index, group_name='x')
        for frame, value in enumerate(values):
            fcurve.keyframe_points.insert(frame, value)
    travel = 0.5
    _convert_finger_control_keys({
        ('CTRL-fingers.L', 'scale', 1): ('CTRL-fingers_curl.L', -travel, travel),
        ('CTRL-fingers.L', 'rotation_euler', 2): ('CTRL-fingers_spread.L', travel / FINGER_SPREAD_MAX_ANGLE, 0.0),
        ('CTRL-finger_index.L', 'scale', 1): ('CTRL-finger_index.L', -travel, travel),
    }, {'CTRL-fingers.L', 'CTRL-finger_index.L'})
    keys = {(fcurve.data_path, fcurve.array_index): [round(k.co.y, 5) for k in fcurve.keyframe_points]
            for fcurve in channelbag.fcurves}
    assert keys == {
        ('pose.bones["CTRL-fingers_curl.L"].location', 1): [0.0, travel],            # Rest, then a fist.
        ('pose.bones["CTRL-fingers_spread.L"].location', 1): [0.0, travel / 2],
        ('pose.bones["CTRL-finger_index.L"].location', 1): [0.0, -travel],           # Rest, then bent back.
        ('pose.bones["CTRL-hand_ik.L"].location', 1): [0.0, 3.0],
    }


def test_make_animate_ready_converts_existing_actions():
    sequence_name = _first_sequence_name(SHREK_PSA_FILEPATH)
    armature_object = _import_psk(SHREK_PSK_FILEPATH, should_fix_bone_orientation=False)
    _import_sequence(armature_object, SHREK_PSA_FILEPATH, sequence_name)
    bpy.context.scene.frame_set(3)
    before = _skinning_matrices(armature_object)
    bpy.context.view_layer.objects.active = armature_object
    assert bpy.ops.psk_rig.make_animate_ready() == {'FINISHED'}
    # Switch to FK to see the (converted) FK animation.
    from io_scene_psk_psa.rig.builder import set_rig_property
    for key in list(armature_object.keys()):
        if key.startswith('ik_') or key == 'eyes_aim':
            set_rig_property(armature_object, key, 0.0)
    bpy.context.scene.frame_set(3)
    after = _skinning_matrices(armature_object)
    assert _max_difference({n: before[n] for n in after}, after) < 1e-3


def _write_png(path: Path, color=(1.0, 0.0, 0.0, 1.0)):
    image = bpy.data.images.new(path.stem, 4, 4, alpha=True)
    # A gradient (solid color textures are treated as placeholders).
    pixels = []
    for i in range(16):
        pixels.extend((color[0] * i / 15, color[1], color[2], color[3]))
    image.pixels = pixels
    image.filepath_raw = str(path)
    image.file_format = 'PNG'
    image.save()
    bpy.data.images.remove(image)


def test_material_setup_from_fmodel_json(tmp_path):
    from io_scene_psk_psa.psk.materials import MaterialTextureFinder, setup_material
    (tmp_path / 'Mesh').mkdir()
    (tmp_path / 'Mat').mkdir()
    (tmp_path / 'Tex').mkdir()
    _write_png(tmp_path / 'Tex' / 'T_Hero_Body_D.png')
    _write_png(tmp_path / 'Tex' / 'T_Hero_Body_N.png', (0.5, 0.5, 1.0, 1.0))
    (tmp_path / 'Mat' / 'MI_Hero_Body.json').write_text(json.dumps({
        'Textures': {
            'Diffuse': '/Game/Hero/Tex/T_Hero_Body_D.T_Hero_Body_D',
            'Normal': '/Game/Hero/Tex/T_Hero_Body_N.T_Hero_Body_N',
            'DetailTexture': '/Game/Common/T_Missing.T_Missing',
        },
        'Parameters': {'BlendMode': 0},
    }))
    finder = MaterialTextureFinder(str(tmp_path / 'Mesh' / 'SK_Hero.psk'))
    material = bpy.data.materials.new('MI_Hero_Body')
    assert setup_material(material, finder)
    images = {n.image.name for n in material.node_tree.nodes if n.type == 'TEX_IMAGE'}
    assert images == {'T_Hero_Body_D.png', 'T_Hero_Body_N.png'}
    assert any(n.type == 'NORMAL_MAP' for n in material.node_tree.nodes)


def test_material_setup_from_umodel_mat_and_by_name(tmp_path):
    from io_scene_psk_psa.psk.materials import MaterialTextureFinder, setup_material
    _write_png(tmp_path / 'T_Gun_D.png')
    (tmp_path / 'M_Gun.mat').write_text('Diffuse=T_Gun_D\nNormal=T_Gun_N\n')
    _write_png(tmp_path / 'T_Villain_Head_D.png')
    finder = MaterialTextureFinder(str(tmp_path / 'SK_Gun.psk'))

    material = bpy.data.materials.new('M_Gun')
    assert setup_material(material, finder)
    assert {n.image.name for n in material.node_tree.nodes if n.type == 'TEX_IMAGE'} == {'T_Gun_D.png'}

    # No material file: matched by name.
    material = bpy.data.materials.new('MI_Villain_Head')
    assert setup_material(material, finder)
    assert {n.image.name for n in material.node_tree.nodes if n.type == 'TEX_IMAGE'} == {'T_Villain_Head_D.png'}

    # No match.
    material = bpy.data.materials.new('MI_Unrelated')
    assert not setup_material(material, finder)


@pytest.mark.skipif(not os.path.isdir(TEST_MODEL_DIRECTORY), reason='Test model not available')
def test_test_model_full_workflow():
    """
    Import the test model with the "Animate Ready" option, import an animation (which is baked to the IK controls)
    and export it again. The exported animation must match the original.
    """
    from io_scene_psk_psa.psa.builder import PsaBuildOptions, PsaBuildSequence, build_psa
    psk_filepath = TEST_MODEL_PSK
    psa_filepath = os.path.join(TEST_MODEL_ANIMATIONS, 'cp', 'cp002_CustomPoseEnd00.psa')
    armature_object = _import_psk(psk_filepath, should_make_animate_ready=True)

    # Textures were found for the materials that have them.
    mesh_object = next(o for o in armature_object.children if o.type == 'MESH')
    materials = {m.name: m for m in mesh_object.data.materials}
    for name in ('MII_Pl13101_body', 'MII_Pl13100_face', 'MII_Pl13100_eye_GPD', 'MII_Pl13100_hairB_ShideC', 'MII_Pl13100_neck'):
        assert any(n.type == 'TEX_IMAGE' and n.image for n in materials[name].node_tree.nodes), name

    reader = PsaReader.from_path(psa_filepath)
    sequence_name = next(iter(reader.sequences.keys()))
    action = _import_sequence(armature_object, psa_filepath, sequence_name)
    assert armature_object['ik_leg_L'] == 1.0

    options = PsaBuildOptions()
    options.armature_objects = [armature_object]
    options.animation_data = armature_object.animation_data
    sequence = PsaBuildSequence(armature_object, armature_object.animation_data)
    sequence.name = sequence_name
    sequence.nla_state.action = action
    sequence.nla_state.frame_start, sequence.nla_state.frame_end = (int(x) for x in action.frame_range)
    sequence.fps = reader.sequences[sequence_name].fps
    options.sequences = [sequence]
    psa = build_psa(bpy.context, options)

    exported_names = [b.name for b in psa.bones]
    assert not any(n.startswith(b'CTRL-') or n.startswith(b'MCH-') for n in exported_names)
    original_names = [b.name for b in reader.bones]
    original_keys = reader.read_sequence_keys(sequence_name)
    frame_count = reader.sequences[sequence_name].frame_count
    for frame in range(frame_count):
        for export_index, name in enumerate(exported_names):
            if name not in original_names:
                continue
            original_key = original_keys[frame * len(original_names) + original_names.index(name)]
            exported_key = psa.keys[frame * len(exported_names) + export_index]
            assert _quaternion_difference(original_key.rotation, exported_key.rotation) < 1e-2, (frame, name)
            assert max(abs(a - b) for a, b in zip(original_key.location, exported_key.location)) < 5e-2, (frame, name)


@pytest.mark.skipif(not os.path.isdir(TEST_MODEL_DIRECTORY), reason='Test model not available')
def test_thumb_curls_across_the_palm():
    """Curling the fingers must fold the thumb toward the palm, not bend it backwards away from the hand."""
    armature_object = _import_psk(TEST_MODEL_PSK, should_make_animate_ready=True)
    bones = armature_object.data.bones
    pose_bones = armature_object.pose.bones
    for side in 'LR':
        palm_centre = (bones[f'{side}_middlefinger_A'].head_local + bones[f'{side}_littlefinger_A'].head_local) / 2
        bpy.context.view_layer.update()
        rest = (pose_bones[f'{side}_thumbfinger_C'].tail - palm_centre).length
        curl = pose_bones[f'CTRL-fingers_curl.{side}']
        curl.location.y = 1e6  # Limited to the end of the slider's track (a fist).
        bpy.context.view_layer.update()
        curled = (pose_bones[f'{side}_thumbfinger_C'].tail - palm_centre).length
        curl.location.y = 0.0
        assert curled < rest * 0.8


def test_eye_style_aim_for_eyeballs():
    armature_object = _import_psk(SARGE_FILEPATH, should_make_animate_ready=True)
    from io_scene_psk_psa.rig.builder import get_rig_info
    info = get_rig_info(armature_object)
    assert info['eye_style'] == 'AIM'
    assert info['eyes'] is not None


@pytest.mark.skipif(not os.path.isdir(TEST_MODEL_DIRECTORY), reason='Test model not available')
def test_eye_style_slide_for_flat_irises():
    """The test model has flat irises that slide across the face (the eye bones translate & scale, never rotate)."""
    psk_filepath = TEST_MODEL_PSK
    armature_object = _import_psk(psk_filepath, should_make_animate_ready=True)
    from io_scene_psk_psa.rig.builder import get_rig_info
    assert get_rig_info(armature_object)['eye_style'] == 'SLIDE'
    pose_bones = armature_object.pose.bones
    bpy.context.view_layer.update()
    head = pose_bones['L_eye'].head.copy()
    rotation = pose_bones['L_eye'].matrix.to_quaternion()
    pose_bones['CTRL-eye.L'].location = (-1.0, 0.0, 0.5)
    pose_bones['CTRL-eyes'].scale = (0.5, 0.5, 0.5)
    bpy.context.view_layer.update()
    moved = pose_bones['L_eye'].head - head
    assert abs(moved.length - (1.0 ** 2 + 0.5 ** 2) ** 0.5) < 1e-3
    assert rotation.rotation_difference(pose_bones['L_eye'].matrix.to_quaternion()).angle < 1e-4
    assert abs(pose_bones['R_eye'].matrix.to_scale().x - 0.5) < 1e-3


@pytest.mark.skipif(not os.path.isdir(TEST_MODEL_DIRECTORY), reason='Test model not available')
def test_face_presets_from_psa():
    from io_scene_psk_psa.rig import face_presets
    psk_filepath = TEST_MODEL_PSK
    armature_object = _import_psk(psk_filepath, should_make_animate_ready=True)
    directory = os.path.join(TEST_MODEL_ANIMATIONS, 'fa')
    filepaths = [os.path.join(directory, f) for f in ('AM_fa100_FaceEyeClose001.psa', 'AM_FaceLipClose.psa', 'AM_FaceLipLoop.psa')]
    presets = face_presets.import_face_presets(bpy.context, armature_object, filepaths)
    names = {p.name for p in presets}
    assert names == {'Face: AM_fa100_FaceEyeClose001', 'Face: AM_FaceLipClose', 'Face: AM_FaceLipLoop'}
    assert not any(a.name.startswith('__psk_face_preset_tmp__') for a in bpy.data.actions)
    face_bones = set(face_presets.get_face_bones(armature_object))
    for preset in presets:
        bones = {fc.data_path.split('"')[1] for layer in preset.layers for strip in layer.strips
                 for cb in strip.channelbags for fc in cb.fcurves}
        assert bones and bones <= face_bones
    # Closing the eyes brings the upper & lower lids together.
    pose_bones = armature_object.pose.bones
    bpy.context.view_layer.update()
    gap = pose_bones['LT_eyelid_C'].head.z - pose_bones['LU_eyelid_B'].head.z
    face_presets.apply_face_preset(armature_object, bpy.data.actions['Face: AM_fa100_FaceEyeClose001'], insert_keys=True)
    bpy.context.view_layer.update()
    assert pose_bones['LT_eyelid_C'].head.z - pose_bones['LU_eyelid_B'].head.z < gap * 0.5
    face_presets.reset_face(armature_object)
    bpy.context.view_layer.update()
    assert abs(pose_bones['LT_eyelid_C'].head.z - pose_bones['LU_eyelid_B'].head.z - gap) < 1e-4


def test_face_preset_from_pose():
    from io_scene_psk_psa.rig import face_presets
    armature_object = _import_psk(SHREK_PSK_FILEPATH, should_make_animate_ready=True)
    jaw = armature_object.pose.bones['body_jaw_joint']
    jaw.location = (0.1, 0.2, 0.3)
    preset = face_presets.face_preset_from_pose(armature_object, 'Test')
    face_presets.reset_face(armature_object)
    assert jaw.location.length == 0.0
    face_presets.apply_face_preset(armature_object, preset, factor=0.5)
    assert (jaw.location - Vector((0.05, 0.1, 0.15))).length < 1e-5


def _material_images(material) -> set[str]:
    if not material.use_nodes:
        return set()
    return {n.image.name for n in material.node_tree.nodes if n.type == 'TEX_IMAGE' and n.image}


@pytest.mark.skipif(not os.path.isdir(TEST_MODEL_DIRECTORY), reason='Test model not available')
def test_material_setup_game_folder_layout():
    """
    The meshes are nested (`Model/<Id>/Mesh/<Mesh>/x.psk`), the material files & textures are in the `Mat` & `Tex`
    folders of other models, and costume variants (`_CV`) have no material files of their own.
    """
    before = set(bpy.data.objects)
    assert bpy.ops.psk.import_file(filepath=TEST_MODEL_PSK.replace('Pl13_101', 'Pl13_100').replace('SK_Pl13_101', 'SK_Pl13_100').replace('SK_pl13_101', 'SK_pl13_100'), components='MESH') == {'FINISHED'}
    mesh_object = next(o for o in set(bpy.data.objects) - before if o.type == 'MESH')
    materials = {m.name: m for m in mesh_object.data.materials}
    expected = {
        'MII_Pl13100_face': {'T_Pl13_101_face_D.png'},
        'MII_Pl13100_body_CV': {'T_Pl13_001_body_D.png'},  # Costume variant of `MII_Pl13101_body`.
        'MII_Pl13100_parts_CV': {'T_Pl13_101_parts_D.png'},
        'MII_Pl13100_eye_GPD': {'T_Pl13_001_eye_D.png'},
        'MII_Pl13100_hairA_hideA': {'T_Pl13_001_hair_D.png'},
        'MII_Pl13100_googgle': {'T_Pl13_001_goggles_D.png'},
    }
    for name, images in expected.items():
        # Placeholder textures (e.g., `T_pl_com_alphatest_D` in the normal map slots) are not used.
        assert _material_images(materials[name]) == images, name


@pytest.mark.skipif(not os.path.isfile(UR_MODEL_PSK), reason='Test model not available')
def test_material_setup_ambient_occlusion():
    before = set(bpy.data.objects)
    assert bpy.ops.psk.import_file(filepath=UR_MODEL_PSK, components='MESH') == {'FINISHED'}
    mesh_object = next(o for o in set(bpy.data.objects) - before if o.type == 'MESH')
    materials = {m.name: m for m in mesh_object.data.materials}
    body = materials['MII_Ch013_Default_00_body_D']
    assert _material_images(body) == {'T_Ch013_Default_00_body_D.png', 'T_Ch013_Default_00_body_AO.png'}
    assert body.node_tree.nodes.get('PSK Ambient Occlusion') is not None
    # The eye uses a placeholder (black) AO texture & shared effect textures for emission, which are not used.
    assert _material_images(materials['MII_Ch013_Default_00_eye_D']) == {'T_Ch013_Default_00_eye_D.png'}
    # The outline texture of the face is not used.
    assert _material_images(materials['MII_Ch013_Default_00_face_D']) == {'T_Ch013_Default_00_face_D.png'}

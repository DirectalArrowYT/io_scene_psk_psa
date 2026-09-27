"""
Tests for the "Fix Bone Tails" (bone re-orientation) and "Animate Ready" (control rig) import options, and for the
automatic material setup.
"""
import json
import os
from pathlib import Path

import bpy
import pytest
from mathutils import Matrix

from psk_psa_py.psk.reader import read_psk_from_file
from psk_psa_py.psa.reader import PsaReader

SHREK_PSK_FILEPATH = 'tests/data/Shrek.psk'
SHREK_PSA_FILEPATH = 'tests/data/Shrek.psa'
SARGE_FILEPATH = 'tests/data/CS_Sarge_S0_Skelmesh.pskx'
UE_MANNEQUIN_FILEPATH = 'tests/data/SK_1033_1033001.pskx'
SWORD_FILEPATH = 'tests/data/WEP_BroadSword_SKEL.psk'
TEST_MODEL_DIRECTORY = 'Test Model Anim'


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
    image.pixels = list(color) * 16
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
    psk_filepath = os.path.join(TEST_MODEL_DIRECTORY, 'Mesh', 'SK_pl02_100.psk')
    psa_filepath = os.path.join(TEST_MODEL_DIRECTORY, 'Animations', 'cp002_CustomPoseEnd00.psa')
    armature_object = _import_psk(psk_filepath, should_make_animate_ready=True)

    # Textures were found for the materials that have them.
    mesh_object = next(o for o in armature_object.children if o.type == 'MESH')
    materials = {m.name: m for m in mesh_object.data.materials}
    for name in ('MII_Pl02100_face_GPA', 'MII_Pl02100_eye', 'MII_Pl02100_hair', 'MII_Pl02100_neck'):
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

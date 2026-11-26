[![Blender](https://img.shields.io/badge/Blender%20Extension-Download-blue?logo=blender&logoColor=white)](https://extensions.blender.org/add-ons/io-scene-psk-psa/ "Download Blender")
[![tests](https://github.com/DarklightGames/io_scene_psk_psa/actions/workflows/main.yml/badge.svg)](https://github.com/DarklightGames/io_scene_psk_psa/actions/workflows/main.yml)

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/L4L3853VR)

This Blender addon allows you to import and export meshes and animations to and from the [PSK and PSA file formats](https://wiki.beyondunreal.com/PSK_%26_PSA_file_formats) used in many versions of the Unreal Engine.

This software is licensed under the [GPLv3](https://www.gnu.org/licenses/gpl-3.0.html) license.

> **About this fork.** This is a fork of [DarklightGames/io_scene_psk_psa](https://github.com/DarklightGames/io_scene_psk_psa)
> by Colin Basnett, made while modding *MY HERO ACADEMIA: All's Justice* and *Ultra Rumble*. It adds **Fix Bone Tails**,
> the **Animate Ready** control rig (IK/FK, foot roll, finger, eye and face controls), **Auto Setup Textures** for
> FModel and UE Viewer exports, faster PSA import, and scale keys on PSA export. Everything else is the original
> addon. Please report problems with these additions here, not to the original project.

# Installation
This fork is not on the Blender Extensions platform. Download the repository as a zip, zip the `io_scene_psk_psa`
folder on its own, then in Blender use `Edit` > `Preferences` > `Get Extensions` > the drop-down menu >
`Install from Disk...` and pick that zip. It uses the same id as the original addon, so it replaces it; disable or
remove the original first if you have it. Needs Blender 4.5 or newer.

The original addon: for Blender 4.2 and higher, download the latest version from the [Blender Extensions](https://extensions.blender.org/add-ons/io-scene-psk-psa/) platform.

For Blender 4.1 and lower, see [Legacy Compatibility](#legacy-compatibility).

# Features
* [Bone collections](https://docs.blender.org/manual/en/latest/animation/armatures/bones/bone_collections.html#bone-collections) can be excluded from PSK/PSA export (useful for excluding non-contributing bones such as IK controllers).
* [Collection Exporters](https://docs.blender.org/manual/en/latest/scene_layout/collections/collections.html#exporters) for reliable, repeatable export workflow.
* Non-standard model data (.pskx) is supported for import only (vertex normals, extra UV channels, vertex colors, shape keys).
* Manual re-ordering of material slots on export.
* Bone scale is imported and exported as scale keys (non-standard animation data, as written by UE Viewer & FModel). On export, the scale keys are only written when a bone is scaled (**Scale Keys** export option).
* Fine-grained PSA sequence importing for efficient workflow when working with large PSA files.
* PSA sequence metadata (e.g., frame rate) is preserved on import, allowing this data to be reused on export.
* PSA sequences can be exported directly from actions or delineated using a scene's [timeline markers](https://docs.blender.org/manual/en/latest/animation/markers.html), pose markers, or NLA track strips, allowing direct use of the [NLA](https://docs.blender.org/manual/en/latest/editors/nla/index.html) when creating sequences.
* Compress exported sequences via resampling ratios or frame quotas.
* **Fix Bone Tails**: bones are re-oriented on import so that their tails point to the next joint (e.g., the upper arm points to the elbow). The original orientations are remembered, so PSA imports and PSK/PSA exports are identical to the originals.
* **Animate Ready**: an optional control rig (IK arms & legs with IK/FK switching, foot roll, finger, eye & facial controls) that is set up on import. The controls are never exported, and PSA animations imported onto the rig are baked onto the controls automatically. See [Animate Ready Rig](#animate-ready-rig).
* **Auto Setup Textures**: materials are set up with their textures automatically on import, using the material files exported by FModel (`.json`) or UE Viewer (`.mat`, `.props.txt`), or by matching texture names.

# Usage

## Import

### Importing a PSK/PSKX
1. Navigate to `File` > `Import` > `Unreal PSK (.psk/.pskx)`.
2. Select the PSK file you want to import and click `Import`.

#### Import options
* **Fix Bone Tails** (Armature, on by default): re-orients the bones so that the bone tails point to the next joint instead of all pointing in the same direction. The original bone orientations are stored on the bones, so PSA imports & PSK/PSA exports are unaffected. Turn this off to get the raw bone orientations (the **Bone Length** option then applies).
* **Animate Ready**: sets up the control rig described below.
* **Auto Setup Textures** (Mesh, on by default): finds the textures for each material & builds the material node tree (base color, ambient occlusion, normal map, ORM/MRAO, emissive, opacity). The folder of the PSK file and up to 5 folders above it (including sub-folders such as `Mat/`, `Tex/`, `Textures/`, so materials & textures shared between models are found), the folder structure of the original asset paths (e.g., `.../Game/Characters/...`) and an optional **Texture Folder** are searched. Material variants without their own material file (e.g., costume variants such as `MI_Hero100_body_CV`) use the definition of the most similar material (e.g., `MI_Hero101_body`). Placeholder textures (solid colors, test patterns) and shared effect textures are ignored. The alpha channel of the base color texture is only used as opacity for masked/translucent materials when it doesn't cut holes in the mesh (it's often used as a mask for other purposes).

### Importing a PSA
1. Select an armature that you want import animations for.
2. Navigate to `File` > `Import` > `Unreal PSA (.psa)`.
3. Select the PSA file you want to import.
4. Select the sequences that you want to import and click `Import`.

## Animate Ready Rig
Tick **Animate Ready** when importing a PSK (or use **Make Animate Ready** in the `PSK Rig` tab of the 3D Viewport's sidebar on an existing armature; existing actions are converted automatically) to set up a control rig for animating. The skeleton is analyzed automatically (bone names from most engines & tools are recognized, and the sides, up & forward directions are determined from the bone positions).

| Controls | Description |
|---|---|
| **Arm & Leg IK** | `CTRL-hand_ik`, `CTRL-foot_ik` & pole targets (`CTRL-elbow_pole`, `CTRL-knee_pole`). The IK is analytic (exact, even for straight limbs) and follows the scale of the limb bones & the bones above them, so the hands & feet stay on the controls when e.g. an arm is scaled. The twist rings (`CTRL-arm_twist`, `CTRL-forearm_twist`, etc.) twist the limbs around their own axis. |
| **IK/FK switch** | Per-limb sliders in the `PSK Rig` panel. In FK mode, the deform bones are animated directly. The snap buttons match IK to FK (or vice versa) on the current frame. |
| **Foot roll** | Rotate `CTRL-foot_roll` (the arrow behind the heel) forward to roll onto the ball of the foot (the heel lifts while the toes bend & stay on the floor), or back to lift the toes. The per-foot slider in the `PSK Rig` panel does the same and adds to the control. `CTRL-toe` controls the toes. |
| **Fingers** | A panel of sliders floats over the back of each hand. `CTRL-fingers_curl` & `CTRL-fingers_spread` (near the wrist) curl & spread all the fingers, and each finger has its own curl slider over its knuckle (`CTRL-finger_index`, etc.). Slide toward the fingertips to curl, toward the wrist to straighten & bend back. The thumb has a 2D pad (`CTRL-thumb`) on its side of the hand: along the hand curls it, across the hand swings it in & out. |
| **Eyes** | `CTRL-eyes` (both eyes) and `CTRL-eye` (per eye). The **Eye Style** is detected from the shape of the eye meshes: **Slide** for flat irises (common in anime-style games), where moving the controls slides the irises across the face and scaling them changes the size of the irises/pupils, or **Aim** for spherical eyeballs, where the controls are targets that the eyes look at. |
| **Face** | Controls in front of the face: blink (`CTRL-blink`, move down to close the eyelids, up to open them wide), brows (`CTRL-brow`, move to raise/lower, rotate to tilt), mouth corners (`CTRL-mouth_corner`, smile/frown/wide/narrow), lips (`CTRL-lip_upper`/`CTRL-lip_lower`, move up/down & forward to pout), cheeks (`CTRL-cheek`) and jaw (`CTRL-jaw`, move down to open, sideways to shift). The individual facial bones can also be animated directly. |

**Face Presets** (`PSK Rig` sidebar tab): a library of facial expressions. Use the import button to select PSA files (e.g., a game's facial animations such as blinks, smiles or mouth shapes); a preset is created from the most expressive frame of each sequence. Presets can also be saved from the current pose of the face. Select a preset and click **Apply** (or **Apply & Key**) to pose the face, with the **Blend** slider controlling how much of the preset is applied. Presets only affect the facial bones, and are also available as pose assets in Blender's Pose Library.

The finger, eye (slide), and face controls are layered on top of the FK animation, so imported animations look exactly the same while the controls are at rest.

Rigs made with an older version of the add-on are updated with **Update Rig** in the `PSK Rig` panel (the IK scale fix is also applied when the file is opened). The controls and their animation are kept; keys on the old finger controls (scale to curl, rotate to spread) are moved onto the sliders.

**Importing animations onto the rig:** when a PSA is imported onto an armature with the rig, the IK & eye controls are keyed on every frame so that the animation looks exactly as it was authored (**Bake to Rig Controls** import option).

**Baking:** in the `PSK Rig` panel, **Bake FK → IK** keys the IK controls from the FK animation of the active action, and **Bake IK → FK** keys the deform bones with the result of all the controls (IK, eyes, fingers & face), switches to FK and removes the control keys. **Remove Rig** removes all the controls, leaving only the deform bones (bake to FK first to keep the animation made with the controls).

**Exporting:** the rig is never exported. PSK & PSA exports only contain the deform bones, with the IK & other controls baked into the exported animation.

## Export
It is highly recommended to use the provided [Collection Exporters](https://docs.blender.org/manual/en/latest/scene_layout/collections/collections.html#exporters) workflow, since it allows for highly reliable, repeatable exports of both PSK and PSA files. However, the traditional export workflow is available as well.

### Exporting a PSK
1. Select the mesh objects you wish to export.
2. Navigate to `File` > `Export` > `Unreal PSK (.psk)`.
3. Enter the file name and click `Export`.

### Exporting a PSA
1. Select the armature objects you wish to export.
2. Navigate to `File` > `Export` > `Unreal PSA (.psa)`.
3. Enter the file name and click `Export`.

> Note that in order to see the imported actions applied to your armature, you must use the [Dope Sheet](https://docs.blender.org/manual/en/latest/editors/dope_sheet/introduction.html) or [Nonlinear Animation](https://docs.blender.org/manual/en/latest/editors/nla/introduction.html) editors.

## Using it with All's Justice and Ultra Rumble exports
These games are Unreal Engine 5 games. Export from them with [FModel](https://fmodel.app/):
* **Meshes:** export the skeletal mesh as PSK, with textures as PNG and the material `.json` files. Keep FModel's
  folder layout (`.../Model/<costume>/Mesh/`, `.../Mat/`, `.../Tex/`) so **Auto Setup Textures** finds every material;
  costume colour variants (`..._CV` materials) borrow the closest material that has a file.
* **Animations:** export as PSA, then import them onto the armature from the PSK.
* **Animating:** import the PSK with **Animate Ready** on, animate the controls, then export a PSA. The controls are
  never exported, and **Fix Bone Tails** keeps the exported bone orientations identical to the game's, so the
  animation can go back into the game unchanged. If the animation scales bones, the export writes scale keys (the
  **Scale Keys** option, on by default).

# FAQ

## Why can't I see the animations imported from my PSA?
Simply importing an animation into the scene will not automatically apply the action to the armature.

The PSA importer creates [Actions](https://docs.blender.org/manual/en/latest/animation/actions.html) for each of the selected sequences in the PSA. These actions can be applied to your armature via the [Action Editor](https://docs.blender.org/manual/en/latest/editors/dope_sheet/action.html) or [NLA Editor](https://docs.blender.org/manual/en/latest/editors/nla/index.html).

The reason for this behavior is in part because a PSA can have multiple sequences imported from it, and also that it's generally bad form for importers to modify the scene in ways that the user may not expect.

## Why are imported PSKs too big/too small?
The PSK format, unlike other more modern formats, has no explicit or implicit unit system. Each game has its own convention as to what the base distance unit will represent. As such, this addon makes no assumptions as to the unit scale of the imported PSKs. If you think that your models are being imported into Blender either too big or too small, there are a couple ways to remedy this.

The method I prefer is to simply change the Blender [scene properties](https://docs.blender.org/manual/en/5.0/scene_layout/scene/properties.html#units) to match the unit system and scale for the game you're using. This is non-destructive and ensures that the unit scaling of any PSK or PSA exports from Blender will match the source file from which it was derived.

The second option is to simply change the `Scale` value on the PSK import dialog. This will scale the armature by the factor provided. Note that this is more destructive, but may be preferable if you don't intend on exporting PSKs or PSAs to a game engine.

## How do I control shading for PSK exports?
The PSK format does not support vertex normals and instead uses [smoothing groups](https://en.wikipedia.org/wiki/Smoothing_group) to control shading. Note that a mesh's Custom Split Normals Data will be ignored when exporting to PSK. Therefore, the best way to control shading is to use sharp edges and the Edge Split modifier.

## Why are the mesh normals not accurate when importing a PSK extracted from [UE Viewer](https://www.gildor.org/en/projects/umodel)?
If preserving the mesh normals of models is important for your workflow, it is *not recommended* to export PSK files from UE Viewer. This is because UE Viewer makes no attempt to reconstruct the original [smoothing groups](https://en.wikipedia.org/wiki/Smoothing_group). As a result, the normals of imported PSK files will be incorrect when imported into Blender and will need to be manually fixed.

There is a [pull request](https://github.com/gildor2/UEViewer/pull/277) to add support for exporting explicit normals from UE Viewer, although UEViewer's maintainer has seemingly abandoned the project.

# Legacy Compatibility
Below is a table of the latest addon versions that are compatible with older versions of Blender. These versions are no longer maintained and may contain bugs that have been fixed in newer versions. It is recommended to use the latest version of the addon for the best experience.

| Blender Version| Addon Version |
|-|-|
| [4.1](https://www.blender.org/download/releases/4-1/)        | [7.0.0](https://github.com/DarklightGames/io_scene_psk_psa/releases/tag/7.0.0) |
| [4.0](https://www.blender.org/download/releases/4-0/)        | [6.2.1](https://github.com/DarklightGames/io_scene_psk_psa/releases/tag/6.2.1) |
| [3.4 - 3.6](https://www.blender.org/download/lts/3-6/)       | [5.0.6](https://github.com/DarklightGames/io_scene_psk_psa/releases/tag/5.0.6) |
| [2.93 - 3.3](https://www.blender.org/download/releases/3-3/) | [4.3.0](https://github.com/DarklightGames/io_scene_psk_psa/releases/tag/4.3.0) |

# Testing
To execute the automated tests, run:

```
./test.sh
````

This will create a [Docker](https://www.docker.com/) container with and run the tests inside it. The tests are executed using [pytest](https://docs.pytest.org/en/stable/) and the results will be displayed in the terminal.

For now, the tests are not exhaustive and primarily focus on sanity checking the most common use cases (PSK & PSA import). New tests will likely be made to cover new features and prevent further regressions of reported issues.

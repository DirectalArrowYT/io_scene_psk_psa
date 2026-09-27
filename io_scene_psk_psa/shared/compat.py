"""
Compatibility helpers for API differences between Blender 4.5 LTS and Blender 5.x.
"""
from bpy.types import Action, ActionSlot, FCurve


def action_ensure_channelbag_for_slot(action: Action, slot: ActionSlot):
    """
    Returns the channelbag for the given slot, creating the layer, keyframe strip and channelbag if necessary.

    Blender 5.0 provides `bpy_extras.anim_utils.action_ensure_channelbag_for_slot`, but it does not exist in 4.5.
    """
    try:
        from bpy_extras.anim_utils import action_ensure_channelbag_for_slot as _ensure
        return _ensure(action, slot)
    except ImportError:
        pass
    if len(action.layers) == 0:
        layer = action.layers.new('Layer')
    else:
        layer = action.layers[0]
    if len(layer.strips) == 0:
        strip = layer.strips.new(type='KEYFRAME')
    else:
        strip = layer.strips[0]
    return strip.channelbag(slot, ensure=True)


def channelbag_ensure_fcurve(channelbag, data_path: str, index: int = 0, group_name: str = '') -> FCurve:
    """
    Returns the F-Curve for the given data path and index, creating it if necessary.

    `ActionChannelbagFCurves.ensure` was added in Blender 5.0.
    """
    fcurves = channelbag.fcurves
    if hasattr(fcurves, 'ensure'):
        return fcurves.ensure(data_path, index=index, group_name=group_name)
    fcurve = fcurves.find(data_path, index=index)
    if fcurve is not None:
        return fcurve
    if group_name:
        group = channelbag.groups.get(group_name)
        if group is None:
            group = channelbag.groups.new(group_name)
        fcurve = fcurves.new(data_path, index=index)
        fcurve.group = group
        return fcurve
    return fcurves.new(data_path, index=index)


def action_ensure_slot(action: Action, id_type: str, name: str) -> ActionSlot:
    """
    Returns the slot with the given ID type and name, creating it if necessary.
    """
    for slot in action.slots:
        if slot.target_id_type == id_type and slot.name_display == name:
            return slot
    return action.slots.new(id_type, name)

"""
Skeleton analysis for the "Animate Ready" control rig.

This module figures out which bones of an arbitrary (usually humanoid) skeleton play which role (e.g., upper arm, hand,
foot, eye, eyelid, etc.). It works on plain data (names, parents, head positions) so that it can be used both while
the armature is in edit mode and in pose mode.

Bone names are matched against the naming conventions of many different games & tools (e.g., Unreal Engine
mannequins, 3ds Max Biped, Mixamo, Source engine, and many Japanese-style conventions such as `L_arm`, `L_elbow`),
while the sides (left/right), up and forward directions are determined from the bone positions, which is much more
reliable than names.
"""
import re
from dataclasses import dataclass, field

from mathutils import Vector


FINGER_NAMES = ('thumb', 'index', 'middle', 'ring', 'pinky')

# Tokens that mark a bone as a helper (i.e., not part of the main deformation chain).
_HELPER_TOKENS = {
    'ex', 'twist', 'twst', 'roll', 'ik', 'fk', 'ctrl', 'control', 'helper', 'hlp', 'socket', 'weapon', 'wpn', 'prop',
    'attach', 'attachment', 'end', 'nub', 'target', 'tgt', 'pole', 'dummy', 'null', 'adjust', 'adj', 'corrective',
    'correct', 'fix', 'offset', 'scale', 'grip', 'slot', 'point', 'mch', 'icon', 'effect', 'fx', 'camera', 'cam',
    'upvector', 'aim', 'sub', 'bend', 'volume', 'vol', 'mus', 'muscle', 'jiggle', 'phys', 'physics', 'dyn', 'cloth',
}

_FINGER_KEYWORDS = {
    'thumb': ('thumb', 'pollex'),
    'index': ('index', 'pointer'),
    'middle': ('middle', 'mid'),
    'ring': ('ring',),
    'pinky': ('pinky', 'little', 'small', 'pinkie'),
}

_FACE_KEYWORDS = (
    'brow', 'lid', 'lash', 'mouth', 'lip', 'cheek', 'nose', 'nostril', 'chin', 'jaw', 'tongue', 'teeth', 'tooth',
    'ear', 'forehead', 'faceline', 'face', 'eye', 'squint', 'temple', 'smile', 'corner', 'philtrum', 'dimple', 'mentalis',
)


def tokenize(name: str) -> list[str]:
    """
    Splits a bone name into lower-case tokens (e.g., `mixamorig:LeftHandIndex1` -> [mixamorig, left, hand, index, 1]).
    """
    s = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    s = re.sub(r'([A-Za-z])(\d)', r'\1 \2', s)
    s = re.sub(r'(\d)([A-Za-z])', r'\1 \2', s)
    return [t for t in re.split(r'[^A-Za-z0-9]+', s.lower()) if t]


_SIDE_TOKENS = {'l', 'r', 'left', 'right', 'lf', 'rt', 'lft', 'rgt'}


def _core_tokens(name: str) -> list[str]:
    return [t for t in tokenize(name) if t not in _SIDE_TOKENS]


def _is_helper(name: str) -> bool:
    return any(t in _HELPER_TOKENS for t in tokenize(name))


@dataclass
class BoneInfo:
    name: str
    parent: str | None
    head: Vector
    children: list[str] = field(default_factory=list)


@dataclass
class ArmChain:
    side: str
    clavicle: str | None
    upper: str
    lower: str
    hand: str
    # Finger name (e.g., 'index') -> chain of bone names from the base to the tip.
    fingers: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class LegChain:
    side: str
    upper: str
    lower: str
    foot: str
    toe: str | None


@dataclass
class FaceMap:
    brows: dict[str, list[str]] = field(default_factory=dict)          # side -> bones
    upper_lids: dict[str, list[str]] = field(default_factory=dict)     # side -> bones
    lower_lids: dict[str, list[str]] = field(default_factory=dict)     # side -> bones
    upper_mouth: list[str] = field(default_factory=list)               # upper lip & mouth bones
    lower_mouth: list[str] = field(default_factory=list)               # lower lip & mouth bones (usually jaw children)
    other: list[str] = field(default_factory=list)                     # all other facial bones (cheeks, nose, etc.)

    @property
    def all_bones(self) -> list[str]:
        bones = []
        for d in (self.brows, self.upper_lids, self.lower_lids):
            for v in d.values():
                bones.extend(v)
        bones.extend(self.upper_mouth)
        bones.extend(self.lower_mouth)
        bones.extend(self.other)
        return bones


@dataclass
class SkeletonMap:
    root: str | None = None
    pelvis: str | None = None
    spine: list[str] = field(default_factory=list)
    neck: str | None = None
    head: str | None = None
    jaw: str | None = None
    eyes: dict[str, str] = field(default_factory=dict)
    arms: dict[str, ArmChain] = field(default_factory=dict)
    legs: dict[str, LegChain] = field(default_factory=dict)
    face: FaceMap = field(default_factory=FaceMap)
    up: Vector = field(default_factory=lambda: Vector((0.0, 0.0, 1.0)))
    forward: Vector = field(default_factory=lambda: Vector((0.0, -1.0, 0.0)))
    # Side vector points to the character's left.
    left: Vector = field(default_factory=lambda: Vector((1.0, 0.0, 0.0)))
    height: float = 1.0


class Skeleton:
    def __init__(self, bones: list[BoneInfo]):
        self.bones: dict[str, BoneInfo] = {b.name: b for b in bones}
        self.order = [b.name for b in bones]
        for b in bones:
            b.children = []
        for b in bones:
            if b.parent is not None and b.parent in self.bones:
                self.bones[b.parent].children.append(b.name)
        self._descendant_counts: dict[str, int] = {}
        heads = [b.head for b in bones]
        if heads:
            mins = Vector((min(h[i] for h in heads) for i in range(3)))
            maxs = Vector((max(h[i] for h in heads) for i in range(3)))
            self.extent = max((maxs - mins).length, 1e-6)
        else:
            self.extent = 1.0
        # Distances below this are considered to be "the same position".
        self.epsilon = self.extent * 1e-4

    def head(self, name: str) -> Vector:
        return self.bones[name].head

    def parent(self, name: str) -> str | None:
        return self.bones[name].parent

    def children(self, name: str) -> list[str]:
        return self.bones[name].children

    def descendant_count(self, name: str) -> int:
        if name not in self._descendant_counts:
            self._descendant_counts[name] = sum(1 + self.descendant_count(c) for c in self.children(name))
        return self._descendant_counts[name]

    def descendants(self, name: str) -> list[str]:
        result = []
        stack = list(reversed(self.children(name)))
        while stack:
            n = stack.pop()
            result.append(n)
            stack.extend(reversed(self.children(n)))
        return result

    def ancestors(self, name: str) -> list[str]:
        result = []
        p = self.parent(name)
        while p is not None:
            result.append(p)
            p = self.parent(p)
        return result

    def is_ancestor(self, ancestor: str, name: str) -> bool:
        return ancestor in self.ancestors(name)

    def distance(self, a: str, b: str) -> float:
        return (self.head(a) - self.head(b)).length

    def distinct_ancestor(self, name: str) -> str | None:
        """
        Returns the nearest ancestor that is not at the same position as the bone.
        """
        p = self.parent(name)
        while p is not None and self.distance(p, name) <= self.epsilon:
            p = self.parent(p)
        return p

    def distinct_descendants(self, name: str) -> list[str]:
        """
        Returns the nearest descendants (through chains of coincident bones) that are not at the same position.
        """
        result = []
        stack = list(self.children(name))
        while stack:
            n = stack.pop()
            if self.distance(n, name) > self.epsilon:
                result.append(n)
            else:
                stack.extend(self.children(n))
        return result


def _snap_to_axis(v: Vector, threshold: float = 0.95) -> Vector:
    v = v.normalized()
    for i in range(3):
        if abs(v[i]) >= threshold:
            axis = Vector((0.0, 0.0, 0.0))
            axis[i] = 1.0 if v[i] > 0 else -1.0
            return axis
    return v


def _side_of(skeleton: Skeleton, name: str, center: Vector, left: Vector) -> str:
    d = (skeleton.head(name) - center).dot(left)
    if d > skeleton.epsilon * 10:
        return 'L'
    if d < -skeleton.epsilon * 10:
        return 'R'
    return 'C'


def _find_by_keywords(skeleton: Skeleton, keywords: tuple[str, ...], exclude_keywords: tuple[str, ...] = (), exact_token: bool = False) -> list[str]:
    result = []
    for name in skeleton.order:
        if _is_helper(name):
            continue
        tokens = _core_tokens(name)
        compact = ''.join(tokens)
        if any(k in compact for k in exclude_keywords):
            continue
        if exact_token:
            if any(k in tokens for k in keywords):
                result.append(name)
        elif any(k in compact for k in keywords):
            result.append(name)
    return result


def _finger_kind(name: str) -> str | None:
    tokens = _core_tokens(name)
    compact = ''.join(tokens)
    for kind, keywords in _FINGER_KEYWORDS.items():
        for keyword in keywords:
            if keyword == 'mid':
                # `mid` is too ambiguous on its own, only accept it as a full token.
                if 'mid' in tokens and ('finger' in compact or 'hand' in compact):
                    return kind
                continue
            if keyword in compact:
                return kind
    # 3ds Max Biped convention: Finger0 = thumb, Finger1 = index, ... Finger01 = thumb (2nd joint), etc.
    for i, token in enumerate(tokens):
        if token in ('finger', 'fing', 'f') and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            digit = int(tokens[i + 1][0])
            if 0 <= digit < len(FINGER_NAMES):
                return FINGER_NAMES[digit]
    return None


def _pick_best(skeleton: Skeleton, names: list[str]) -> str | None:
    if not names:
        return None
    return max(names, key=lambda n: (skeleton.descendant_count(n), -len(n)))


def analyze_skeleton(skeleton: Skeleton) -> SkeletonMap:
    result = SkeletonMap()

    roots = [n for n in skeleton.order if skeleton.parent(n) is None]
    result.root = _pick_best(skeleton, roots)

    # Head & neck.
    heads = [n for n in _find_by_keywords(skeleton, ('head',), exact_token=True)
             if not any(k in ''.join(_core_tokens(n)) for k in ('top', 'end', 'forehead', 'headtop'))]
    result.head = _pick_best(skeleton, heads)
    necks = _find_by_keywords(skeleton, ('neck',), exclude_keywords=('guard', 'back', 'neckline'))
    if result.head is not None:
        necks = [n for n in necks if skeleton.is_ancestor(n, result.head)]
    result.neck = necks[-1] if necks else (skeleton.distinct_ancestor(result.head) if result.head else None)

    # Hands & feet (for determining the sides).
    hands = [n for n in _find_by_keywords(skeleton, ('hand', 'wrist'), exclude_keywords=('handle', 'finger', 'thumb', 'index', 'middle', 'ring', 'pinky', 'little'))
             if _finger_kind(n) is None]
    # Prefer bones named "hand" over "wrist".
    named_hands = [n for n in hands if 'hand' in ''.join(_core_tokens(n))]
    if len(named_hands) >= 2:
        hands = named_hands
    feet = _find_by_keywords(skeleton, ('foot', 'ankle'), exclude_keywords=('toe', 'ball', 'heel', 'footstep', 'ik'))

    # Establish the up direction from the feet to the head, and the left direction from the hands & feet.
    up = Vector((0.0, 0.0, 1.0))
    if result.head is not None and feet:
        feet_center = sum((skeleton.head(f) for f in feet), Vector()) / len(feet)
        v = skeleton.head(result.head) - feet_center
        if v.length > skeleton.epsilon:
            up = v.normalized()
    result.up = up
    # By default, assume Blender's convention (character faces -Y, left is +X).
    left = Vector((1.0, 0.0, 0.0))
    left = (left - up * left.dot(up))
    left = left.normalized() if left.length > 1e-6 else Vector((1.0, 0.0, 0.0))
    center = skeleton.head(result.head) if result.head else Vector()
    # Try to figure out the left direction from the names of the hands & feet.
    lr_vector = Vector()
    for n in hands + feet:
        tokens = tokenize(n)
        is_left = any(t in ('l', 'left', 'lf', 'lft') for t in tokens) or n.lower().startswith('l')
        is_right = any(t in ('r', 'right', 'rt', 'rgt') for t in tokens) or n.lower().startswith('r')
        if is_left == is_right:
            continue
        v = skeleton.head(n) - center
        v = v - up * v.dot(up)
        lr_vector += v if is_left else -v
    if lr_vector.length > skeleton.epsilon:
        left = lr_vector.normalized()
    # Snap the up & left directions to the principal axes if they are close to them (they usually are).
    up = _snap_to_axis(up)
    left = _snap_to_axis(left - up * left.dot(up))
    result.up = up
    result.left = left
    # Right-handed: left x up = forward (e.g., in Blender's convention, X x Z = -Y).
    result.forward = left.cross(up).normalized()

    heights = [(skeleton.head(n) - center).dot(up) for n in skeleton.order]
    result.height = max(max(heights) - min(heights), skeleton.epsilon) if heights else 1.0

    # Arms.
    for hand in hands:
        side = _side_of(skeleton, hand, center, left)
        if side == 'C':
            continue
        lower = skeleton.distinct_ancestor(hand)
        upper = skeleton.distinct_ancestor(lower) if lower else None
        if lower is None or upper is None:
            continue
        upper_length = skeleton.distance(upper, lower)
        lower_length = skeleton.distance(lower, hand)
        if not (0.33 < upper_length / max(lower_length, 1e-9) < 3.0):
            continue
        clavicle = skeleton.distinct_ancestor(upper)
        chain = ArmChain(side=side, clavicle=clavicle, upper=upper, lower=lower, hand=hand)
        # Fingers.
        for name in skeleton.descendants(hand):
            kind = _finger_kind(name)
            if kind is None or _is_helper(name):
                continue
            parent = skeleton.parent(name)
            if parent is not None and _finger_kind(parent) == kind:
                continue
            if kind in chain.fingers:
                continue
            bones = [name]
            while True:
                children = [c for c in skeleton.children(bones[-1]) if _finger_kind(c) == kind and not _is_helper(c)]
                if not children:
                    break
                bones.append(max(children, key=lambda c: skeleton.descendant_count(c)))
            chain.fingers[kind] = bones
        existing = result.arms.get(side)
        if existing is None or skeleton.descendant_count(hand) > skeleton.descendant_count(existing.hand):
            result.arms[side] = chain

    # Legs.
    for foot in feet:
        side = _side_of(skeleton, foot, center, left)
        if side == 'C':
            continue
        # The foot must not be a descendant of the arm chains (e.g., "ankle" bones on a weapon).
        lower = skeleton.distinct_ancestor(foot)
        upper = skeleton.distinct_ancestor(lower) if lower else None
        if lower is None or upper is None:
            continue
        upper_length = skeleton.distance(upper, lower)
        lower_length = skeleton.distance(lower, foot)
        if not (0.33 < upper_length / max(lower_length, 1e-9) < 3.0):
            continue
        toes = [c for c in skeleton.children(foot) if not _is_helper(c) and skeleton.distance(c, foot) > skeleton.epsilon]
        named_toes = [c for c in toes if any(k in ''.join(_core_tokens(c)) for k in ('toe', 'ball'))]
        toe = _pick_best(skeleton, named_toes) or _pick_best(skeleton, toes)
        chain = LegChain(side=side, upper=upper, lower=lower, foot=foot, toe=toe)
        existing = result.legs.get(side)
        if existing is None or skeleton.descendant_count(foot) > skeleton.descendant_count(existing.foot):
            result.legs[side] = chain

    # Pelvis & spine.
    if result.legs:
        thighs = [leg.upper for leg in result.legs.values()]
        common = [a for a in skeleton.ancestors(thighs[0]) if all(a in skeleton.ancestors(t) for t in thighs)]
        pelvis_candidates = [n for n in common if any(k in ''.join(_core_tokens(n)) for k in ('pelvis', 'hip', 'cog', 'waist'))]
        result.pelvis = pelvis_candidates[0] if pelvis_candidates else (common[0] if common else None)
    if result.neck is not None:
        spine = []
        p = skeleton.distinct_ancestor(result.neck)
        while p is not None and p != result.pelvis and p != result.root:
            spine.append(p)
            p = skeleton.parent(p)
        result.spine = list(reversed(spine))

    # Face.
    if result.head is not None:
        _analyze_face(skeleton, result)

    return result


def _analyze_face(skeleton: Skeleton, result: SkeletonMap):
    head = result.head
    assert head is not None
    up, left = result.up, result.left
    head_position = skeleton.head(head)

    descendants = skeleton.descendants(head)

    # Eyes.
    for name in descendants:
        tokens = _core_tokens(name)
        if not ('eye' in tokens or 'eyeball' in tokens or ''.join(tokens) in ('eye', 'eyeball')):
            continue
        if any(k in ''.join(tokens) for k in ('lid', 'brow', 'lash', 'socket', 'shadow', 'line', 'target', 'look', 'highlight')):
            continue
        if _is_helper(name):
            continue
        side = _side_of(skeleton, name, head_position, left)
        if side == 'C':
            continue
        if side not in result.eyes:
            result.eyes[side] = name

    # Jaw.
    jaws = [n for n in descendants if 'jaw' in ''.join(_core_tokens(n)) and not _is_helper(n)]
    result.jaw = _pick_best(skeleton, jaws)

    excluded = set(result.eyes.values())
    if result.jaw is not None:
        excluded.add(result.jaw)

    face = FaceMap()
    eye_centers = {side: skeleton.head(name) for side, name in result.eyes.items()}

    mouth_bones = []
    for name in descendants:
        if name in excluded or _is_helper(name):
            continue
        compact = ''.join(_core_tokens(name))
        if not any(k in compact for k in _FACE_KEYWORDS):
            # Only consider bones in the face (e.g., hair or accessory bones are not facial bones).
            parent = skeleton.parent(name)
            if parent is None or parent == head or not any(k in ''.join(_core_tokens(parent)) for k in _FACE_KEYWORDS):
                continue
        side = _side_of(skeleton, name, head_position, left)
        if 'brow' in compact:
            # Only the top-most brow bones are moved by the brow controls.
            parent = skeleton.parent(name)
            if parent is not None and 'brow' in ''.join(_core_tokens(parent)):
                continue
            face.brows.setdefault(side if side != 'C' else 'C', []).append(name)
        elif ('lid' in compact or 'lash' in compact) and 'line' not in compact:
            if side == 'C':
                face.other.append(name)
                continue
            # Sorted into upper & lower lids below.
            face.upper_lids.setdefault(side, []).append(name)
        elif 'lip' in compact or 'mouth' in compact:
            mouth_bones.append(name)
        elif compact in ('face', 'facial', 'facialroot', 'faceroot'):
            continue
        else:
            face.other.append(name)

    # Split the eyelids into the upper & lower lids. The line between the inner-most and outer-most lid bones (i.e.,
    # the corners of the eye) is used as the dividing line, since eyes are often tilted.
    for side, lids in list(face.upper_lids.items()):
        if len(lids) < 3:
            eye_center = eye_centers.get(side)
            if eye_center is not None:
                face.upper_lids[side] = [n for n in lids if (skeleton.head(n) - eye_center).dot(up) >= 0.0]
                face.lower_lids[side] = [n for n in lids if (skeleton.head(n) - eye_center).dot(up) < 0.0]
            continue
        lateral = {n: skeleton.head(n).dot(left) for n in lids}
        inner = min(lids, key=lambda n: abs(lateral[n]))
        outer = max(lids, key=lambda n: abs(lateral[n]))
        a, b = skeleton.head(inner), skeleton.head(outer)
        span = (b - a).dot(left)
        upper, lower = [], []
        for n in lids:
            if n in (inner, outer):
                # The corners of the eye don't move when blinking.
                face.other.append(n)
                continue
            t = (skeleton.head(n) - a).dot(left) / span if abs(span) > skeleton.epsilon else 0.0
            line_height = a.dot(up) + t * (b - a).dot(up)
            (upper if skeleton.head(n).dot(up) >= line_height else lower).append(n)
        face.upper_lids[side] = upper
        face.lower_lids[side] = lower

    # Split the mouth into the upper & lower parts. Bones that follow the jaw belong to the lower mouth.
    if mouth_bones:
        for name in mouth_bones:
            if result.jaw is not None and skeleton.is_ancestor(result.jaw, name):
                face.lower_mouth.append(name)
            else:
                face.upper_mouth.append(name)
        if not face.lower_mouth or not face.upper_mouth:
            # No jaw hierarchy, use the height of the bones instead.
            mean = sum(skeleton.head(n).dot(up) for n in mouth_bones) / len(mouth_bones)
            face.upper_mouth = [n for n in mouth_bones if skeleton.head(n).dot(up) >= mean]
            face.lower_mouth = [n for n in mouth_bones if skeleton.head(n).dot(up) < mean]

    result.face = face

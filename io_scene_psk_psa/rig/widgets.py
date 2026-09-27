"""
Custom shape ("widget") meshes for the control rig bones.

The widget objects are not linked to any collection in the scene, so they never clutter the scene or get exported;
they are kept alive by the pose bones that use them as custom shapes.
"""
import math

import bpy
from bpy.types import Object

WIDGET_PREFIX = 'WGT-psk_'


def _circle(segments: int = 32, radius: float = 1.0, plane: str = 'XZ', offset: float = 0.0):
    vertices = []
    for i in range(segments):
        a = 2.0 * math.pi * i / segments
        u, v = math.cos(a) * radius, math.sin(a) * radius
        match plane:
            case 'XZ':
                vertices.append((u, offset, v))
            case 'XY':
                vertices.append((u, v, offset))
            case 'YZ':
                vertices.append((offset, u, v))
    edges = [(i, (i + 1) % segments) for i in range(segments)]
    return vertices, edges


def _merge(*shapes):
    vertices, edges = [], []
    for v, e in shapes:
        o = len(vertices)
        vertices.extend(v)
        edges.extend((a + o, b + o) for a, b in e)
    return vertices, edges


def _box(sx: float = 1.0, sy: float = 1.0, sz: float = 1.0, y_offset: float = 0.0):
    vertices = [(x * sx, y * sy + y_offset, z * sz) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
    edges = []
    for i in range(8):
        for j in range(i + 1, 8):
            a, b = vertices[i], vertices[j]
            if sum(1 for k in range(3) if a[k] != b[k]) == 1:
                edges.append((i, j))
    return vertices, edges


def _rectangle(sx: float, sz: float, y: float = 0.0, corner: float = 0.0):
    # A (rounded) rectangle in the XZ plane.
    if corner <= 0.0:
        vertices = [(-sx, y, -sz), (sx, y, -sz), (sx, y, sz), (-sx, y, sz)]
        return vertices, [(0, 1), (1, 2), (2, 3), (3, 0)]
    vertices = []
    for cx, cz, start in ((sx - corner, sz - corner, 0), (-sx + corner, sz - corner, 90), (-sx + corner, -sz + corner, 180), (sx - corner, -sz + corner, 270)):
        for i in range(5):
            a = math.radians(start + i * 22.5)
            vertices.append((cx + math.cos(a) * corner, y, cz + math.sin(a) * corner))
    edges = [(i, (i + 1) % len(vertices)) for i in range(len(vertices))]
    return vertices, edges


def _shape_data(name: str):
    match name:
        case 'circle':
            return _circle()
        case 'circle_thick':
            return _merge(_circle(), _circle(radius=0.9))
        case 'sphere':
            return _merge(_circle(plane='XZ'), _circle(plane='XY'), _circle(plane='YZ'))
        case 'cube':
            return _box()
        case 'square':
            return _rectangle(1.0, 1.0)
        case 'rounded_square':
            return _rectangle(1.0, 1.0, corner=0.3)
        case 'foot':
            # A foot shaped rectangle on the XY plane (the bone's Y axis points forward, Z is up).
            v, e = _rectangle(1.0, 1.0, corner=0.35)
            return [(x, z, y) for x, y, z in v], e
        case 'diamond':
            vertices = [(1, 0, 0), (0, 0, 1), (-1, 0, 0), (0, 0, -1), (0, 1, 0), (0, -1, 0)]
            edges = [(0, 1), (1, 2), (2, 3), (3, 0), (0, 4), (1, 4), (2, 4), (3, 4), (0, 5), (1, 5), (2, 5), (3, 5)]
            return vertices, edges
        case 'eyes':
            # Two circles joined by a rounded rectangle (like a pair of glasses).
            return _merge(_rectangle(2.0, 1.0, corner=0.9))
        case 'arrow_updown':
            vertices = [(0, 0, 1), (0.4, 0, 0.6), (-0.4, 0, 0.6), (0, 0, -1), (0.4, 0, -0.6), (-0.4, 0, -0.6)]
            edges = [(0, 3), (0, 1), (0, 2), (3, 4), (3, 5)]
            return vertices, edges
        case 'fingers':
            # A fan of lines for the finger master control.
            vertices, edges = _rectangle(1.0, 0.25, corner=0.2)
            return vertices, edges
        case _:
            raise ValueError(f'Unknown widget: {name}')


def get_widget(name: str) -> Object:
    object_name = WIDGET_PREFIX + name
    obj = bpy.data.objects.get(object_name)
    if obj is not None and obj.type == 'MESH':
        return obj
    vertices, edges = _shape_data(name)
    mesh = bpy.data.meshes.new(object_name)
    mesh.from_pydata(vertices, edges, [])
    mesh.update()
    obj = bpy.data.objects.new(object_name, mesh)
    return obj

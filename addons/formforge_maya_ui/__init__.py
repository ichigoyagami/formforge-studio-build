"""FormForge Studio: a Maya-inspired workflow layer for Blender.

This add-on deliberately uses Blender's public Python API so the same workflow
layer can run in the stable packaged runtime and in the supplied Blender 5.3
source tree. It does not copy Autodesk code or assets.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import traceback
from datetime import datetime, timezone

import bpy
import bmesh
from bpy.props import BoolProperty, EnumProperty, PointerProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper
from bpy_extras import view3d_utils
from mathutils import Vector
from mathutils.bvhtree import BVHTree


bl_info = {
    "name": "FormForge Studio - Maya Workflow",
    "author": "FormForge Studio contributors",
    "version": (0, 19, 0),
    "blender": (4, 5, 0),
    "location": "3D Viewport > FormForge",
    "description": "Maya-inspired menus, shelf, channel box and developer diagnostics",
    "category": "3D View",
}

LOG_TEXT_NAME = "FormForge Developer Log"
ADDON_VERSION = ".".join(map(str, bl_info["version"]))
_ORIGINAL_HELP_DRAW = bpy.types.TOPBAR_MT_help.draw
_ORIGINAL_TOPBAR_DRAW = bpy.types.TOPBAR_MT_editor_menus.draw
_ORIGINAL_VIEW3D_MENU_DRAW = bpy.types.VIEW3D_MT_editor_menus.draw
_ADDON_KEYMAPS = []
_TEMP_TOOL_STATE = {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log_path() -> str:
    try:
        base = bpy.utils.user_resource("CONFIG", path="formforge", create=False)
        os.makedirs(base, exist_ok=True)
    except OSError:
        base = os.path.join(tempfile.gettempdir(), "formforge")
        os.makedirs(base, exist_ok=True)
    return os.path.join(base, "formforge-dev.log")


def log_event(level: str, message: str, **details) -> None:
    """Write one JSON line to both the internal text block and disk log."""
    entry = {
        "time": _utc_now(),
        "level": level.upper(),
        "message": message,
        "details": details,
    }
    line = json.dumps(entry, ensure_ascii=False, default=str)
    try:
        text = bpy.data.texts.get(LOG_TEXT_NAME) or bpy.data.texts.new(LOG_TEXT_NAME)
        text.write(line + "\n")
    except Exception:
        pass
    try:
        with open(_log_path(), "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _active_mesh(context):
    obj = context.active_object
    return obj if obj and obj.type == "MESH" else None


class FORGE_Preferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    maya_navigation: BoolProperty(
        name="Maya Navigation",
        description="Use Blender's Industry Compatible keymap where available",
        default=True,
    )
    auto_dev_log: BoolProperty(
        name="Developer Logging",
        description="Record FormForge actions and exceptions",
        default=True,
    )
    confirm_destructive: BoolProperty(
        name="Confirm destructive operations",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "maya_navigation")
        layout.prop(self, "auto_dev_log")
        layout.prop(self, "confirm_destructive")
        layout.operator("formforge.apply_maya_setup", icon="PREFERENCES")
        layout.operator("formforge.open_dev_log", icon="TEXT")


class FORGE_OT_apply_maya_setup(bpy.types.Operator):
    bl_idname = "formforge.apply_maya_setup"
    bl_label = "Apply Maya-style Setup"
    bl_description = "Apply the dark studio theme, Maya navigation and viewport defaults"
    bl_options = {"REGISTER"}

    def execute(self, context):
        warnings = []
        try:
            theme = context.preferences.themes[0]
            view = theme.view_3d.space
            # Neutral Maya-style graphite palette with a mid-grey viewport,
            # dark chrome and cyan active-tool accents.
            view.back = (0.235, 0.235, 0.235)
            view.text = (0.82, 0.82, 0.82)
            view.header = (0.145, 0.145, 0.145, 1.0)
            view.button = (0.21, 0.21, 0.21, 1.0)
            view.button_title = (0.84, 0.84, 0.84, 1.0)
            theme.user_interface.wcol_tool.inner = (0.16, 0.16, 0.16, 1.0)
            theme.user_interface.wcol_tool.inner_sel = (0.17, 0.48, 0.62, 1.0)
            for editor_name in ("outliner", "properties", "dopesheet_editor", "topbar"):
                editor = getattr(theme, editor_name, None)
                if editor and hasattr(editor, "space"):
                    editor.space.back = (0.115, 0.115, 0.115)
                    editor.space.header = (0.145, 0.145, 0.145, 1.0)
            context.preferences.view.show_splash = False
            context.preferences.view.show_statusbar_stats = True
        except Exception as exc:
            warnings.append(f"theme: {exc}")

        try:
            preset = bpy.utils.preset_find("Industry_Compatible", "keyconfig")
            if preset:
                bpy.ops.preferences.keyconfig_activate(filepath=preset)
            else:
                warnings.append("Industry Compatible keymap was not found")
        except Exception as exc:
            warnings.append(f"keymap: {exc}")

        for area in context.screen.areas if context.screen else []:
            if area.type != "VIEW_3D":
                continue
            try:
                space = area.spaces.active
                space.overlay.show_stats = True
                space.overlay.show_text = True
                space.shading.type = "SOLID"
                space.shading.light = "STUDIO"
                space.shading.color_type = "MATERIAL"
            except Exception as exc:
                warnings.append(f"viewport: {exc}")

        log_event("INFO", "Maya-style setup applied", warnings=warnings)
        if warnings:
            self.report({"WARNING"}, "Setup applied with notes; see Developer Log")
        else:
            self.report({"INFO"}, "Maya-style setup applied")
        return {"FINISHED"}


class FORGE_OT_add_primitive(bpy.types.Operator):
    bl_idname = "formforge.add_primitive"
    bl_label = "Create Primitive"
    bl_options = {"REGISTER", "UNDO"}

    primitive: EnumProperty(
        items=(
            ("CUBE", "Cube", "Polygon cube"),
            ("SPHERE", "Sphere", "UV sphere"),
            ("CYLINDER", "Cylinder", "Polygon cylinder"),
            ("PLANE", "Plane", "Polygon plane"),
            ("TORUS", "Torus", "Polygon torus"),
            ("CONE", "Cone", "Polygon cone"),
        ),
        default="CUBE",
    )

    def execute(self, _context):
        operators = {
            "CUBE": bpy.ops.mesh.primitive_cube_add,
            "SPHERE": bpy.ops.mesh.primitive_uv_sphere_add,
            "CYLINDER": bpy.ops.mesh.primitive_cylinder_add,
            "PLANE": bpy.ops.mesh.primitive_plane_add,
            "TORUS": bpy.ops.mesh.primitive_torus_add,
            "CONE": bpy.ops.mesh.primitive_cone_add,
        }
        try:
            operators[self.primitive]()
            log_event("INFO", "Primitive created", primitive=self.primitive)
            return {"FINISHED"}
        except Exception as exc:
            log_event("ERROR", "Primitive creation failed", error=str(exc))
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class FORGE_OT_freeze_transforms(bpy.types.Operator):
    bl_idname = "formforge.freeze_transforms"
    bl_label = "Freeze Transformations"
    bl_description = "Apply location, rotation and scale like Maya Freeze Transformations"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not context.selected_editable_objects:
            self.report({"WARNING"}, "Select at least one object")
            return {"CANCELLED"}
        try:
            bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
            log_event("INFO", "Transforms frozen", count=len(context.selected_editable_objects))
            return {"FINISHED"}
        except Exception as exc:
            log_event("ERROR", "Freeze transformations failed", error=str(exc))
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class FORGE_OT_center_pivot(bpy.types.Operator):
    bl_idname = "formforge.center_pivot"
    bl_label = "Center Pivot"
    bl_description = "Move the selected object's pivot to the center of its geometry"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not context.selected_editable_objects:
            self.report({"WARNING"}, "Select at least one object")
            return {"CANCELLED"}
        bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
        log_event("INFO", "Pivots centered", count=len(context.selected_editable_objects))
        self.report({"INFO"}, "Pivot centered on selected geometry")
        return {"FINISHED"}


class FORGE_OT_component_mode(bpy.types.Operator):
    bl_idname = "formforge.component_mode"
    bl_label = "Component Selection Mode"
    bl_options = {"REGISTER"}

    mode: EnumProperty(
        items=(
            ("OBJECT", "Object", "Object selection"),
            ("VERT", "Vertex", "Vertex component selection"),
            ("EDGE", "Edge", "Edge component selection"),
            ("FACE", "Face", "Face component selection"),
        ),
        default="OBJECT",
    )

    def execute(self, context):
        obj = context.active_object
        if self.mode == "OBJECT":
            if context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            return {"FINISHED"}
        if not obj or obj.type != "MESH":
            self.report({"WARNING"}, "Select a polygon mesh first")
            return {"CANCELLED"}
        if context.mode != "EDIT_MESH":
            bpy.ops.object.mode_set(mode="EDIT")
        selection = {
            "VERT": (True, False, False),
            "EDGE": (False, True, False),
            "FACE": (False, False, True),
        }
        context.tool_settings.mesh_select_mode = selection[self.mode]
        log_event("INFO", "Component mode changed", mode=self.mode)
        return {"FINISHED"}


class FORGE_OT_multi_cut(bpy.types.Operator):
    bl_idname = "formforge.multi_cut"
    bl_label = "Multi-Cut"
    bl_description = "Start the interactive multi-cut tool on the active mesh"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not context.active_object or context.active_object.type != "MESH":
            self.report({"WARNING"}, "Select a polygon mesh first")
            return {"CANCELLED"}
        if context.mode != "EDIT_MESH":
            bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.wm.tool_set_by_id(name="builtin.knife")
        log_event("INFO", "Multi-Cut started")
        self.report({"INFO"}, "Multi-Cut active: click cuts, Enter/right-click confirms, Esc cancels")
        return {"FINISHED"}


class FORGE_OT_select_tool(bpy.types.Operator):
    bl_idname = "formforge.select_tool"
    bl_label = "Select Tool"
    bl_description = "Return to the Select Tool"

    def execute(self, _context):
        bpy.ops.wm.tool_set_by_id(name="builtin.select_box")
        return {"FINISHED"}


class FORGE_OT_repeat_last(bpy.types.Operator):
    bl_idname = "formforge.repeat_last"
    bl_label = "Repeat Last Command"
    bl_description = "Repeat the most recently executed command"

    def execute(self, _context):
        return bpy.ops.screen.repeat_last()


class FORGE_OT_save_project(bpy.types.Operator, ExportHelper):
    bl_idname = "formforge.save_project"
    bl_label = "Save FormForge Project"
    bl_description = "Save the current project with FormForge's .forge extension"
    filename_ext = ".forge"
    filter_glob: StringProperty(default="*.forge", options={"HIDDEN"})

    def execute(self, _context):
        path = bpy.path.ensure_ext(self.filepath, self.filename_ext)
        result = bpy.ops.wm.save_as_mainfile(filepath=path)
        log_event("INFO", "FormForge project saved", filepath=path)
        return result


class FORGE_OT_open_project(bpy.types.Operator, ImportHelper):
    bl_idname = "formforge.open_project"
    bl_label = "Open FormForge Project"
    bl_description = "Open a .forge project; legacy .blend projects remain compatible"
    filename_ext = ".forge"
    filter_glob: StringProperty(default="*.forge;*.blend", options={"HIDDEN"})

    def execute(self, _context):
        log_event("INFO", "Opening FormForge project", filepath=self.filepath)
        return bpy.ops.wm.open_mainfile(filepath=self.filepath)


class FORGE_OT_maya_hold_control(bpy.types.Operator):
    bl_idname = "formforge.maya_hold_control"
    bl_label = "Maya Hold Control"
    bl_description = "Temporary Maya-style pivot editing or vertex snapping"

    control: EnumProperty(
        items=(("PIVOT", "Pivot", "Edit object pivot"), ("VERTEX", "Vertex Snap", "Snap to vertices"))
    )
    enabled: BoolProperty(default=True)

    def execute(self, context):
        settings = context.scene.tool_settings
        if self.control == "PIVOT":
            if context.mode != "OBJECT":
                return {"PASS_THROUGH"}
            if self.enabled:
                _TEMP_TOOL_STATE["origin"] = settings.use_transform_data_origin
                settings.use_transform_data_origin = True
                try:
                    bpy.ops.wm.tool_set_by_id(name="builtin.move")
                except RuntimeError:
                    pass
            else:
                settings.use_transform_data_origin = _TEMP_TOOL_STATE.pop("origin", False)
        elif self.control == "VERTEX":
            if self.enabled:
                _TEMP_TOOL_STATE["snap"] = (settings.use_snap, set(settings.snap_elements))
                settings.use_snap = True
                settings.snap_elements = {"VERTEX"}
            else:
                use_snap, elements = _TEMP_TOOL_STATE.pop("snap", (False, {"INCREMENT"}))
                settings.use_snap = use_snap
                settings.snap_elements = elements
        return {"FINISHED"}


class FORGE_OT_extrude_interactive(bpy.types.Operator):
    bl_idname = "formforge.extrude_interactive"
    bl_label = "Extrude"
    bl_description = "Extrude selected components interactively; move the mouse or type a value"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, _event):
        if context.mode != "EDIT_MESH":
            self.report({"WARNING"}, "Extrude requires Mesh Edit Mode")
            return {"CANCELLED"}
        # Invoke the native modal operation immediately. 0.18 only selected the
        # toolbar tool, which looked like a broken button because the user then
        # had to discover and drag a separate handle.
        try:
            result = bpy.ops.mesh.extrude_region_move("INVOKE_DEFAULT")
            self.report({"INFO"}, "Extrude: move/type a distance, click to confirm; F9 reopens options")
            log_event("INFO", "Native interactive Extrude invoked")
            return result
        except RuntimeError as exc:
            self.report({"ERROR"}, "Select one or more mesh components to extrude")
            log_event("ERROR", "Extrude failed to start", error=str(exc))
            return {"CANCELLED"}


class FORGE_OT_bevel_interactive(bpy.types.Operator):
    bl_idname = "formforge.bevel_interactive"
    bl_label = "Bevel"
    bl_description = "Bevel selected edges interactively; mouse sets width and wheel sets segments"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, _event):
        if context.mode != "EDIT_MESH":
            self.report({"WARNING"}, "Bevel requires selected edges in Mesh Edit Mode")
            return {"CANCELLED"}
        try:
            result = bpy.ops.mesh.bevel("INVOKE_DEFAULT", affect="EDGES")
            self.report({"INFO"}, "Bevel: move for width, wheel for segments, click to confirm; F9 reopens options")
            log_event("INFO", "Native interactive Bevel invoked")
            return result
        except RuntimeError as exc:
            self.report({"ERROR"}, "Select one or more edges to bevel")
            log_event("ERROR", "Bevel failed to start", error=str(exc))
            return {"CANCELLED"}


class FORGE_OT_bridge_options(bpy.types.Operator):
    bl_idname = "formforge.bridge_options"
    bl_label = "Bridge Edge Loops Options"
    bl_description = "Bridge selected boundary loops with exposed construction options"
    bl_options = {"REGISTER", "UNDO"}

    number_cuts: bpy.props.IntProperty(name="Divisions", default=0, min=0, max=1000)
    twist_offset: bpy.props.IntProperty(name="Twist", default=0)
    smoothness: bpy.props.FloatProperty(name="Smoothness", default=1.0, min=0.0, max=10.0)
    profile_shape_factor: bpy.props.FloatProperty(name="Profile", default=0.0, min=-1.0, max=1.0)

    def invoke(self, context, _event):
        if context.mode != "EDIT_MESH":
            self.report({"WARNING"}, "Bridge requires two selected boundary loops")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self, width=360)

    def execute(self, _context):
        try:
            result = bpy.ops.mesh.bridge_edge_loops(
                number_cuts=self.number_cuts,
                twist_offset=self.twist_offset,
                smoothness=self.smoothness,
                profile_shape_factor=self.profile_shape_factor,
            )
            log_event("INFO", "Bridge executed", divisions=self.number_cuts, twist=self.twist_offset)
            return result
        except RuntimeError as exc:
            self.report({"ERROR"}, "Select two compatible open edge loops")
            log_event("ERROR", "Bridge failed", error=str(exc))
            return {"CANCELLED"}


def _poll_live_mesh(_scene, obj):
    return obj is not None and obj.type == "MESH"


class FORGE_OT_make_live(bpy.types.Operator):
    bl_idname = "formforge.make_live"
    bl_label = "Make Live"
    bl_description = "Use the selected mesh as the projection surface for Quad Draw"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        target = context.active_object
        if context.mode != "OBJECT" or not target or target.type != "MESH":
            self.report({"WARNING"}, "Select one mesh in Object Mode")
            return {"CANCELLED"}
        previous = context.scene.formforge_live_surface
        if previous and previous != target:
            previous.pop("formforge_live", None)
        context.scene.formforge_live_surface = target
        target["formforge_live"] = True
        target.show_wire = True
        log_event("INFO", "Live surface set", target=target.name)
        self.report({"INFO"}, f"{target.name} is now the live surface")
        return {"FINISHED"}


class FORGE_OT_remove_live(bpy.types.Operator):
    bl_idname = "formforge.remove_live"
    bl_label = "Remove Live"
    bl_description = "Clear the current Quad Draw live surface"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        target = context.scene.formforge_live_surface
        if target:
            name = target.name
            target.pop("formforge_live", None)
            context.scene.formforge_live_surface = None
            log_event("INFO", "Live surface cleared", target=name)
            self.report({"INFO"}, f"Live removed from {name}")
        return {"FINISHED"}


class FORGE_OT_quad_draw_modal(bpy.types.Operator):
    bl_idname = "formforge.quad_draw"
    bl_label = "FormForge Quad Draw"
    bl_description = "Draw connected quad strips directly on a live mesh surface"
    bl_options = {"REGISTER", "UNDO", "BLOCKING"}

    def _surface_hit(self, context, event):
        region = context.region
        rv3d = context.region_data
        coord = (event.mouse_region_x, event.mouse_region_y)
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        local_origin = self.target_inverse @ origin
        local_direction = (self.target_inverse.to_3x3() @ direction).normalized()
        location, normal, _face, _distance = self.surface_bvh.ray_cast(
            local_origin, local_direction
        )
        if location is None:
            return None
        world_location = self.target_matrix @ location
        world_normal = (self.target_matrix.to_3x3() @ normal).normalized()
        return world_location + world_normal * 0.002

    def _sync_mesh(self):
        """Commit topology transactionally without clear_geometry/from_pydata churn."""
        bm = bmesh.new()
        try:
            vertices = [bm.verts.new(Vector(point)) for point in self.points]
            bm.verts.ensure_lookup_table()
            for face in self.faces:
                if len(set(face)) < 3 or any(i >= len(vertices) for i in face):
                    continue
                try:
                    bm.faces.new([vertices[i] for i in face])
                except ValueError:
                    pass
            bm.normal_update()
            bm.to_mesh(self.mesh)
            self.mesh.update()
        finally:
            bm.free()

    def _nearest_vertex(self, context, event, radius=18.0):
        coord = Vector((event.mouse_region_x, event.mouse_region_y))
        nearest = None
        best = radius
        for index, point in enumerate(self.points):
            projected = view3d_utils.location_3d_to_region_2d(
                context.region, context.region_data, Vector(point)
            )
            if projected is None:
                continue
            distance = (projected - coord).length
            if distance < best:
                nearest, best = index, distance
        return nearest

    def _nearest_edge(self, context, event, radius=20.0):
        coord = Vector((event.mouse_region_x, event.mouse_region_y))
        edges = set()
        for face in self.faces:
            for index in range(len(face)):
                edges.add(tuple(sorted((face[index], face[(index + 1) % len(face)]))))
        nearest = None
        best = radius
        for edge in edges:
            a = view3d_utils.location_3d_to_region_2d(
                context.region, context.region_data, Vector(self.points[edge[0]])
            )
            b = view3d_utils.location_3d_to_region_2d(
                context.region, context.region_data, Vector(self.points[edge[1]])
            )
            if a is None or b is None:
                continue
            segment = b - a
            factor = 0.0 if segment.length_squared == 0 else max(
                0.0, min(1.0, (coord - a).dot(segment) / segment.length_squared)
            )
            distance = (coord - (a + segment * factor)).length
            if distance < best:
                nearest, best = edge, distance
        return nearest

    def _project_to_surface(self, _context, world_point):
        location, normal, _face, _distance = self.surface_bvh.find_nearest(
            self.target_inverse @ Vector(world_point)
        )
        if location is None:
            return tuple(world_point)
        point = self.target_matrix @ location
        world_normal = (self.target_matrix.to_3x3() @ normal).normalized()
        return tuple(point + world_normal * 0.002)

    def _delete_vertex(self, index):
        self.faces = [face for face in self.faces if index not in face]
        self.points.pop(index)
        self.faces = [
            tuple(vertex - (vertex > index) for vertex in face) for face in self.faces
        ]
        self.pending = [
            vertex - (vertex > index) for vertex in self.pending if vertex != index
        ]
        if self.last_edge and index in self.last_edge:
            self.last_edge = None
        elif self.last_edge:
            self.last_edge = tuple(vertex - (vertex > index) for vertex in self.last_edge)
        self._sync_mesh()

    def _insert_face_loop(self, context, edge):
        for face_index, face in enumerate(self.faces):
            if len(face) != 4:
                continue
            ordered = list(face)
            found = False
            for offset in range(4):
                rotated = ordered[offset:] + ordered[:offset]
                if {rotated[0], rotated[1]} == set(edge):
                    ordered = rotated
                    found = True
                    break
            if not found:
                continue
            a, b, c, d = ordered
            midpoint_ab = self._project_to_surface(
                context, (Vector(self.points[a]) + Vector(self.points[b])) * 0.5
            )
            midpoint_dc = self._project_to_surface(
                context, (Vector(self.points[d]) + Vector(self.points[c])) * 0.5
            )
            first_mid = len(self.points)
            self.points.extend((midpoint_ab, midpoint_dc))
            second_mid = first_mid + 1
            self.faces[face_index] = (a, first_mid, second_mid, d)
            self.faces.append((first_mid, b, c, second_mid))
            self._sync_mesh()
            return

    def _relax_vertex(self, context, index):
        neighbors = set()
        for face in self.faces:
            if index not in face:
                continue
            position = face.index(index)
            neighbors.add(face[position - 1])
            neighbors.add(face[(position + 1) % len(face)])
        if not neighbors:
            return
        average = sum((Vector(self.points[i]) for i in neighbors), Vector()) / len(neighbors)
        relaxed = Vector(self.points[index]).lerp(average, 0.22)
        self.points[index] = self._project_to_surface(context, relaxed)
        self._sync_mesh()

    def _add_point(self, location):
        self.points.append(tuple(location))
        self.pending.append(len(self.points) - 1)
        if self.last_edge is None and len(self.pending) == 4:
            self.faces.append(tuple(self.pending))
            self.last_edge = (self.pending[3], self.pending[2])
            self.pending.clear()
        elif self.last_edge is not None and len(self.pending) == 2:
            first, second = self.pending
            self.faces.append((self.last_edge[0], self.last_edge[1], second, first))
            self.last_edge = (first, second)
            self.pending.clear()
        self._sync_mesh()

    def _cleanup(self, context, delete_object=False):
        if getattr(self, "cleaned", False):
            return
        self.cleaned = True
        try:
            context.window.cursor_modal_restore()
        except Exception:
            pass
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass
        if delete_object and getattr(self, "retopo", None):
            if self.created_retopo:
                mesh = self.retopo.data
                bpy.data.objects.remove(self.retopo, do_unlink=True)
                if mesh and mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
            elif getattr(self, "backup_mesh", None):
                edited_mesh = self.retopo.data
                self.retopo.data = self.backup_mesh
                self.retopo.matrix_world = self.original_matrix
                if edited_mesh.users == 0:
                    bpy.data.meshes.remove(edited_mesh)

    def _finish(self, context):
        self._cleanup(context)
        context.view_layer.objects.active = self.retopo
        self.retopo.select_set(True)
        if self.points:
            shrink = self.retopo.modifiers.new(
                name="FormForge Live Surface", type="SHRINKWRAP"
            )
            shrink.target = self.target
            shrink.wrap_method = "NEAREST_SURFACEPOINT"
            shrink.wrap_mode = "ABOVE_SURFACE"
            shrink.offset = 0.002
        if getattr(self, "backup_mesh", None) and self.backup_mesh.users == 0:
            bpy.data.meshes.remove(self.backup_mesh)
            self.backup_mesh = None
        log_event(
            "INFO",
            "Quad Draw completed",
            vertices=len(self.points),
            faces=len(self.faces),
            target=self.target.name,
        )

    def _modal_impl(self, context, event):
        # Maya-style camera navigation remains available while Quad Draw owns
        # the modeling clicks. Alt+LMB/MMB/RMB orbit/pan/zoom; wheel, trackpad
        # and 3D-mouse events are also handed back to the viewport.
        if (
            (event.alt and event.type in {"LEFTMOUSE", "MIDDLEMOUSE", "RIGHTMOUSE"})
            or event.type in {
                "WHEELUPMOUSE", "WHEELDOWNMOUSE", "WHEELINMOUSE", "WHEELOUTMOUSE",
                "TRACKPADPAN", "TRACKPADZOOM", "MOUSEROTATE", "MOUSESMARTZOOM",
                "NDOF_MOTION", "NDOF_BUTTON_FIT",
            }
        ):
            return {"PASS_THROUGH"}
        if event.type in {"RET", "NUMPAD_ENTER", "RIGHTMOUSE"} and event.value == "PRESS":
            self._finish(context)
            return {"FINISHED"}
        if event.type == "ESC" and event.value == "PRESS":
            self._cleanup(context, delete_object=True)
            return {"CANCELLED"}
        if event.type == "MIDDLEMOUSE":
            if event.value == "PRESS":
                self.tweak_index = self._nearest_vertex(context, event)
                if self.tweak_index is not None:
                    return {"RUNNING_MODAL"}
            elif event.value == "RELEASE":
                self.tweak_index = None
                return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            self.relax_index = None
            return {"RUNNING_MODAL"}
        if event.type == "TAB":
            self.tab_extend = event.value == "PRESS"
            return {"RUNNING_MODAL"}
        if event.type == "MOUSEMOVE" and self.tweak_index is not None:
            location = self._surface_hit(context, event)
            if location is not None:
                self.points[self.tweak_index] = tuple(location)
                self._sync_mesh()
            return {"RUNNING_MODAL"}
        if event.type == "MOUSEMOVE" and self.relax_index is not None:
            self._relax_vertex(context, self.relax_index)
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            if event.ctrl and event.shift:
                index = self._nearest_vertex(context, event)
                if index is not None:
                    self._delete_vertex(index)
                return {"RUNNING_MODAL"}
            if event.ctrl:
                edge = self._nearest_edge(context, event)
                if edge is not None:
                    self._insert_face_loop(context, edge)
                return {"RUNNING_MODAL"}
            if event.shift:
                self.relax_index = self._nearest_vertex(context, event)
                if self.relax_index is not None:
                    self._relax_vertex(context, self.relax_index)
                return {"RUNNING_MODAL"}
            location = self._surface_hit(context, event)
            if location is not None:
                if self.tab_extend and self.last_edge:
                    edge_vector = (
                        Vector(self.points[self.last_edge[1]]) -
                        Vector(self.points[self.last_edge[0]])
                    )
                    self._add_point(
                        self._project_to_surface(context, location - edge_vector * 0.5)
                    )
                    self._add_point(
                        self._project_to_surface(context, location + edge_vector * 0.5)
                    )
                else:
                    self._add_point(location)
            return {"RUNNING_MODAL"}
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        try:
            return self._modal_impl(context, event)
        except Exception as exc:
            log_event("ERROR", "Quad Draw safely cancelled", error=str(exc), trace=traceback.format_exc())
            self._cleanup(context, delete_object=True)
            self.report({"ERROR"}, "Quad Draw stopped safely; see Developer Log")
            return {"CANCELLED"}

    def cancel(self, context):
        self._cleanup(context, delete_object=True)

    def invoke(self, context, _event):
        if context.area.type != "VIEW_3D":
            self.report({"WARNING"}, "Start Quad Draw from the 3D viewport")
            return {"CANCELLED"}
        target = context.scene.formforge_live_surface
        if not target or target.type != "MESH" or context.mode != "OBJECT":
            self.report({"WARNING"}, "Select a mesh and choose Make Live first")
            return {"CANCELLED"}
        try:
            depsgraph = context.evaluated_depsgraph_get()
            surface_bvh = BVHTree.FromObject(target, depsgraph, deform=True, cage=False)
        except Exception as exc:
            log_event("ERROR", "Could not cache live surface", error=str(exc))
            self.report({"ERROR"}, "Could not prepare the live surface; see Developer Log")
            return {"CANCELLED"}
        if surface_bvh is None:
            self.report({"ERROR"}, "The live mesh has no usable surface")
            return {"CANCELLED"}
        self.target = target
        self.surface_bvh = surface_bvh
        self.target_matrix = target.matrix_world.copy()
        self.target_inverse = self.target_matrix.inverted_safe()
        self.cleaned = False
        self.points = []
        self.faces = []
        self.pending = []
        self.last_edge = None
        self.tweak_index = None
        self.relax_index = None
        self.tab_extend = False
        selected = context.active_object
        self.created_retopo = not (
            selected and selected.type == "MESH" and selected != target
        )
        self.backup_mesh = None
        self.original_matrix = None
        if self.created_retopo:
            self.mesh = bpy.data.meshes.new(f"Retopo_{target.name}")
            self.retopo = bpy.data.objects.new(f"Retopo_{target.name}", self.mesh)
            context.collection.objects.link(self.retopo)
        else:
            # A selected non-live mesh is the editable retopology output. Work
            # in world coordinates while retaining a full backup for Esc.
            self.retopo = selected
            self.original_matrix = selected.matrix_world.copy()
            self.backup_mesh = selected.data.copy()
            self.points = [tuple(selected.matrix_world @ vertex.co) for vertex in selected.data.vertices]
            self.faces = [tuple(poly.vertices) for poly in selected.data.polygons]
            self.mesh = selected.data
            selected.matrix_world.identity()
            self._sync_mesh()
        self.retopo.show_in_front = True
        self.retopo.display_type = "SOLID"
        self.retopo.color = (0.06, 0.72, 0.82, 1.0)
        target.select_set(False)
        self.retopo.select_set(True)
        context.view_layer.objects.active = self.retopo
        context.window.cursor_modal_set("CROSSHAIR")
        context.workspace.status_text_set(
            "Quad Draw: Alt+mouse navigates | wheel zooms | 4 clicks make first quad | 2 clicks extend | MMB tweak | Enter/RMB finish"
        )
        context.window_manager.modal_handler_add(self)
        log_event(
            "INFO", "Safe Quad Draw started", target=target.name,
            retopo=self.retopo.name, reused_selected=not self.created_retopo,
        )
        return {"RUNNING_MODAL"}


class FORGE_OT_delete_history(bpy.types.Operator):
    bl_idname = "formforge.delete_history"
    bl_label = "Delete Construction History"
    bl_description = "Apply visible modifiers on selected mesh objects"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and bool(context.selected_editable_objects)

    def invoke(self, context, _event):
        prefs = context.preferences.addons.get(__name__)
        if prefs and prefs.preferences.confirm_destructive:
            return context.window_manager.invoke_confirm(self, _event)
        return self.execute(context)

    def execute(self, context):
        original_active = context.view_layer.objects.active
        applied = 0
        skipped = []
        for obj in list(context.selected_editable_objects):
            if obj.type != "MESH":
                continue
            context.view_layer.objects.active = obj
            for modifier in list(obj.modifiers):
                try:
                    bpy.ops.object.modifier_apply(modifier=modifier.name)
                    applied += 1
                except Exception as exc:
                    skipped.append(f"{obj.name}/{modifier.name}: {exc}")
        context.view_layer.objects.active = original_active
        log_event("INFO", "Construction history deleted", applied=applied, skipped=skipped)
        self.report({"INFO"}, f"Applied {applied} modifier(s)")
        return {"FINISHED"}


class FORGE_OT_smooth_preview(bpy.types.Operator):
    bl_idname = "formforge.smooth_preview"
    bl_label = "Smooth Mesh Preview"
    bl_description = "Toggle a non-destructive subdivision preview (Maya key 3 equivalent)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = _active_mesh(context)
        if not obj:
            self.report({"WARNING"}, "Select a mesh object")
            return {"CANCELLED"}
        modifier = obj.modifiers.get("FormForge Smooth Preview")
        if modifier:
            modifier.show_viewport = not modifier.show_viewport
            enabled = modifier.show_viewport
        else:
            modifier = obj.modifiers.new("FormForge Smooth Preview", "SUBSURF")
            modifier.levels = 2
            modifier.render_levels = 2
            enabled = True
        for polygon in obj.data.polygons:
            polygon.use_smooth = enabled
        log_event("INFO", "Smooth preview toggled", object=obj.name, enabled=enabled)
        return {"FINISHED"}


class FORGE_OT_xray_toggle(bpy.types.Operator):
    bl_idname = "formforge.xray_toggle"
    bl_label = "X-Ray"
    bl_options = {"REGISTER"}

    def execute(self, context):
        space = context.space_data
        if not space or space.type != "VIEW_3D":
            return {"CANCELLED"}
        space.shading.show_xray = not space.shading.show_xray
        log_event("INFO", "X-Ray toggled", enabled=space.shading.show_xray)
        return {"FINISHED"}


class FORGE_OT_open_dev_log(bpy.types.Operator):
    bl_idname = "formforge.open_dev_log"
    bl_label = "Developer Log"
    bl_description = "Open the in-app FormForge developer log"

    def execute(self, context):
        text = bpy.data.texts.get(LOG_TEXT_NAME) or bpy.data.texts.new(LOG_TEXT_NAME)
        area = context.area
        if not area:
            self.report({"ERROR"}, "No active editor area")
            return {"CANCELLED"}
        area.type = "TEXT_EDITOR"
        area.spaces.active.text = text
        return {"FINISHED"}


class FORGE_OT_restore_viewport(bpy.types.Operator):
    bl_idname = "formforge.restore_viewport"
    bl_label = "Return to 3D View"

    def execute(self, context):
        if context.area:
            context.area.type = "VIEW_3D"
        return {"FINISHED"}


class FORGE_OT_export_diagnostics(bpy.types.Operator, ExportHelper):
    bl_idname = "formforge.export_diagnostics"
    bl_label = "Export Diagnostic Report"
    filename_ext = ".txt"
    filter_glob: StringProperty(default="*.txt", options={"HIDDEN"})

    def execute(self, context):
        text = bpy.data.texts.get(LOG_TEXT_NAME)
        report = [
            "FormForge Studio Diagnostic Report",
            f"Generated: {_utc_now()}",
            f"FormForge: {ADDON_VERSION}",
            f"Blender: {bpy.app.version_string}",
            f"Platform: {platform.platform()}",
            f"Python: {platform.python_version()}",
            f"Scene: {context.scene.name if context.scene else 'None'}",
            f"Objects: {len(bpy.data.objects)}",
            "",
            "Events (oldest to newest):",
            text.as_string() if text else "No events recorded.",
        ]
        try:
            with open(self.filepath, "w", encoding="utf-8") as handle:
                handle.write("\n".join(report))
            log_event("INFO", "Diagnostic report exported", path=self.filepath)
            self.report({"INFO"}, "Diagnostic report exported")
            return {"FINISHED"}
        except OSError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class FORGE_OT_about(bpy.types.Operator):
    bl_idname = "formforge.about"
    bl_label = "About FormForge Studio"

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=460)

    def execute(self, _context):
        return {"FINISHED"}

    def draw(self, _context):
        layout = self.layout
        layout.label(text="FormForge Studio", icon="MESH_CUBE")
        layout.label(text=f"Version {ADDON_VERSION}")
        layout.separator()
        layout.label(text="Professional 3D creation, modelling and sculpting environment.")
        layout.label(text="Engine foundation: Blender open-source technology.")
        layout.label(text="Distributed under the GNU General Public License.")


class FORGE_MT_create(bpy.types.Menu):
    bl_label = "Create"
    bl_idname = "FORGE_MT_create"

    def draw(self, _context):
        layout = self.layout
        for key, label, icon in (
            ("CUBE", "Polygon Cube", "MESH_CUBE"),
            ("SPHERE", "Polygon Sphere", "MESH_UVSPHERE"),
            ("CYLINDER", "Polygon Cylinder", "MESH_CYLINDER"),
            ("PLANE", "Polygon Plane", "MESH_PLANE"),
            ("TORUS", "Polygon Torus", "MESH_TORUS"),
            ("CONE", "Polygon Cone", "MESH_CONE"),
        ):
            op = layout.operator("formforge.add_primitive", text=label, icon=icon)
            op.primitive = key
        layout.separator()
        layout.operator("object.camera_add", text="Camera", icon="CAMERA_DATA")
        layout.menu("VIEW3D_MT_light_add", text="Lights", icon="LIGHT")


class FORGE_MT_modify(bpy.types.Menu):
    bl_label = "Modify"
    bl_idname = "FORGE_MT_modify"

    def draw(self, _context):
        layout = self.layout
        layout.operator("formforge.freeze_transforms", icon="CHECKMARK")
        layout.operator("formforge.center_pivot", icon="PIVOT_BOUNDBOX")
        layout.operator("formforge.delete_history", icon="TRASH")
        layout.separator()
        layout.operator("object.location_clear", text="Reset Transforms")
        layout.operator("object.visual_transform_apply", text="Bake Pivot")


class FORGE_MT_select(bpy.types.Menu):
    bl_label = "Select"
    bl_idname = "FORGE_MT_select"

    def draw(self, _context):
        layout = self.layout
        layout.operator("object.select_all", text="All").action = "SELECT"
        layout.operator("object.select_all", text="Deselect All").action = "DESELECT"
        layout.operator("object.select_all", text="Invert Selection").action = "INVERT"
        layout.separator()
        layout.operator("object.select_hierarchy", text="Select Hierarchy").direction = "CHILD"


class FORGE_MT_display(bpy.types.Menu):
    bl_label = "Display"
    bl_idname = "FORGE_MT_display"

    def draw(self, _context):
        layout = self.layout
        layout.operator("view3d.view_axis", text="Front").type = "FRONT"
        layout.operator("view3d.view_axis", text="Side").type = "RIGHT"
        layout.operator("view3d.view_axis", text="Top").type = "TOP"
        layout.separator()
        layout.operator("formforge.xray_toggle", text="X-Ray")
        layout.operator("object.hide_view_set", text="Hide Selected")
        layout.operator("object.hide_view_clear", text="Show All")


class FORGE_MT_mesh(bpy.types.Menu):
    bl_label = "Mesh"
    bl_idname = "FORGE_MT_mesh"

    def draw(self, context):
        layout = self.layout
        if context.mode == "OBJECT":
            layout.operator("object.join", text="Combine", icon="AUTOMERGE_ON")
            layout.operator("mesh.separate", text="Separate", icon="AUTOMERGE_OFF")
            layout.operator("formforge.smooth_preview", icon="MOD_SUBSURF")
        else:
            layout.operator("formforge.bridge_options", text="Bridge Options…")
            layout.operator("mesh.fill", text="Fill Hole")
            layout.operator("mesh.quads_convert_to_tris", text="Triangulate")
            layout.operator("mesh.tris_convert_to_quads", text="Quadrangulate")


class FORGE_MT_edit_mesh(bpy.types.Menu):
    bl_label = "Edit Mesh"
    bl_idname = "FORGE_MT_edit_mesh"

    def draw(self, _context):
        layout = self.layout
        layout.operator("formforge.extrude_interactive", text="Extrude")
        layout.operator("formforge.bevel_interactive", text="Bevel")
        layout.operator("mesh.inset", text="Inset")
        layout.operator("mesh.loopcut_slide", text="Insert Edge Loop")
        layout.operator("formforge.multi_cut", text="Multi-Cut / Knife")
        layout.operator("mesh.merge", text="Merge Components")
        layout.operator("mesh.dissolve_mode", text="Delete Edge/Vertex")


class FORGE_MT_deform(bpy.types.Menu):
    bl_label = "Deform"
    bl_idname = "FORGE_MT_deform"

    def draw(self, _context):
        layout = self.layout
        layout.operator("object.modifier_add", text="Lattice").type = "LATTICE"
        layout.operator("object.modifier_add", text="Bend / Simple Deform").type = "SIMPLE_DEFORM"
        layout.operator("object.modifier_add", text="Shrinkwrap").type = "SHRINKWRAP"
        layout.operator("object.modifier_add", text="Surface Deform").type = "SURFACE_DEFORM"


class FORGE_MT_windows(bpy.types.Menu):
    bl_label = "Windows"
    bl_idname = "FORGE_MT_windows"

    def draw(self, _context):
        layout = self.layout
        layout.operator("formforge.open_dev_log", icon="TEXT")
        layout.operator("formforge.export_diagnostics", icon="EXPORT")
        layout.separator()
        layout.operator("screen.userpref_show", text="Settings / Preferences", icon="PREFERENCES")
        layout.operator("wm.console_toggle", text="System Console", icon="CONSOLE")


class FORGE_MT_mesh_tools(bpy.types.Menu):
    bl_label = "Mesh Tools"
    bl_idname = "FORGE_MT_mesh_tools"

    def draw(self, _context):
        layout = self.layout
        layout.operator("formforge.multi_cut", text="Multi-Cut / Knife", icon="KNIFE")
        layout.operator("formforge.make_live", text="Make Live", icon="SNAP_FACE")
        layout.operator("formforge.remove_live", text="Remove Live", icon="X")
        layout.operator("formforge.quad_draw", text="Quad Draw", icon="MOD_SHRINKWRAP")
        layout.operator("mesh.loopcut_slide", text="Insert Edge Loop")
        layout.operator("formforge.extrude_interactive", text="Extrude")
        layout.operator("formforge.bevel_interactive", text="Bevel")
        layout.operator("mesh.inset", text="Inset")
        layout.operator("formforge.bridge_options", text="Bridge Options…")
        layout.operator("mesh.merge", text="Merge")


class FORGE_MT_mesh_display(bpy.types.Menu):
    bl_label = "Mesh Display"
    bl_idname = "FORGE_MT_mesh_display"

    def draw(self, _context):
        layout = self.layout
        layout.operator("object.shade_smooth", text="Smooth Shading")
        layout.operator("object.shade_flat", text="Flat Shading")
        layout.operator("formforge.smooth_preview", text="Smooth Preview")
        layout.operator("formforge.xray_toggle", text="X-Ray")


class FORGE_MT_curves(bpy.types.Menu):
    bl_label = "Curves"
    bl_idname = "FORGE_MT_curves"

    def draw(self, _context):
        layout = self.layout
        layout.operator("curve.primitive_bezier_curve_add", text="Bezier Curve")
        layout.operator("curve.primitive_bezier_circle_add", text="Bezier Circle")
        layout.operator("curve.primitive_nurbs_curve_add", text="NURBS Curve")
        layout.operator("object.convert", text="Convert Object")


class FORGE_MT_surfaces(bpy.types.Menu):
    bl_label = "Surfaces"
    bl_idname = "FORGE_MT_surfaces"

    def draw(self, _context):
        layout = self.layout
        layout.operator("surface.primitive_nurbs_surface_plane_add", text="NURBS Plane")
        layout.operator("surface.primitive_nurbs_surface_cylinder_add", text="NURBS Cylinder")
        layout.operator("surface.primitive_nurbs_surface_sphere_add", text="NURBS Sphere")
        layout.operator("surface.primitive_nurbs_surface_torus_add", text="NURBS Torus")


class FORGE_MT_uv(bpy.types.Menu):
    bl_label = "UV"
    bl_idname = "FORGE_MT_uv"

    def draw(self, _context):
        layout = self.layout
        layout.operator("uv.unwrap", text="Unwrap")
        layout.operator("uv.smart_project", text="Smart UV Project")
        layout.operator("uv.cube_project", text="Cube Projection")
        layout.operator("uv.pack_islands", text="Layout / Pack Islands")


class FORGE_MT_generate(bpy.types.Menu):
    bl_label = "Generate"
    bl_idname = "FORGE_MT_generate"

    def draw(self, _context):
        layout = self.layout
        for modifier, label in (
            ("MIRROR", "Mirror"),
            ("ARRAY", "Array"),
            ("SOLIDIFY", "Solidify"),
            ("BOOLEAN", "Boolean"),
            ("REMESH", "Remesh"),
            ("SUBSURF", "Subdivision Surface"),
        ):
            op = layout.operator("object.modifier_add", text=label)
            op.type = modifier


class FORGE_MT_cache(bpy.types.Menu):
    bl_label = "Cache"
    bl_idname = "FORGE_MT_cache"

    def draw(self, _context):
        layout = self.layout
        layout.operator("ptcache.bake_all", text="Bake All Dynamics").bake = True
        layout.operator("ptcache.free_bake_all", text="Delete All Caches")


class FORGE_MT_component_pie(bpy.types.Menu):
    bl_label = "FormForge Component Marking Menu"
    bl_idname = "FORGE_MT_component_pie"

    def draw(self, context):
        pie = self.layout.menu_pie()
        op = pie.operator("formforge.component_mode", text="Vertex", icon="VERTEXSEL")
        op.mode = "VERT"
        op = pie.operator("formforge.component_mode", text="Face", icon="FACESEL")
        op.mode = "FACE"
        op = pie.operator("formforge.component_mode", text="Object", icon="OBJECT_DATA")
        op.mode = "OBJECT"
        op = pie.operator("formforge.component_mode", text="Edge", icon="EDGESEL")
        op.mode = "EDGE"
        select_operator = "mesh.select_all" if context.mode == "EDIT_MESH" else "object.select_all"
        pie.operator(select_operator, text="Select All").action = "SELECT"
        pie.operator(select_operator, text="Deselect All").action = "DESELECT"


class FORGE_MT_modeling_pie(bpy.types.Menu):
    bl_label = "FormForge Modeling Marking Menu"
    bl_idname = "FORGE_MT_modeling_pie"

    def draw(self, context):
        pie = self.layout.menu_pie()
        if context.mode == "EDIT_MESH":
            pie.operator("formforge.extrude_interactive", text="Extrude", icon="ORIENTATION_NORMAL")
            pie.operator("formforge.bevel_interactive", text="Bevel", icon="MOD_BEVEL")
            pie.operator("formforge.multi_cut", text="Multi-Cut", icon="KNIFE")
            pie.operator("mesh.loopcut_slide", text="Edge Loop", icon="MOD_EDGESPLIT")
            pie.operator("formforge.bridge_options", text="Bridge Options…")
            pie.operator("mesh.merge", text="Merge")
            pie.operator("mesh.inset", text="Inset")
            pie.operator("mesh.dissolve_mode", text="Dissolve")
        else:
            pie.operator("formforge.freeze_transforms", text="Freeze Transforms", icon="CHECKMARK")
            pie.operator("formforge.center_pivot", text="Center Pivot", icon="PIVOT_BOUNDBOX")
            pie.operator("formforge.smooth_preview", text="Smooth Preview", icon="MOD_SUBSURF")
            pie.operator("formforge.delete_history", text="Delete History", icon="TRASH")
            pie.operator("object.duplicate_move", text="Duplicate", icon="DUPLICATE")
            pie.operator("object.delete", text="Delete", icon="X")
            pie.operator("formforge.quad_draw", text="Quad Draw", icon="MOD_SHRINKWRAP")


def draw_maya_menus(self, context):
    self.layout.menu("VIEW3D_MT_view")
    self.layout.menu("FORGE_MT_select")
    self.layout.menu("FORGE_MT_create")
    self.layout.menu("FORGE_MT_modify")
    if context.active_object and context.active_object.type == "MESH":
        self.layout.menu("FORGE_MT_mesh")
        self.layout.menu("FORGE_MT_edit_mesh")
    self.layout.menu("FORGE_MT_deform")
    self.layout.menu("FORGE_MT_uv")
    self.layout.menu("FORGE_MT_generate")
    self.layout.menu("FORGE_MT_cache")
    self.layout.menu("FORGE_MT_display")
    self.layout.menu("FORGE_MT_windows")


def draw_formforge_topbar(self, _context):
    layout = self.layout
    layout.menu("TOPBAR_MT_file")
    layout.menu("TOPBAR_MT_edit")
    layout.menu("FORGE_MT_create")
    layout.menu("FORGE_MT_modify")
    layout.menu("FORGE_MT_mesh")
    layout.menu("FORGE_MT_edit_mesh")
    layout.menu("FORGE_MT_mesh_tools")
    layout.menu("FORGE_MT_mesh_display")
    layout.menu("FORGE_MT_curves")
    layout.menu("FORGE_MT_surfaces")
    layout.menu("FORGE_MT_deform")
    layout.menu("FORGE_MT_uv")
    layout.menu("FORGE_MT_generate")
    layout.menu("FORGE_MT_cache")
    layout.menu("FORGE_MT_display")
    layout.menu("FORGE_MT_windows")
    layout.menu("TOPBAR_MT_help")


def draw_formforge_file(self, _context):
    layout = self.layout
    layout.operator("formforge.open_project", text="Open FormForge Project…", icon="FILE_FOLDER")
    layout.operator("formforge.save_project", text="Save FormForge Project As…", icon="FILE_TICK")
    layout.separator()


def draw_shelf(self, context):
    layout = self.layout
    layout.separator_spacer()
    row = layout.row(align=True)
    row.operator("formforge.add_primitive", text="", icon="MESH_CUBE").primitive = "CUBE"
    row.operator("formforge.add_primitive", text="", icon="MESH_UVSPHERE").primitive = "SPHERE"
    row.operator("formforge.add_primitive", text="", icon="MESH_CYLINDER").primitive = "CYLINDER"
    row.separator()
    if context.mode == "OBJECT":
        row.operator("formforge.freeze_transforms", text="", icon="CHECKMARK")
        row.operator("formforge.center_pivot", text="", icon="PIVOT_BOUNDBOX")
        row.operator("formforge.smooth_preview", text="", icon="MOD_SUBSURF")
    row.operator("formforge.xray_toggle", text="", icon="XRAY")
    row.operator("formforge.open_dev_log", text="", icon="TEXT")


class FORGE_PT_quick_shelf(bpy.types.Panel):
    bl_label = "FormForge Shelf"
    bl_idname = "FORGE_PT_quick_shelf"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "FormForge"
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        for mode, label, icon in (
            ("OBJECT", "Object", "OBJECT_DATA"),
            ("VERT", "Vertex", "VERTEXSEL"),
            ("EDGE", "Edge", "EDGESEL"),
            ("FACE", "Face", "FACESEL"),
        ):
            op = row.operator("formforge.component_mode", text=label, icon=icon)
            op.mode = mode
        layout.separator()
        box = layout.box()
        box.label(text="Polygon Modeling", icon="MESH_DATA")
        grid = box.grid_flow(columns=4, align=True)
        for key, label, icon in (
            ("CUBE", "Cube", "MESH_CUBE"),
            ("SPHERE", "Sphere", "MESH_UVSPHERE"),
            ("CYLINDER", "Cylinder", "MESH_CYLINDER"),
            ("PLANE", "Plane", "MESH_PLANE"),
        ):
            grid.operator("formforge.add_primitive", text=label, icon=icon).primitive = key
        if context.mode == "EDIT_MESH":
            box = layout.box()
            box.label(text="Components", icon="EDITMODE_HLT")
            grid = box.grid_flow(columns=2, align=True)
            grid.operator("formforge.extrude_interactive", text="Extrude")
            grid.operator("formforge.bevel_interactive", text="Bevel")
            grid.operator("mesh.loopcut_slide", text="Edge Loop")
            grid.operator("formforge.multi_cut", text="Multi-Cut")
        else:
            layout.operator("formforge.quad_draw", text="Quad Draw / Retopology", icon="MOD_SHRINKWRAP")


class FORGE_PT_channel_box(bpy.types.Panel):
    bl_label = "Channel Box"
    bl_idname = "FORGE_PT_channel_box"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "FormForge"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        if not obj:
            layout.label(text="No object selected", icon="INFO")
            return
        layout.label(text=obj.name, icon="OBJECT_DATA")
        col = layout.column(align=True)
        col.prop(obj, "location", text="Translate")
        col.prop(obj, "rotation_euler", text="Rotate")
        col.prop(obj, "scale", text="Scale")
        if obj.type == "MESH":
            layout.separator()
            layout.label(text=f"Vertices: {len(obj.data.vertices):,}")
            layout.label(text=f"Edges: {len(obj.data.edges):,}")
            layout.label(text=f"Faces: {len(obj.data.polygons):,}")


class FORGE_PT_modeling(bpy.types.Panel):
    bl_label = "Modeling Toolkit"
    bl_idname = "FORGE_PT_modeling"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "FormForge"

    def draw(self, context):
        layout = self.layout
        grid = layout.grid_flow(columns=3, align=True)
        for key, label, icon in (
            ("CUBE", "Cube", "MESH_CUBE"),
            ("SPHERE", "Sphere", "MESH_UVSPHERE"),
            ("CYLINDER", "Cylinder", "MESH_CYLINDER"),
            ("PLANE", "Plane", "MESH_PLANE"),
            ("TORUS", "Torus", "MESH_TORUS"),
            ("CONE", "Cone", "MESH_CONE"),
        ):
            grid.operator("formforge.add_primitive", text=label, icon=icon).primitive = key
        layout.separator()
        if context.mode == "OBJECT":
            layout.operator("formforge.freeze_transforms")
            layout.operator("formforge.center_pivot")
            layout.operator("formforge.smooth_preview")
            layout.operator("formforge.delete_history")
            if context.active_object and context.active_object.type == "MESH":
                layout.operator("formforge.make_live", text="Make Live")
            live = context.scene.formforge_live_surface
            if live:
                box = layout.box()
                box.label(text=f"Live: {live.name}", icon="SNAP_FACE")
                row = box.row(align=True)
                row.operator("formforge.quad_draw", text="Quad Draw")
                row.operator("formforge.remove_live", text="Remove")
            else:
                layout.operator("formforge.quad_draw", text="Quad Draw / Retopology")
        elif context.mode == "EDIT_MESH":
            info = layout.box()
            info.label(text="Native tools: drag in viewport", icon="INFO")
            info.label(text="Settings appear in the tool header / sidebar")
            row = layout.row(align=True)
            row.operator("formforge.extrude_interactive", text="Extrude")
            row.operator("formforge.bevel_interactive", text="Bevel")
            row = layout.row(align=True)
            row.operator("mesh.loopcut_slide", text="Edge Loop")
            row.operator("formforge.multi_cut", text="Multi-Cut")
            row = layout.row(align=True)
            row.operator("formforge.bridge_options", text="Bridge Options…")
            row.operator("mesh.inset", text="Inset")


class FORGE_PT_diagnostics(bpy.types.Panel):
    bl_label = "Developer"
    bl_idname = "FORGE_PT_diagnostics"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "FormForge"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, _context):
        layout = self.layout
        layout.label(text=f"FormForge {ADDON_VERSION}")
        layout.label(text=f"Blender {bpy.app.version_string}")
        layout.operator("formforge.open_dev_log", icon="TEXT")
        layout.operator("formforge.export_diagnostics", icon="EXPORT")
        layout.operator("formforge.apply_maya_setup", icon="PREFERENCES")


def draw_text_header(self, _context):
    space = self.layout.context_pointer_set
    _ = space
    if getattr(bpy.context.space_data, "text", None) and bpy.context.space_data.text.name == LOG_TEXT_NAME:
        self.layout.operator("formforge.restore_viewport", icon="VIEW3D")
        self.layout.operator("formforge.export_diagnostics", icon="EXPORT")


def draw_formforge_help(self, _context):
    layout = self.layout
    layout.operator("formforge.about", icon="INFO")
    layout.operator("formforge.open_dev_log", icon="TEXT")
    layout.operator("formforge.export_diagnostics", icon="EXPORT")
    layout.separator()
    op = layout.operator("wm.url_open", text="3D Engine Manual", icon="HELP")
    op.url = "https://docs.blender.org/manual/en/latest/"


def _brand_native_windows():
    if sys.platform != "win32" or bpy.app.background:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        process_id = os.getpid()
        windows = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def enum_callback(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == process_id and user32.IsWindowVisible(hwnd):
                windows.append(hwnd)
            return True

        user32.EnumWindows(enum_callback, 0)
        for hwnd in windows:
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            old_title = buffer.value
            if old_title and "FormForge Studio" not in old_title:
                project = os.path.basename(bpy.data.filepath) if bpy.data.filepath else "Untitled"
                user32.SetWindowTextW(hwnd, f"{project} - FormForge Studio {ADDON_VERSION}")
    except Exception as exc:
        log_event("WARNING", "Native window branding could not be applied", error=str(exc))
    return 2.0


def _load_handler(_unused):
    log_event("INFO", "Scene loaded", filepath=bpy.data.filepath or "Unsaved")


def _apply_formforge_startup():
    """Apply the visible FormForge defaults once Blender has a window."""
    try:
        bpy.ops.formforge.apply_maya_setup()
        rename = {
            "Layout": "General",
            "Modeling": "Polygon",
            "Sculpting": "Sculpt",
            "UV Editing": "UV",
            "Shading": "Lookdev",
            "Animation": "Animate",
            "Rendering": "Render",
            "Compositing": "Composite",
            "Geometry Nodes": "Procedural",
            "Scripting": "Developer",
        }
        for old_name, new_name in rename.items():
            workspace = bpy.data.workspaces.get(old_name)
            if workspace and not bpy.data.workspaces.get(new_name):
                workspace.name = new_name
        for screen in bpy.data.screens:
            for area in screen.areas:
                if area.type == "VIEW_3D":
                    area.spaces.active.show_region_ui = True
        # Persist the enabled extension, keymap and FormForge interface settings
        # so users do not have to activate the UI again on the next launch.
        bpy.ops.wm.save_userpref()
        log_event("INFO", "FormForge startup workspace applied")
    except Exception as exc:
        log_event("ERROR", "FormForge startup failed", error=str(exc))
    return None


CLASSES = (
    FORGE_Preferences,
    FORGE_OT_apply_maya_setup,
    FORGE_OT_add_primitive,
    FORGE_OT_component_mode,
    FORGE_OT_multi_cut,
    FORGE_OT_select_tool,
    FORGE_OT_repeat_last,
    FORGE_OT_save_project,
    FORGE_OT_open_project,
    FORGE_OT_maya_hold_control,
    FORGE_OT_extrude_interactive,
    FORGE_OT_bevel_interactive,
    FORGE_OT_bridge_options,
    FORGE_OT_make_live,
    FORGE_OT_remove_live,
    FORGE_OT_quad_draw_modal,
    FORGE_OT_freeze_transforms,
    FORGE_OT_center_pivot,
    FORGE_OT_delete_history,
    FORGE_OT_smooth_preview,
    FORGE_OT_xray_toggle,
    FORGE_OT_open_dev_log,
    FORGE_OT_restore_viewport,
    FORGE_OT_export_diagnostics,
    FORGE_OT_about,
    FORGE_MT_create,
    FORGE_MT_modify,
    FORGE_MT_select,
    FORGE_MT_display,
    FORGE_MT_mesh,
    FORGE_MT_edit_mesh,
    FORGE_MT_deform,
    FORGE_MT_windows,
    FORGE_MT_mesh_tools,
    FORGE_MT_mesh_display,
    FORGE_MT_curves,
    FORGE_MT_surfaces,
    FORGE_MT_uv,
    FORGE_MT_generate,
    FORGE_MT_cache,
    FORGE_MT_component_pie,
    FORGE_MT_modeling_pie,
    FORGE_PT_quick_shelf,
    FORGE_PT_channel_box,
    FORGE_PT_modeling,
    FORGE_PT_diagnostics,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.formforge_live_surface = PointerProperty(
        name="FormForge Live Surface",
        description="Mesh used as the stable Quad Draw projection surface",
        type=bpy.types.Object,
        poll=_poll_live_mesh,
    )
    bpy.types.VIEW3D_MT_editor_menus.draw = draw_maya_menus
    bpy.types.TOPBAR_MT_editor_menus.draw = draw_formforge_topbar
    bpy.types.TOPBAR_MT_file.prepend(draw_formforge_file)
    bpy.types.VIEW3D_HT_header.append(draw_shelf)
    bpy.types.TEXT_HT_header.append(draw_text_header)
    bpy.types.TOPBAR_MT_help.draw = draw_formforge_help
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig:
        keymap = keyconfig.keymaps.new(name="3D View", space_type="VIEW_3D")
        for key, mode in (
            ("F8", "OBJECT"),
            ("F9", "VERT"),
            ("F10", "EDGE"),
            ("F11", "FACE"),
        ):
            item = keymap.keymap_items.new("formforge.component_mode", key, "PRESS")
            item.properties.mode = mode
            _ADDON_KEYMAPS.append((keymap, item))
        # Native context menus live in mode-specific maps, which take priority
        # over the generic 3D View map. Register FormForge marking menus in the
        # same maps so right-click consistently behaves like Maya.
        for keymap_name, space_type in (
            ("Object Mode", "EMPTY"),
            ("Mesh", "EMPTY"),
        ):
            mode_keymap = keyconfig.keymaps.new(
                name=keymap_name, space_type=space_type, region_type="WINDOW"
            )
            item = mode_keymap.keymap_items.new(
                "wm.call_menu_pie", "RIGHTMOUSE", "PRESS"
            )
            item.properties.name = "FORGE_MT_component_pie"
            _ADDON_KEYMAPS.append((mode_keymap, item))
            item = mode_keymap.keymap_items.new(
                "wm.call_menu_pie", "RIGHTMOUSE", "PRESS", shift=True
            )
            item.properties.name = "FORGE_MT_modeling_pie"
            _ADDON_KEYMAPS.append((mode_keymap, item))
            item = mode_keymap.keymap_items.new(
                "formforge.select_tool", "Q", "PRESS"
            )
            _ADDON_KEYMAPS.append((mode_keymap, item))
            item = mode_keymap.keymap_items.new(
                "formforge.repeat_last", "G", "PRESS"
            )
            _ADDON_KEYMAPS.append((mode_keymap, item))
            for key, control in (("D", "PIVOT"), ("V", "VERTEX")):
                item = mode_keymap.keymap_items.new(
                    "formforge.maya_hold_control", key, "PRESS"
                )
                item.properties.control = control
                item.properties.enabled = True
                _ADDON_KEYMAPS.append((mode_keymap, item))
                item = mode_keymap.keymap_items.new(
                    "formforge.maya_hold_control", key, "RELEASE"
                )
                item.properties.control = control
                item.properties.enabled = False
                _ADDON_KEYMAPS.append((mode_keymap, item))
        window_keymap = keyconfig.keymaps.new(name="Window", space_type="EMPTY", region_type="WINDOW")
        item = window_keymap.keymap_items.new("formforge.open_project", "O", "PRESS", ctrl=True)
        _ADDON_KEYMAPS.append((window_keymap, item))
        item = window_keymap.keymap_items.new(
            "formforge.save_project", "S", "PRESS", ctrl=True, shift=True
        )
        _ADDON_KEYMAPS.append((window_keymap, item))
    if _load_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_handler)
    if not bpy.app.background and not bpy.app.timers.is_registered(_brand_native_windows):
        bpy.app.timers.register(_brand_native_windows, first_interval=0.5, persistent=True)
    if not bpy.app.background and not bpy.app.timers.is_registered(_apply_formforge_startup):
        bpy.app.timers.register(_apply_formforge_startup, first_interval=0.8)
    log_event(
        "INFO",
        "FormForge add-on registered",
        addon_version=ADDON_VERSION,
        blender=bpy.app.version_string,
    )


def unregister():
    if bpy.app.timers.is_registered(_apply_formforge_startup):
        bpy.app.timers.unregister(_apply_formforge_startup)
    if bpy.app.timers.is_registered(_brand_native_windows):
        bpy.app.timers.unregister(_brand_native_windows)
    if _load_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_handler)
    bpy.types.TOPBAR_MT_help.draw = _ORIGINAL_HELP_DRAW
    bpy.types.TOPBAR_MT_editor_menus.draw = _ORIGINAL_TOPBAR_DRAW
    bpy.types.VIEW3D_MT_editor_menus.draw = _ORIGINAL_VIEW3D_MENU_DRAW
    try:
        bpy.types.TOPBAR_MT_file.remove(draw_formforge_file)
    except Exception:
        pass
    for keymap, item in _ADDON_KEYMAPS:
        keymap.keymap_items.remove(item)
    _ADDON_KEYMAPS.clear()
    for owner, callback in (
        (bpy.types.TEXT_HT_header, draw_text_header),
        (bpy.types.VIEW3D_HT_header, draw_shelf),
    ):
        try:
            owner.remove(callback)
        except Exception:
            pass
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            traceback.print_exc()
    if hasattr(bpy.types.Scene, "formforge_live_surface"):
        del bpy.types.Scene.formforge_live_surface


if __name__ == "__main__":
    register()

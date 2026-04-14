"""
N-panel UI for the s&box Bridge v2 addon.
Provides connect/disconnect, sync controls, grid, warnings, and material management.
"""

import bpy
import traceback
from . import connection
from . import sync


# ── Operators ─────────────────────────────────────────────────────────────

class SBOX_OT_Connect(bpy.types.Operator):
    bl_idname = "sbox.bridge_connect"
    bl_label = "Connect"
    bl_description = "Connect to the s&box bridge server"

    def execute(self, context):
        settings = context.scene.sbox_bridge
        success, session_id = connection.connect(
            host=settings.host,
            port=settings.port,
        )
        if success:
            settings.is_connected = True
            sync.start_timer()
            # Send unsynced Blender objects first, then request sync from s&box
            for obj in list(bpy.data.objects):
                if obj.get("sbox_scene_id") or obj.get("sbox_type"):
                    continue
                if obj.type == "MESH" and not sync.get_bridge_id(obj):
                    sync.send_create(obj)
                elif obj.type == "LIGHT" and not sync.get_bridge_id(obj):
                    if obj.data and obj.data.type not in sync.UNSUPPORTED_LIGHT_TYPES:
                        sync.send_create_light(obj)
            sync.send_sync()
            self.report({"INFO"}, f"Connected to s&box at {settings.host}:{settings.port}")
        else:
            settings.is_connected = False
            self.report({"ERROR"}, f"Failed to connect to {settings.host}:{settings.port}")
        return {"FINISHED"}


class SBOX_OT_Disconnect(bpy.types.Operator):
    bl_idname = "sbox.bridge_disconnect"
    bl_label = "Disconnect"
    bl_description = "Disconnect from the s&box bridge server"

    def execute(self, context):
        connection.disconnect()
        sync.stop_timer()
        context.scene.sbox_bridge.is_connected = False
        self.report({"INFO"}, "Disconnected from s&box")
        return {"FINISHED"}


class SBOX_OT_SyncAll(bpy.types.Operator):
    bl_idname = "sbox.bridge_sync_all"
    bl_label = "Sync All"
    bl_description = "Send all Blender objects to s&box and request s&box objects"

    @classmethod
    def poll(cls, context):
        return connection.is_connected()

    def execute(self, context):
        created = 0
        updated = 0
        for obj in list(bpy.data.objects):
            if obj.get("sbox_scene_id") or obj.get("sbox_type"):
                continue
            if obj.type == "MESH":
                if not sync.get_bridge_id(obj):
                    sync.send_create(obj)
                    created += 1
                else:
                    sync.send_update_mesh(obj)
                    updated += 1
            elif obj.type == "LIGHT":
                if obj.data and obj.data.type not in sync.UNSUPPORTED_LIGHT_TYPES:
                    if not sync.get_bridge_id(obj):
                        sync.send_create_light(obj)
                        created += 1
                    else:
                        sync.send_update_light(obj)
                        updated += 1
        sync.send_sync()
        self.report({"INFO"}, f"Synced {created + updated} objects ({created} new, {updated} updated)")
        return {"FINISHED"}


class SBOX_OT_ForceResync(bpy.types.Operator):
    bl_idname = "sbox.bridge_force_resync"
    bl_label = "Force Resync"
    bl_description = "Strip all bridge IDs and re-create everything from scratch"

    @classmethod
    def poll(cls, context):
        return connection.is_connected()

    def execute(self, context):
        count = 0
        for obj in list(bpy.data.objects):
            if obj.get("sbox_scene_id") or obj.get("sbox_type"):
                continue
            if obj.type in ("MESH", "LIGHT"):
                for key in ["sbox_bridge_id", "_remote_update_time"]:
                    if key in obj:
                        del obj[key]
                count += 1

        sync._last_known_bridge_ids.clear()
        sync._last_scale.clear()
        sync._last_write_seq.clear()
        sync._material_hash_cache.clear()

        for obj in list(bpy.data.objects):
            if obj.get("sbox_scene_id") or obj.get("sbox_type"):
                continue
            if obj.type == "MESH" and not sync.get_bridge_id(obj):
                sync.send_create(obj)
            elif obj.type == "LIGHT" and not sync.get_bridge_id(obj):
                if obj.data and obj.data.type not in sync.UNSUPPORTED_LIGHT_TYPES:
                    sync.send_create_light(obj)

        sync.send_sync()
        self.report({"INFO"}, f"Force resynced {count} objects")
        return {"FINISHED"}


class SBOX_OT_SendToScene(bpy.types.Operator):
    bl_idname = "sbox.bridge_send_to_scene"
    bl_label = "Send to Scene"
    bl_description = "Send selected objects to s&box (create new or update existing)"

    @classmethod
    def poll(cls, context):
        return connection.is_connected() and context.selected_objects

    def execute(self, context):
        created = 0
        updated = 0
        skipped = 0

        for obj in context.selected_objects:
            if obj.get("sbox_scene_id") or obj.get("sbox_type"):
                skipped += 1
                continue

            if obj.type == "MESH":
                if sync.get_bridge_id(obj):
                    sync.send_update_mesh(obj)
                    updated += 1
                else:
                    sync.send_create(obj)
                    created += 1
            elif obj.type == "LIGHT":
                if obj.data and obj.data.type in sync.UNSUPPORTED_LIGHT_TYPES:
                    sync.add_warning(f"Skipped '{obj.name}': AREA lights not supported")
                    skipped += 1
                    continue
                if sync.get_bridge_id(obj):
                    sync.send_update_light(obj)
                    updated += 1
                else:
                    sync.send_create_light(obj)
                    created += 1
            else:
                skipped += 1

        parts = []
        if created:
            parts.append(f"{created} new")
        if updated:
            parts.append(f"{updated} updated")
        if skipped:
            parts.append(f"{skipped} skipped")
        self.report({"INFO"}, f"Sent {created + updated} objects ({', '.join(parts)})")
        return {"FINISHED"}


class SBOX_OT_ConfirmPendingDeletes(bpy.types.Operator):
    bl_idname = "sbox.bridge_confirm_deletes"
    bl_label = "Confirm Deletes"
    bl_description = "Immediately confirm all pending deletions"

    @classmethod
    def poll(cls, context):
        return connection.is_connected() and sync.get_pending_deletes()

    def execute(self, context):
        pending = sync.get_pending_deletes()
        for bid, _ in pending:
            sync.send_delete(bid)
        sync._pending_deletes.clear()
        self.report({"INFO"}, f"Confirmed {len(pending)} deletions")
        return {"FINISHED"}


class SBOX_OT_CancelPendingDeletes(bpy.types.Operator):
    bl_idname = "sbox.bridge_cancel_deletes"
    bl_label = "Cancel Deletes"
    bl_description = "Cancel pending deletions (objects remain in s&box)"

    @classmethod
    def poll(cls, context):
        return bool(sync.get_pending_deletes())

    def execute(self, context):
        count = len(sync.get_pending_deletes())
        sync.cancel_pending_deletes()
        self.report({"INFO"}, f"Cancelled {count} pending deletions")
        return {"FINISHED"}


class SBOX_OT_SetGrid(bpy.types.Operator):
    bl_idname = "sbox.set_grid"
    bl_label = "Set Grid"
    bl_description = "Set Blender grid to match s&box grid size"

    grid_size: bpy.props.IntProperty(name="Grid Size", default=16)

    def execute(self, context):
        try:
            _apply_sbox_grid(context, self.grid_size)
            self.report({"INFO"}, f"Grid set to {self.grid_size} units")
        except Exception as e:
            self.report({"WARNING"}, f"Grid error: {e}")
            traceback.print_exc()
        return {"FINISHED"}


class SBOX_OT_DeleteBridgeMaterial(bpy.types.Operator):
    bl_idname = "sbox.delete_bridge_material"
    bl_label = "Delete Material"
    bl_description = "Delete a generated bridge material (.vmat and textures)"

    material_name: bpy.props.StringProperty()

    def execute(self, context):
        import os
        settings = context.scene.sbox_bridge
        assets_path = bpy.path.abspath(settings.project_assets_path) if settings.project_assets_path else None

        if not assets_path or not os.path.isdir(assets_path):
            self.report({"ERROR"}, "Set Assets Path first")
            return {"CANCELLED"}

        bridge_dir = os.path.join(assets_path, "materials", "blender_bridge")
        if not os.path.isdir(bridge_dir):
            self.report({"WARNING"}, "No bridge materials found")
            return {"CANCELLED"}

        deleted = 0
        for f in os.listdir(bridge_dir):
            if f.startswith(self.material_name):
                try:
                    os.remove(os.path.join(bridge_dir, f))
                    deleted += 1
                except Exception:
                    pass

        self.report({"INFO"}, f"Deleted {deleted} file(s) for '{self.material_name}'")
        return {"FINISHED"}


class SBOX_OT_OpenBridgeMaterialFolder(bpy.types.Operator):
    bl_idname = "sbox.open_bridge_material_folder"
    bl_label = "Open Folder"
    bl_description = "Open the bridge materials folder"

    def execute(self, context):
        import os
        import subprocess
        settings = context.scene.sbox_bridge
        assets_path = bpy.path.abspath(settings.project_assets_path) if settings.project_assets_path else None

        if not assets_path:
            self.report({"ERROR"}, "Set Assets Path first")
            return {"CANCELLED"}

        bridge_dir = os.path.join(assets_path, "materials", "blender_bridge")
        if os.path.isdir(bridge_dir):
            subprocess.Popen(f'explorer "{bridge_dir}"')
        else:
            self.report({"WARNING"}, "Folder doesn't exist yet")
        return {"FINISHED"}


# ── Grid Helper ──────────────────────────────────────────────────────────

def _apply_sbox_grid(context, grid_size):
    """Set Blender grid to match s&box grid size."""
    try:
        sf = context.scene.sbox_bridge.scale_factor if hasattr(context.scene, "sbox_bridge") else 1.0
    except Exception:
        sf = 1.0
    inv_sf = 1.0 / sf if sf != 0 else 1.0

    try:
        context.scene.unit_settings.system = 'NONE'
        context.scene.unit_settings.scale_length = 1.0
    except Exception:
        pass

    try:
        context.scene.tool_settings.snap_elements = {'INCREMENT'}
        context.scene.tool_settings.use_snap = True
    except Exception:
        pass

    blender_grid = grid_size * inv_sf

    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D':
                            space.overlay.grid_scale = blender_grid
                            space.overlay.grid_subdivisions = 1
    except Exception:
        pass

    try:
        if hasattr(context.scene, "sbox_bridge"):
            context.scene.sbox_bridge.grid_size = grid_size
    except Exception:
        pass


# ── Main Panel ───────────────────────────────────────────────────────────

class SBOX_PT_BridgePanel(bpy.types.Panel):
    bl_label = "s&box Bridge"
    bl_idname = "SBOX_PT_bridge_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "s&box"

    def draw(self, context):
        layout = self.layout
        is_live = connection.is_connected()
        is_recon = connection.is_reconnecting()

        # ── Connection ────────────────────────────────────────────
        try:
            box = layout.box()
            row = box.row()
            if is_live:
                row.label(text="Status: Connected", icon="CHECKMARK")
            elif is_recon:
                attempt = connection.get_reconnect_attempt()
                row.label(text=f"Reconnecting... (attempt {attempt})", icon="SORTTIME")
            else:
                row.label(text="Status: Disconnected", icon="ERROR")

            settings = context.scene.sbox_bridge
            col = box.column(align=True)
            col.enabled = not is_live and not is_recon
            col.prop(settings, "host")
            col.prop(settings, "port")

            row = box.row()
            if is_live or is_recon:
                row.operator("sbox.bridge_disconnect", icon="CANCEL")
            else:
                row.operator("sbox.bridge_connect", icon="PLAY")
        except Exception:
            pass

        layout.separator()

        # ── Grid ──────────────────────────────────────────────────
        try:
            box = layout.box()
            box.label(text="Grid Size", icon="SNAP_GRID")
            row = box.row(align=True)
            for size in [2, 4, 8, 16, 32]:
                op = row.operator("sbox.set_grid", text=str(size))
                op.grid_size = size
            if hasattr(context.scene, "sbox_bridge"):
                box.label(text=f"s&box Grid: {context.scene.sbox_bridge.grid_size}")
        except Exception:
            pass

        layout.separator()

        # ── Sync ──────────────────────────────────────────────────
        try:
            box = layout.box()
            box.label(text="Sync", icon="FILE_REFRESH")
            settings = context.scene.sbox_bridge
            box.prop(settings, "auto_sync")
            box.prop(settings, "scale_factor")
            box.prop(settings, "project_assets_path")
            box.prop(settings, "auto_reconnect")

            row = box.row(align=True)
            row.operator("sbox.bridge_sync_all", icon="FILE_REFRESH")
            row.operator("sbox.bridge_force_resync", icon="RECOVER_LAST", text="Force Resync")

            if context.selected_objects:
                row = box.row()
                row.operator("sbox.bridge_send_to_scene", icon="EXPORT")
        except Exception:
            pass

        layout.separator()

        # ── Info ──────────────────────────────────────────────────
        try:
            box = layout.box()
            box.label(text="Info", icon="INFO")
            mesh_count = sum(
                1 for obj in bpy.data.objects
                if obj.type == "MESH" and obj.get("sbox_bridge_id")
            )
            light_count = sum(
                1 for obj in bpy.data.objects
                if obj.type == "LIGHT" and obj.get("sbox_bridge_id")
            )
            box.label(text=f"Synced: {mesh_count} meshes, {light_count} lights")

            latency = connection.get_latency_ms()
            if latency > 0 and is_live:
                box.label(text=f"Latency: {latency:.0f}ms")

            if sync.is_play_mode():
                row = box.row()
                row.alert = True
                row.label(text="s&box Play Mode Active", icon="PLAY")
        except Exception:
            pass

        # ── Pending Deletes ───────────────────────────────────────
        try:
            pending = sync.get_pending_deletes()
            if pending:
                layout.separator()
                box = layout.box()
                import time
                oldest = min(t for _, t in pending)
                remaining = max(0, sync.PENDING_DELETE_TIMEOUT - (time.time() - oldest))
                box.label(text=f"{len(pending)} pending deletion(s) ({remaining:.0f}s)", icon="TRASH")
                row = box.row(align=True)
                row.operator("sbox.bridge_confirm_deletes", icon="CHECKMARK")
                row.operator("sbox.bridge_cancel_deletes", icon="CANCEL")
        except Exception:
            pass

        # ── Warnings ──────────────────────────────────────────────
        try:
            warnings = sync.get_warnings()
            if warnings:
                layout.separator()
                box = layout.box()
                box.label(text="Warnings", icon="ERROR")
                for timestamp, msg in warnings[-5:]:
                    row = box.row()
                    row.alert = True
                    row.label(text=msg, icon="DOT")
        except Exception:
            pass

        layout.separator()

        # ── Materials ─────────────────────────────────────────────
        try:
            import os
            settings = context.scene.sbox_bridge
            assets_path = bpy.path.abspath(settings.project_assets_path) if settings.project_assets_path else None

            box = layout.box()
            row = box.row()
            row.label(text="Bridge Materials", icon="MATERIAL")
            row.operator("sbox.open_bridge_material_folder", text="", icon="FILE_FOLDER")

            if assets_path:
                bridge_dir = os.path.join(assets_path, "materials", "blender_bridge")
                if os.path.isdir(bridge_dir):
                    vmats = [f for f in os.listdir(bridge_dir) if f.endswith(".vmat")]
                    if vmats:
                        for vmat in sorted(vmats):
                            mat_name = vmat[:-5]
                            row = box.row(align=True)
                            row.label(text=mat_name, icon="SHADING_RENDERED")
                            op = row.operator("sbox.delete_bridge_material", text="", icon="TRASH")
                            op.material_name = mat_name
                    else:
                        box.label(text="No materials generated yet")
                else:
                    box.label(text="No materials generated yet")
            else:
                box.label(text="Set Assets Path to manage materials")
        except Exception:
            pass

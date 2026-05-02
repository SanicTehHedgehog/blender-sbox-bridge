"""
s&box Bridge v3 - Bidirectional scene sync between Blender and s&box.
Features sync direction controls, hierarchy mapping, status indicators, and geometry hashing.
"""

bl_info = {
    "name": "s&box Bridge",
    "author": "SanicTehHedgehog",
    "version": (3, 5, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > s&box",
    "description": "Bidirectional scene sync with s&box game engine",
    "category": "Scene",
}

import bpy
from . import connection
from . import sync
from . import panel


def _on_scale_factor_changed(self, context):
    """Project scale_factor changed. The factor is baked into vertex positions
    on the wire, so every existing bridge object now has stale geometry on the
    s&box side. Clear all stored geometry hashes and queue a deferred force-
    resync of every bridge object. Deferred via timer because property-update
    callbacks run mid-write — we can't safely walk bpy.data inline."""
    def deferred():
        if not connection.is_connected():
            return None
        for obj in bpy.data.objects:
            if "sbox_bridge_hash" in obj:
                del obj["sbox_bridge_hash"]
            if obj.data is not None and "sbox_bridge_hash" in obj.data:
                del obj.data["sbox_bridge_hash"]
        count = 0
        for obj in list(bpy.data.objects):
            if obj.type == "MESH" and not sync._should_skip_object(obj):
                if sync.get_bridge_id(obj):
                    sync.send_update_mesh(obj)
                    count += 1
            elif obj.type == "LIGHT" and sync.get_bridge_id(obj):
                sync.send_update_light(obj)
                count += 1
        sync.log_activity(f"Project scale -> {self.scale_factor:.2f}, resynced {count} bridge object(s)")
        return None
    bpy.app.timers.register(deferred, first_interval=0.1)


# ── Addon Properties ───────────────────────────────────────────────���──────

class SboxBridgeSettings(bpy.types.PropertyGroup):
    host: bpy.props.StringProperty(
        name="Host", default="localhost",
        description="s&box bridge server hostname",
    )
    port: bpy.props.IntProperty(
        name="Port", default=8099, min=1024, max=65535,
        description="s&box bridge server port",
    )
    is_connected: bpy.props.BoolProperty(name="Connected", default=False)
    scale_factor: bpy.props.FloatProperty(
        name="Scale Factor", default=16.0, min=0.001, max=10000.0,
        description=(
            "Blender unit to s&box source-unit multiplier. 16 is the idiomatic "
            "default (1 Blender unit = 16 source units). Changing this re-bakes "
            "every bridged mesh's vertices and triggers a full resync."
        ),
        update=_on_scale_factor_changed,
    )
    auto_sync: bpy.props.BoolProperty(
        name="Auto Sync", default=True,
        description="Automatically sync scene changes",
    )
    project_assets_path: bpy.props.StringProperty(
        name="Assets Path", default="", subtype='DIR_PATH',
        description="Path to the s&box project's Assets folder (for material/texture export)",
    )
    grid_size: bpy.props.IntProperty(
        name="Grid Size", default=16, min=1, max=256,
        description="Active s&box grid size",
    )
    auto_reconnect: bpy.props.BoolProperty(
        name="Auto Reconnect", default=True,
        description="Automatically reconnect on connection loss",
    )
    reconnect_interval: bpy.props.FloatProperty(
        name="Reconnect Interval", default=3.0, min=1.0, max=30.0,
        description="Base interval between reconnect attempts (seconds)",
    )
    sync_mode: bpy.props.EnumProperty(
        name="Sync Mode",
        items=[
            ('BIDIRECTIONAL', "Bidirectional", "Full two-way sync (default)"),
            ('EXPORT_ONLY', "Export Only", "Blender to s&box only. s&box never overwrites Blender mesh data"),
            ('MANUAL', "Manual Only", "No auto-sync. Use Send to Scene / Sync All explicitly"),
        ],
        default='BIDIRECTIONAL',
        description="Controls how data flows between Blender and s&box",
    )
    show_activity_log: bpy.props.BoolProperty(
        name="Activity Log", default=False,
        description="Show the activity log",
    )


# ── Registration ──────────────────────────────────────────────────────────

classes = (
    SboxBridgeSettings,
    panel.SBOX_OT_Connect,
    panel.SBOX_OT_Disconnect,
    panel.SBOX_OT_ForceResync,
    panel.SBOX_OT_DeleteBridgeMaterial,
    panel.SBOX_OT_OpenBridgeMaterialFolder,
    panel.SBOX_OT_SyncAll,
    panel.SBOX_OT_SendToScene,
    panel.SBOX_OT_ApplyMaterial,
    panel.SBOX_OT_RemoveFromScene,
    panel.SBOX_OT_ClearBridgeID,
    panel.SBOX_OT_SendChildren,
    panel.SBOX_OT_SelectBridgeObject,
    panel.SBOX_OT_ConfirmPendingDeletes,
    panel.SBOX_OT_CancelPendingDeletes,
    panel.SBOX_PT_BridgePanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sbox_bridge = bpy.props.PointerProperty(type=SboxBridgeSettings)
    if sync.on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(sync.on_depsgraph_update)
    if sync.on_undo_post not in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.append(sync.on_undo_post)
    if sync.on_undo_post not in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.append(sync.on_undo_post)
    print("[s&box Bridge v2] Addon registered.")


def unregister():
    connection.disconnect()
    sync.stop_timer()
    if sync.on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(sync.on_depsgraph_update)
    if sync.on_undo_post in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.remove(sync.on_undo_post)
    if sync.on_undo_post in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.remove(sync.on_undo_post)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene, "sbox_bridge"):
        del bpy.types.Scene.sbox_bridge
    print("[s&box Bridge v2] Addon unregistered.")

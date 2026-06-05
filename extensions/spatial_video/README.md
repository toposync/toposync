# Toposync Spatial Video Extension

Extension ID: `com.toposync.spatial_video`

This extension adds render views that project live video onto mapped composition
geometry:

- `360 View 2D`: an orthographic Three.js scene with vector-like 2D behavior.
- `360 View 3D`: a perspective Three.js scene that keeps the existing 3D view
  intact and adds projected video as a separate render mode.

The extension is intentionally frontend-only in its first version. It consumes
camera mapping, PTZ, composition, and streaming playback APIs owned by their
respective extensions.

## Design boundaries

- Do not put camera, streaming, PTZ, or mapping policy in core.
- Do not open camera RTSP URLs directly from this extension.
- Do not create a separate streaming stack for spatial playback.
- Do not make spatial video replace the normal 2D, 3D, vector, or transmission
  views. It is an additional render view.
- Do not treat projection warnings as stream failures. A camera can have a
  healthy stream and still have an imprecise or clipped spatial projection.

## Candidate selection

A camera element is eligible for projection only when all of these are true:

- the element belongs to the active composition;
- the element has a `camera_id`;
- the camera has at least one active live view/publication;
- the element has at least one complete calibrated view with four corner pairs;
- the selected variant can provide a playable browser transport.

The preferred stream variant for spatial views is low-cost:

1. `sub`;
2. `main`;
3. `zoom`;
4. `custom`.

`stream_scope.compatible_roles` and `stream_scope.compatible_source_ids` can
narrow which live-view variants match a calibrated view.

## Render views

### 360 View 2D

The 2D view uses an orthographic Three.js camera and overlays normal composition
elements around the video layer:

```text
background / areas
projected video
walls / elements / markers / notifications
```

The video is projected into a mesh on the X/Z plane with a small Y offset,
`depthWrite=false`, `depthTest=true`, `polygonOffset`, and fixed render order to
avoid z-fighting.

### 360 View 3D

The 3D view reuses the same projection pipeline, but renders it inside a
perspective Three.js scene with orbit controls. Existing element 3D
representations are reused through `ElementType.create3D` when available.

In this version, video is still projected on the ground plane. Projection onto
walls, furniture, or arbitrary 3D surfaces is intentionally out of scope.

## Projection strategies

Projection strategies live behind the `ProjectionStrategy` interface.

### Calibrated (`homography_grid`)

This is the default strategy. It:

- solves the base homography from the four calibrated corners;
- builds a subdivided mesh;
- applies local refinement points through radial weights in image space;
- protects corners and edges so local edits do not destroy the global transform;
- supports area clipping and media `content_rect` UV remapping.

Mesh density is configurable in browser settings:

- `34`: default balance;
- `64`: more detail for visible curvature;
- `96`: dense validation mode for difficult mappings.

### Trapezoid (`constrained_trapezoid`)

This strategy creates a constrained four-corner projection. It is cheaper and
less deformable, so it is useful for comparison and performance validation.

It intentionally ignores local refinement points because refinement and a
constrained trapezoid are different projection models.

## Camera calibration data

Spatial video reads calibrated views from camera element props. A calibrated view
contains:

- `projection_model.image_region`: normalized useful image region;
- `projection_model.world_quad`: four world corners in composition coordinates;
- `projection_model.refinement`: optional internal local refinement points;
- `pose_reference`: optional PTZ preset/pan/tilt/zoom reference;
- `stream_scope`: optional variant/source matching hints.

The four corners remain the global transform. Internal refinement points are
local corrections. This distinction is important because pipelines and visual
rendering must agree on the same camera-to-world mapping.

## PTZ pose handling

The extension polls PTZ status only for projected cameras:

- normal polling is around 1500 ms;
- moving/active PTZ polling is around 300 ms.

Pose resolution can return:

- `matched`: current pose matches a calibrated view;
- `interpolated`: pose is between calibrated views;
- `extrapolated`: pose is near the calibrated envelope but outside it;
- `nearest_reference`: pose is too far, so the nearest view is shown with a
  stronger warning;
- `single_reference`: only one calibrated view exists, so that view is shown
  with a warning;
- `fallback` or `unmatched`: not enough PTZ data or no usable pose.

Streams are not restarted when PTZ pose changes. The texture source stays alive;
only projection geometry is rebuilt.

## Area clipping

Camera elements can optionally select one area from the same composition as a
spatial-video clip area. The editor should offer only area elements with valid
polygons, and preferably only areas intersecting at least one calibrated view.

Clipping is applied in CPU geometry generation:

1. build the normal projection mesh;
2. clip triangles against the area polygon with Sutherland-Hodgman in X/Z;
3. interpolate UVs for new vertices;
4. discard empty geometry;
5. avoid starting a stream when the final projection has no geometry.

Area clipping is a spatial crop. It can make the projected image look cropped or
zoomed even when streaming letterbox correction is correct.

## Streaming integration

The extension calls the live-view playback API with `context=spatial_map` and
uses the existing playback plan. It supports:

- MSE through Toposync's signed go2rtc proxy;
- HLS through the signed HLS proxy and `hls.js` when needed;
- JSMpeg through the signed WebSocket fallback.

WebRTC is not used by default in spatial views. It can be considered later for
explicit PTZ/low-latency interaction, but passive map rendering should avoid
opening WebRTC sessions for every projected camera.

`StreamTextureSource` owns the playback session, heartbeat, video/canvas
texture, and cleanup. When a spatial view unmounts, it must stop heartbeat,
destroy decoders, close WebSockets, and dispose textures.

## Letterbox and `content_rect`

Camera calibration uses normalized coordinates in the useful camera image.
Streaming outputs may be resized with `contain`, creating black bars around the
useful image.

Streaming playback URLs expose:

```json
{
  "content_rect": { "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0 }
}
```

For portrait cameras in a landscape output, `content_rect` can be a narrow
center rectangle. Spatial video remaps mesh UVs from `[0..1]` into that rectangle
so black padding is not projected. This is automatic and should not require user
recalibration.

If metadata is missing, the texture source may detect black padding from the
first frame as a defensive fallback. That fallback is deliberately conservative:
ambiguous detections keep the full frame.

## Markers and warnings

Projected camera markers show stream and pose state without replacing the normal
element interaction model. Clicking a camera should still open the normal camera
modal with embedded transmission controls.

Error priority:

1. stream/playback error;
2. loading/warmup;
3. clip area invalid or empty for the current pose;
4. pose quality warning;
5. normal marker.

Warnings about imprecise mapping or area clipping should not be reported as
transport failures.

## Build and validation

Recommended checks after changing this extension:

```bash
npm --workspace @toposync/extension-spatial-video-ui run build
node scripts/check_spatial_video_area_clip.mjs
node scripts/check_spatial_video_ptz_projection.mjs
```

Use browser validation for visual changes. For letterbox or crop bugs, save
frames/contact sheets that compare:

- camera snapshot used for calibration;
- full transport frame;
- transport frame cropped by `content_rect`;
- final spatial projection with and without area clipping.

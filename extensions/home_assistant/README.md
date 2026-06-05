# Home Assistant

First-party extension scaffold to configure one or more Home Assistant servers (host + API token) via the global Settings modal.

## Special visualizations

In the **Home Assistant item** element editor, you can choose a special
visualization for a single item:

- **Light fixture** for compatible domains.
- **Climate airflow** for climate entities.
- **3D model** uploads a `.glb/.gltf` file and uses the model as the
  device/entity visual. When the entity is on or active, the model plays its
  animations when available.

# Models (3D)

First-party extension that lets you upload and place GLB/GLTF assets as composition elements, including a generated top-down preview used by the 2D editor.

This extension depends on the core backend `/api/files/upload` + `/files/*` endpoints.


The editor accepts a known object height in meters and applies a uniform scale, preserving proportions. Imported models are grounded at their lowest point by default. The scale multiplier remains available under advanced settings; it is not a measurement inferred from photographs.

Import checks selected files against a combined 64 MiB limit. Before preview and scene rendering, geometry must have finite vertices and transforms, at most 2 million vertices and at most 16,777,216 decoded texture pixels in total. Decoded assets are released after preview generation and on failed or superseded loads. These checks run after the loader decodes the model; they do not impose a hard peak-memory limit on decoding or remote resources referenced by GLTF.

Image-to-model generation and metric reconstruction are not provided. Import an existing GLB/GLTF asset; supplying a height reference establishes its scale, not its accuracy.

Focused verification (compile output must stay outside the repository):

```sh
npx tsc extensions/models/ui/src/validation.test.ts --module commonjs --target ES2022 --moduleResolution node --esModuleInterop --skipLibCheck --outDir "$MODEL_TEST_OUTPUT"
NODE_PATH="$PWD/node_modules" node --test "$MODEL_TEST_OUTPUT/validation.test.js"
```

Leaving the model tool or pressing Escape aborts pending file uploads and discards
late import results. A cancelled operation cannot place a model or reset a newer
operation. Already stored files are not deleted; a preview decoder that has already
started may finish, but its result is discarded after cancellation.

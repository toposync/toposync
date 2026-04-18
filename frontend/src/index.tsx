declare const __webpack_init_sharing__: (scope: string) => Promise<void>;

import { installRuntimeNetworkShims } from "./util/runtimeBasePath";

async function start(): Promise<void> {
  installRuntimeNetworkShims();
  await __webpack_init_sharing__("default");
  await import("./bootstrap");
}

start().catch((err) => {
  console.error(err);
});

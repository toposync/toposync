declare const __webpack_init_sharing__: (scope: string) => Promise<void>;

async function start(): Promise<void> {
  await __webpack_init_sharing__("default");
  await import("./bootstrap");
}

start().catch((err) => {
  console.error(err);
});

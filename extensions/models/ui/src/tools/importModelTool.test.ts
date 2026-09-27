import { createImportModelTool } from "./importModelTool";

/** Browser check: delayed uploads deliberately resolve even after abort. */
export async function checkImportCancellation() {
  const originalFetch = window.fetch;
  const pending: { url: string; signal: AbortSignal; finish: () => void }[] = [];
  const created: unknown[] = [];
  const assert = (condition: unknown, message: string) => { if (!condition) throw new Error(message); };
  const settle = () => new Promise(resolve => setTimeout(resolve, 0));
  let session: any;
  window.fetch = ((_url: unknown, init: RequestInit) => new Promise<Response>(resolve => {
    assert(["/api/files/upload", "/api/models/generate"].some(path => String(_url).endsWith(path)), "Cancelled import started preview work");
    pending.push({ url: String(_url), signal: init.signal as AbortSignal, finish: () => resolve(new Response(JSON.stringify({
      dir: "test", filename: "test.glb", url: "/files/test/test.glb",
    }), { headers: { "content-type": "application/json" } })) });
  })) as typeof fetch;
  function begin() {
    session = createImportModelTool({ t: (key: string) => key } as any).createSession({
      createElement: (...args: unknown[]) => { created.push(args); return "created"; },
      openEditor: () => { throw new Error("Cancelled import opened editor"); },
    } as any);
  }
  function select(filename: string | string[] = "test.glb") {
    session.onPointerEvent({ kind: "down", button: 0, world: { x: 1, z: 2 }, screen: { x: 1, y: 2 } });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const transfer = new DataTransfer();
    for (const name of Array.isArray(filename) ? filename : [filename]) transfer.items.add(new File(["delayed"], name));
    input.files = transfer.files; input.dispatchEvent(new Event("change"));
  }
  try {
    begin(); select(); assert(pending.length === 1, "Upload not started");
    session.onKeyDown({ key: "Escape" }); assert(pending[0].signal.aborted, "Escape did not abort upload");
    select(); assert(pending.length === 2, "Could not start a new operation");
    pending[0].finish(); await settle();
    assert(session.getCursor() === "wait", "Old operation reset the new operation");
    session.onKeyDown({ key: "Escape" }); pending[1].finish(); await settle();
    assert(session.getCursor() === "copy", "Cancelled state did not reset");
    session.dispose(); begin(); select(); session.dispose();
    assert(pending[2].signal.aborted, "Disposal did not abort upload");
    pending[2].finish(); await settle();
    begin(); select("object.png");
    assert(pending.length === 4 && pending[3].url.endsWith("/api/models/generate"), "Image generation not started");
    assert(document.querySelector("dialog[open] [role=status]")?.textContent?.includes("generating"), "Generation status missing");
    (document.querySelector("dialog[open] button") as HTMLButtonElement).click();
    assert(pending[3].signal.aborted, "Cancel button did not abort generation");
    pending[3].finish(); await settle(); session.dispose();
    begin(); select(["scene.gltf", "texture.png"]);
    assert(pending[4].url.endsWith("/api/files/upload"), "Texture selection started generation instead of model import");
    session.dispose(); pending[4].finish(); await settle();
    begin(); select(["front.png", "back.png"]); await settle();
    assert(pending.length === 5, "Multiple images unexpectedly started generation");
    assert(document.querySelector("dialog[open] [role=status]")?.textContent?.includes("pick_entry"), "Invalid selection has no explanation");
    session.dispose();
    assert(!document.querySelector("dialog"), "Progress dialog leaked");
    assert(created.length === 0, "Abandoned import created an element");
    assert(document.querySelector('input[type="file"]') === null, "File input leaked");
    return { generationCancellation: true, escape: true, disposal: true, staleCompletion: true, newOperationPreserved: true };
  } finally { session?.dispose(); window.fetch = originalFetch; }
}

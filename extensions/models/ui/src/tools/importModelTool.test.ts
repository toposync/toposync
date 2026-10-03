import { createImportModelTool } from "./importModelTool";

/** Browser check: delayed requests deliberately resolve even after abort. */
export async function checkImportCancellation() {
  const originalFetch = window.fetch;
  const pending: { url: string; signal: AbortSignal; form: FormData; finish: (response?: Response) => void }[] = [];
  const created: unknown[] = [];
  const assert = (condition: unknown, message: string) => { if (!condition) throw new Error(message); };
  const settle = () => new Promise(resolve => setTimeout(resolve, 0));
  const until = async (condition: () => boolean) => {
    const deadline = Date.now() + 3000;
    while (!condition() && Date.now() < deadline) await settle();
    assert(condition(), "Review did not reach the expected state");
  };
  const canvas = document.createElement("canvas"); canvas.width = canvas.height = 2;
  canvas.getContext("2d")!.fillRect(0, 0, 1, 1);
  const image = await new Promise<Blob>(resolve => canvas.toBlob(blob => resolve(blob!)));
  const digest = "a".repeat(64);
  const prepared = () => new Response(image, { headers: { "content-type": "image/png", "X-Toposync-Prepared-Sha256": digest } });
  const generate = () => document.querySelector("dialog[open] .pillButtonPrimary") as HTMLButtonElement | null;
  let session: any;
  window.fetch = ((_url: unknown, init: RequestInit) => new Promise<Response>(resolve => {
    assert(["/api/files/upload", "/api/models/prepare-image", "/api/models/generate"].some(path => String(_url).endsWith(path)), "Cancelled import started preview work");
    pending.push({ url: String(_url), signal: init.signal as AbortSignal, form: init.body as FormData, finish: response => resolve(response ?? new Response(JSON.stringify({
      dir: "test", filename: "test.glb", url: "/files/test/test.glb",
    }), { headers: { "content-type": "application/json" } })) });
  })) as typeof fetch;
  function begin() {
    session = createImportModelTool({ t: (key: string) => key } as any).createSession({
      createElement: (...args: unknown[]) => { created.push(args); return "created"; },
      openEditor: () => { throw new Error("Cancelled import opened editor"); },
    } as any);
  }
  function select(filename: string | string[] = "test.glb", place = true) {
    if (place) session.onPointerEvent({ kind: "down", button: 0, world: { x: 1, z: 2 }, screen: { x: 1, y: 2 } });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const transfer = new DataTransfer();
    for (const name of Array.isArray(filename) ? filename : [filename]) transfer.items.add(new File([name.endsWith(".png") ? image : "delayed"], name));
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
    assert(pending.length === 4 && pending[3].url.endsWith("/api/models/prepare-image"), "Image preparation not started");
    assert(document.querySelector("dialog[open] [role=status]")?.textContent?.includes("preparing"), "Preparation status missing");
    (document.querySelector("dialog[open] button") as HTMLButtonElement).click();
    assert(pending[3].signal.aborted, "Cancel button did not abort preparation");
    pending[3].finish(prepared()); await settle();
    assert(!generate(), "Cancelled preparation opened a review"); session.dispose();
    begin(); select(["scene.gltf", "texture.png"]);
    assert(pending[4].url.endsWith("/api/files/upload"), "Texture selection started generation instead of model import");
    session.dispose(); pending[4].finish(); await settle();
    begin(); select(["front.png", "back.png"]); await settle();
    assert(pending.length === 5, "Multiple images unexpectedly started generation");
    assert(document.querySelector("dialog[open] [role=status]")?.textContent?.includes("pick_entry"), "Invalid selection has no explanation");
    session.dispose();

    begin(); select("original.png"); pending[5].finish(prepared());
    await until(() => !!generate() && !generate()!.disabled);
    assert(pending.length === 6, "Review started generation before confirmation");
    assert(document.querySelectorAll("dialog[open] img").length === 2, "Original and prepared images missing");
    let pickerOpened = false;
    document.querySelector('input[type="file"]')!.addEventListener("click", event => { event.preventDefault(); pickerOpened = true; });
    (document.querySelector("dialog[open] .chipButton") as HTMLButtonElement).click(); await settle();
    assert(pickerOpened && !document.querySelector("dialog[open]"), "Replacing image did not reopen the picker");
    select("replacement.png", false); pending[6].finish(prepared());
    await until(() => !!generate() && !generate()!.disabled);
    generate()!.click(); await settle();
    assert(pending[7].url.endsWith("/api/models/generate"), "Reviewed image did not start generation");
    assert(pending[7].form.get("expected_prepared_sha256") === digest, "Generation lost the reviewed digest");
    assert((pending[7].form.get("file") as File).name === "replacement.png", "Generation used the previous image");
    assert(document.querySelector("dialog[open] [role=status]")?.textContent?.includes("generating"), "Generation status missing");
    (document.querySelector("dialog[open] button") as HTMLButtonElement).click();
    assert(pending[7].signal.aborted, "Cancel button did not abort generation");
    pending[7].finish(); await settle(); session.dispose();

    begin(); select("review.png"); pending[8].finish(prepared());
    await until(() => !!generate() && !generate()!.disabled);
    document.querySelector("dialog[open]")!.dispatchEvent(new Event("cancel", { cancelable: true })); await settle();
    assert(!document.querySelector("dialog[open]") && session.getCursor() === "copy", "Escape did not clear review");
    select("dispose.png"); pending[9].finish(prepared());
    await until(() => !!generate() && !generate()!.disabled); session.dispose();

    begin(); select("broken.png");
    pending[10].finish(new Response("invalid PNG", { headers: { "content-type": "image/png", "X-Toposync-Prepared-Sha256": digest } }));
    await until(() => !!document.querySelector("dialog[open] [role=status]")?.textContent?.includes("invalid_prepared_image"));
    assert(generate()?.disabled && pending.length === 11, "Undecodable image enabled generation"); session.dispose();
    assert(!document.querySelector("dialog"), "Progress dialog leaked");
    assert(created.length === 0, "Abandoned import created an element");
    assert(document.querySelector('input[type="file"]') === null, "File input leaked");
    return { preparationCancellation: true, generationCancellation: true, reviewEscape: true, reviewDisposal: true, replaceRetainsPlacement: true, reviewedDigest: true, decodeFailureClosed: true, escape: true, disposal: true, staleCompletion: true, newOperationPreserved: true };
  } finally { session?.dispose(); window.fetch = originalFetch; }
}

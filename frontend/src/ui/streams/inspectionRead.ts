let previousRead = Promise.resolve();
let queued = 0;

// One network read at a time across the selected previews; producers never wait here.
export async function inspectionRead<T>(read: (signal: AbortSignal) => Promise<T>, signal: AbortSignal): Promise<T> {
  if (queued >= 4) throw new Error("inspection_client_capacity");
  queued++;
  const previous = previousRead;
  let release!: () => void;
  previousRead = new Promise<void>(resolve => { release = resolve; });
  const controller = new AbortController();
  const abort = () => controller.abort();
  let timeout: ReturnType<typeof setTimeout> | undefined;
  try {
    await previous;
    if (signal.aborted) throw new DOMException("Inspection closed", "AbortError");
    signal.addEventListener("abort", abort, { once: true });
    timeout = setTimeout(abort, 2500);
    return await read(controller.signal);
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener("abort", abort);
    queued--;
    release();
  }
}

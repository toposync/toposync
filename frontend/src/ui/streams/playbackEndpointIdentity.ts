/** Credentials authorize a connection; rotating them does not replace its media. */
export function playbackEndpointIdentity(value: string | null): string {
  if (!value) return '';
  try {
    const url = new URL(value, 'http://toposync.invalid');
    // Only this query field is the renewable credential in the media API.
    // Source, quality, ingress prefix, origin and every other parameter remain.
    url.searchParams.delete('media_token');
    return url.href;
  } catch {
    return value;
  }
}

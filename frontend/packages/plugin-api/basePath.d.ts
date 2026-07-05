export function getToposyncBasePath(): string;
export function resolveToposyncUrl(input: string): string;
export function requestJson<T = unknown>(input: string, init?: RequestInit): Promise<T>;
export function requestVoid(input: string, init?: RequestInit): Promise<void>;
export function requestForm<T = unknown>(input: string, form: FormData, init?: RequestInit): Promise<T>;

import { postForm } from "./api";

/**
 * Getting a file from the browser into the design chat.
 *
 * The DataTransfer walking here follows the shape hermes-agent uses in
 * `web/src/lib/chatImagePaste.ts`, because that part of the problem is the same
 * everywhere: clipboard and drop payloads are awkward in the same specific ways
 * in every browser, and the dedupe-by-identity trick below is the fix.
 *
 * What is *not* borrowed is the rest of it. Hermes uploads bytes and then types
 * `/image <path>` into a terminal, because its chat is an xterm mirror of a TUI
 * running in a container that cannot see your clipboard. This chat is a real
 * DOM component talking to an API, so an attachment is simply part of the
 * message.
 */

export type Attachment = {
  id: string;
  name: string;
  media_type: string;
  bytes: number;
  kind: "image" | "document";
};

/** What the picker offers and what the server will actually accept. */
export const ACCEPTED = "image/png,image/jpeg,image/gif,image/webp,application/pdf";

const ACCEPTED_TYPES = new Set(ACCEPTED.split(","));

// Chrome fires both `items` and `files` for the same drop, and a paste of two
// screenshots can repeat one. Identity is the tuple that actually distinguishes
// two files a user meant to attach separately.
function key(file: File): string {
  return `${file.name}\0${file.type}\0${file.size}\0${file.lastModified}`;
}

function collect(data: DataTransfer | null, only: (f: File) => boolean): File[] {
  if (!data) return [];
  const out: File[] = [];
  const seen = new Set<string>();
  const add = (file: File | null) => {
    if (!file || !only(file)) return;
    const k = key(file);
    if (seen.has(k)) return;
    seen.add(k);
    out.push(file);
  };
  for (const item of Array.from(data.items ?? [])) {
    if (item.kind === "file") add(item.getAsFile());
  }
  for (const file of Array.from(data.files ?? [])) add(file);
  return out;
}

/** Every attachable file in a clipboard or drop payload. */
export function filesFromTransfer(data: DataTransfer | null): File[] {
  return collect(data, (f) => ACCEPTED_TYPES.has(f.type));
}

/**
 * Whether a drag looks like it carries files at all.
 *
 * Deliberately looser than `filesFromTransfer`: during dragover the browser
 * hides the type for security, so insisting on a known one here would refuse
 * every drop before it happened. The real check runs on drop, and the server
 * checks magic bytes regardless.
 */
export function dragHasFiles(data: DataTransfer | null): boolean {
  if (!data) return false;
  return (
    Array.from(data.items ?? []).some((i) => i.kind === "file") ||
    (data.files?.length ?? 0) > 0
  );
}

/** Upload files for a message not yet sent; resolves to their stored records. */
export async function upload(
  projectId: string,
  conversation: string,
  files: File[],
): Promise<Attachment[]> {
  const form = new FormData();
  for (const f of files) form.append("files", f, f.name);
  return postForm<Attachment[]>(
    `/api/projects/${projectId}/conversations/${conversation}/attachments`,
    form,
  );
}

export function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} kB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

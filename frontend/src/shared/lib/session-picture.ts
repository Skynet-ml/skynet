/**
 * The web app shows the provider picture of the session, but the Mac app shows
 * the account picture that hosted saved (`users.image_url`). A session that
 * started before hosted saved pictures has a picture that hosted never got, so
 * the jwt callback sends it once per session.
 */

/** The picture hosted keeps: https and at most 2048 characters, the column width. */
export function savablePicture(url: unknown): string | null {
  return typeof url === "string" && url.startsWith("https://") && url.length <= 2048 ? url : null;
}

interface PictureToken {
  email?: string | null;
  picture?: string | null;
  pictureSaved?: string;
}

/**
 * Send the session picture to hosted unless it went there already. A failed
 * save leaves the token unmarked, so the next session read tries again.
 */
export async function saveSessionPicture(
  token: PictureToken,
  save: (email: string, image: string) => Promise<boolean>,
): Promise<void> {
  const image = savablePicture(token.picture);
  if (!image || !token.email || token.pictureSaved === image) return;
  if (await save(token.email, image)) token.pictureSaved = image;
}

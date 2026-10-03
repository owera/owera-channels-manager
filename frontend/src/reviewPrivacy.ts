/** Review privacy select → API value.

A null Video.privacy inherits channel.default_privacy at publish
(video.privacy or channel.default_privacy). The review page used to
coerce that null to "public" and write it on approve and save, so an
unlisted or private channel went out public.
*/

export function reviewPrivacySelectValue(stored: string | null | undefined): string {
  if (stored === "public" || stored === "unlisted" || stored === "private") return stored;
  return "";
}

export function reviewPrivacyForApi(selected: string): string | null {
  if (selected === "public" || selected === "unlisted" || selected === "private") return selected;
  return null;
}

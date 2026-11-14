/** Google Limited Use consent checkbox (LegalOps text, verbatim) + privacy link.
 * Unchecked by default; the parent owns the checked state and sends
 * `copy.sha` / `copy.locale` (the exact string displayed) to the server. */
import { GoogleLimitedUseCopy } from "./googleLimitedUse";
import { POLICY_LINKS } from "./ui";

export function GoogleLimitedUseBox({ copy, checked, onChange, describedBy }: {
  copy: GoogleLimitedUseCopy; checked: boolean; onChange: (v: boolean) => void; describedBy?: string;
}) {
  return (
    <label className="flex gap-3 text-sm text-fog-200 mt-3" data-google-limited-use="">
      <input type="checkbox" className="mt-0.5 h-4 w-4 accent-[#c9f24e] shrink-0" checked={checked}
        aria-describedby={describedBy} data-google-limited-use-sha={copy.sha}
        onChange={(e) => onChange(e.target.checked)} />
      <span lang={copy.lang}>
        <span data-google-limited-use-text="">{copy.text}</span>{" "}
        <a href={POLICY_LINKS.privacy} target="_blank" rel="noopener noreferrer" className="underline"
          data-google-limited-use-privacy="">{copy.linkLabel}</a>
      </span>
    </label>
  );
}

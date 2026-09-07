/** Parse a number-input blur. Empty / NaN / non-int / below-min → null (skip PATCH).

Number("") === 0, and after the API floor that 0 is a 400 for concurrency.
Reject empty BEFORE Number() so the SPA never sends the stall-value.
*/
export function intFromBlur(raw: string, min: number): number | null {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  if (!Number.isInteger(n) || n < min) return null;
  return n;
}

"""Board: HTTP 409 "held…" is a neutral "Segurado" state, not an error (CoS 08/10).

Run: PYTHONPATH=. .venv/bin/python tests/verify_produce_held.py

The frontend has no JS test runner, so this suite pins the contract from
both ends:
  * frontend source: one fetch wrapper (api.ts) turns 409 + detail starting
    with "held" into a HeldError + "Segurado" notice (held.ts) for EVERY call
    (approve / produce / produce-all / create / retry …); the card shows a
    neutral amber "Segurado: <reason>" badge (also for a video whose stored
    error starts with "held", e.g. #87's render-time park); a global toast
    covers bulk calls and the Review page; nothing else is touched;
  * held.ts behaviour (when node + the frontend's esbuild are installed):
    409 "held: needs vm_pass — …" → reason "needs vm_pass — …"; #85-style
    produce hold → reason; 409 without "held", 4xx/5xx, objects → null;
  * backend contract (when the vm_pass lock, #87, is in the tree): the approve
    409 detail starts with "held: needs vm_pass".
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend"
SRC = FE / "src"
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


print("frontend source")
held = (SRC / "held.ts").read_text()
api = (SRC / "api.ts").read_text()
ui = (SRC / "ui.tsx").read_text()
board = (SRC / "pages" / "Board.tsx").read_text()
app = (SRC / "App.tsx").read_text()
ok("export function heldReason(status: number, body: unknown)" in held
   and "if (status !== 409) return null;" in held and "/^held\\b/i" in held,
   "held.ts: hold = 409 AND a detail starting with 'held' (generic, case-insensitive)")
ok("const reason = heldReason(res.status, body);" in api and "throw e;" in api
   and "new HeldError(reason, videoIdOf(path), path)" in api and "pushHeld(e)" in api,
   "api.ts: the single fetch wrapper raises HeldError + a Segurado notice for every call")
ok(api.count("async function api<") == 1 and "fetch(`/api${path}`" in api,
   "…and every board mutation (approve / produce / produce-all / create / retry) goes through it")
for name, path in (("approveVideo", "/approve"), ("produceVideo", "/produce"),
                   ("produceBulk", '"/videos/produce"'), ("createVideo", 'api("/videos"')):
    i = api.index(f"{name}: useMutation(")
    ok(path in api[i:i + 300] and "api(" in api[i:i + 300], f"{name} uses the wrapper")
ok("export function HeldBadge" in ui and "Segurado:" in ui and "#f5a524" in ui
   and "#f7768e" not in ui.split("export function HeldBadge")[1],
   "HeldBadge: neutral amber 'Segurado: <reason>' (never the error red)")
ok("export function HeldToasts" in ui and 'role="status"' in ui and "<HeldToasts />" in app,
   "global Segurado toast mounted in the app (bulk produce / Review approve)")
ok("useHeld().find((n) => n.videoId === v.id" in board and "<HeldBadge reason={hold.reason} />" in board,
   "board card shows the Segurado badge for its own held approve / produce")
ok("/^held\\b/i.test(v.error.trim())" in board,
   "a stored error starting with 'held' (render-time park) renders as Segurado, not red")

print("held.ts behaviour (node)")
esb = FE / "node_modules" / ".bin" / "esbuild"
node = shutil.which("node")
if node and esb.exists():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "held.mjs"
        subprocess.run([str(esb), str(SRC / "held.ts"), "--format=esm", f"--outfile={out}"],
                       check=True, capture_output=True)
        js = (f"import * as h from {json.dumps(str(out))};\n"
              "const r = {\n"
              " vm: h.heldReason(409, {detail: \"held: needs vm_pass — the VM's Gate B PASS on this render is required\"}),\n"
              " produce: h.heldReason(409, {detail: 'Held: produce slot taken (topic 47)'}),\n"
              " obj: h.heldReason(409, {detail: {reason: 'held — daily cap'}}),\n"
              " bare: h.heldReason(409, {detail: 'held'}),\n"
              " other409: h.heldReason(409, {detail: 'cannot approve: duplicate episode'}),\n"
              " holdword: h.heldReason(409, {detail: 'video is not held'}),\n"
              " s403: h.heldReason(403, {detail: 'held: x'}),\n"
              " s500: h.heldReason(500, null),\n"
              " id1: h.videoIdOf('/videos/1449/approve'), id2: h.videoIdOf('/videos/produce'),\n"
              " id3: h.videoIdOf('/videos/12/produce'),\n"
              " err: (() => { const e = new h.HeldError('needs vm_pass', 3, '/videos/3/approve');\n"
              "   return [h.isHeld(e), h.isHeld(new Error('x')), e.message]; })(),\n"
              "};\n"
              "h.pushHeld(new h.HeldError('a', 7, '/videos/7/approve'));\n"
              "h.pushHeld(new h.HeldError('b', 7, '/videos/7/approve'));\n"
              "r.store = h.heldSnapshot().map((n) => [n.videoId, n.reason]);\n"
              "console.log(JSON.stringify(r));\n")
        (Path(td) / "t.mjs").write_text(js)
        r = json.loads(subprocess.run([node, str(Path(td) / "t.mjs")], check=True,
                                      capture_output=True, text=True).stdout)
    ok(r["vm"] == "needs vm_pass — the VM's Gate B PASS on this render is required",
       "#87 approve 409 'held: needs vm_pass — …' → Segurado reason")
    ok(r["produce"] == "produce slot taken (topic 47)" and r["obj"] == "daily cap" and r["bare"] == "held",
       "#85-style produce holds (string / object detail, any case) → reason")
    ok(r["other409"] is None and r["holdword"] is None and r["s403"] is None and r["s500"] is None,
       "a 409 without the 'held' prefix, or any other status, stays an error")
    ok(r["id1"] == 1449 and r["id2"] is None and r["id3"] == 12, "the notice is tied to the card's video id")
    ok(r["err"] == [True, False, "Segurado: needs vm_pass"], "HeldError is recognisable and reads 'Segurado: …'")
    ok(r["store"] == [[7, "b"]], "one notice per video (the latest hold wins)")
else:
    print("  (skipped: node / frontend node_modules not installed — run `npm ci` in frontend/)")

print("backend contract")
from app.services import review_guard  # noqa: E402
if hasattr(review_guard, "VM_PASS_HELD"):
    ok(review_guard.VM_PASS_HELD == "held: needs vm_pass",
       "the vm_pass lock (#87) answers 409 with a detail starting 'held: needs vm_pass'")
    ok(review_guard.VM_PASS_HELD.lower().startswith("held"), "…which the board keys on")
else:
    print("  (vm_pass lock #87 not in this tree yet — contract pinned in verify_vm_pass_topics)")

print()
print(f"ALL {_checks} CHECKS PASSED")

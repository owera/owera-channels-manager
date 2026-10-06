"""Channels Manager teaser guard + review-gate actor audit (2026-10-03).

Why: a Channels Manager (CM) product teaser was approved/requeued by the
growth agent while its final render had no Video Maker (VM) Gate B PASS —
#1385 (Shipping 8) published with a VM FAIL. A CM teaser now ships ONLY
with the VM's Gate B PASS on the exact final render, and only Channels
approves (or requeues) it.

Teaser detection (``is_cm_teaser``) — any of:
  * series Shipping: the title/subject carries "· Shipping N" or the topic is
    named Shipping (craft.is_product_teaser);
  * the title/subject/script names "Channels Manager" (provided-path CM
    teasers carry the product name in the provided script);
  * the video is tied to a CM PR: overrides_json or creation_config carries
    a ``cm_pr`` key.

vm_pass: POST /api/videos/{id}/vm-pass (Channels/VM only) on a rendered
review item stores creation_config["vm_pass"] = {"result": "PASS", "actor",
"at" (UTC ISO), "video_path", "note"}. It is bound to the render artifact:
a re-render writes a new creation_config (and a new video_path), so a stale
vm_pass never carries over to a new render.

Actor (every approve / requeue / reject / vm_pass): the ``X-Actor`` request
header (lower-cased, ≤40 chars), else the HTTP Basic auth username, else
"unknown". It is recorded in the transition's JobRun detail ("actor=<name>";
JobRun.created_at is the timestamp) and, for vm_pass, inside the record.
The actor is self-declared: this is a process guard and an audit trail,
not an authentication boundary (every caller shares the one app password).
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone

# Who may do what on a CM teaser (CoS decision 2026-10-06):
#   * approve            → Channels ONLY (CHANNELS_ACTORS);
#   * record vm_pass     → Channels or the Video Maker (VM_PASS_ACTORS) — the
#                          VM records its Gate B PASS but never approves;
#   * requeue            → VM_PASS_ACTORS (unchanged from #71).
CHANNELS_ACTORS = frozenset({"channels"})
VM_ACTORS = frozenset({"vm", "video-maker", "videomaker"})
VM_PASS_ACTORS = CHANNELS_ACTORS | VM_ACTORS
_ACTOR_RE = re.compile(r"[^a-z0-9_.@\-]+")


def actor_of(request) -> str:
    """X-Actor header → Basic auth username → "unknown"."""
    if request is None:
        return "unknown"
    raw = (request.headers.get("X-Actor") or "").strip()
    if not raw:
        auth = request.headers.get("Authorization", "") or ""
        if auth.startswith("Basic "):
            try:
                raw = base64.b64decode(auth[6:]).decode("utf-8", errors="replace").partition(":")[0]
            except Exception:
                raw = ""
    actor = _ACTOR_RE.sub("-", raw.strip().lower())[:40].strip("-")
    return actor or "unknown"


def is_channels_actor(actor: str | None) -> bool:
    """Channels only — the CM teaser approve allowlist."""
    return (actor or "").lower() in CHANNELS_ACTORS


def can_record_vm_pass(actor: str | None) -> bool:
    """Channels or the Video Maker — who may record the Gate B PASS."""
    return (actor or "").lower() in VM_PASS_ACTORS


def _cc(v) -> dict:
    try:
        cc = json.loads(v.creation_config or "{}")
    except (TypeError, ValueError):
        return {}
    return cc if isinstance(cc, dict) else {}


def _overrides(v) -> dict:
    try:
        o = json.loads(getattr(v, "overrides_json", None) or "{}")
    except (TypeError, ValueError):
        return {}
    return o if isinstance(o, dict) else {}


def is_cm_teaser(v, topic_name: str | None = None) -> bool:
    """True for a Channels Manager product teaser (see module doc)."""
    from app.services import craft

    title = v.title or v.subject or ""
    if craft.is_product_teaser(title, topic_name) or craft.is_product_teaser(v.subject, topic_name):
        return True
    blob = " ".join(x for x in (v.title, v.subject, v.script) if x).lower()
    if "channels manager" in blob:
        return True
    return "cm_pr" in _overrides(v) or "cm_pr" in _cc(v)


def vm_pass_of(v) -> dict | None:
    """The vm_pass record for the CURRENT render artifact, else None."""
    rec = _cc(v).get("vm_pass")
    if not isinstance(rec, dict) or rec.get("result") != "PASS":
        return None
    if not v.video_path or rec.get("video_path") != v.video_path:
        return None  # recorded for another (older) render
    return rec


def set_vm_pass(v, actor: str, note: str | None = None) -> dict:
    """Write creation_config["vm_pass"] for the current artifact; returns it."""
    cc = _cc(v)
    rec = {"result": "PASS", "actor": actor,
           "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
           "video_path": v.video_path, "note": (note or "")[:300]}
    cc["vm_pass"] = rec
    v.creation_config = json.dumps(cc)
    return rec


def teaser_approve_block(v, actor: str, topic_name: str | None = None):
    """(status, message) when approving this video must be refused, else None."""
    if not is_cm_teaser(v, topic_name):
        return None
    if not is_channels_actor(actor):
        return (403, f"CM teaser: only Channels approves it (actor={actor!r}; send "
                     "X-Actor: channels). The growth agent and the Video Maker never "
                     "approve a CM teaser (the VM records vm-pass).")
    if vm_pass_of(v) is None:
        return (409, "CM teaser: approve requires the VM's Gate B PASS on this final render "
                     "(POST /api/videos/{id}/vm-pass by Channels/VM first; a re-render "
                     "clears it).")
    return None


def teaser_requeue_block(v, actor: str, topic_name: str | None = None):
    """(status, message) when requeueing this video must be refused, else None.
    Chosen rule: a CM teaser is requeued by Channels only (blocked otherwise)."""
    if is_cm_teaser(v, topic_name) and not can_record_vm_pass(actor):
        return (403, f"CM teaser: only Channels requeues it (actor={actor!r}; send "
                     "X-Actor: channels). The growth agent never requeues a CM teaser.")
    return None


# ---------------------------------------------------------------------------
# Shipping autogen guard (CMO decision 2026-10-06, OS Gate B).
#
# #1400-#1402 were created by idea autogen on topic t45 "Shipping" with no
# merged Channels Manager PR behind them: their claims were unverified product
# behavior, VM Gate B failed and Channels rejected them. Shipping is reserved
# for teasers backed by a real PR (the #37/#39 flow: the teaser is created from
# the merged PR with a provided script). So idea generation (autofill tick,
# POST /api/topics/{id}/generate, trend adopt) never creates a Shipping row,
# and generated ideas that read as a CM teaser on any topic are dropped.
AUTOGEN_SHIPPING_REASON = (
    "Shipping (Channels Manager teaser) rows need a source Channels Manager PR: "
    "create the teaser from the merged PR (POST /api/videos with the PR's provided "
    "script), never by idea autogen (CMO 2026-10-06, #1400-#1402)")


def autogen_block_reason(topic_name: str | None) -> str | None:
    """Reason idea autogen must not create rows on this topic, else None."""
    from app.services import craft

    if craft.is_product_teaser(None, topic_name):
        return AUTOGEN_SHIPPING_REASON
    return None


def drop_teaser_ideas(ideas) -> list:
    """Generated idea subjects minus the ones that read as a CM teaser
    ("· Shipping N" suffix or naming Channels Manager)."""
    from types import SimpleNamespace

    out = []
    for idea in ideas or []:
        probe = SimpleNamespace(title=None, subject=idea or "", script=None,
                                overrides_json=None, creation_config=None)
        if not is_cm_teaser(probe):
            out.append(idea)
    return out

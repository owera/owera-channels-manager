"""Regression checks for POST /api/profiles channel_id bool coercion.

channel_id bool (backlog #58): lax Optional[int] coerces JSON true→1 /
false→0 before the handler. true binds the new render profile to channel
id=1, so only that channel's filtered list offers it. false stores 0,
which is not null: the profile is neither shared (null appears on every
channel's list) nor visible on any real channel's list.

Integer ids, integer 0, null, and omitted stay as they are. PATCH does
not accept channel_id (the editor locks scope after create); a bool on
PATCH must not rebind.

Workspace isolation: LIST/GET/PATCH/DELETE/POST went through session.get,
so tenant-b could read, rewrite, and delete an owera render profile
(voice, prompts, engine) and bind a new profile onto owera's channel.
These checks pin foreign ids as 404 profile/channel not found (no params
leak, no row change) while shared null and orphan channel_id=0 stay
visible — they have no tenant.

Uses an in-memory DB and FastAPI's TestClient (no real manager.db, no
network, lifespan/scheduler never started). Exits non-zero on the first
failed assertion.
"""
import inspect
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, RenderProfile, Workspace
import app.schemas as schemas
from app.routers import profiles as profiles_router

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(Path(profiles_router.__file__).resolve().parents[2]
   == Path(__file__).resolve().parents[1],
   "profiles module loaded from this tree")
ok(Path(schemas.__file__).resolve().parents[1] == Path(__file__).resolve().parents[1],
   "schemas loaded from this tree")
ok("channel_id" not in schemas.ProfileUpdate.model_fields,
   "ProfileUpdate has no channel_id (PATCH cannot rebind scope)")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Channel(slug="a", name="A", workspace_id=1))
    s.add(Channel(slug="b", name="B", workspace_id=1))
    s.commit()
    ch_b = s.exec(select(Channel).where(Channel.slug == "b")).one()
    ch_b_id = ch_b.id
    s.add(RenderProfile(name="SharedPin", channel_id=None, engine="mpt",
                        params_json="{}"))
    s.add(RenderProfile(name="OnB", channel_id=ch_b_id, engine="hyperframes",
                        params_json='{"video_aspect": "16:9"}'))
    s.commit()

ok(ch_b_id != 1, "precondition: channel B is not id 1 (true coerces to 1)")


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
_orig_mw = settings.multi_workspace
settings.app_password = "testpw"
# Legacy checks resolve the owera workspace. A developer .env with
# multi_workspace on would treat Basic username "x" as the tenant slug.
settings.multi_workspace = False
client = TestClient(main.app)
auth = ("x", "testpw")


def names():
    with Session(engine) as s:
        return {p.name: p.channel_id for p in s.exec(select(RenderProfile)).all()}


def by_name(name):
    with Session(engine) as s:
        row = s.exec(select(RenderProfile).where(RenderProfile.name == name)).first()
        if row is None:
            return None
        return {"id": row.id, "name": row.name, "channel_id": row.channel_id,
                "engine": row.engine, "params_json": row.params_json}


def listed(channel_id=None):
    url = "/api/profiles" if channel_id is None else f"/api/profiles?channel_id={channel_id}"
    r = client.get(url, auth=auth)
    ok(r.status_code == 200, f"GET {url} is 200")
    return {p["name"]: p["channel_id"] for p in r.json()}


def _compact(resp):
    return resp.text.replace(" ", "")


before = names()
ok(before == {"SharedPin": None, "OnB": ch_b_id},
   "precondition: shared null + profile on channel B")

r = client.post("/api/profiles", json={"name": "NoAuth", "channel_id": True})
ok(r.status_code == 401, "unauthenticated create is 401")
ok(names() == before, "unauthenticated create writes nothing")

r = client.post("/api/profiles", auth=auth, json={"channel_id": 1})
ok(r.status_code == 422, "missing name is 422")
ok(names() == before, "missing name writes nothing")

r = client.post("/api/profiles", auth=auth, json={
    "name": "BoolTrue", "channel_id": True})
ok(r.status_code in (400, 422),
   "create channel_id=true is 4xx (must not coerce to 1 and 201)")
ok("boolean" in r.text and '"input":true' in _compact(r),
   "create true names boolean and keeps input true (mode=after would show 1)")
ok(by_name("BoolTrue") is None, "create true writes no row")
ok(names() == before, "create true does not insert a profile")

r = client.post("/api/profiles", auth=auth, json={
    "name": "BoolFalse", "channel_id": False})
ok(r.status_code in (400, 422),
   "create channel_id=false is 4xx (must not coerce to 0)")
ok("boolean" in r.text and '"input":false' in _compact(r),
   "create false names boolean (a stored 0 would 201)")
ok(by_name("BoolFalse") is None, "create false writes no row")
ok(names() == before, "create false does not insert a profile")

# false→None would 201 a shared profile visible on every channel list.
on_one = listed(1)
on_b = listed(ch_b_id)
ok("BoolFalse" not in on_one and "BoolTrue" not in on_one,
   "rejected bools are absent from channel 1's list")
ok("BoolFalse" not in on_b and "BoolTrue" not in on_b,
   "rejected bools are absent from channel B's list")
ok("SharedPin" in on_one and "SharedPin" in on_b,
   "shared null profile still appears on both channel lists")
ok("OnB" not in on_one and on_b.get("OnB") == ch_b_id,
   "channel B profile stays off channel 1's list")

r = client.post("/api/profiles", auth=auth, json={
    "name": "OnBInt", "channel_id": ch_b_id, "engine": "hyperframes",
    "params": {"subtitle_enabled": False, "paragraph_number": 2}})
ok(r.status_code == 201, "create integer channel_id is 201")
body = r.json()
ok(body.get("channel_id") == ch_b_id and body.get("name") == "OnBInt",
   "create integer response is channel B (always-raise dies here)")
ok(body.get("engine") == "hyperframes", "create integer keeps the engine")
row = by_name("OnBInt")
ok(row is not None and row["channel_id"] == ch_b_id,
   "create integer persisted on channel B, not channel 1")
ok('"subtitle_enabled": false' in row["params_json"]
   and '"paragraph_number": 2' in row["params_json"],
   "create integer persisted params including a JSON false")
on_one = listed(1)
on_b = listed(ch_b_id)
ok("OnBInt" not in on_one, "integer-B profile is absent from channel 1's list")
ok(on_b.get("OnBInt") == ch_b_id,
   "integer-B profile is on channel B's list (true→1 would miss this)")

r = client.post("/api/profiles", auth=auth, json={"name": "OmitScope"})
ok(r.status_code == 201, "create omitting channel_id is 201")
ok(by_name("OmitScope")["channel_id"] is None, "omitted channel_id stays shared")
ok(r.json().get("engine") == "mpt", "omitted engine defaults to mpt")

r = client.post("/api/profiles", auth=auth, json={
    "name": "NullScope", "channel_id": None})
ok(r.status_code == 201, "create channel_id=null is 201")
ok(by_name("NullScope")["channel_id"] is None,
   "create null stays shared (reject-None would 422)")
on_one = listed(1)
on_b = listed(ch_b_id)
ok("OmitScope" in on_one and "NullScope" in on_one,
   "omitted and null profiles appear on channel 1's list")
ok("OmitScope" in on_b and "NullScope" in on_b,
   "omitted and null profiles appear on channel B's list")

r = client.post("/api/profiles", auth=auth, json={
    "name": "ZeroScope", "channel_id": 0})
ok(r.status_code == 201, "create integer channel_id=0 is 201")
ok(r.json().get("channel_id") == 0, "create integer 0 echoes 0 (not null)")
ok(by_name("ZeroScope")["channel_id"] == 0,
   "integer 0 persists (bool false is the reject; 0 is not shared)")
on_one = listed(1)
on_b = listed(ch_b_id)
all_rows = listed()
ok("ZeroScope" not in on_one and "ZeroScope" not in on_b,
   "integer 0 is on neither real channel's list (not treated as shared)")
ok(all_rows.get("ZeroScope") == 0, "integer 0 is on the unfiltered list")

# PATCH has no channel_id field. Extra keys are ignored, so a bool must
# not rebind OnB — and neither must an integer (scope is create-only).
on_b_id = by_name("OnB")["id"]
r = client.patch(f"/api/profiles/{on_b_id}", auth=auth,
                 json={"channel_id": True})
ok(r.status_code == 200, "PATCH channel_id=true is ignored (no such field)")
ok(by_name("OnB")["channel_id"] == ch_b_id, "PATCH true left the profile on B")
r = client.patch(f"/api/profiles/{on_b_id}", auth=auth,
                 json={"channel_id": False, "name": "OnB"})
ok(r.status_code == 200, "PATCH channel_id=false is ignored")
ok(by_name("OnB")["channel_id"] == ch_b_id and by_name("OnB")["name"] == "OnB",
   "PATCH false left the profile on B")
r = client.patch(f"/api/profiles/{on_b_id}", auth=auth,
                 json={"channel_id": 1, "engine": "mpt"})
ok(r.status_code == 200, "PATCH integer channel_id is ignored")
patched = by_name("OnB")
ok(patched["channel_id"] == ch_b_id and patched["engine"] == "mpt",
   "PATCH integer left channel B and still applied engine")

print("workspace isolation: profile routes follow require_channel / require_profile")
# A tenant who knows another workspace's ids could LIST/GET the voice
# and prompts, PATCH them, DELETE the row, and POST a profile onto
# that channel. Shared null and orphan channel_id=0 have no tenant.
settings.multi_workspace = True
auth_ow = ("owera", "testpw")
auth_b = ("tenant-b", "testpw")
_SECRET = "owera-profile-secret"
try:
    with Session(engine) as s:
        s.add(Workspace(slug="tenant-b", name="Tenant B"))
        s.commit()
        ws_b = s.exec(select(Workspace).where(Workspace.slug == "tenant-b")).one()
        s.add(Channel(slug="tb-ch", name="Tenant B Ch", workspace_id=ws_b.id))
        s.commit()
        ch_tb = s.exec(select(Channel).where(Channel.slug == "tb-ch")).one()
        ch_tb_id = ch_tb.id
        on_b = s.exec(select(RenderProfile).where(RenderProfile.name == "OnB")).one()
        on_b.params_json = json.dumps({"voice_name": _SECRET})
        s.add(on_b)
        s.commit()
        on_b_id = on_b.id
        shared_id = s.exec(select(RenderProfile).where(
            RenderProfile.name == "SharedPin")).one().id
    ok(ch_tb_id != 1 and ch_tb_id != ch_b_id,
       "precondition: tenant-b channel is neither owera channel")

    def _n_profiles():
        with Session(engine) as s:
            return len(list(s.exec(select(RenderProfile)).all()))

    n_before = _n_profiles()
    r = client.get(f"/api/profiles/{on_b_id}", auth=auth_b)
    ok(r.status_code == 404 and "profile not found" in r.text,
       f"tenant-b GET owera profile → 404 profile not found ({r.status_code})")
    ok(_SECRET not in r.text and "OnB" not in r.text,
       "tenant-b GET does not leak the owera profile name or voice")

    r = client.get("/api/profiles", auth=auth_b)
    ok(r.status_code == 200, "tenant-b LIST is 200")
    listed_b = {p["name"]: p["channel_id"] for p in r.json()}
    ok("OnB" not in listed_b, "tenant-b LIST omits the owera-bound profile")
    ok(_SECRET not in r.text, "tenant-b LIST does not leak the owera voice")
    ok(listed_b.get("SharedPin") is None and "SharedPin" in listed_b,
       "tenant-b LIST still shows the shared null profile")
    ok(listed_b.get("ZeroScope") == 0,
       "tenant-b LIST still shows orphan channel_id=0 (no tenant)")

    r = client.get("/api/profiles", params={"channel_id": ch_b_id}, auth=auth_b)
    ok(r.status_code == 404 and "channel not found" in r.text,
       "tenant-b LIST ?channel_id=owera → 404 channel not found")
    ok(_SECRET not in r.text and "OnB" not in r.text,
       "tenant-b LIST-by-owera-channel does not leak the profile")

    r = client.patch(f"/api/profiles/{on_b_id}", auth=auth_b,
                     json={"name": "Hijacked", "params": {"voice_name": "stolen"}})
    ok(r.status_code == 404 and "profile not found" in r.text,
       "tenant-b PATCH owera profile → 404")
    ok(by_name("OnB")["name"] == "OnB"
       and _SECRET in by_name("OnB")["params_json"],
       "tenant-b PATCH left the owera name and voice untouched")
    ok(by_name("Hijacked") is None, "tenant-b PATCH did not rename the profile")

    r = client.delete(f"/api/profiles/{on_b_id}", auth=auth_b)
    ok(r.status_code == 404 and "profile not found" in r.text,
       "tenant-b DELETE owera profile → 404")
    ok(by_name("OnB") is not None and _SECRET in by_name("OnB")["params_json"],
       "tenant-b DELETE left the owera profile row in place")

    r = client.post("/api/profiles", auth=auth_b, json={
        "name": "BoundToOwera", "channel_id": ch_b_id})
    ok(r.status_code == 404 and "channel not found" in r.text,
       "tenant-b POST onto owera channel → 404 channel not found")
    ok(by_name("BoundToOwera") is None, "tenant-b POST onto owera wrote no row")
    ok(_n_profiles() == n_before, "foreign GET/PATCH/DELETE/POST added no profile")

    r = client.post("/api/profiles", auth=auth_b, json={
        "name": "TbOwn", "channel_id": ch_tb_id, "engine": "hyperframes"})
    ok(r.status_code == 201 and r.json().get("channel_id") == ch_tb_id,
       "tenant-b POST onto its own channel is 201")
    ok(by_name("TbOwn")["channel_id"] == ch_tb_id,
       "tenant-b own profile persisted on tb-ch")

    r = client.get("/api/profiles", auth=auth_ow)
    owera_names = {p["name"] for p in r.json()}
    ok(r.status_code == 200 and "OnB" in owera_names and "TbOwn" not in owera_names,
       "owera LIST still shows OnB and omits the tenant-b profile")
    ok(_SECRET in r.text, "owera LIST still carries its own voice")

    r = client.get(f"/api/profiles/{on_b_id}", auth=auth_ow)
    ok(r.status_code == 200 and r.json().get("name") == "OnB"
       and _SECRET in r.text,
       "owera GET own profile still 200 with the voice")
    r = client.patch(f"/api/profiles/{on_b_id}", auth=auth_ow, json={"engine": "mpt"})
    ok(r.status_code == 200 and r.json().get("name") == "OnB",
       "owera PATCH own profile still 200")

    tb_own_id = by_name("TbOwn")["id"]
    r = client.get(f"/api/profiles/{tb_own_id}", auth=auth_ow)
    ok(r.status_code == 404 and "profile not found" in r.text and "TbOwn" not in r.text,
       "owera GET tenant-b profile → 404 (not only tenant-b is walled off)")
    r = client.delete(f"/api/profiles/{tb_own_id}", auth=auth_ow)
    ok(r.status_code == 404 and by_name("TbOwn") is not None,
       "owera DELETE tenant-b profile → 404 and the row stays")

    r = client.get(f"/api/profiles/{shared_id}", auth=auth_b)
    ok(r.status_code == 200 and r.json().get("name") == "SharedPin"
       and r.json().get("channel_id") is None,
       "tenant-b GET shared null profile still 200 (no tenant)")

    r = client.get("/api/profiles", params={"channel_id": ch_tb_id}, auth=auth_b)
    ok(r.status_code == 200, "tenant-b LIST ?channel_id=own is 200")
    own_list = {p["name"]: p["channel_id"] for p in r.json()}
    ok(own_list.get("TbOwn") == ch_tb_id and "SharedPin" in own_list,
       "tenant-b own-channel list has TbOwn and the shared profile")
    ok("OnB" not in own_list and "ZeroScope" not in own_list,
       "tenant-b own-channel list omits owera-bound and orphan-0 profiles")
    r = client.delete(f"/api/profiles/{tb_own_id}", auth=auth_b)
    ok(r.status_code == 204 and by_name("TbOwn") is None,
       "tenant-b DELETE own profile is 204 and removes the row")

    r = client.post("/api/profiles", auth=auth_b, json={
        "name": "TbZero", "channel_id": 0})
    ok(r.status_code == 201 and r.json().get("channel_id") == 0,
       "tenant-b POST channel_id=0 is still 201 (orphan persist)")

    r = client.get("/api/profiles/99999", auth=auth_ow)
    ok(r.status_code == 404 and "profile not found" in r.text,
       "missing profile GET is 404 profile not found")
    r = client.delete("/api/profiles/99999", auth=auth_ow)
    ok(r.status_code == 404 and "profile not found" in r.text,
       "missing profile DELETE is 404 (same as GET; a silent 204 hid foreign ids)")
    r = client.get(f"/api/profiles/{on_b_id}")
    ok(r.status_code == 401, "GET profile still requires auth")

    src_list = inspect.getsource(profiles_router.list_profiles)
    src_get = inspect.getsource(profiles_router.get_profile)
    src_patch = inspect.getsource(profiles_router.update_profile)
    src_del = inspect.getsource(profiles_router.delete_profile)
    src_create = inspect.getsource(profiles_router.create_profile)
    src_scope = inspect.getsource(profiles_router._scope_channel)
    ok("current_workspace" in src_list and "require_channel" in src_list
       and "profiles_visible_to" in src_list,
       "list_profiles uses current_workspace, require_channel, and "
       "profiles_visible_to (bare select(RenderProfile) is the LIST leak)")
    ok("require_profile" in src_get and "current_workspace" in src_get,
       "get_profile goes through require_profile + current_workspace")
    ok("require_profile" in src_patch and "current_workspace" in src_patch,
       "update_profile goes through require_profile + current_workspace")
    ok("require_profile" in src_del and "current_workspace" in src_del,
       "delete_profile goes through require_profile + current_workspace "
       "(session.get then delete is the cross-tenant delete)")
    ok("_scope_channel" in src_create and "current_workspace" in src_create,
       "create_profile scopes channel_id before insert")
    ok("require_channel" in src_scope and "session.get(Channel" in src_scope,
       "_scope_channel require_channel only when the Channel row exists "
       "(integer 0 must still persist)")
    ok("require_profile" in inspect.getsource(profiles_router),
       "profiles router uses workspace.require_profile")
finally:
    settings.multi_workspace = _orig_mw

print(f"\nALL {_checks} CHECKS PASSED")
settings.app_password = _orig_pw
settings.multi_workspace = _orig_mw

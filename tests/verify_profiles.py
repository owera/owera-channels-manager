"""Regression checks for POST /api/profiles channel_id bool coercion.

channel_id bool (backlog #58): lax Optional[int] coerces JSON true→1 /
false→0 before the handler. true binds the new render profile to channel
id=1, so only that channel's filtered list offers it. false stores 0,
which is not null: the profile is neither shared (null appears on every
channel's list) nor visible on any real channel's list.

Integer ids, integer 0, null, and omitted stay as they are. PATCH does
not accept channel_id (the editor locks scope after create); a bool on
PATCH must not rebind.

Uses an in-memory DB and FastAPI's TestClient (no real manager.db, no
network, lifespan/scheduler never started). Exits non-zero on the first
failed assertion.
"""
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, RenderProfile
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
    s.add(Channel(slug="a", name="A"))
    s.add(Channel(slug="b", name="B"))
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
settings.app_password = "testpw"
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

print(f"\nALL {_checks} CHECKS PASSED")
settings.app_password = _orig_pw

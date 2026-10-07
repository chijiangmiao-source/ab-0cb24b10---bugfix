"""End-to-end API tests over a real uvicorn ASGI app (in-process)."""
import hashlib
import importlib
import json
import os
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    data_dir = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", data_dir)
    monkeypatch.setenv("CRASH_MODE", "soft")
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c, data_dir, main
    shutil.rmtree(data_dir, ignore_errors=True)


DEMO_DRILL = {
    "name": "晨间过境演练",
    "artifacts": [
        {"name": "光谱校准帧", "fragments": ["帧头-S07", "光谱 550", "帧尾-9F"]},
        {"name": "地形条带A", "fragments": ["帧头-S07", "地形 A1 +3.2m", "帧尾-9F"]},
        {"name": "云量速报", "fragments": ["帧头-S07", "云量 18%", "帧尾-9F"]},
    ],
}


def create(client, payload=None):
    res = client.post("/api/drills", json=payload or DEMO_DRILL)
    assert res.status_code == 201, res.text
    return res.json()


def test_health_and_static_page(client):
    c, _, _ = client
    assert c.get("/health").json()["status"] == "ok"
    page = c.get("/")
    assert page.status_code == 200
    assert "紧凑存储" in page.text
    assert c.get("/static/app.js").status_code == 200


def test_validate_artifact_count(client):
    c, _, _ = client
    bad = {"name": "x", "artifacts": [{"name": "a", "fragments": ["x"]}]}
    assert c.post("/api/drills", json=bad).status_code == 422
    too_many = {
        "name": "x",
        "artifacts": [
            {"name": f"a{i}", "fragments": ["x"]} for i in range(9)
        ],
    }
    assert c.post("/api/drills", json=too_many).status_code == 422


def test_compact_flow_and_reassemble(client):
    c, _, _ = client
    drill = create(c)
    did = drill["id"]
    res = c.post(
        f"/api/drills/{did}/compact",
        json={"consolidation_id": "CID-WEB-1", "crash_after": "none"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["generation"] == 1
    view = body["drill"]
    assert view["active_generation"] == 1
    assert view["recovery"]["complete"] is True
    # the shared "帧头-S07" fragment is one physical copy referenced thrice
    head = hashlib.sha256("帧头-S07".encode("utf-8")).hexdigest()
    head_refs = [
        f for a in view["artifacts"] for f in a["fragments"]
        if f["digest"] == head
    ]
    assert len(head_refs) == 3
    assert len({(r["segment"], r["offset"]) for r in head_refs}) == 1
    assert {r["reused"] for r in head_refs} == {False, True}
    # reassembly endpoint reproduces original text
    ra = c.get(f"/api/drills/{did}/artifacts/1/reassemble").json()
    assert ra["text"] == "帧头-S07地形 A1 +3.2m帧尾-9F"


def test_crash_segments_reopen_retransmit_api(client):
    c, data_dir, main = client
    drill = create(c)
    did = drill["id"]
    crash = c.post(
        f"/api/drills/{did}/compact",
        json={"consolidation_id": "CID-CRASH", "crash_after": "segments"},
    )
    assert crash.status_code == 503
    assert crash.json()["status"] == "simulated_crash"
    # view shows staged generation, no active catalog
    view = c.get(f"/api/drills/{did}").json()
    assert view["active_generation"] is None
    assert any(g["status"] == "staged" for g in view["generations"])
    assert view["recovery"]["complete"] is False

    # power returns
    report = c.post("/api/admin/reopen").json()
    assert report["recovered"] == ["CID-CRASH"]
    view = c.get(f"/api/drills/{did}").json()
    assert view["active_generation"] == 1
    assert view["recovery"]["complete"] is True

    # same CID retransmission must not create a new segment or generation
    segs = view["segments"]
    again = c.post(
        f"/api/drills/{did}/compact",
        json={"consolidation_id": "CID-CRASH", "crash_after": "none"},
    ).json()
    assert again["generation"] == 1
    view2 = c.get(f"/api/drills/{did}").json()
    assert view2["segments"] == segs
    assert len([g for g in view2["generations"] if g["status"] == "active"]) == 1


def test_second_generation_crash_switch_then_sweep(client):
    c, _, _ = client
    drill = create(c)
    did = drill["id"]
    c.post(f"/api/drills/{did}/compact",
           json={"consolidation_id": "CID-V1"}).raise_for_status()
    segs1 = c.get(f"/api/drills/{did}").json()["segments"]
    assert len(segs1) == 1

    # re-register a changed artifact set (new fragment added)
    changed = {
        "name": "晨间过境演练",
        "artifacts": DEMO_DRILL["artifacts"] + [
            {"name": "新增工件", "fragments": ["帧头-S07", "全新片段 QQQ"]},
        ],
    }
    c.put(f"/api/drills/{did}/artifacts", json=changed).raise_for_status()
    # old active catalog can no longer cover everything
    view = c.get(f"/api/drills/{did}").json()
    assert view["recovery"]["complete"] is False

    crash = c.post(f"/api/drills/{did}/compact",
                   json={"consolidation_id": "CID-V2", "crash_after": "switch"})
    assert crash.status_code == 503
    mid = c.get(f"/api/drills/{did}").json()
    assert mid["active_generation"] == 2
    assert len(mid["segments"]) == 2  # old segment not yet swept

    # reopen converges and sweeps the old segment
    report = c.post("/api/admin/reopen").json()
    assert report["recovered"] == ["CID-V2"]
    after = c.get(f"/api/drills/{did}").json()
    assert after["active_generation"] == 2
    assert len(after["segments"]) == 1
    assert after["segments"] != segs1
    assert after["recovery"]["complete"] is True
    statuses = {g["generation"]: g["status"] for g in after["generations"]}
    assert statuses == {1: "retired", 2: "active"}
    # all four artifacts rebuild
    for i in range(4):
        r = c.get(f"/api/drills/{did}/artifacts/{i}/reassemble")
        assert r.status_code == 200
    assert c.get(f"/api/drills/{did}/artifacts/3/reassemble").json()["text"] == \
        "帧头-S07全新片段 QQQ"
    # retransmitting V2 changes nothing
    again = c.post(f"/api/drills/{did}/compact",
                   json={"consolidation_id": "CID-V2"}).json()
    assert again["generation"] == 2
    assert c.get(f"/api/drills/{did}").json()["segments"] == after["segments"]


def test_rejection_first_reason_returned_and_active_retained(client):
    c, _, _ = client
    drill = create(c)
    did = drill["id"]
    c.post(f"/api/drills/{did}/compact",
           json={"consolidation_id": "CID-R"}).raise_for_status()

    # different artifact set reusing the CID
    other = create(c, {
        "name": "另一演练",
        "artifacts": [
            {"name": "X", "fragments": ["1"]},
            {"name": "Y", "fragments": ["2"]},
        ],
    })
    r1 = c.post(f"/api/drills/{other['id']}/compact",
                json={"consolidation_id": "CID-R"})
    assert r1.status_code == 409
    assert r1.json()["reject"]["code"] == "artifact_set_mismatch"
    assert r1.json()["drill"]["active_generation"] is None


def test_historical_consolidation_id_frozen_across_restart(client):
    """Full scenario over HTTP with a real service restart in the middle:
    two generations, restart on the preserved data dir, re-register the
    first-generation artifacts, retransmit the first-generation id."""
    c, data_dir, main = client
    drill = create(c)
    did = drill["id"]
    r1 = c.post(f"/api/drills/{did}/compact",
                json={"consolidation_id": "CID-HIST-1"})
    assert r1.status_code == 200 and r1.json()["generation"] == 1

    changed = {
        "name": "晨间过境演练",
        "artifacts": [
            {"name": "光谱校准帧", "fragments": ["帧头V2", "光谱 640", "帧尾V2"]},
            {"name": "地形条带A", "fragments": ["帧头V2", "地形 B4 -1.1m", "帧尾V2"]},
            {"name": "云量速报", "fragments": ["帧头V2", "云量 23%", "帧尾V2"]},
        ],
    }
    c.put(f"/api/drills/{did}/artifacts", json=changed).raise_for_status()
    r2 = c.post(f"/api/drills/{did}/compact",
                json={"consolidation_id": "CID-HIST-2"})
    assert r2.status_code == 200 and r2.json()["generation"] == 2

    # restart the service over the SAME preserved data directory
    importlib.reload(main)
    with TestClient(main.app) as c2:
        assert c2.get("/health").json()["status"] == "ok"
        view = c2.get(f"/api/drills/{did}").json()
        assert view["active_generation"] == 2
        # baseline after the restart: reopen only ever sweeps, never adds
        segs_restart = view["segments"]
        assert len(segs_restart) == 1

        # re-register the first-generation artifacts, retransmit the first id
        c2.put(f"/api/drills/{did}/artifacts", json=DEMO_DRILL).raise_for_status()
        r = c2.post(f"/api/drills/{did}/compact",
                    json={"consolidation_id": "CID-HIST-1"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["generation"] == 1
        assert body["retransmission"] is True
        view = body["drill"]
        # no extra generation, segment set unchanged, active catalog kept
        assert view["active_generation"] == 2
        assert view["segments"] == segs_restart
        statuses = {g["generation"]: g["status"] for g in view["generations"]}
        assert statuses == {1: "retired", 2: "active"}

        # again: still the frozen first-generation result, nothing created
        r = c2.post(f"/api/drills/{did}/compact",
                    json={"consolidation_id": "CID-HIST-1"})
        assert r.json()["generation"] == 1
        assert c2.get(f"/api/drills/{did}").json()["segments"] == segs_restart

        # same id with different content: existing rejection, catalog kept
        tampered = {
            "name": "晨间过境演练",
            "artifacts": [
                {"name": "光谱校准帧",
                 "fragments": ["帧头-S07", "光谱 550 被篡改", "帧尾-9F"]},
                {"name": "地形条带A",
                 "fragments": ["帧头-S07", "地形 A1 +3.2m", "帧尾-9F"]},
                {"name": "云量速报",
                 "fragments": ["帧头-S07", "云量 18%", "帧尾-9F"]},
            ],
        }
        c2.put(f"/api/drills/{did}/artifacts", json=tampered).raise_for_status()
        rj = c2.post(f"/api/drills/{did}/compact",
                     json={"consolidation_id": "CID-HIST-1"})
        assert rj.status_code == 409
        assert rj.json()["reject"]["code"] == "fragment_digest_mismatch"
        assert rj.json()["reject"]["active_generation"] == 2
        assert rj.json()["drill"]["active_generation"] == 2
        assert c2.get(f"/api/drills/{did}").json()["segments"] == segs_restart

        # the second-generation id is unaffected by the historical traffic
        c2.put(f"/api/drills/{did}/artifacts", json=changed).raise_for_status()
        r = c2.post(f"/api/drills/{did}/compact",
                    json={"consolidation_id": "CID-HIST-2"})
        assert r.json()["generation"] == 2
        assert c2.get(f"/api/drills/{did}").json()["segments"] == segs_restart


def test_hard_crash_mode_process_restart(tmp_path, monkeypatch):
    """Hard mode exits the process; restarting must converge."""
    import subprocess
    import sys

    data_dir = str(tmp_path)
    script = tmp_path / "drive.py"
    script.write_text(
        "import json, os, sys\n"
        "sys.path.insert(0, %r)\n"
        "os.environ['DATA_DIR'] = %r\n"
        "from app.store import Store, SimulatedPowerLoss\n"
        "st = Store(%r, crash_mode=os.environ.get('CRASH_MODE','soft'))\n"
        "arts = [{'name':'A','fragments':['h','x']},{'name':'B','fragments':['h','y']}]\n"
        "did = st.create_drill('d', arts)['id']\n"
        "open(%r,'w').write(did)\n"
        "st.compact(did, 'CID-HARD', crash_after='segments')\n"
        % (os.getcwd(), data_dir, data_dir, str(tmp_path / "did.txt")),
        encoding="utf-8",
    )
    # CRASH_MODE=hard -> exit code 7
    env = dict(os.environ, DATA_DIR=data_dir, CRASH_MODE="hard")
    p = subprocess.run([sys.executable, str(script)], env=env, capture_output=True)
    assert p.returncode == 7, p.stderr
    # restart with no crash injection: convergence
    restart = tmp_path / "restart.py"
    restart.write_text(
        "import os, sys\n"
        "sys.path.insert(0, %r)\n"
        "from app.store import Store\n"
        "st = Store(%r)\n"
        "did = open(%r).read()\n"
        "cat = st._read_current(did)\n"
        "assert cat and cat['generation'] == 1\n"
        "assert st._verify_catalog(cat) == []\n"
        "assert len(st._list_segments()) == 1\n"
        "print('CONVERGED')\n"
        % (os.getcwd(), data_dir, str(tmp_path / "did.txt")),
        encoding="utf-8",
    )
    p2 = subprocess.run([sys.executable, str(restart)], env=env,
                        capture_output=True, text=True)
    assert p2.returncode == 0, p2.stderr
    assert "CONVERGED" in p2.stdout

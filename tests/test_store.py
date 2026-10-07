"""Storage engine tests: crash safety, convergence and rejection rules."""
import json
import os
import shutil
import tempfile

import pytest

from app.store import (
    DONE,
    SEGMENTS_WRITTEN,
    SWITCHED,
    Rejected,
    SimulatedPowerLoss,
    Store,
)


HEAD = "【帧头】站点S-07 2026-10-07"
TAIL = "【帧尾】校验 0x9F"


def make_drill(store, name="晨间过境演练", fragments_by_art=None):
    if fragments_by_art is None:
        fragments_by_art = [
            [HEAD, "光谱基线 L=550", TAIL],
            [HEAD, "地形条带 A1 起伏 +3.2m", TAIL],
        ]
    arts = [
        {"name": f"工件{chr(65 + i)}", "fragments": texts}
        for i, texts in enumerate(fragments_by_art)
    ]
    return store.create_drill(name, arts)["id"]


def reopen(data_dir):
    """A fresh process: new Store over the same data directory."""
    return Store(data_dir, crash_mode="soft")


def list_segments(store):
    return store._list_segments()


def assert_one_complete_catalog(store, drill_id, expected_gen=None):
    cat = store._read_current(drill_id)
    assert cat is not None, "必须存在唯一活动目录"
    if expected_gen is not None:
        assert cat["generation"] == expected_gen
    assert store._verify_catalog(cat) == []
    # pointer file exists and holds exactly one generation number
    with open(store._current_path(drill_id), encoding="utf-8") as fh:
        assert fh.read().strip().isdigit()
    return cat


def test_compact_basic_and_rebuild():
    d = tempfile.mkdtemp()
    try:
        st = Store(d)
        did = make_drill(st)
        res = st.compact(did, "CID-1")
        assert res["status"] == "active"
        assert res["generation"] == 1
        cat = assert_one_complete_catalog(st, did, 1)
        # shared fragment stored once: HEAD + TAIL + two unique middles
        index = read_idx(st, cat["segment"])
        assert len(index["fragments"]) == 4
        assert rebuild(st, did, 0) == HEAD + "光谱基线 L=550" + TAIL
        assert rebuild(st, did, 1) == HEAD + "地形条带 A1 起伏 +3.2m" + TAIL
    finally:
        shutil.rmtree(d, ignore_errors=True)


def read_idx(store, segid):
    return json.load(open(store._pack_paths(segid)[1], encoding="utf-8"))


def rebuild(store, drill_id, i):
    cat = store._read_current(drill_id)
    digests = cat["artifacts"][i]["digests"]
    return b"".join(store._read_fragment(cat["segment"], x) for x in digests).decode()


def test_fragment_dedup_in_segment():
    d = tempfile.mkdtemp()
    try:
        st = Store(d)
        did = make_drill(st, fragments_by_art=[
            ["a", "b", "a"],
            ["a", "c"],
        ])
        st.compact(did, "CID-X")
        cat = st._read_current(did)
        assert len(cat["fragment_index"]) == 3  # a,b,c
        offsets = {k: v["offset"] for k, v in cat["fragment_index"].items()}
        assert len(set(offsets.values())) == 3
        assert rebuild(st, did, 0) == "aba"
        assert rebuild(st, did, 1) == "ac"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_crash_after_segments_then_reopen_converges():
    d = tempfile.mkdtemp()
    try:
        st = Store(d)
        did = make_drill(st)
        with pytest.raises(SimulatedPowerLoss):
            st.compact(did, "CID-CRASH-A", crash_after="segments")
        # no active catalog yet, but segment landed
        assert st._read_current(did) is None
        assert len(list_segments(st)) == 1
        wal = read_wal(st, "CID-CRASH-A")
        assert wal["state"] == SEGMENTS_WRITTEN

        # power returns: convergence
        st2 = reopen(d)
        report = st2.last_recovery
        assert report["recovered"] == ["CID-CRASH-A"]
        assert_one_complete_catalog(st2, did, 1)
        # no stray segment, no stale wal
        assert len(list_segments(st2)) == 1
        assert not os.path.exists(st2._wal_path("CID-CRASH-A"))
        assert rebuild(st2, did, 0) == HEAD + "光谱基线 L=550" + TAIL
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_retransmit_same_cid_is_idempotent():
    d = tempfile.mkdtemp()
    try:
        st = Store(d)
        did = make_drill(st)
        first = st.compact(did, "CID-IDEM")
        segs_after_first = set(list_segments(st))
        # same request again: no new segment, same generation, same result
        second = st.compact(did, "CID-IDEM")
        assert second["generation"] == first["generation"]
        assert set(list_segments(st)) == segs_after_first
        assert second["status"] == "active"
        # registry text was stripped; idempotency still holds
        for art in st.registry["drills"][did]["artifacts"]:
            for f in art["fragments"]:
                assert f["text"] is None
        third = st.compact(did, "CID-IDEM")
        assert third["generation"] == first["generation"]
        assert set(list_segments(st)) == segs_after_first
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_retransmit_after_segment_loss_is_content_addressed():
    d = tempfile.mkdtemp()
    try:
        st = Store(d)
        did = make_drill(st)
        st.compact(did, "CID-LOSS")
        segid = st._read_current(did)["segment"]
        os.remove(st._pack_paths(segid)[0])
        os.remove(st._pack_paths(segid)[1])
        assert list_segments(st) == []
        # registry text was stripped => cannot retransmit => reject
        with pytest.raises(Rejected) as ei:
            st.compact(did, "CID-LOSS")
        assert ei.value.code == "segment_missing"
        # active catalog retained and still registered (pointer intact)
        assert st._read_current(did) is not None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_reopen_retransmits_segment_without_new_segment_id():

    d = tempfile.mkdtemp()
    try:
        st = Store(d)
        did = make_drill(st)
        with pytest.raises(SimulatedPowerLoss):
            st.compact(did, "CID-RT", crash_after="segments")
        segid_before = list_segments(st)[0]
        # catastrophic: segment bytes vanish while WAL survives
        os.remove(st._pack_paths(segid_before)[0])
        os.remove(st._pack_paths(segid_before)[1])
        assert list_segments(st) == []
        # reopen: WAL has the texts, segment recreated with the SAME id
        st2 = reopen(d)
        assert list_segments(st2) == [segid_before]
        assert_one_complete_catalog(st2, did, 1)
        assert segid_before in st2.last_recovery["retransmitted"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cid_cannot_be_shared_across_drills_even_with_equal_content():
    d = tempfile.mkdtemp()
    try:
        st = Store(d)
        did1 = make_drill(st)
        st.compact(did1, "CID-SHARED")
        # a different drill with byte-identical artifacts still cannot adopt
        # the other drill's consolidation id/catalog
        did2 = make_drill(st, name="副本演练")
        with pytest.raises(Rejected) as ei:
            st.compact(did2, "CID-SHARED")
        assert ei.value.code == "artifact_set_mismatch"
        assert st._read_current(did2) is None
        assert st._read_current(did1)["generation"] == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_reject_artifact_set_mismatch():
    d = tempfile.mkdtemp()
    try:
        st = Store(d)
        did = make_drill(st)
        st.compact(did, "CID-R")
        gen = st._read_current(did)["generation"]
        # different artifact names (set differs)
        st.registry["drills"][did]["artifacts"][0]["name"] = "工件改了"
        with pytest.raises(Rejected) as ei:
            st.compact(did, "CID-R")
        assert ei.value.code == "artifact_set_mismatch"
        assert st._read_current(did)["generation"] == gen
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_reject_fragment_digest_mismatch():
    d = tempfile.mkdtemp()
    try:
        st = Store(d)
        did = make_drill(st)
        st.compact(did, "CID-R")
        gen = st._read_current(did)["generation"]
        seg_before = st._read_current(did)["segment"]
        # same artifact names, one fragment text changed
        st.registry["drills"][did]["artifacts"][0]["fragments"][1]["text"] = "被篡改的片段"
        from app.store import sha256_text
        target = st.registry["drills"][did]["artifacts"][0]["fragments"][1]
        target["digest"] = sha256_text(target["text"])
        with pytest.raises(Rejected) as ei:
            st.compact(did, "CID-R")
        assert ei.value.code == "fragment_digest_mismatch"
        assert st._read_current(did)["generation"] == gen
        assert st._read_current(did)["segment"] == seg_before
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_next_generation_sweeps_old_segment_after_full_rebuild():
    d = tempfile.mkdtemp()
    try:
        st = Store(d)
        did = make_drill(st)
        st.compact(did, "CID-V1")
        seg_v1 = st._read_current(did)["segment"]

        # re-register a changed artifact set, then a new generation under a
        # new (different) consolidation id
        st.re_register_artifacts(did, [
            {"name": "工件A", "fragments": [{"text": HEAD, "digest": None},
                                            {"text": "光谱基线 L=550", "digest": None},
                                            {"text": TAIL, "digest": None}]},
            {"name": "工件B", "fragments": [{"text": HEAD, "digest": None},
                                            {"text": "地形条带 A1 起伏 +3.2m", "digest": None},
                                            {"text": TAIL, "digest": None}]},
            {"name": "工件C", "fragments": [{"text": "新增片段 ZZZ", "digest": None}]},
        ])
        with pytest.raises(SimulatedPowerLoss):
            st.compact(did, "CID-V2", crash_after="switch")
        # both segments on disk; gen2 active; old one not yet swept
        assert set(list_segments(st)) == {seg_v1, st._read_current(did)["segment"]}
        assert st._read_current(did)["generation"] == 2

        st2 = reopen(d)
        cat = assert_one_complete_catalog(st2, did, 2)
        # old segment swept only after new catalog proven complete
        assert list_segments(st2) == [cat["segment"]]
        assert cat["segment"] != seg_v1
        assert rebuild(st2, did, 2) == "新增片段 ZZZ"
        # retired generation catalog remains for audit, marked retired
        view = st2.drill_view(did)
        statuses = {g["generation"]: g["status"] for g in view["generations"]}
        assert statuses == {1: "retired", 2: "active"}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_reregister_verdict_incomplete_until_recompacted():
    d = tempfile.mkdtemp()
    try:
        st = Store(d)
        did = make_drill(st)
        st.compact(did, "CID-E")
        st.re_register_artifacts(did, [
            {"name": "工件A", "fragments": ["新内容 1"]},
            {"name": "工件B", "fragments": ["新内容 2"]},
        ])
        v = st.recovery_verdict(did)
        assert v["complete"] is False
        assert len(v["missing_fragments"]) == 2
        # original catalog still intact, not pointing at wrong offsets
        assert st._read_current(did)["generation"] == 1
        st.compact(did, "CID-E2")
        assert st.recovery_verdict(did)["complete"] is True
    finally:
        shutil.rmtree(d, ignore_errors=True)


def read_wal(store, cid):
    return json.load(open(store._wal_path(cid), encoding="utf-8"))

#!/usr/bin/env python3
"""One-shot verification service.

Checks, in order:

  1. build check  — byte-compile the application and import the ASGI app
  2. code tests   — the full pytest suite (engine crashes, retransmission,
                    rejection rules, hard-crash process restart)
  3. API/HTTP smoke against a live server:
       - /health and the web page
       - 2..8 artifact validation
       - compaction with shared fragments
       - simulated power loss AFTER segment durability  -> reopen converges
       - a second generation with power loss AFTER the catalog switch
         -> reopen converges and sweeps the old segment
       - retransmission with the same consolidation id creates no new
         segment and changes no result
       - historical retransmission after a reopen: re-registering the
         first-generation artifacts and retransmitting the first id returns
         the frozen gen-1 result — no new segment, no new generation, and
         the active catalog does not move backwards; the same id with
         different content is rejected with the existing reason
       - the three rejection reasons keep the active catalog:
         artifact set mismatch, fragment digest mismatch, missing segment
  4. recovery & retransmission verdicts are asserted from the live responses

Exit code: 0 when every check passed, 1 otherwise.

BASE_URL may point at an already-running server (used by Compose, where the
web service is a dependency). When unset, a local uvicorn is started on an
ephemeral port with a throwaway DATA_DIR.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

failures: List[str] = []


def ok(label: str) -> None:
    print(f"{GREEN}  ✓{RESET} {label}")


def fail(label: str, detail: str = "") -> bool:
    failures.append(label)
    print(f"{RED}  ✗ {label}{RESET}")
    if detail:
        print("    " + detail.replace("\n", "\n    "))
    return False


def step(title: str) -> None:
    print(f"\n{YELLOW}=== {title} ==={RESET}")


def run(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=ROOT, **kw)


# --------------------------------------------------------------------- HTTP

class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, body: Any = None,
             expect: Optional[int] = None) -> Tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                code, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read()
        parsed = json.loads(raw or b"{}")
        if expect is not None and code != expect:
            raise AssertionError(f"{method} {path}: expected {expect}, got {code}: {raw!r}")
        return code, parsed

    def get(self, path: str, expect: int = 200):
        return self.call("GET", path, expect=expect)

    def post(self, path: str, body=None, expect: int = 200):
        return self.call("POST", path, body, expect)

    def put(self, path: str, body=None, expect: int = 200):
        return self.call("PUT", path, body, expect=expect)


def wait_for_health(base: str, attempts: int = 40) -> bool:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.5)
    return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn_server() -> Tuple[subprocess.Popen, str, str]:
    port = free_port()
    data_dir = tempfile.mkdtemp(prefix="verify-data-")
    env = dict(os.environ, DATA_DIR=data_dir, CRASH_MODE="soft")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    if not wait_for_health(base):
        out = proc.stdout.read() if proc.stdout else ""
        raise RuntimeError(f"local server failed to start:\n{out}")
    return proc, base, data_dir


# -------------------------------------------------------------------- checks

def check_build() -> bool:
    step("1/4 构建检查（compileall + ASGI 导入）")
    r = run([sys.executable, "-m", "compileall", "-q", "app", "scripts", "web"])
    if r.returncode != 0:
        return fail("compileall 失败")
    ok("所有 Python 模块字节编译通过")
    r = run([sys.executable, "-c",
             "import app.main; assert app.main.app is not None"])
    if r.returncode != 0:
        return fail("ASGI 应用无法导入")
    ok("app.main:app 可导入")
    return True


def check_tests() -> bool:
    step("2/4 代码测试（pytest）")
    r = run([sys.executable, "-m", "pytest", "tests", "-q"],
            capture_output=True, text=True)
    tail = "\n".join(r.stdout.splitlines()[-6:])
    if r.returncode != 0:
        return fail("pytest 未全部通过", tail)
    ok("pytest 全部通过")
    print("    " + tail.replace("\n", "\n    "))
    return True


def check_smoke(c: Client) -> bool:
    step("3/4 API / HTTP 冒烟（真实 HTTP 联调）")
    passed = True

    _, health = c.get("/health")
    if health.get("status") == "ok":
        ok(f"/health 正常（crash_mode={health.get('crash_mode')}）")
    else:
        passed &= fail("/health 异常", json.dumps(health))

    with urllib.request.urlopen(c.base + "/") as r:
        page = r.read().decode()
    if "紧凑存储" in page:
        ok("首页由真实服务返回")
    else:
        passed &= fail("首页内容不符")

    tag = uuid.uuid4().hex[:8]
    drill_body = {
        "name": f"verify-演练-{tag}",
        "artifacts": [
            {"name": "光谱校准帧", "fragments": [f"帧头-{tag}", "光谱 550", "帧尾-9F"]},
            {"name": "地形条带A", "fragments": [f"帧头-{tag}", "地形 A1", "帧尾-9F"]},
            {"name": "云量速报", "fragments": [f"帧头-{tag}", "云量 18%", "帧尾-9F"]},
        ],
    }
    _, drill = c.post("/api/drills", drill_body, expect=201)
    did = drill["id"]
    ok(f"创建含 {len(drill_body['artifacts'])} 份工件、共享片段的演练 ({did})")

    bad = {"name": "bad", "artifacts": [{"name": "a", "fragments": ["x"]}]}
    code, _ = c.call("POST", "/api/drills", bad)
    if code == 422:
        ok("工件数量下限校验（2–8）返回 422")
    else:
        passed &= fail("工件数量下限未校验", f"got {code}")

    cid = f"CID-VERIFY-{tag}"

    # ---- crash point A: after segments, before catalog switch
    code, crash1 = c.post(
        f"/api/drills/{did}/compact",
        {"consolidation_id": cid, "crash_after": "segments"}, expect=503,
    )
    view = crash1["drill"]
    cond = crash1["status"] == "simulated_crash" and view["active_generation"] is None
    if cond:
        ok("新段落盘后断电：503，且尚无活动目录（未错误切换）")
    else:
        passed &= fail("断电点 A 状态错误", json.dumps(crash1, ensure_ascii=False)[:400])

    _, report = c.post("/api/admin/reopen", {})
    _, view = c.get(f"/api/drills/{did}")
    if (view["active_generation"] == 1 and view["recovery"]["complete"]
            and cid in report.get("recovered", [])):
        ok("重开收敛为唯一完整目录（第 1 代），恢复报告含整理标识")
    else:
        passed &= fail("重开未收敛", json.dumps(report) + "\n" + json.dumps(view["recovery"])[:400])

    # retransmission: same CID must not create a new segment/generation
    segs = view["segments"]
    _, again = c.post(f"/api/drills/{did}/compact",
                      {"consolidation_id": cid, "crash_after": "none"})
    _, view = c.get(f"/api/drills/{did}")
    if again["generation"] == 1 and view["segments"] == segs:
        ok("同标识重传幂等：未新建段、未新增代次、结果不变")
    else:
        passed &= fail("重传产生了新段或新代次",
                       f"gen={again.get('generation')} segs={view['segments']}")

    # reassembly reproduces exact text
    _, ra = c.get(f"/api/drills/{did}/artifacts/1/reassemble")
    if ra["text"] == f"帧头-{tag}地形 A1帧尾-9F":
        ok("从段中按偏移重新拼出工件，字节一致")
    else:
        passed &= fail("重组文本不一致", ra.get("text", ""))

    # ---- crash point B: after catalog switch, before sweep
    changed = dict(drill_body)
    changed["artifacts"] = drill_body["artifacts"] + [
        {"name": "新增工件", "fragments": [f"帧头-{tag}", f"全新片段-{tag}"]},
    ]
    c.put(f"/api/drills/{did}/artifacts", changed, expect=200)
    _, mid = c.get(f"/api/drills/{did}")
    if mid["recovery"]["complete"] is False:
        ok("重新登记后、再压缩前：恢复裁决判不完整且原目录保留")
    else:
        passed &= fail("重新登记后裁决仍误判完整")

    cid2 = f"CID-VERIFY2-{tag}"
    _, crash2 = c.post(
        f"/api/drills/{did}/compact",
        {"consolidation_id": cid2, "crash_after": "switch"}, expect=503,
    )
    _, mid = c.get(f"/api/drills/{did}")
    if mid["active_generation"] == 2 and len(mid["segments"]) == 2:
        ok("目录切换后断电：第 2 代已活动，旧段尚未清扫（2 个段并存）")
    else:
        passed &= fail("断电点 B 状态错误", json.dumps(mid)[:400])

    _, report2 = c.post("/api/admin/reopen", {})
    _, after = c.get(f"/api/drills/{did}")
    if (after["active_generation"] == 2
            and len(after["segments"]) == 1
            and after["segments"] != segs
            and after["recovery"]["complete"] is True
            and cid2 in report2.get("recovered", [])):
        ok("重开收敛：新目录可拼出全部工件后旧段才被清扫")
    else:
        passed &= fail("第 2 代重开收敛/清扫错误",
                       f"report={json.dumps(report2)}\nsegments={after['segments']}")
    _, again2 = c.post(f"/api/drills/{did}/compact",
                       {"consolidation_id": cid2})
    _, after2 = c.get(f"/api/drills/{did}")
    if again2["generation"] == 2 and after2["segments"] == after["segments"]:
        ok("第 2 代同标识重传同样幂等")
    else:
        passed &= fail("第 2 代重传非幂等")

    # ---- historical retransmission across a service reopen --------------
    # Generations 1 (cid) and 2 (cid2) completed above and every catalog is
    # complete. Reopen with the data volume preserved, re-register the
    # first-generation artifacts, then retransmit the first-generation id:
    # it must return the frozen gen-1 result — no new segment, no new
    # generation, and the active catalog must not move backwards.
    _, rep = c.post("/api/admin/reopen", {})
    _, pre = c.get(f"/api/drills/{did}")
    c.put(f"/api/drills/{did}/artifacts", drill_body, expect=200)
    code, hist = c.call("POST", f"/api/drills/{did}/compact",
                        {"consolidation_id": cid})
    _, hv = c.get(f"/api/drills/{did}")
    if (rep.get("recovered") == [] and rep.get("swept") == []
            and code == 200
            and hist["generation"] == 1
            and hist.get("retransmission") is True
            and hist.get("historical") is True
            and hv["active_generation"] == 2
            and hv["segments"] == pre["segments"]
            and [g["generation"] for g in hv["generations"]] == [1, 2]
            and {g["generation"]: g["status"] for g in hv["generations"]}
                == {1: "retired", 2: "active"}):
        ok("保留数据重开后重传首代标识：返回首代冻结结果，无新段/新代次，活动目录保持第 2 代")
    else:
        passed &= fail(
            "历史标识重传破坏了与首代结果的不可变关联",
            f"reopen={json.dumps(rep)}\ncode={code} hist={json.dumps(hist)[:200]}\n"
            f"active={hv['active_generation']} segs={hv['segments']}\n"
            f"gens={[(g['generation'], g['status']) for g in hv['generations']]}",
        )

    # same first-generation id + different artifact content: the current
    # active catalog is retained and the existing rejection reason returned
    tampered = {
        "name": drill_body["name"],
        "artifacts": [
            {"name": "光谱校准帧", "fragments": [f"帧头-{tag}", "光谱 550", "帧尾-9F"]},
            {"name": "地形条带A", "fragments": [f"帧头-{tag}", f"被篡改-{tag}", "帧尾-9F"]},
            {"name": "云量速报", "fragments": [f"帧头-{tag}", "云量 18%", "帧尾-9F"]},
        ],
    }
    c.put(f"/api/drills/{did}/artifacts", tampered, expect=200)
    code, r5 = c.call("POST", f"/api/drills/{did}/compact",
                      {"consolidation_id": cid})
    if (code == 409 and r5["reject"]["code"] == "fragment_digest_mismatch"
            and r5["drill"]["active_generation"] == 2):
        ok("首代标识异内容重传：409 既有拒因，活动目录（第 2 代）保留")
    else:
        passed &= fail("首代标识异内容重传未被正确拒绝",
                       f"code={code} body={json.dumps(r5)[:300]}")

    # restore the second-generation artifact set so the drill converges again
    c.put(f"/api/drills/{did}/artifacts", changed, expect=200)

    # ---- rejection rules: active catalog retained, first reason returned
    # reason 1: artifact set mismatch
    other = {
        "name": f"verify-另一演练-{tag}",
        "artifacts": [
            {"name": "完全不同-X", "fragments": ["1"]},
            {"name": "完全不同-Y", "fragments": ["2"]},
        ],
    }
    _, other_drill = c.post("/api/drills", other, expect=201)
    code, r1 = c.call("POST", f"/api/drills/{other_drill['id']}/compact",
                      {"consolidation_id": cid})
    if code == 409 and r1["reject"]["code"] == "artifact_set_mismatch":
        ok("拒因① 工件集合不同：409 且原活动目录保留")
    else:
        passed &= fail("工件集合不符未被拒绝", json.dumps(r1)[:300])

    # reason 2: fragment digest mismatch — same drill, same artifact names,
    # but one fragment's content changed
    _, dm = c.post("/api/drills", {
        "name": f"verify-摘要演练-{tag}",
        "artifacts": [
            {"name": "M1", "fragments": ["相同帧头", f"原文-{tag}"]},
            {"name": "M2", "fragments": ["相同帧头", f"另一原文-{tag}"]},
        ],
    }, expect=201)
    mid2 = dm["id"]
    cid_dm = f"CID-DIGEST-{tag}"
    c.call("POST", f"/api/drills/{mid2}/compact",
           {"consolidation_id": cid_dm})
    c.put(f"/api/drills/{mid2}/artifacts", {
        "name": f"verify-摘要演练-{tag}",
        "artifacts": [
            {"name": "M1", "fragments": ["相同帧头", f"被篡改-{tag}"]},
            {"name": "M2", "fragments": ["相同帧头", f"另一原文-{tag}"]},
        ],
    }, expect=200)
    code, r2 = c.call("POST", f"/api/drills/{mid2}/compact",
                      {"consolidation_id": cid_dm})
    if code == 409 and r2["reject"]["code"] == "fragment_digest_mismatch" \
            and r2["drill"]["active_generation"] == 1:
        ok("拒因② 片段摘要不符：409 且原活动目录（第1代）保留")
    else:
        passed &= fail("片段摘要不符未被拒绝", json.dumps(r2)[:300])

    # reason 3: required segment missing, source bytes already compacted away
    data_dir = health.get("data_dir")
    seg_dir = os.path.join(data_dir, "seg") if data_dir else None
    if seg_dir and os.path.isdir(seg_dir):
        _, loss_drill = c.post("/api/drills", {
            "name": f"verify-丢段演练-{tag}",
            "artifacts": [
                {"name": "L1", "fragments": [f"丢段文本A-{tag}"]},
                {"name": "L2", "fragments": [f"丢段文本B-{tag}"]},
            ],
        }, expect=201)
        lid = loss_drill["id"]
        cid3 = f"CID-VERIFY3-{tag}"
        _, loss_ok = c.post(f"/api/drills/{lid}/compact",
                            {"consolidation_id": cid3})
        active = [g for g in loss_ok["drill"]["generations"]
                  if g["status"] == "active"]
        lost_seg = active[0]["segment"]
        for suffix in (".pack", ".idx"):
            os.remove(os.path.join(seg_dir, lost_seg + suffix))
        code, r3 = c.call("POST", f"/api/drills/{lid}/compact",
                          {"consolidation_id": cid3})
        r3view = r3.get("drill", {})
        if (code == 409 and r3["reject"]["code"] == "segment_missing"
                and r3view.get("active_generation") == 1):
            ok("拒因③ 新段缺失且无源字节：409，目录登记仍保留（不新建段）")
        else:
            passed &= fail("段缺失场景未正确拒绝",
                           f"code={code} body={json.dumps(r3)[:300]}")
    else:
        print(f"{YELLOW}  ! 跳过拒因③ 在线删段（数据目录不可见：{data_dir}）{RESET}")

    # active catalog of the original drill is untouched after all rejections
    _, final = c.get(f"/api/drills/{did}")
    if final["active_generation"] == 2 and final["recovery"]["complete"]:
        ok("全部拒绝均未改变原活动目录与恢复裁决")
    else:
        passed &= fail("拒绝处理影响了活动目录")

    step("4/4 恢复与重传结果核对")
    _, rec = c.get("/api/recovery")
    # The most recent reopen is the historical-retransmission one above: a
    # clean convergence with nothing left to recover or sweep. (cid/cid2
    # recovery evidence was asserted at their own reopen steps.)
    if (rec.get("reopened_at")
            and isinstance(rec.get("recovered"), list)
            and isinstance(rec.get("retransmitted"), list)
            and isinstance(rec.get("swept"), list)):
        ok(f"最近恢复报告结构完整：recovered={rec['recovered']} "
           f"retransmitted={rec['retransmitted']} swept={rec['swept']}")
    else:
        passed &= fail("恢复报告内容不符", json.dumps(rec, ensure_ascii=False))
    # missing-segment rejection is exercised at engine level by pytest;
    # surface the live verdict for this converged drill as final evidence.
    if after2["recovery"]["complete"] and not after2["recovery"]["missing_fragments"]:
        ok("活动目录恢复裁决 complete=true，缺失片段列表为空")
    else:
        passed &= fail("最终恢复裁决不完整", json.dumps(after2["recovery"])[:300])

    return passed


def main() -> int:
    print("地面成像站紧凑存储 · verify 单次核对服务")
    print(f"工作目录: {ROOT}")

    if not check_build():
        return finish()
    if not check_tests():
        return finish()

    base = os.environ.get("BASE_URL")
    proc = None
    spawned = False
    if base:
        if not wait_for_health(base, attempts=10):
            fail(f"BASE_URL 不可达: {base}")
            return finish()
        print(f"\n使用已运行的服务: {base}")
    else:
        print("\n未设置 BASE_URL，本地拉起临时 uvicorn …")
        proc, base, _ = spawn_server()
        spawned = True
        print(f"临时服务就绪: {base}")

    try:
        smoke_ok = check_smoke(Client(base))
    finally:
        if spawned and proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    return finish()


def finish() -> int:
    print("\n" + "=" * 60)
    if failures:
        print(f"{RED}verify 失败：{len(failures)} 项{RESET}")
        for f in failures:
            print(f"  - {f}")
        code = 1
    else:
        print(f"{GREEN}verify 全部通过：恢复、重传、测试、构建与 HTTP 冒烟均成功{RESET}")
        code = 0
    print(f"退出码: {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())

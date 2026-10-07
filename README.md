# 地面成像站 · 共享片段紧凑存储演练

档案员把可重复引用的短文本片段整理进紧凑**段（pack segment）**前，必须保证
**任何断电都不会让已登记工件指向错误偏移、也不会让旧段被提前清除**。本项目用
一个 FastAPI 服务 + 网页，把这条安全不变式做成可演练、可注入断电、可重开收敛
的完整系统。

## 安全不变式（实现如何保证）

1. **先持久化新段与完整索引，再切换目录**
   - 段文件 `<segid>.pack`（拼接字节）与 `<segid>.idx`（digest→offset/length +
     整段校验和）都用「临时文件 + fsync + `os.replace` + 目录 fsync」原子落盘。
   - 索引文件是提交标记：pack 先写、idx 后写；任何时刻读到的段都可自校验。
2. **原子切换唯一目录代次**
   - 每代目录 `gen/<drill>-gen-N.json` 不可变；`gen/<drill>.current` 指针用
     `os.replace` 原子发布，任何时刻都只有**一个**活动代次。
3. **旧段只能在新目录可重新拼出全部工件后清扫**
   - 切换后先用段按偏移逐片段重建所有已登记工件并校验摘要；确认无缺失才删除
     上一代段，清扫前再次检查所有活动目录都完整。
4. **两个断电注入点，重开必收敛为一份完整目录**
   - `crash_after=segments`：新段已落盘、目录尚未切换；
   - `crash_after=switch`：目录已切换、旧段尚未清扫。
   - 重开（进程重启或 `POST /api/admin/reopen`）重放 WAL（planned→
     segments_written→switched→done），缺失的段按**内容寻址**以**同一 segid**
     重传，绝不新建段，随后完成切换/清扫。
   - 整理完成后才丢段的演练属于**降级而非不一致**：重开照常收敛其余演练，
     在恢复报告 `degraded` 与恢复裁决中如实列出缺失片段，服务不因此拒绝启动。
5. **整理标识一经成功即永久绑定**：标识 + 首次工件摘要 + 目录代次 + 段不可变；
   新一代整理只把旧标识记入审计历史，**绝不删除其绑定**。服务重开后重传
   早期标识，返回其首次冻结的代次与结果——不创建段、不新增代次、活动目录
   不倒退，第二代目录及其审计历史不受历史重传影响。
6. **三类拒因（保留原活动目录，返回首个拒因，HTTP 409）**
   - `artifact_set_mismatch`：整理标识已绑定**不同工件集合**；
   - `fragment_digest_mismatch`：集合形状相同但**片段摘要不符**；
   - `segment_missing`：**新段缺失**且本地无源字节可重传。

> 压缩完成后，源片段文本会从注册表中剥离（真正的紧凑存储），只保留摘要与偏移；
> 进行中的 WAL 始终保留文本快照，专门用于断电重开时的内容寻址重传。

## 数据目录布局（`DATA_DIR`，默认 `/data`）

```
registry.json            演练、工件摘要、整理标识绑定（永久保留，原子写）
seg/<segid>.pack|.idx    内容寻址段与偏移索引
wal/<cid>.json           压缩 WAL（收敛驱动，完成即删）
gen/<drill>-gen-N.json   不可变目录代次
gen/<drill>.current      唯一活动代次指针
crash.log                模拟断电记录
```

## 运行（Docker Compose）

```bash
# 宿主端口由 Compose 配置（HOST_PORT，默认 8080），健康入口 /health
HOST_PORT=8080 docker compose up web
# 浏览器打开 http://localhost:8080

# 名为 verify 的单次服务：恢复/重传核对 + pytest + 构建检查 + API/HTTP 冒烟
# + 历史标识重传验收（经 /api/admin/reopen 重开收敛后核对冻结结果），
# 等待 web 健康后执行，以退出码结束（0 成功 / 1 失败）
docker compose run --rm verify
```

## 本地开发（无 Docker）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
DATA_DIR=.runtime-data .venv/bin/python -m uvicorn app.main:app --port 8080
.venv/bin/python scripts/verify.py     # 会自动拉起临时服务做全量核对
                                       # （含保留数据目录真实重开进程后的
                                       #   历史标识重传验收）
.venv/bin/python -m pytest tests -q    # 仅跑代码测试（23 项）
```

## 网页操作流

1. 建立含 **2–8 份工件**的演练，每份工件由短片段按序组成（可「填入示例」体验
   跨工件共享片段）；
2. 用**稳定整理标识**发起压缩，可选择在「新段落盘后」或「目录切换后」注入断电；
3. 查看**活动代次**、每份工件的**重组摘要**、每个片段**所在段与偏移**、以及
   **恢复裁决**（complete / 缺失片段 / 是否发生重传）；
4. 「模拟重开」后视图收敛为一份完整目录；同标识「重传」不产生新段；重传
   **早期代次**的标识时，页面标注「第 N 代冻结结果 · 活动目录仍为第 M 代」，
   不创建段或代次、活动目录不倒退；
5. 「把编辑器内容重新登记到所选演练」后，裁决变为不完整且旧目录保留；用**新的
   整理标识**再压缩即产生下一代，旧段在验证全部可重组后才被清扫。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康入口（Compose 健康检查使用） |
| POST | `/api/drills` | 创建演练（2–8 份工件） |
| GET | `/api/drills` / `/api/drills/{id}` | 列表 / 详情（代次、摘要、偏移、裁决） |
| PUT | `/api/drills/{id}/artifacts` | 重新登记工件集合（原活动目录保留） |
| POST | `/api/drills/{id}/compact` | 压缩；`crash_after` 注入断电；409 返回拒因 |
| GET | `/api/drills/{id}/artifacts/{i}/reassemble` | 从段按偏移重新拼出工件原文 |
| POST | `/api/admin/reopen` | 模拟断电后重开收敛 |
| GET | `/api/recovery` | 最近一次重开恢复报告 |

断电响应为 `503 {"status":"simulated_crash",...}`；重开收敛后再以同一标识请求
即为普通幂等重传。`CRASH_MODE=hard` 时断电点会以退出码 7 直接终止进程（容器
重启收敛），默认 `soft` 仅抛出并把崩溃现场留在磁盘上便于网页演练。

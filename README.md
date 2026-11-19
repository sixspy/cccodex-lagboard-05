# 源到达SLA

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 源到达SLA |
| 目录 | `06-lagboard` |
| 领域建议 | 数据采集、爬虫与 RPA |
| 接口 | http://127.0.0.1:10815 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

源时钟 vs 摄入时钟、分级处罚、降级可读不可写、补齐解冻

## 避开

别做成纯监控大盘；须有写路径与状态机

## 互斥切口候选

- 超SLA冻结 vs 降级只读
- 补齐整源解冻 vs 按业务日窗口解冻
- 只认源时钟 vs 双时钟取严

## 批量补采（同败同成）

同一影子批的批量补采只允许走下列一种写路径，甲、乙不得混用；当天只认一本批量同命运册。

| 路径 | 机制 | 落账口径 |
|------|------|----------|
| 甲 | 外键同批号 | 整批到达，正式侧以外键同批号绑定批头；缺一行则两侧拒收，正式侧零新增 |
| 乙 | 旁路影子批表 | 批行先入旁路影子批表核对行命运，整批晋升；遇半成功则影子批表清空，正式行不增 |

- 入口：前端「补齐恢复」页内的「批量补采（同败同成）」区，不新开批采台；口令沿用本仓批采侧 `admin/123456`。
- 库：`data/app.db`，同库新增 `batch_queue / batch_shadow / batch_ledger / batch_register / batch_contention / batch_deviation / batch_lock / batch_source_touch / watermarks`。
- 落板即写批量同命运说明并封档；`batch/queue` 每条标出 `effective_mechanism`（外键同批号 / 旁路影子批表何者生效）。
- 两路调度交同一补采批须占 `batch_lock`；后交只记「批量补采争用」，禁止重复晋升同一影子批。
- 补采队列、到达列表、源目录、水位页与对账账册禁止半成功行；`GET /api/batch/audit` 五处对齐校验。
- 偏离同命运说明（同日换路）即驳回并记「批量偏了」。

## 启动（一键）
`powershell
cd projects/06-lagboard
docker compose up -d --build
`

浏览器：http://127.0.0.1:10815/  
健康检查：GET http://127.0.0.1:10815/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10603
python -m pytest -q
`

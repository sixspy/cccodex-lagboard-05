
import hashlib, secrets, sqlite3
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

MECH_A = "外键同批号"
MECH_B = "旁路影子批表"
PATH_MECH = {"甲": MECH_A, "乙": MECH_B}
SAME_FATE_NOTE = {
    "甲": "批量同命运说明：甲路整批到达，正式侧以批量补采外键同批号约束绑定批头，缺一行则两侧拒收，正式侧零新增。",
    "乙": "批量同命运说明：乙路批行先入旁路影子批表核对行命运，全部到站后整批晋升；遇半成功则影子批表清空，正式行不增。",
}
DAY_REGISTER_NOTE = "当天仅允许一本批量同命运册，不得更换路径；偏离即驳回并记批量偏了。"

def ensure_batch_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS batch_register(
      day TEXT PRIMARY KEY,
      path TEXT NOT NULL,
      mechanism TEXT NOT NULL,
      note TEXT NOT NULL,
      sealed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS batch_queue(
      batch_no TEXT PRIMARY KEY,
      day TEXT NOT NULL,
      path TEXT NOT NULL,
      mechanism TEXT NOT NULL,
      status TEXT NOT NULL,
      same_fate TEXT NOT NULL,
      rows_declared INTEGER NOT NULL,
      rows_arrived INTEGER NOT NULL,
      formal_added INTEGER NOT NULL DEFAULT 0,
      message TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      finished_at TEXT
    );
    CREATE TABLE IF NOT EXISTS batch_shadow(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      batch_no TEXT NOT NULL,
      seq INTEGER NOT NULL,
      source TEXT NOT NULL,
      source_clock TEXT NOT NULL,
      ingest_clock TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS batch_ledger(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      batch_no TEXT NOT NULL,
      day TEXT NOT NULL,
      path TEXT NOT NULL,
      mechanism TEXT NOT NULL,
      verdict TEXT NOT NULL,
      declared INTEGER NOT NULL,
      arrived INTEGER NOT NULL,
      formal_added INTEGER NOT NULL,
      shadow_cleared INTEGER NOT NULL DEFAULT 0,
      note TEXT NOT NULL,
      sealed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS batch_contention(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      batch_no TEXT NOT NULL,
      detail TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS batch_deviation(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      batch_no TEXT NOT NULL,
      day TEXT NOT NULL,
      detail TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS batch_lock(
      batch_no TEXT PRIMARY KEY,
      holder TEXT NOT NULL,
      acquired_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS batch_source_touch(
      batch_no TEXT NOT NULL,
      source TEXT NOT NULL,
      PRIMARY KEY(batch_no, source)
    );
    CREATE TABLE IF NOT EXISTS watermarks(
      source TEXT PRIMARY KEY,
      rows INTEGER NOT NULL,
      batch_no TEXT,
      updated_at TEXT NOT NULL
    );
    """)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(arrivals)").fetchall()}
    if "batch_no" not in cols:
        conn.execute("ALTER TABLE arrivals ADD COLUMN batch_no TEXT REFERENCES batch_queue(batch_no)")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(catchups)").fetchall()}
    if "batch_no" not in cols:
        conn.execute("ALTER TABLE catchups ADD COLUMN batch_no TEXT")

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "源到达SLA"
SKELETON = "多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          sla_minutes INTEGER NOT NULL,
          output_mode TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS arrivals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_clock TEXT NOT NULL,
          ingest_clock TEXT NOT NULL,
          lag_minutes INTEGER NOT NULL,
          level TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS catchups(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          restored_mode TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        ensure_batch_schema(conn)
        if conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0:
            conn.executemany("INSERT INTO sources(name,sla_minutes,output_mode) VALUES (?,?,?)", [
                ("订单源", 15, "可写"), ("支付源", 20, "降级只读"), ("物流源", 30, "冻结"),
            ])
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("支付源","2026-11-16T08:00:00","2026-11-16T08:35:00",35,"告警"))
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("物流源","2026-11-16T07:00:00","2026-11-16T09:10:00",130,"违约"))
        # 水位页与到达列表对齐：任何已有到达行的源都要有水位记录；已有记录不覆盖批号留痕
        for row in conn.execute("SELECT DISTINCT source FROM arrivals").fetchall():
            source = row["source"]
            if conn.execute("SELECT 1 FROM watermarks WHERE source=?", (source,)).fetchone():
                continue
            _refresh_watermark(conn, source, None)

def level_of(lag, sla):
    if lag <= sla: return "正常"
    if lag <= sla * 2: return "告警"
    return "违约"

def mode_of(level):
    return {"正常":"可写", "告警":"降级只读", "违约":"冻结"}[level]

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "sources": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/sources")
def sources(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id").fetchall()]}

class ArrivalIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str
    batch_no: str | None = None

@app.post("/api/arrivals")
def add_arrival(body: ArrivalIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    # 批量补采只能走 /api/batch/submit 的同败同成写法，禁止逐行提交撕裂同一批
    if body.batch_no:
        raise HTTPException(409, "批量补采须走批量补采同败同成落账写法，不得经单行到达逐行提交")
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        # lag by minute rough parse
        def parse(t):
            return datetime.fromisoformat(t)
        lag = int((parse(body.ingest_clock) - parse(body.source_clock)).total_seconds() // 60)
        if lag < 0: lag = 0
        lv = level_of(lag, src["sla_minutes"])
        mode = mode_of(lv)
        c.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                  (body.source, body.source_clock, body.ingest_clock, lag, lv))
        c.execute("UPDATE sources SET output_mode=? WHERE name=?", (mode, body.source))
        _refresh_watermark(c, body.source, None)
        return {"lag_minutes": lag, "level": lv, "output_mode": mode}

@app.get("/api/arrivals")
def arrivals(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM arrivals ORDER BY id DESC").fetchall()]}

@app.post("/api/catchup/{source}")
def catchup(source: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        c.execute("UPDATE sources SET output_mode='可写' WHERE name=?", (source,))
        c.execute("INSERT INTO catchups(source,status,restored_mode,note,created_at) VALUES (?,?,?,?,?)",
                  (source, "成功", "可写", "补齐后恢复可写", now))
        return {"ok": True, "output_mode": "可写"}

@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM catchups ORDER BY id DESC").fetchall()]}

# ---------- 批量补采：同败同成落账 ----------
# 甲路：外键同批号，整批到达；缺一行则两侧拒收，正式侧零新增。
# 乙路：批行先入旁路影子批表核对行命运，整批晋升；半成功则影子批表清空，正式行不增。

class BatchRowIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str

class BatchIn(BaseModel):
    batch_no: str
    path: str
    rows: list[BatchRowIn]
    declared_rows: int | None = None
    day: str | None = None

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")

def _refresh_watermark(conn, source: str, batch_no: str | None):
    cnt = conn.execute("SELECT COUNT(*) c FROM arrivals WHERE source=?", (source,)).fetchone()["c"]
    conn.execute(
        "INSERT INTO watermarks(source,rows,batch_no,updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET rows=excluded.rows,batch_no=excluded.batch_no,updated_at=excluded.updated_at",
        (source, cnt, batch_no, _now()))

def _parse_lag(row: BatchRowIn) -> int:
    lag = int((datetime.fromisoformat(row.ingest_clock) - datetime.fromisoformat(row.source_clock)).total_seconds() // 60)
    return max(lag, 0)

def _validate_rows(conn, rows: list[BatchRowIn]) -> list[dict]:
    """整批校验；任一行不合格即抛出批量失败，调用方负责同败落账。"""
    checked = []
    for row in rows:
        src = conn.execute("SELECT * FROM sources WHERE name=?", (row.source,)).fetchone()
        if not src:
            raise ValueError(f"未知源 {row.source}")
        try:
            lag = _parse_lag(row)
        except Exception:
            raise ValueError(f"源时钟非法 {row.source}: {row.source_clock} → {row.ingest_clock}")
        lv = level_of(lag, src["sla_minutes"])
        checked.append({"row": row, "lag": lag, "level": lv, "mode": mode_of(lv)})
    return checked

def _queue_failed(conn, body: BatchIn, day: str, mech: str, arrived: int, declared: int, message: str, shadow_cleared: int):
    now = _now()
    conn.execute(
        "INSERT INTO batch_queue(batch_no,day,path,mechanism,status,same_fate,rows_declared,rows_arrived,formal_added,message,created_at,finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (body.batch_no, day, body.path, mech, "失败", "同败", declared, arrived, 0, message, now, now))
    conn.execute(
        "INSERT INTO batch_ledger(batch_no,day,path,mechanism,verdict,declared,arrived,formal_added,shadow_cleared,note,sealed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (body.batch_no, day, body.path, mech, "同败", declared, arrived, 0, shadow_cleared,
         SAME_FATE_NOTE[body.path] + " 本批同败，正式侧零新增，已封档。", now))

def _promote(conn, body: BatchIn, day: str, mech: str, checked: list[dict], declared: int, shadow_cleared: int):
    now = _now()
    conn.execute(
        "INSERT INTO batch_queue(batch_no,day,path,mechanism,status,same_fate,rows_declared,rows_arrived,formal_added,message,created_at,finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (body.batch_no, day, body.path, mech, "成功", "同成", declared, len(checked), len(checked),
         SAME_FATE_NOTE[body.path], now, now))
    touched = {}
    for item in checked:
        row = item["row"]
        conn.execute(
            "INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level,batch_no) VALUES (?,?,?,?,?,?)",
            (row.source, row.source_clock, row.ingest_clock, item["lag"], item["level"], body.batch_no))
        conn.execute("UPDATE sources SET output_mode=? WHERE name=?", (item["mode"], row.source))
        conn.execute(
            "INSERT INTO catchups(source,status,restored_mode,note,created_at,batch_no) VALUES (?,?,?,?,?,?)",
            (row.source, "同成", item["mode"], SAME_FATE_NOTE[body.path], now, body.batch_no))
        conn.execute("INSERT OR IGNORE INTO batch_source_touch(batch_no,source) VALUES (?,?)",
                     (body.batch_no, row.source))
        touched[row.source] = True
    for source in touched:
        _refresh_watermark(conn, source, body.batch_no)
    conn.execute(
        "INSERT INTO batch_ledger(batch_no,day,path,mechanism,verdict,declared,arrived,formal_added,shadow_cleared,note,sealed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (body.batch_no, day, body.path, mech, "同成", declared, len(checked), len(checked), shadow_cleared,
         SAME_FATE_NOTE[body.path] + " 本批同成，正式侧整批落账，已封档。", now))

@app.post("/api/batch/submit")
def batch_submit(body: BatchIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    if body.path not in PATH_MECH:
        raise HTTPException(400, "未知批量补采路径，只认甲路或乙路")
    batch_no = body.batch_no.strip()
    if not batch_no:
        raise HTTPException(400, "缺批量补采批号")
    body.batch_no = batch_no
    rows = body.rows or []
    if not rows:
        raise HTTPException(400, "空批：批量补采须至少一行，不得空批落板")
    declared = body.declared_rows if body.declared_rows is not None else len(rows)
    day = body.day or datetime.now().date().isoformat()
    mech = PATH_MECH[body.path]
    now = _now()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 同一补采批只允许一次落板：在途/已落板均记争用，后交不得重复晋升
        prior = conn.execute("SELECT batch_no FROM batch_queue WHERE batch_no=?", (batch_no,)).fetchone()
        locked = conn.execute("SELECT batch_no FROM batch_lock WHERE batch_no=?", (batch_no,)).fetchone()
        if prior or locked:
            conn.execute("INSERT INTO batch_contention(batch_no,detail,created_at) VALUES (?,?,?)",
                         (batch_no, "批量补采争用：同一补采批已被调度占用或已落板，后交只记争用", now))
            conn.execute("COMMIT")
            raise HTTPException(409, "批量补采争用：同一补采批只允许一次落板，后交只记批量补采争用")
        reg = conn.execute("SELECT * FROM batch_register WHERE day=?", (day,)).fetchone()
        if reg and reg["path"] != body.path:
            conn.execute("INSERT INTO batch_deviation(batch_no,day,detail,created_at) VALUES (?,?,?,?)",
                         (batch_no, day, f"批量偏了：当天同命运册已封档为{reg['path']}路，本批改用{body.path}路，偏离同命运说明", now))
            conn.execute("COMMIT")
            raise HTTPException(409, "批量偏了：当天批量同命运册已封档，禁止更换路径")
        if not reg:
            conn.execute("INSERT INTO batch_register(day,path,mechanism,note,sealed_at) VALUES (?,?,?,?,?)",
                         (day, body.path, mech, SAME_FATE_NOTE[body.path] + DAY_REGISTER_NOTE, now))
        conn.execute("INSERT INTO batch_lock(batch_no,holder,acquired_at) VALUES (?,?,?)", (batch_no, user, now))

        if body.path == "乙":
            # 乙路：先入旁路影子批表，再核对行命运
            for seq, row in enumerate(rows, 1):
                conn.execute(
                    "INSERT INTO batch_shadow(batch_no,seq,source,source_clock,ingest_clock) VALUES (?,?,?,?,?)",
                    (batch_no, seq, row.source, row.source_clock, row.ingest_clock))
            arrived = len(rows)
            try:
                if arrived != declared:
                    raise ValueError(f"缺行：声明 {declared} 行，实际到达 {arrived} 行")
                checked = _validate_rows(conn, rows)
            except ValueError as e:
                conn.execute("DELETE FROM batch_shadow WHERE batch_no=?", (batch_no,))
                _queue_failed(conn, body, day, mech, arrived, declared, str(e), arrived)
                conn.execute("COMMIT")
                raise HTTPException(409, f"{e}；乙路影子批表已清空，正式行不增")
            conn.execute("DELETE FROM batch_shadow WHERE batch_no=?", (batch_no,))
            _promote(conn, body, day, mech, checked, declared, len(rows))
            conn.execute("COMMIT")
            return {"ok": True, "batch_no": batch_no, "path": body.path, "mechanism": mech,
                    "verdict": "同成", "formal_added": len(checked), "same_fate_note": SAME_FATE_NOTE[body.path]}

        # 甲路：整批到达，外键同批号；先校验，再在同一事务内整批落正式侧
        arrived = len(rows)
        try:
            if arrived != declared:
                raise ValueError(f"缺行：声明 {declared} 行，实际到达 {arrived} 行")
            checked = _validate_rows(conn, rows)
        except ValueError as e:
            _queue_failed(conn, body, day, mech, arrived, declared, str(e), 0)
            conn.execute("COMMIT")
            raise HTTPException(409, f"{e}；两侧拒收，正式侧零新增")
        _promote(conn, body, day, mech, checked, declared, 0)
        conn.execute("COMMIT")
        return {"ok": True, "batch_no": batch_no, "path": body.path, "mechanism": mech,
                "verdict": "同成", "formal_added": len(checked), "same_fate_note": SAME_FATE_NOTE[body.path]}
    except HTTPException:
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()

def _mechanisms_for(conn, day: str | None):
    if day:
        reg = conn.execute("SELECT * FROM batch_register WHERE day=?", (day,)).fetchone()
    else:
        reg = conn.execute("SELECT * FROM batch_register ORDER BY sealed_at DESC, rowid DESC LIMIT 1").fetchone()
    if not reg:
        return {MECH_A: "未启用", MECH_B: "未启用"}, None
    return ({MECH_A: "生效" if reg["path"] == "甲" else "未启用",
             MECH_B: "生效" if reg["path"] == "乙" else "未启用"}, reg)

@app.get("/api/batch/queue")
def batch_queue(day: str | None = None, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        sql = "SELECT * FROM batch_queue"
        args: tuple = ()
        if day:
            sql += " WHERE day=?"
            args = (day,)
        sql += " ORDER BY created_at DESC, rowid DESC"
        items = [dict(r) for r in c.execute(sql, args).fetchall()]
        for item in items:
            item["effective_mechanism"] = item["mechanism"]
            item["same_fate_note"] = SAME_FATE_NOTE.get(item["path"], "")
        mechs, reg = _mechanisms_for(c, day)
        return {"items": items, "mechanisms": mechs,
                "register": dict(reg) if reg else None}

@app.get("/api/batch/ledger")
def batch_ledger(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM batch_ledger ORDER BY id DESC").fetchall()]}

@app.get("/api/batch/shadow")
def batch_shadow(batch_no: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute(
            "SELECT * FROM batch_shadow WHERE batch_no=? ORDER BY seq", (batch_no,)).fetchall()]}

@app.get("/api/batch/contention")
def batch_contention(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM batch_contention ORDER BY id DESC").fetchall()]}

@app.get("/api/batch/deviation")
def batch_deviation(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM batch_deviation ORDER BY id DESC").fetchall()]}

@app.get("/api/batch/register")
def batch_register(day: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        reg = c.execute("SELECT * FROM batch_register WHERE day=?", (day,)).fetchone()
        if not reg:
            raise HTTPException(404, "当天无批量同命运册")
        return dict(reg)

@app.get("/api/watermarks")
def watermarks(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM watermarks ORDER BY source").fetchall()]}

@app.get("/api/batch/audit")
def batch_audit(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        places = {k: {"half_success_rows": 0, "checked_batches": 0} for k in
                  ("catchup_queue", "arrivals", "sources", "watermarks", "ledger")}
        batches = c.execute("SELECT * FROM batch_queue ORDER BY rowid").fetchall()
        total = 0
        for b in batches:
            no = b["batch_no"]
            ok_batch = b["status"] == "成功"
            fa = b["formal_added"]
            arr_rows = c.execute("SELECT rowid, source, level FROM arrivals WHERE batch_no=?", (no,)).fetchall()
            arr_cnt = len(arr_rows)
            cap_cnt = c.execute("SELECT COUNT(*) c FROM catchups WHERE batch_no=?", (no,)).fetchone()["c"]
            touch = {r["source"] for r in c.execute("SELECT source FROM batch_source_touch WHERE batch_no=?", (no,)).fetchall()}
            arr_src = {r["source"] for r in arr_rows}
            led = c.execute("SELECT * FROM batch_ledger WHERE batch_no=?", (no,)).fetchone()
            shadow_left = c.execute("SELECT COUNT(*) c FROM batch_shadow WHERE batch_no=?", (no,)).fetchone()["c"]
            # 乙路已落板（不论成败）都不得在旁路影子批表留行：失败须清空，成功须整批晋升后清空
            shadow_bad = shadow_left > 0
            # 逐源核对：本批触及的源必须都存在；水位页行数须等于到达列表实计数。
            # 不拿 sources.output_mode 与本批最后一行比：后续合法单行到达/补齐恢复会改动它，不属于半成功。
            src_bad = 0
            wm_bad = 0
            for source in arr_src:
                if not c.execute("SELECT 1 FROM sources WHERE name=?", (source,)).fetchone():
                    src_bad += 1
                wm = c.execute("SELECT * FROM watermarks WHERE source=?", (source,)).fetchone()
                actual = c.execute("SELECT COUNT(*) c FROM arrivals WHERE source=?", (source,)).fetchone()["c"]
                if not wm or wm["rows"] != actual:
                    wm_bad += 1
            if touch != arr_src:
                src_bad += 1
            expected_fa = arr_cnt
            if not ok_batch:
                expected_fa = 0
            flags = {
                # 补采队列：成功批 formal_added 须等于到达行数且逐行留补齐记录；失败批须零新增、同败且补采记录不留半成功
                "catchup_queue": fa != expected_fa or b["same_fate"] != ("同成" if ok_batch else "同败")
                                 or (ok_batch and cap_cnt != arr_cnt)
                                 or (not ok_batch and cap_cnt != 0),
                "arrivals": arr_cnt != expected_fa or shadow_bad,
                "sources": arr_cnt > 0 and src_bad > 0,
                "watermarks": arr_cnt > 0 and wm_bad > 0,
                "ledger": not led or led["verdict"] != ("同成" if ok_batch else "同败")
                          or led["formal_added"] != expected_fa or led["arrived"] != b["rows_arrived"]
                          or arr_cnt != expected_fa,
            }
            for k, bad in flags.items():
                places[k]["checked_batches"] += 1
                if bad:
                    places[k]["half_success_rows"] += 1
            if any(flags.values()):
                total += 1
        return {"aligned": total == 0, "half_success_rows": total, "places": places,
                "checked_batches": len(batches),
                "successful_batches": sum(1 for b in batches if b["status"] == "成功")}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

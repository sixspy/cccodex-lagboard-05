import threading

import pytest
from fastapi.testclient import TestClient

from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "batch.db")
    init_db()
    yield


def _auth(c):
    tok = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _row(source, sc="2026-11-18T10:00:00", ic="2026-11-18T10:05:00"):
    return {"source": source, "source_clock": sc, "ingest_clock": ic}


def _submit(c, h, batch_no, rows, path="甲", declared=None, day=None):
    body = {"batch_no": batch_no, "path": path, "rows": rows}
    if declared is not None:
        body["declared_rows"] = declared
    if day is not None:
        body["day"] = day
    return c.post("/api/batch/submit", headers=h, json=body)


def _arrivals(c, h):
    return c.get("/api/arrivals", headers=h).json()["items"]


def test_path_a_bad_row_rejects_both_sides_zero_new():
    c = TestClient(app)
    h = _auth(c)
    before = len(_arrivals(c, h))
    modes = {s["name"]: s["output_mode"] for s in c.get("/api/sources", headers=h).json()["items"]}

    r = _submit(c, h, "B-A-BAD", [_row("订单源"), _row("不存在的源")], path="甲")
    assert r.status_code == 409
    assert len(_arrivals(c, h)) == before
    assert {s["name"]: s["output_mode"] for s in c.get("/api/sources", headers=h).json()["items"]} == modes

    q = c.get("/api/batch/queue", headers=h).json()
    item = next(x for x in q["items"] if x["batch_no"] == "B-A-BAD")
    assert item["effective_mechanism"] == "外键同批号"
    assert item["status"] == "失败"

    led = next(x for x in c.get("/api/batch/ledger", headers=h).json()["items"] if x["batch_no"] == "B-A-BAD")
    assert led["verdict"] == "同败" and led["formal_added"] == 0

    audit = c.get("/api/batch/audit", headers=h).json()
    assert audit["aligned"] is True and audit["half_success_rows"] == 0


def test_audit_turns_red_on_leftover_shadow_row():
    c = TestClient(app)
    h = _auth(c)
    r = _submit(c, h, "B-SH-BAD", [_row("订单源"), _row("不存在的源")], path="乙", day="2030-01-09")
    assert r.status_code == 409
    assert c.get("/api/batch/shadow", headers=h, params={"batch_no": "B-SH-BAD"}).json()["items"] == []

    # 模拟半成功残留：影子批表没清干净，对账必须转红
    import sqlite3
    from app import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO batch_shadow(batch_no,seq,source,source_clock,ingest_clock) VALUES (?,?,?,?,?)",
                 ("B-SH-BAD", 1, "订单源", "2030-01-09T10:00:00", "2030-01-09T10:01:00"))
    conn.commit()
    conn.close()

    audit = c.get("/api/batch/audit", headers=h).json()
    assert audit["aligned"] is False
    assert audit["places"]["arrivals"]["half_success_rows"] >= 1


def test_audit_turns_red_on_planted_half_success_row():
    c = TestClient(app)
    h = _auth(c)
    r = _submit(c, h, "B-AUDIT-BAD", [_row("订单源"), _row("不存在的源")], path="甲", day="2030-01-07")
    assert r.status_code == 409
    assert c.get("/api/batch/audit", headers=h).json()["aligned"] is True

    # 绕过写路径直接塞一行正式落账（模拟撕裂残留），对账必须转红
    import sqlite3
    from app import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level,batch_no) VALUES (?,?,?,?,?,?)",
                 ("订单源", "2030-01-07T10:00:00", "2030-01-07T10:01:00", 1, "正常", "B-AUDIT-BAD"))
    conn.commit()
    conn.close()

    audit = c.get("/api/batch/audit", headers=h).json()
    assert audit["aligned"] is False
    assert audit["half_success_rows"] >= 1
    assert audit["places"]["arrivals"]["half_success_rows"] >= 1
    assert audit["places"]["ledger"]["half_success_rows"] >= 1


def test_empty_batch_rejected():
    c = TestClient(app)
    h = _auth(c)
    before = len(_arrivals(c, h))
    r = _submit(c, h, "B-EMPTY", [], path="甲", day="2030-01-08")
    assert r.status_code == 400
    assert len(_arrivals(c, h)) == before
    led = c.get("/api/batch/ledger", headers=h).json()["items"]
    assert not any(x["batch_no"] == "B-EMPTY" for x in led)


def test_single_row_arrival_cannot_pretend_to_be_batch():
    c = TestClient(app)
    h = _auth(c)
    before = len(_arrivals(c, h))
    # 旧写法：把同一补采批逐行丢给单行入口。第一行成功、第二行失败即为半成功撕裂。
    body = {"source": "订单源", "source_clock": "2026-11-18T10:00:00",
            "ingest_clock": "2026-11-18T10:05:00", "batch_no": "B-TEAR"}
    r = c.post("/api/arrivals", headers=h, json=body)
    assert r.status_code == 409
    assert "同败同成" in r.text
    assert len(_arrivals(c, h)) == before


def test_path_a_missing_declared_row_rejects_whole_batch():
    c = TestClient(app)
    h = _auth(c)
    before = len(_arrivals(c, h))
    r = _submit(c, h, "B-A-MISS", [_row("订单源"), _row("支付源")], path="甲", declared=3)
    assert r.status_code == 409
    assert "缺行" in r.text
    assert len(_arrivals(c, h)) == before
    led = next(x for x in c.get("/api/batch/ledger", headers=h).json()["items"] if x["batch_no"] == "B-A-MISS")
    assert led["verdict"] == "同败" and led["formal_added"] == 0


def test_path_b_half_success_clears_shadow_and_formal_zero():
    c = TestClient(app)
    h = _auth(c)
    before = len(_arrivals(c, h))

    r = _submit(c, h, "B-B-BAD", [_row("订单源"), _row("不存在的源")], path="乙", day="2030-01-01")
    assert r.status_code == 409
    assert len(_arrivals(c, h)) == before
    shadow = c.get("/api/batch/shadow", headers=h, params={"batch_no": "B-B-BAD"}).json()["items"]
    assert shadow == []

    led = next(x for x in c.get("/api/batch/ledger", headers=h).json()["items"] if x["batch_no"] == "B-B-BAD")
    assert led["verdict"] == "同败"
    assert led["formal_added"] == 0
    assert led["shadow_cleared"] == 2

    # 半成功后重试同一影子批：只记争用，不得重复晋升
    r2 = _submit(c, h, "B-B-BAD", [_row("订单源"), _row("支付源")], path="乙", day="2030-01-01")
    assert r2.status_code == 409
    assert "批量补采争用" in r2.text
    assert len(_arrivals(c, h)) == before


def test_duplicate_shadow_promotion_blocked_with_contention_trace():
    c = TestClient(app)
    h = _auth(c)
    before = len(_arrivals(c, h))
    r = _submit(c, h, "B-B-OK", [_row("订单源"), _row("支付源")], path="乙", day="2030-01-02")
    assert r.status_code == 200
    after = _arrivals(c, h)
    assert len(after) == before + 2
    assert {x["batch_no"] for x in after[:2]} == {"B-B-OK"}

    r2 = _submit(c, h, "B-B-OK", [_row("订单源"), _row("支付源")], path="乙", day="2030-01-02")
    assert r2.status_code == 409
    assert "批量补采争用" in r2.text
    assert len(_arrivals(c, h)) == before + 2
    trace = c.get("/api/batch/contention", headers=h).json()["items"]
    assert any(x["batch_no"] == "B-B-OK" for x in trace)


def test_dual_dispatch_same_batch_takes_lock_and_records_contention():
    c = TestClient(app)
    h = _auth(c)
    before = len(_arrivals(c, h))
    results = []
    barrier = threading.Barrier(2)

    def worker():
        cc = TestClient(app)
        hh = _auth(cc)
        barrier.wait()
        rr = _submit(cc, hh, "B-LOCK", [_row("订单源"), _row("物流源")], path="甲", day="2030-01-03")
        results.append(rr.status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 409], results
    assert len(_arrivals(c, h)) == before + 2
    trace = c.get("/api/batch/contention", headers=h).json()["items"]
    assert any(x["batch_no"] == "B-LOCK" for x in trace)


def test_day_register_locks_path_and_deviation_recorded():
    c = TestClient(app)
    h = _auth(c)
    assert _submit(c, h, "B-DAY-1", [_row("订单源")], path="甲", day="2030-01-04").status_code == 200
    before = len(_arrivals(c, h))
    r = _submit(c, h, "B-DAY-2", [_row("支付源")], path="乙", day="2030-01-04")
    assert r.status_code == 409
    assert "批量偏了" in r.text
    assert len(_arrivals(c, h)) == before
    reg = c.get("/api/batch/register", headers=h, params={"day": "2030-01-04"}).json()
    assert reg["path"] == "甲" and reg["sealed_at"]
    dev = c.get("/api/batch/deviation", headers=h).json()["items"]
    assert any("批量偏了" in x["detail"] for x in dev)


def test_five_places_aligned_and_queue_marks_effective_mechanism():
    c = TestClient(app)
    h = _auth(c)
    r = _submit(c, h, "B-FIVE-A", [_row("订单源"), _row("支付源")], path="甲", day="2030-01-05")
    assert r.status_code == 200

    q = c.get("/api/batch/queue", headers=h).json()
    item = next(x for x in q["items"] if x["batch_no"] == "B-FIVE-A")
    assert item["effective_mechanism"] == "外键同批号"
    assert item["same_fate"] == "同成"
    assert q["mechanisms"]["外键同批号"] == "生效"
    assert q["mechanisms"]["旁路影子批表"] == "未启用"

    arr = _arrivals(c, h)
    assert sum(1 for x in arr if x.get("batch_no") == "B-FIVE-A") == 2
    srcs = {s["name"]: s["output_mode"] for s in c.get("/api/sources", headers=h).json()["items"]}
    assert srcs["订单源"] == "可写" and srcs["支付源"] == "可写"

    wm = {x["source"]: x for x in c.get("/api/watermarks", headers=h).json()["items"]}
    assert wm["订单源"]["rows"] == sum(1 for x in arr if x["source"] == "订单源")
    assert wm["支付源"]["rows"] == sum(1 for x in arr if x["source"] == "支付源")
    assert wm["订单源"]["batch_no"] == "B-FIVE-A"

    led = next(x for x in c.get("/api/batch/ledger", headers=h).json()["items"] if x["batch_no"] == "B-FIVE-A")
    assert led["verdict"] == "同成" and led["formal_added"] == 2

    audit = c.get("/api/batch/audit", headers=h).json()
    assert audit["aligned"] is True
    assert audit["half_success_rows"] == 0
    for place in ("catchup_queue", "arrivals", "sources", "watermarks", "ledger"):
        assert place in audit["places"]
        assert audit["places"][place]["half_success_rows"] == 0

    # 另一天走乙路，队列须改标旁路影子批表生效
    assert _submit(c, h, "B-FIVE-B", [_row("物流源")], path="乙", day="2030-01-06").status_code == 200
    q2 = c.get("/api/batch/queue", headers=h, params={"day": "2030-01-06"}).json()
    item2 = next(x for x in q2["items"] if x["batch_no"] == "B-FIVE-B")
    assert item2["effective_mechanism"] == "旁路影子批表"
    assert q2["mechanisms"]["外键同批号"] == "未启用"
    assert q2["mechanisms"]["旁路影子批表"] == "生效"

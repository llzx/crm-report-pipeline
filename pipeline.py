#!/usr/bin/env python3
"""
GHL -> SQLite -> LLM -> 报告 自动化管道（最小可用版）

设计目标（对应客户需求）：
  1. 通过 API 把 CRM 数据抽到数据库        -> fetch() + upsert()
  2. 定时运行、无需人工                    -> --loop 或 cron / n8n 调用
  3. LLM 处理：打标签、汇总、找趋势         -> analyze()
  4. 自动更新看板 + 周报                    -> build_report() / build_dashboard()
  5. 自动质检：去重、补缺失、异常告警        -> validate()

数据源优先级：
  a) 环境变量 GHL_TOKEN + GHL_LOCATION_ID 存在 -> 走 GoHighLevel v2 API
  b) data/export.json 存在                    -> 读本地导出
  c) 都没有                                    -> 生成带"脏数据"的合成样本（用于演示）

用法：
  python pipeline.py                # 跑一次
  python pipeline.py --loop 3600    # 每小时跑一次
  python pipeline.py --reset        # 清空数据库重来
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

try:                                   # Windows 控制台默认 GBK，中文会乱码
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "ghl.db")
OUT_DIR = os.path.join(HERE, "out")
EXPORT_PATH = os.path.join(HERE, "data", "export.json")

STAGES = ["New Lead", "Contacted", "Appointment Set", "Showed", "Proposal", "Won", "Lost"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id          TEXT PRIMARY KEY,
    first_name  TEXT,
    last_name   TEXT,
    email       TEXT,
    phone       TEXT,
    tags        TEXT,
    source      TEXT,
    created_at  TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS opportunities (
    id           TEXT PRIMARY KEY,
    contact_id   TEXT,
    name         TEXT,
    stage        TEXT,
    monetary_value REAL,
    created_at   TEXT,
    updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS run_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT,
    finished_at  TEXT,
    source       TEXT,
    contacts_seen INTEGER,
    contacts_new  INTEGER,
    opps_seen     INTEGER,
    opps_new      INTEGER,
    dup_removed   INTEGER,
    fields_fixed  INTEGER,
    anomalies     INTEGER,
    notes         TEXT
);
"""


# ---------------------------------------------------------------- fetch layer

def _ghl_get(path: str, params: dict, token: str, location_id: str,
             version: str = "2021-07-28", timeout: int = 30) -> dict:
    """GHL v2 的单次请求。分页由调用方处理。

    注意：endpoint 与 Header 以当前官方文档为准，这里是标准形态。
    """
    q = urllib.parse.urlencode({**params, "locationId": location_id})
    url = f"https://services.leadconnectorhq.com/{path}?{q}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Version": version,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_from_ghl() -> tuple[list[dict], list[dict], str]:
    """游标分页拉取 contacts / opportunities。"""
    token = os.environ["GHL_TOKEN"]
    loc = os.environ["GHL_LOCATION_ID"]
    contacts: list[dict] = []
    opps: list[dict] = []

    cursor = None
    for _ in range(200):                      # 硬上限，防止分页死循环
        params = {"limit": 100}
        if cursor:
            params["startAfterId"] = cursor
        page = _ghl_get("contacts/", params, token, loc)
        batch = page.get("contacts", [])
        contacts.extend(batch)
        cursor = (page.get("meta") or {}).get("startAfterId")
        if not batch or not cursor:
            break

    cursor = None
    for _ in range(200):
        params = {"limit": 100}
        if cursor:
            params["startAfterId"] = cursor
        page = _ghl_get("opportunities/", params, token, loc)
        batch = page.get("opportunities", [])
        opps.extend(batch)
        cursor = (page.get("meta") or {}).get("startAfterId")
        if not batch or not cursor:
            break

    return contacts, opps, "ghl-api"


def fetch_from_export() -> tuple[list[dict], list[dict], str]:
    with open(EXPORT_PATH, encoding="utf-8") as f:
        d = json.load(f)
    return d.get("contacts", []), d.get("opportunities", []), "local-export"


def fetch_synthetic() -> tuple[list[dict], list[dict], str]:
    """生成一批故意带脏数据的合成 CRM 数据。

    脏数据的种类就是 validate() 要处理的种类：
      - 邮箱/电话重复（大小写、空格不一致）
      - 缺 email / 缺 phone / 缺 stage
      - 未来日期、以及 1970 年那种坏时间戳
      - 金额异常（0 或极大值）
    """
    rng = random.Random(20261026)
    first = ["James", "Olivia", "Noah", "Emma", "Liam", "Ava", "Mateo", "Sofia",
             "Ethan", "Mia", "Lucas", "Amelia", "Kai", "Chloe", "Arjun", "Zara"]
    last = ["Smith", "Johnson", "Brown", "Garcia", "Nguyen", "Patel", "Kim",
            "Wilson", "Taylor", "Chen", "Ahmed", "Rossi"]
    sources = ["facebook", "instagram", "google", "referral", "website"]

    today = datetime.now()
    contacts: list[dict] = []
    for i in range(140):
        cid = f"c{i:04d}"
        fn, ln = rng.choice(first), rng.choice(last)
        email = f"{fn.lower()}.{ln.lower()}{i}@example.com"
        phone = f"+1{rng.randint(2000000000, 9999999999)}"
        created = today - timedelta(days=rng.randint(0, 120), hours=rng.randint(0, 23))
        c = {
            "id": cid,
            "firstName": fn,
            "lastName": ln,
            "email": email,
            "phone": phone,
            "tags": rng.sample(["new", "warm", "hot", "cold", "vip"], rng.randint(0, 2)),
            "source": rng.choice(sources),
            "dateAdded": created.isoformat(timespec="seconds"),
        }
        contacts.append(c)

    # 故意制造的脏数据
    for k in range(14):                                    # 重复（邮箱大小写/空格差异）
        src = contacts[rng.randrange(len(contacts))].copy()
        src["id"] = f"dup{k:03d}"
        if src["email"]:
            src["email"] = src["email"].upper() + " "
        contacts.append(src)
    for k in range(9):                                     # 缺字段
        c = contacts[rng.randrange(len(contacts))].copy()
        c["id"] = f"miss{k:03d}"
        rng.choice(["email", "phone"]) and c.__setitem__(rng.choice(["email", "phone"]), None)
        contacts.append(c)
    for k in range(3):                                     # 坏日期
        c = contacts[rng.randrange(len(contacts))].copy()
        c["id"] = f"baddate{k:03d}"
        c["dateAdded"] = rng.choice(["1970-01-01T00:00:00", "2099-12-31T12:00:00", ""])
        contacts.append(c)

    opps: list[dict] = []
    for i in range(70):
        c = contacts[rng.randrange(len(contacts))]
        stage = rng.choice(STAGES[:6]) if rng.random() > 0.12 else None
        value = float(rng.choice([0, 0, 1500, 2500, 4800, 7500, 12000, 250000]))
        created = today - timedelta(days=rng.randint(0, 90))
        opps.append({
            "id": f"o{i:04d}",
            "contactId": c["id"],
            "name": f"{stage or 'Deal'} - {c.get('firstName','')}",
            "stage": stage,
            "monetaryValue": value,
            "dateAdded": created.isoformat(timespec="seconds"),
        })

    return contacts, opps, "synthetic"


# ---------------------------------------------------------------- normalize

def _norm_email(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip().lower()
    return v or None


def _norm_phone(v: str | None) -> str | None:
    if not v:
        return None
    digits = re.sub(r"\D", "", v)
    return digits or None


def _norm_date(v: str | None) -> str | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None
    now = datetime.now()
    if dt.year < 2000 or dt > now + timedelta(days=1):
        return None                                   # 坏时间戳直接判为缺失
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------- validate

def validate(contacts: list[dict], opps: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """去重 + 补缺失 + 记录异常。返回清洗后的数据和质检统计。"""
    stats = {"dup_removed": 0, "fields_fixed": 0, "anomalies": []}

    clean: list[dict] = []
    seen_email: dict[str, str] = {}
    seen_phone: dict[str, str] = {}

    for c in contacts:
        row = dict(c)
        row["email"] = _norm_email(row.get("email"))
        row["phone"] = _norm_phone(row.get("phone"))
        row["dateAdded"] = _norm_date(row.get("dateAdded"))

        for f in ("email", "phone", "dateAdded"):
            if row.get(f) is None and c.get(f) not in (None, ""):
                stats["fields_fixed"] += 1            # 有值但格式坏 -> 清成空
            elif row.get(f) is None:
                stats["fields_fixed"] += 1

        # 去重：邮箱优先，其次电话
        key = row["email"] or f"tel:{row['phone']}"
        if row["email"] and row["email"] in seen_email:
            stats["dup_removed"] += 1
            continue
        if not row["email"] and row["phone"] and row["phone"] in seen_phone:
            stats["dup_removed"] += 1
            continue
        if row["email"]:
            seen_email[row["email"]] = row["id"]
        if row["phone"]:
            seen_phone[row["phone"]] = row["id"]

        clean.append(row)

    clean_ids = {c["id"] for c in clean}
    clean_opps: list[dict] = []
    for o in opps:
        row = dict(o)
        row["stage"] = (row.get("stage") or "").strip() or None
        row["dateAdded"] = _norm_date(row.get("dateAdded"))
        v = row.get("monetaryValue")
        if v is None:
            row["monetaryValue"] = 0.0
        elif v > 100000:
            stats["anomalies"].append(
                f"机会 {row['id']} 金额异常 ${v:,.0f}（疑似单位错误或测试数据）")
        if row.get("stage") is None:
            stats["anomalies"].append(f"机会 {row['id']} 缺 stage，已归入 'Unknown'")
            row["stage"] = "Unknown"
        if row.get("contactId") not in clean_ids:
            stats["anomalies"].append(f"机会 {row['id']} 指向的 contact 不存在（孤儿记录）")
            continue
        clean_opps.append(row)

    return clean, clean_opps, stats


# ---------------------------------------------------------------- upsert

def upsert(conn: sqlite3.Connection, contacts: list[dict], opps: list[dict]) -> tuple[int, int]:
    new_c = new_o = 0
    cur = conn.cursor()
    for c in contacts:
        cur.execute("SELECT 1 FROM contacts WHERE id=?", (c["id"],))
        if cur.fetchone() is None:
            new_c += 1
        cur.execute(
            """INSERT INTO contacts (id,first_name,last_name,email,phone,tags,source,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 first_name=excluded.first_name, last_name=excluded.last_name,
                 email=excluded.email, phone=excluded.phone, tags=excluded.tags,
                 source=excluded.source, updated_at=excluded.updated_at""",
            (c["id"], c.get("firstName"), c.get("lastName"), c.get("email"),
             c.get("phone"), ",".join(c.get("tags") or []) if isinstance(c.get("tags"), list) else (c.get("tags") or ""),
             c.get("source"), c.get("dateAdded"), datetime.now().isoformat(timespec="seconds")))

    for o in opps:
        cur.execute("SELECT 1 FROM opportunities WHERE id=?", (o["id"],))
        if cur.fetchone() is None:
            new_o += 1
        cur.execute(
            """INSERT INTO opportunities (id,contact_id,name,stage,monetary_value,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 stage=excluded.stage, monetary_value=excluded.monetary_value,
                 updated_at=excluded.updated_at""",
            (o["id"], o.get("contactId"), o.get("name"), o.get("stage"),
             float(o.get("monetaryValue") or 0), o.get("dateAdded"),
             datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return new_c, new_o


# ---------------------------------------------------------------- LLM layer

def analyze(conn: sqlite3.Connection, api_key: str | None) -> dict:
    """对近期机会打标签 + 生成叙事摘要。

    有 ANTHROPIC_API_KEY 就走真模型；没有就走确定性规则，
    保证管道在没有 key 的情况下也能跑通（CI / 初次演示）。
    """
    cur = conn.cursor()
    cur.execute("""SELECT o.id, o.name, o.stage, o.monetary_value, c.first_name, c.source
                   FROM opportunities o LEFT JOIN contacts c ON c.id = o.contact_id
                   WHERE o.stage NOT IN ('Won','Lost')""")
    open_opps = cur.fetchall()

    if api_key:
        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 500,
            "messages": [{
                "role": "user",
                "content": (
                    "下面是 CRM 里未关闭的商机列表（id, 名称, 阶段, 金额, 联系人, 来源）。"
                    "请输出 JSON：{\"tags\": {id: [标签...]}, \"summary\": \"三句话以内的业务观察\"}。"
                    "标签从 [high_value, stalled, follow_up_needed, price_sensitive, fast_mover] 里选。"
                    f"\n\n{json.dumps(open_opps, ensure_ascii=False)[:12000]}"
                ),
            }],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in resp.get("content", []))
        try:
            m = re.search(r"\{.*\}", text, re.S)
            parsed = json.loads(m.group(0))
            return {"backend": "claude", "tags": parsed.get("tags", {}),
                    "summary": parsed.get("summary", "")}
        except Exception:
            return {"backend": "claude(parse-failed)", "tags": {}, "summary": text[:300]}

    # 规则回退：让管道在无 key 时依然可用
    tags: dict[str, list[str]] = {}
    for oid, name, stage, value, fname, source in open_opps:
        t = []
        if (value or 0) >= 7500:
            t.append("high_value")
        if stage in ("New Lead", "Contacted"):
            t.append("follow_up_needed")
        if stage == "Proposal":
            t.append("fast_mover")
        tags[oid] = t or ["stalled"]
    return {"backend": "rules", "tags": tags,
            "summary": f"{len(open_opps)} 个未关闭商机；"
                       f"{sum(1 for o in open_opps if (o[3] or 0) >= 7500)} 个高价值。"}


# ---------------------------------------------------------------- report

def kpis(conn: sqlite3.Connection) -> dict:
    cur = conn.cursor()
    q = lambda s, a=(): cur.execute(s, a).fetchone()[0]
    total_c = q("SELECT COUNT(*) FROM contacts")
    week_ago = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    new_c = q("SELECT COUNT(*) FROM contacts WHERE created_at >= ?", (week_ago,))
    # 金额异常（>10 万）不计入管道总额，单独统计，避免被测试数据污染
    open_value = q("SELECT COALESCE(SUM(monetary_value),0) FROM opportunities "
                   "WHERE stage NOT IN ('Won','Lost') AND monetary_value <= 100000")
    excluded = q("SELECT COALESCE(SUM(monetary_value),0) FROM opportunities "
                 "WHERE stage NOT IN ('Won','Lost') AND monetary_value > 100000")
    won = q("SELECT COUNT(*) FROM opportunities WHERE stage='Won'")
    lost = q("SELECT COUNT(*) FROM opportunities WHERE stage='Lost'")
    by_stage = dict(cur.execute(
        "SELECT stage, COUNT(*) FROM opportunities GROUP BY stage ORDER BY 2 DESC").fetchall())
    return {"contacts": total_c, "new_7d": new_c, "open_value": open_value, "excluded_value": excluded,
            "won": won, "lost": lost, "by_stage": by_stage}


def build_report(conn, k: dict, ai: dict, stats: dict, source: str) -> str:
    today = date.today().isoformat()
    lines = [
        f"# CRM 自动化日报 · {today}",
        "",
        f"- 数据源：`{source}`",
        f"- LLM 后端：`{ai['backend']}`",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 关键指标",
        "",
        f"| 指标 | 数值 |",
        f"|---|---|",
        f"| 联系人总数 | {k['contacts']} |",
        f"| 近 7 天新增 | {k['new_7d']} |",
        f"| 未关闭商机金额 | ${k['open_value']:,.0f} |",
        f"| 其中已排除的异常金额 | ${k.get('excluded_value',0):,.0f} |",
        f"| 成交 / 流失 | {k['won']} / {k['lost']} |",
        "",
        "## 管道分布",
        "",
        "| 阶段 | 数量 |",
        "|---|---|",
    ]
    for st, n in k["by_stage"].items():
        lines.append(f"| {st or '(空)'} | {n} |")
    lines += [
        "",
        "## 数据质检",
        "",
        f"- 去重删除：**{stats['dup_removed']}** 条",
        f"- 字段修复：**{stats['fields_fixed']}** 处",
        f"- 异常发现：**{len(stats['anomalies'])}** 条",
        "",
    ]
    if stats["anomalies"]:
        for a in stats["anomalies"][:10]:
            lines.append(f"  - ⚠️ {a}")
        if len(stats["anomalies"]) > 10:
            lines.append(f"  - …另有 {len(stats['anomalies']) - 10} 条")
    lines += ["", "## AI 观察", "", ai["summary"], ""]

    high = [oid for oid, t in ai["tags"].items() if "high_value" in t]
    if high:
        lines += ["## 需要跟进的商机", ""]
        cur = conn.cursor()
        for oid in high[:8]:
            cur.execute("""SELECT o.name, o.stage, o.monetary_value FROM opportunities o
                           WHERE o.id=?""", (oid,))
            r = cur.fetchone()
            if r:
                lines.append(f"- **{r[0]}** · {r[1]} · ${r[2]:,.0f}")
        lines.append("")
    return "\n".join(lines)


def build_dashboard(k: dict, ai: dict, path: str) -> None:
    stages = list(k["by_stage"].items())
    mx = max([n for _, n in stages] + [1])
    bars = []
    y = 40
    for st, n in stages:
        w = int(380 * n / mx)
        bars.append(
            f'<text x="10" y="{y+13}" font-size="13" fill="#8b93a1">{st or "(空)"}</text>'
            f'<rect x="150" y="{y}" width="{w}" height="18" rx="4" fill="#3ba55d"/>'
            f'<text x="{150+w+8}" y="{y+13}" font-size="13" fill="#e8ecf1">{n}</text>')
        y += 30
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>CRM Dashboard</title>
<style>
body{{margin:0;background:#0e1014;color:#e8ecf1;font-family:-apple-system,"Segoe UI",sans-serif;padding:32px}}
h1{{font-size:20px;color:#7d8794;font-weight:600;margin:0 0 24px}}
.card{{background:#161a20;border:1px solid #232830;border-radius:12px;padding:20px 24px;margin-bottom:16px}}
.kpis{{display:flex;gap:16px;flex-wrap:wrap}}
.kpi{{flex:1;min-width:160px;background:#161a20;border:1px solid #232830;border-radius:12px;padding:18px}}
.kpi .v{{font-size:30px;font-weight:700;color:#6fd08c}}
.kpi .l{{font-size:13px;color:#7d8794;margin-top:6px}}
</style></head><body>
<h1>CRM AUTOMATED REPORT · {date.today().isoformat()} · backend: {ai['backend']}</h1>
<div class="kpis">
  <div class="kpi"><div class="v">{k['contacts']}</div><div class="l">contacts</div></div>
  <div class="kpi"><div class="v">{k['new_7d']}</div><div class="l">new in 7 days</div></div>
  <div class="kpi"><div class="v">${k['open_value']:,.0f}</div><div class="l">open pipeline</div></div>
  <div class="kpi"><div class="v">{k['won']}/{k['lost']}</div><div class="l">won / lost</div></div>
</div>
<div class="card"><svg width="620" height="{max(120, y+10)}">{''.join(bars)}</svg></div>
<div class="card"><b>AI summary</b><p style="color:#aab2be;line-height:1.6">{ai['summary']}</p></div>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# ---------------------------------------------------------------- main

def run_once() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    started = datetime.now()
    if os.environ.get("GHL_TOKEN") and os.environ.get("GHL_LOCATION_ID"):
        contacts, opps, source = fetch_from_ghl()
    elif os.path.exists(EXPORT_PATH):
        contacts, opps, source = fetch_from_export()
    else:
        contacts, opps, source = fetch_synthetic()

    clean_c, clean_o, stats = validate(contacts, opps)

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    new_c, new_o = upsert(conn, clean_c, clean_o)

    ai = analyze(conn, os.environ.get("ANTHROPIC_API_KEY"))
    k = kpis(conn)

    report = build_report(conn, k, ai, stats, source)
    rp = os.path.join(OUT_DIR, f"report_{date.today().isoformat()}.md")
    with open(rp, "w", encoding="utf-8") as f:
        f.write(report)
    dp = os.path.join(OUT_DIR, "dashboard.html")
    build_dashboard(k, ai, dp)

    conn.execute(
        """INSERT INTO run_log (started_at,finished_at,source,contacts_seen,contacts_new,
           opps_seen,opps_new,dup_removed,fields_fixed,anomalies,notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (started.isoformat(timespec="seconds"), datetime.now().isoformat(timespec="seconds"),
         source, len(contacts), new_c, len(opps), new_o,
         stats["dup_removed"], stats["fields_fixed"], len(stats["anomalies"]),
         f"backend={ai['backend']}"))
    conn.commit()

    print(f"source        : {source}")
    print(f"contacts      : {len(contacts)} 条 -> 新增 {new_c}，去重删除 {stats['dup_removed']}")
    print(f"opportunities : {len(opps)} 条 -> 新增 {new_o}")
    print(f"字段修复      : {stats['fields_fixed']}   异常 {len(stats['anomalies'])}")
    print(f"LLM backend   : {ai['backend']}")
    print(f"report        : {rp}")
    print(f"dashboard     : {dp}")
    conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="每 N 秒跑一次")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    if a.reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print("database reset")
    if a.loop:
        while True:
            run_once()
            print(f"--- sleeping {a.loop}s ---\n")
            time.sleep(a.loop)
    return run_once()


if __name__ == "__main__":
    sys.exit(main())

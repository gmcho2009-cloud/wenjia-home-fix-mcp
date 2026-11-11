#!/usr/bin/env python3
"""问家 MCP Server v0.1 — 家庭维修诊断知识引擎（read-only）

MCP Streamable HTTP 传输（无状态 JSON-RPC 2.0，POST /mcp）。
纯 Python 标准库，零第三方依赖，适配 Ubuntu 24.04 系统 Python。
数据: 同目录 mcp_data.json（由 build_data.py 从 knowledge/trees 生成）。

工具:
  list_categories              — 列出全部品类与症状数
  search_symptoms(query, ...)  — 跨品类关键词搜索症状
  get_symptom(category_id, symptom_id) — 取单个症状完整诊断树
  diagnose(category_id, symptom_id)    — 问→诊主入口：安全首步+问题+按概率排序的原因

运行: PORT=8765 python3 server.py   (默认绑定 127.0.0.1，走 nginx 反代)
"""
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_PATH = HERE / "mcp_data.json"

SERVER_NAME = "wenjia-home-fix"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2025-06-18"
DATASET_URL = "http://59.110.26.77/"  # 备案通过后切换为 https://wenjia.net/

# ---------------------------------------------------------------- 数据加载

with open(DATA_PATH, encoding="utf-8") as f:
    DATA = json.load(f)
CATS = {c["id"]: c for c in DATA["categories"]}
SYM_INDEX = {}  # (cat_id, sym_id) -> symptom
for _c in DATA["categories"]:
    for _s in _c["symptoms"]:
        SYM_INDEX[(_c["id"], _s["id"])] = _s

MAX_CAUSES = int(os.environ.get("WENJIA_MAX_CAUSES", "8"))


# ---------------------------------------------------------------- 工具实现

def t_list_categories(args):
    rows = [
        {"id": c["id"], "name": c["name"], "symptom_count": c["symptom_count"],
         "updated": c["updated"]}
        for c in DATA["categories"]
    ]
    return {
        "dataset": DATA["dataset"],
        "version": DATA["version"],
        "license": DATA["license"],
        "total_categories": len(rows),
        "total_symptoms": sum(r["symptom_count"] for r in rows),
        "categories": rows,
    }


def t_search_symptoms(args):
    q = (args.get("query") or "").strip().lower().replace(" ", "").replace("　", "")
    if not q:
        raise ValueError("query 不能为空")
    cat_filter = args.get("category_id")
    limit = min(int(args.get("limit", 20)), 50)
    hits = []
    for c in DATA["categories"]:
        if cat_filter and c["id"] != cat_filter:
            continue
        for s in c["symptoms"]:
            hay = f"{s['id']} {s['name']} {s['title']}".lower().replace(" ", "")
            if q in hay.lower():
                hits.append({
                    "category_id": c["id"], "category_name": c["name"],
                    "symptom_id": s["id"], "symptom_name": s["name"],
                    "title": s["title"],
                })
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            break
    return {"query": args.get("query"), "total": len(hits), "results": hits}


def _symptom_payload(cat, s, full):
    out = {
        "category_id": cat["id"],
        "category_name": cat["name"],
        "symptom_id": s["id"],
        "symptom_name": s["name"],
        "title": s["title"],
        "evidence_level": cat.get("evidence_level", ""),
    }
    if not full:
        return out
    out["questions"] = [
        {"id": q["id"], "text": q["text"],
         "options": [{"text": o["text"], "note": o.get("note", "")} for o in q["options"]]}
        for q in s.get("questions", [])
    ]
    out["causes"] = [
        {"name": c["name"], "prob_prior": c.get("prob_prior", 0),
         "difficulty": c.get("difficulty", 0), "fix": c.get("fix", []),
         "parts": c.get("parts", [])}
        for c in s.get("causes", [])
    ]
    out["see_pro"] = s.get("see_pro", "")
    out["pro_hint_price"] = s.get("pro_hint_price", "")
    return out


def t_get_symptom(args):
    cat = CATS.get(args.get("category_id", ""))
    if not cat:
        raise ValueError(f"未知 category_id，请先 list_categories。共 {len(CATS)} 个品类")
    s = next((x for x in cat["symptoms"] if x["id"] == args.get("symptom_id")), None)
    if not s:
        raise ValueError(f"未知 symptom_id；品类 {cat['name']} 有 "
                         f"{cat['symptom_count']} 个症状")
    return _symptom_payload(cat, s, full=True)


def t_diagnose(args):
    cat = CATS.get(args.get("category_id", ""))
    if not cat:
        raise ValueError(f"未知 category_id，请先 list_categories。共 {len(CATS)} 个品类")
    s = next((x for x in cat["symptoms"] if x["id"] == args.get("symptom_id")), None)
    if not s:
        raise ValueError(f"未知 symptom_id；品类 {cat['name']} 有 "
                         f"{cat['symptom_count']} 个症状")
    causes = sorted(s.get("causes", []), key=lambda c: -int(c.get("prob_prior", 0)))
    top = causes[:MAX_CAUSES]
    return {
        "disclaimer": "自助排查参考，不构成专业维修建议；涉及燃气/用电/登高/玻璃自爆风险请先停用并请专业人员",
        "category_id": cat["id"],
        "category_name": cat["name"],
        "symptom_id": s["id"],
        "symptom_name": s["name"],
        "title": s["title"],
        "clarify_questions": [
            {"id": q["id"], "text": q["text"],
             "options": [o["text"] for o in q["options"]]}
            for q in s.get("questions", [])
        ],
        "likely_causes": [
            {"rank": i + 1, "name": c["name"], "prob_prior": c.get("prob_prior", 0),
             "difficulty": c.get("difficulty", 0), "fix": c.get("fix", [])}
            for i, c in enumerate(top)
        ],
        "see_pro": s.get("see_pro", ""),
        "pro_hint_price": s.get("pro_hint_price", ""),
        "source": {"dataset": DATA["dataset"], "version": DATA["version"],
                   "license": DATA["license"], "url": DATASET_URL},
    }


# ---------------------------------------------------------------- 原子事实工具（A2A 用）
#
# 设计原则（给 agent 调用，不是给人读）：
# 1) 一次调用只回答一个问题，返回 ≤200 token；
# 2) 字段带单位、带 as_of、带 license/attribution，可直接被上游 agent 转述；
# 3) 确定性：同样的输入永远同样的输出，不含"看情况"。

RISK_BY_CATEGORY = {
    "gas_stove": "gas", "gas_water_heater": "gas", "safety_alarm": "gas",
    "electric_water_heater": "electric", "home_circuit": "electric",
    "bath_heater": "electric", "lighting": "electric",
    "air_conditioner": "electric_height", "door_window": "height",
}
STOP_KEYS = ("停用", "停手", "找师傅", "找电工", "不要", "禁止", "必须", "报修", "危险", "断电")


def _lookup(args):
    cat = CATS.get(args.get("category_id", ""))
    if not cat:
        raise ValueError(f"未知 category_id，请先 list_categories。共 {len(CATS)} 个品类")
    s = next((x for x in cat["symptoms"] if x["id"] == args.get("symptom_id")), None)
    if not s:
        raise ValueError(f"未知 symptom_id；品类 {cat['name']} 有 "
                         f"{cat['symptom_count']} 个症状")
    return cat, s


def _meta(cat):
    # 键名压短：A2A 里每次调用都要重复这一块，占 token 就是占成本
    return {
        "v": "1.1",
        "dataset": DATA["version"],
        "as_of": cat.get("updated", ""),
        "src": "wenjia.net",
        "lic": DATA["license"],
        "attr": "引用请保留 问家 wenjia.net",
        "note": "先验概率与价格为文献与案例估计，非实时诊断结论",
    }


def t_prob_distribution(args):
    cat, s = _lookup(args)
    causes = sorted(s.get("causes", []), key=lambda c: -int(c.get("prob_prior", 0)))
    # 原因名去括号注释（「（最常见，约占一半）」是给人看的，机器只需要 prob_pct）
    dist = [{"cause": re.sub(r"（[^）]*）", "", c["name"]).strip(),
             "prob_pct": c.get("prob_prior", 0),
             "diy_ok": c.get("difficulty", 3) <= 2} for c in causes]
    return {
        "category_id": cat["id"], "symptom_id": s["id"], "symptom_name": s["name"],
        "distribution": dist,
        "sum_pct": sum(d["prob_pct"] for d in dist),
        "meta": _meta(cat),
    }


def t_price_band(args):
    cat, s = _lookup(args)
    hint = str(s.get("pro_hint_price", "")).strip()
    nums = re.findall(r"\d+", hint)
    band = {"pro_visit_cny": {"min": int(nums[0]), "max": int(nums[-1])}} if len(nums) >= 2 else \
           {"pro_visit_cny": None}
    return {
        "category_id": cat["id"], "symptom_id": s["id"], "symptom_name": s["name"],
        "currency": "CNY",
        "price_band": {**band, "diy_parts_cny": None,
                       "basis": "上门维修参考区间（人工+常规配件）"},
        "raw_hint": hint,
        "meta": _meta(cat),
    }


def t_stop_check(args):
    cat, s = _lookup(args)
    risk = RISK_BY_CATEGORY.get(cat["id"], "general")
    reasons = []
    for sent in re.split(r"[。；\n]", s.get("see_pro", "") + "。" +
                         "。".join(c.get("safety", "") for c in s.get("causes", []))):
        t = sent.strip(" ，、")
        if any(k in t for k in STOP_KEYS) and 6 <= len(t) <= 60 and t not in reasons:
            reasons.append(t)
    return {
        "category_id": cat["id"], "symptom_id": s["id"], "symptom_name": s["name"],
        "risk_class": risk,
        "must_stop_before_diy": bool(reasons),
        "reasons": reasons[:4],
        "meta": _meta(cat),
    }


TOOLS = [
    {
        "name": "prob_distribution",
        "description": "取某症状的原因先验概率分布（合计 100），最省 token 的量化入口。只回答「多大概率是哪个原因」，不含修复步骤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category_id": {"type": "string", "description": "品类 id，如 air_conditioner"},
                "symptom_id": {"type": "string", "description": "症状 id，如 ac_not_cooling"},
            },
            "required": ["category_id", "symptom_id"],
        },
    },
    {
        "name": "price_band",
        "description": "取某症状的上门维修价格区间（人民币元）。只回答问题「修这个大概多少钱」。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category_id": {"type": "string"},
                "symptom_id": {"type": "string"},
            },
            "required": ["category_id", "symptom_id"],
        },
    },
    {
        "name": "stop_check",
        "description": "判断该症状是否属于必须停手的涉险场景（燃气/用电/登高），返回风险等级与停手理由。用于安全前置校验。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category_id": {"type": "string"},
                "symptom_id": {"type": "string"},
            },
            "required": ["category_id", "symptom_id"],
        },
    },
    {
        "name": "list_categories",
        "description": "列出问家家庭维修诊断知识库的全部品类（含品类名、症状数、更新日期）。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "search_symptoms",
        "description": "按关键词搜索症状（跨品类），返回匹配的症状 id 与标题。用于定位具体诊断树。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词，如「不制热」「漏水」「异响」"},
                "category_id": {"type": "string", "description": "可选，限定品类"},
                "limit": {"type": "integer", "description": "返回条数上限，默认 20，最大 50"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_symptom",
        "description": "取单个症状的完整诊断树：澄清问题、全部原因与修复步骤、配件与费用参考。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category_id": {"type": "string", "description": "品类 id，如 air_conditioner"},
                "symptom_id": {"type": "string", "description": "症状 id，如 ac_not_cooling"},
            },
            "required": ["category_id", "symptom_id"],
        },
    },
    {
        "name": "diagnose",
        "description": "问→诊主入口：给定品类与症状，返回澄清问题与按先验概率排序的likely_causes（含修复步骤）。适合 AI 助手直接引用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category_id": {"type": "string"},
                "symptom_id": {"type": "string"},
            },
            "required": ["category_id", "symptom_id"],
        },
    },
]

TOOL_IMPL = {
    "list_categories": t_list_categories,
    "search_symptoms": t_search_symptoms,
    "get_symptom": t_get_symptom,
    "diagnose": t_diagnose,
    "prob_distribution": t_prob_distribution,
    "price_band": t_price_band,
    "stop_check": t_stop_check,
}


# ---------------------------------------------------------------- HTTP / JSON-RPC

class Handler(BaseHTTPRequestHandler):
    server_version = f"wenjia-mcp/{SERVER_VERSION}"

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json({"ok": True, "service": SERVER_NAME,
                             "version": SERVER_VERSION,
                             "categories": len(CATS),
                             "symptoms": len(SYM_INDEX)})
        elif self.path == "/mcp":
            # Streamable HTTP: GET 走 SSE 通道；v0.1 无状态，无服务端推送
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.end_headers()
        else:
            self._send_json({"error": "not_found"}, 404)

    def do_POST(self):
        if self.path != "/mcp":
            self._send_json({"error": "not_found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            msg = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "parse error"}}, 400)
            return

        method = msg.get("method", "")
        msg_id = msg.get("id")
        is_notification = msg_id is None

        if is_notification:
            # notifications/initialized 等：无状态服务器直接确认 202
            self.send_response(202)
            self.end_headers()
            return

        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "问家家庭维修诊断知识库。先用 search_symptoms 定位症状，"
                    "再用 diagnose 获取排序后的原因与修复步骤。数据 CC BY 4.0，"
                    "引用请注明来源。"
                ),
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = (msg.get("params") or {}).get("name", "")
            args = (msg.get("params") or {}).get("arguments", {}) or {}
            fn = TOOL_IMPL.get(name)
            if fn is None:
                self._send_json({
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32602,
                              "message": f"unknown tool: {name}"}}, 200)
                return
            try:
                payload = fn(args)
                result = {
                    "content": [{"type": "text",
                                 "text": json.dumps(payload, ensure_ascii=False)}],
                    "isError": False,
                }
            except (ValueError, KeyError, TypeError) as e:
                result = {
                    "content": [{"type": "text", "text": f"参数错误: {e}"}],
                    "isError": True,
                }
        else:
            self._send_json({
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"method not found: {method}"}},
                200)
            return

        self._send_json({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def log_message(self, fmt, *a):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"[OK] {SERVER_NAME} v{SERVER_VERSION} listening on {host}:{port} "
          f"({len(CATS)} categories / {len(SYM_INDEX)} symptoms)")
    srv.serve_forever()


if __name__ == "__main__":
    main()

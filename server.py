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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _resolve_data_path():
    """数据文件位置随分发形态不同：pip 包内同目录，public repo 里在 data/ 下。

    WENJIA_DATA 显式指定优先（容器/自定义部署用），否则按候选路径顺序找第一个存在的，
    都不存在时回落到同目录名，让报错信息指向最可能的位置而不是误导性的相对路径。
    """
    env = os.environ.get("WENJIA_DATA", "").strip()
    if env:
        return Path(env).expanduser()
    for cand in (HERE / "mcp_data.json", HERE / "data" / "mcp_data.json"):
        if cand.exists():
            return cand
    return HERE / "mcp_data.json"


DATA_PATH = _resolve_data_path()

SERVER_NAME = "wenjia-home-fix"
SERVER_VERSION = "0.2.3"
PROTOCOL_VERSION = "2025-06-18"
DATASET_URL = "http://59.110.26.77/"  # 备案通过后切换为 https://wenjia.net/
CALL_LOG = Path(os.environ.get("WENJIA_CALL_LOG", str(HERE / "calls.jsonl")))

# ---------------------------------------------------------------- 数据加载

with open(DATA_PATH, encoding="utf-8") as f:
    DATA = json.load(f)
CATS = {c["id"]: c for c in DATA["categories"]}
SYM_INDEX = {}  # (cat_id, sym_id) -> symptom
for _c in DATA["categories"]:
    for _s in _c["symptoms"]:
        SYM_INDEX[(_c["id"], _s["id"])] = _s

MAX_CAUSES = int(os.environ.get("WENJIA_MAX_CAUSES", "8"))


# ---------------------------------------------------------------- 调用埋点（A2A 证据）
# 目的：证明"是否真的被 agent 调用"。只记结构化字段，不记 query 原文、不记 IP 明文。

_UA_MAP = (
    ("smithery", "smithery"), ("mcp-inspector", "inspector"), ("inspector", "inspector"),
    ("modelcontextprotocol", "mcp-sdk"), ("langchain", "langchain"), ("llamaindex", "llamaindex"),
    ("claude", "claude"), ("anthropic", "claude"), ("cursor", "cursor"), ("vscode", "vscode"),
    ("windsurf", "windsurf"), ("cline", "cline"), ("zed", "zed"), ("chatgpt", "chatgpt"),
    ("gemini", "gemini"), ("doubao", "doubao"), ("coze", "coze"), ("dify", "dify"),
    ("openai", "openai-sdk"), ("httpx", "httpx"), ("aiohttp", "aiohttp"),
    ("urllib", "urllib"), ("powershell", "powershell"), ("go-http-client", "go-sdk"),
    ("okhttp", "okhttp"), ("java", "java-sdk"), ("postman", "postman"),
    ("axios", "node-sdk"), ("node", "node-sdk"), ("python", "python-sdk"),
    ("curl", "curl"),
)
_PRIVATE_PREFIX = ("10.", "127.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
                   "172.2", "172.30.", "172.31.", "169.254.")


def _client_tag(ctx):
    """把调用方压成一个短标签：ua 产品名 + 网络 /24。不保留原 IP。

    自测必须能单独摘出来，否则「有没有外部调用」这个唯一硬指标永远看不清。
    """
    ctx = ctx or {}
    if ctx.get("selftest"):
        return "selftest"
    ua = (ctx.get("ua") or "").lower()
    product = "other"
    for key, tag in _UA_MAP:
        if key in ua:
            product = tag
            break
    ip = ctx.get("ip") or ""
    if ip.count(".") != 3 or ip.startswith(_PRIVATE_PREFIX):
        net = "local"
    else:
        net = ".".join(ip.split(".")[:3])
    return f"{product}|{net}"


def _pick_ip(handler):
    """反代后面取真实来源 IP：优先 X-Real-IP，其次 X-Forwarded-For 第一段。"""
    for key in ("X-Real-IP", "X-Forwarded-For"):
        v = (handler.headers.get(key) or "").strip()
        if v:
            return v.split(",")[0].strip()
    return handler.client_address[0] if handler.client_address else ""


def _log_call(tool, ok, chars, args, ctx):
    rec = {
        "t": int(time.time()),
        "tool": tool,
        "ok": bool(ok),
        "chars": chars,
        "client": _client_tag(ctx),
        "via": (ctx or {}).get("via", "http"),
    }
    for k in ("category_id", "symptom_id"):
        v = args.get(k)
        if isinstance(v, str) and v:
            rec[k] = v[:40]
    # 只在失败时留 query：用来补别名、补症状，成功的调用不留原话
    if not ok and isinstance(args.get("query"), str) and args["query"]:
        rec["q"] = args["query"][:40]
        rec["miss"] = 1
    try:
        with open(CALL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass


def stats_payload(window_days=30):
    """从 calls.jsonl 现算：总量、按工具、按天、按客户端、热门症状。"""
    now = time.time()
    cutoff = now - window_days * 86400
    total = 0
    by_tool = {}
    by_day = {}
    by_client = {}
    by_symptom = {}
    err = 0
    if CALL_LOG.exists():
        size = CALL_LOG.stat().st_size
        if size < 200 * 1024 * 1024:  # 超过 200MB 不再全量扫描，避免拖垮服务
            with open(CALL_LOG, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("t", 0) < cutoff:
                        continue
                    total += 1
                    if not r.get("ok"):
                        err += 1
                    by_tool[r.get("tool", "?")] = by_tool.get(r.get("tool", "?"), 0) + 1
                    day = time.strftime("%Y-%m-%d", time.localtime(r.get("t", 0)))
                    by_day[day] = by_day.get(day, 0) + 1
                    by_client[r.get("client", "?")] = by_client.get(r.get("client", "?"), 0) + 1
                    sy = r.get("symptom_id")
                    if sy:
                        key = f"{r.get('category_id','?')}/{sy}"
                        by_symptom[key] = by_symptom.get(key, 0) + 1
    return {
        "ok": True,
        "service": SERVER_NAME,
        "version": SERVER_VERSION,
        "window_days": window_days,
        "calls": total,
        "errors": err,
        # distinct_clients 只数外部客户端：自测（X-Wenjia-SelfTest）不算，
        # 否则「做出来有没有人真调」这个唯一硬指标会被自家测试吃掉。
        "external_calls": sum(v for k, v in by_client.items() if k != "selftest"),
        "distinct_clients": len([k for k in by_client if k != "selftest"]),
        "selftest_calls": by_client.get("selftest", 0),
        "by_tool": by_tool,
        "by_day": dict(sorted(by_day.items())),
        "by_client": by_client,
        "top_symptoms": dict(sorted(by_symptom.items(), key=lambda kv: -kv[1])[:20]),
        "log_path": str(CALL_LOG),
    }


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
    q = args.get("query") or ""
    if not _norm(q):
        raise ValueError("query 不能为空，例如「不制冷」「漏水」「异响」，也可以直接给一句口语")
    cat_filter = args.get("category_id")
    if cat_filter:
        c = _find_cat(cat_filter)
        if not c:
            raise ValueError(f"没认出品类「{cat_filter}」")
        cat_filter = c["id"]
    limit = min(int(args.get("limit", 20)), 50)
    cand = _candidates(q, cat_filter=cat_filter, limit=limit)
    results = [{
        "category_id": c["id"], "category_name": c["name"],
        "symptom_id": s["id"], "symptom_name": s["name"],
        "title": s["title"], "score": sc,
    } for sc, c, s in cand]
    return {
        "query": q, "total": len(results), "results": results,
        "hint": ("结果按 score 从高到低排，最前面那条最可能是用户要的。"
                 "要方案用 fix_plan（传 category_id + symptom_id）；"
                 "只要概率/价格/停手用 prob_distribution / price_band / stop_check。"),
        "meta": {"dataset": DATA["version"], "total_symptoms": len(SYM_INDEX)},
    }


def t_find_symptom(args):
    """自然语言入口：用户原话直接进来，不用先猜 id。"""
    q = str(args.get("query") or "").strip()
    if not q:
        raise ValueError("query 不能为空，给一句人话即可，例如「空调不凉」「热水器打不着火」「马桶堵了」")
    limit = min(int(args.get("limit", 3)), 10)
    cand = _candidates(q, limit=limit)
    if not cand:
        cats = [{"category_id": c["id"], "name": c["name"], "symptoms": c["symptom_count"]}
                for c in DATA["categories"]]
        return {
            "query": q, "matched": False, "candidates": [],
            "note": "库里没有对上的症状。下面是可问的品类（共 %d 个），换个说法再来一次。" % len(cats),
            "categories": cats,
        }
    out = []
    for sc, c, s in cand:
        causes = sorted(s.get("causes", []), key=lambda x: -int(x.get("prob_prior", 0) or 0))
        top = causes[0] if causes else {}
        out.append({
            "category_id": c["id"], "category_name": c["name"],
            "symptom_id": s["id"], "symptom_name": s["name"],
            "title": s["title"], "score": sc,
            "top_cause": re.sub(r"（[^）]*）", "", top.get("name", "")).strip(),
            "top_prob_pct": top.get("prob_prior", 0),
            "see_pro": s.get("see_pro", ""),
        })
    return {
        "query": q, "matched": True, "total": len(out), "candidates": out,
        "next": "命中第一条就是主症状；要完整方案调 fix_plan，不要整棵树时用原子工具。",
        "meta": {"dataset": DATA["version"], "total_categories": len(CATS),
                 "total_symptoms": len(SYM_INDEX)},
    }


def t_get_category(args):
    c = _find_cat(str(args.get("category_id") or args.get("category") or args.get("query") or ""))
    if not c:
        raise ValueError("没认出品类；先用 list_categories 看 40 个品类的 id 与名称")
    return {
        "category_id": c["id"], "category_name": c["name"],
        "symptom_count": c["symptom_count"], "updated": c.get("updated", ""),
        "evidence_level": c.get("evidence_level", ""),
        "symptoms": [{"symptom_id": s["id"], "symptom_name": s["name"], "title": s["title"]}
                     for s in c["symptoms"]],
        "meta": _meta(c),
    }


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


_PUNCT = "　 \t\n-_/，。？！、：；·（）()\"'“”‘’？"


def _norm(x):
    x = str(x or "").lower()
    for ch in _PUNCT:
        x = x.replace(ch, "")
    return x


# 问句里的废话：不删掉会让「浴霸开着不热」对不上「浴霸不热」这种标准说法
_FILLERS = ("怎么回事", "为什么", "怎么办", "怎样", "如何", "越来越", "一坐就", "一翻身就",
            "开着", "开了", "按了", "关上", "老是", "总是", "一直", "突然", "天天", "每次",
            "特别", "有点", "好像", "感觉", "最近", "现在", "我家", "家里",
            "了", "的", "着", "很", "太", "就")


def _qnorm(x):
    x = _norm(x)
    for f in _FILLERS:
        x = x.replace(f, "")
    return x


# 口语别名：(说法, 品类, 症状)。只收高频、有歧义价值的说法，不追求穷举。
ALIASES = [
    ("空调不凉", "air_conditioner", "not_cooling"),
    ("空调不冷", "air_conditioner", "not_cooling"),
    ("空调不降温", "air_conditioner", "not_cooling"),
    ("空调制冷不行", "air_conditioner", "not_cooling"),
    ("空调滴水", "air_conditioner", "indoor_leaking"),
    ("空调漏水", "air_conditioner", "indoor_leaking"),
    ("空调开不了机", "air_conditioner", "no_power"),
    ("空调按了没反应", "air_conditioner", "no_power"),
    ("空调有味道", "air_conditioner", "noise_odor"),
    ("空调嗡嗡响", "air_conditioner", "noise_odor"),
    ("空调遥控没反应", "air_conditioner", "remote_fault_short_cycling"),
    ("冰箱不冷", "fridge", "not_cooling_light_on"),
    ("冰箱不凉", "fridge", "not_cooling_light_on"),
    ("冰箱结冰", "fridge", "heavy_frost"),
    ("冰箱结霜", "fridge", "heavy_frost"),
    ("冰箱嗡嗡响", "fridge", "noise_vibration"),
    ("冰箱积水", "fridge", "fridge_water_odor"),
    ("冰箱有异味", "fridge", "fridge_water_odor"),
    ("冰箱漏水", "fridge", "external_leaking"),
    ("洗衣机不脱水", "washing_machine", "no_spin"),
    ("洗衣机甩不干", "washing_machine", "no_spin"),
    ("洗衣机不排水", "washing_machine", "no_drain"),
    ("洗衣机排水慢", "washing_machine", "no_drain"),
    ("洗衣机漏水", "washing_machine", "leaking"),
    ("洗衣机噪音大", "washing_machine", "noise"),
    ("洗衣机不转", "washing_machine", "no_power"),
    ("热水器打不着火", "gas_water_heater", "no_ignition"),
    ("热水器点不着", "gas_water_heater", "no_ignition"),
    ("洗澡水忽冷忽热", "gas_water_heater", "unstable_temp"),
    ("水温不稳定", "gas_water_heater", "unstable_temp"),
    ("热水器水太小", "gas_water_heater", "low_flow"),
    ("电热水器不出热水", "electric_water_heater", "no_hot_water"),
    ("热水洗一半就凉", "electric_water_heater", "hot_water_runs_out"),
    ("热水器跳闸", "electric_water_heater", "tripping"),
    ("燃气灶打不着", "gas_stove", "no_ignite"),
    ("灶打不着火", "gas_stove", "no_ignite"),
    ("一个灶眼不着", "gas_stove", "one_burner_dead"),
    ("火苗发黄", "gas_stove", "flame_abnormal"),
    ("红火", "gas_stove", "flame_abnormal"),
    ("闻到燃气味", "gas_stove", "gas_smell"),
    ("松手就灭", "gas_stove", "auto_extinguish"),
    ("跳闸", "home_circuit", "breaker_repeated"),
    ("总闸跳了", "home_circuit", "breaker_repeated"),
    ("空开推不上", "home_circuit", "breaker_wont_reset"),
    ("插座没电", "home_circuit", "outlet_dead"),
    ("插座发烫", "home_circuit", "outlet_hot_smell"),
    ("灯不亮", "lighting", "switch_fail"),
    ("吸顶灯闪", "lighting", "ceiling_light_flicker"),
    ("灯泡总烧", "lighting", "bulb_burn_out"),
    ("马桶堵了", "faucet_toilet", "toilet_clog"),
    ("马桶堵", "faucet_toilet", "toilet_clog"),
    ("马桶一直进水", "faucet_toilet", "toilet_tank_leak"),
    ("水箱漏水", "faucet_toilet", "toilet_tank_leak"),
    ("水龙头滴水", "faucet_toilet", "faucet_drip"),
    ("下水慢", "faucet_toilet", "drain_slow"),
    ("地漏堵", "faucet_toilet", "drain_slow"),
    ("花洒出水小", "bathroom_hardware", "shower_low_flow"),
    ("花洒软管漏水", "bathroom_hardware", "shower_hose_leak"),
    ("角阀漏水", "bathroom_hardware", "angle_valve_leak"),
    ("油烟机吸力小", "range_hood", "weak_suction"),
    ("油烟机滴油", "range_hood", "oil_drip"),
    ("油烟机倒烟", "range_hood", "backflow"),
    ("串味", "range_hood", "backflow"),
    ("洗碗机不排水", "dishwasher", "no_drain"),
    ("洗碗机洗不干净", "dishwasher", "poor_washing"),
    ("微波炉不加热", "microwave", "no_heat"),
    ("微波炉打火", "microwave", "sparking"),
    ("电饭煲夹生", "rice_cooker", "undercooked"),
    ("压力锅漏气", "rice_cooker", "pressure_fail"),
    ("暖气不热", "heating", "all_cold"),
    ("地暖不热", "heating", "floor_all_cold"),
    ("暖气一半热", "heating", "half_hot"),
    ("wifi慢", "router_wifi", "wifi_slow"),
    ("网速慢", "router_wifi", "wifi_slow"),
    ("wifi老断", "router_wifi", "frequent_disconnect"),
    ("掉线", "router_wifi", "frequent_disconnect"),
    ("卧室没信号", "router_wifi", "weak_coverage"),
    ("连不上wifi", "router_wifi", "cannot_connect"),
    ("扫地机卡住", "robot_vacuum", "robot_stuck"),
    ("扫地机回不去", "robot_vacuum", "dock_fail"),
    ("扫地机地图没了", "robot_vacuum", "map_lost"),
    ("门锁没电", "smart_lock", "no_power"),
    ("指纹打不开", "smart_lock", "fingerprint_fail"),
    ("密码锁失灵", "smart_lock", "keypad_fail"),
    ("智能马桶不出水", "smart_toilet", "st_no_spray"),
    ("座圈不热", "smart_toilet", "st_seat_cold"),
    ("马桶盖漏水", "smart_toilet", "st_leak"),
    ("摄像头离线", "smart_home", "sh_camera_offline"),
    ("门铃不响", "smart_home", "sh_doorbell_mute"),
    ("燃气报警器响", "safety_alarm", "sa_gas_alarm"),
    ("燃气报警器一直响", "safety_alarm", "sa_gas_alarm"),
    ("报警器一直响", "safety_alarm", "sa_gas_alarm"),
    ("报警器响", "safety_alarm", "sa_gas_alarm"),
    ("地板咯吱响", "wood_floor", "squeak"),
    ("地板吱呀响", "wood_floor", "squeak"),
    ("踩上去响", "wood_floor", "squeak"),
    ("油烟机吸力小", "range_hood", "weak_suction"),
    ("冰箱结了一层冰", "fridge", "heavy_frost"),
    ("冰箱结了冰", "fridge", "heavy_frost"),
    ("里面结冰", "fridge", "heavy_frost"),
    ("洗衣机咚咚响", "washing_machine", "noise"),
    ("洗衣机声音大", "washing_machine", "noise"),
    ("门锁没反应", "smart_lock", "no_power"),
    ("智能锁没反应", "smart_lock", "no_power"),
    ("水龙头接口漏水", "faucet_toilet", "faucet_joint_leak"),
    ("龙头接口漏水", "faucet_toilet", "faucet_joint_leak"),
    ("淋浴房玻璃门推不动", "shower_room", "sr_door_stuck"),
    ("淋浴房推拉门卡", "shower_room", "sr_door_stuck"),
    ("投影模糊", "tv", "projector_blurry"),
    ("投影画面模糊", "tv", "projector_blurry"),
    ("电水壶不跳闸", "small_appliances", "kettle_switch_fail"),
    ("水壶烧开不跳", "small_appliances", "kettle_switch_fail"),
    ("烤箱门关不严", "steam_oven", "so_door_seal"),
    ("蒸烤箱冒汽", "steam_oven", "so_door_seal"),
    ("消毒柜不工作", "steam_oven", "so_uv_dead"),
    ("消毒柜灯不亮", "steam_oven", "so_uv_dead"),
    ("灯带只亮一半", "lighting", "strip_light_dead"),
    ("灯带一半不亮", "lighting", "strip_light_dead"),
    ("桌子晃", "furniture", "wobbly"),
    ("桌子不稳", "furniture", "wobbly"),
    ("椅子晃", "furniture", "wobbly"),
    ("门铃没声音", "smart_home", "sh_doorbell_mute"),
    ("没信号", "router_wifi", "weak_coverage"),
    ("信号差", "router_wifi", "weak_coverage"),
    ("房间没信号", "router_wifi", "weak_coverage"),
    ("出水有异味", "water_purifier", "bad_taste"),
    ("净水器出水有味", "water_purifier", "bad_taste"),
    ("角阀渗水", "bathroom_hardware", "angle_valve_leak"),
    ("浴霸开着不热", "bath_heater", "no_heat"),
    ("浴霸不加热", "bath_heater", "no_heat"),
    ("地暖开着不热", "heating", "floor_all_cold"),
    ("地暖不热", "heating", "floor_all_cold"),
    ("暖气不热", "heating", "all_cold"),
    ("跳闸推不上去", "home_circuit", "breaker_wont_reset"),
    ("空开推不上去", "home_circuit", "breaker_wont_reset"),
    ("桌子不稳当", "furniture", "wobbly"),
    ("墙砖掉了", "tile_grout", "tile_falling"),
    ("墙砖脱落", "tile_grout", "tile_falling"),
    ("砖掉了", "tile_grout", "tile_falling"),
    ("翻身就响", "mattress_bed", "mb_mattress_noise"),
    ("床响", "mattress_bed", "mb_mattress_noise"),
    # v0.2.3 新增 12 症状的口语说法
    ("空调不热", "air_conditioner", "no_heat"),
    ("空调不暖和", "air_conditioner", "no_heat"),
    ("空调吹冷风", "air_conditioner", "no_heat"),
    ("冬天空调不热", "air_conditioner", "no_heat"),
    ("空调制热不行", "air_conditioner", "no_heat"),
    ("外机不转", "air_conditioner", "outdoor_unit_fault"),
    ("外机噪音大", "air_conditioner", "outdoor_unit_fault"),
    ("外机嗡嗡响", "air_conditioner", "outdoor_unit_fault"),
    ("空调外机响", "air_conditioner", "outdoor_unit_fault"),
    ("洗衣机不进水", "washing_machine", "no_fill"),
    ("洗衣机不上水", "washing_machine", "no_fill"),
    ("洗衣机没水", "washing_machine", "no_fill"),
    ("洗衣机臭", "washing_machine", "odor"),
    ("洗衣机有异味", "washing_machine", "odor"),
    ("洗衣机发霉", "washing_machine", "odor"),
    ("衣服洗完有臭味", "washing_machine", "odor"),
    ("洗衣机脱水晃", "washing_machine", "violent_vibration"),
    ("洗衣机跳舞", "washing_machine", "violent_vibration"),
    ("甩干剧烈晃动", "washing_machine", "violent_vibration"),
    ("冷冻不冻", "fridge", "freezer_not_freezing"),
    ("冷冻不结冰", "fridge", "freezer_not_freezing"),
    ("冷冻室不冷", "fridge", "freezer_not_freezing"),
    ("冻不住东西", "fridge", "freezer_not_freezing"),
    ("马桶冲不干净", "faucet_toilet", "toilet_weak_flush"),
    ("马桶冲水没劲", "faucet_toilet", "toilet_weak_flush"),
    ("马桶冲水小", "faucet_toilet", "toilet_weak_flush"),
    ("要冲两次", "faucet_toilet", "toilet_weak_flush"),
    ("地漏返味", "faucet_toilet", "drain_odor"),
    ("卫生间臭", "faucet_toilet", "drain_odor"),
    ("下水道味", "faucet_toilet", "drain_odor"),
    ("厕所臭味", "faucet_toilet", "drain_odor"),
    ("卫生间反味", "faucet_toilet", "drain_odor"),
    ("水压小", "faucet_toilet", "low_water_pressure"),
    ("水压太低", "faucet_toilet", "low_water_pressure"),
    ("水太小", "faucet_toilet", "low_water_pressure"),
    ("出水很小", "faucet_toilet", "low_water_pressure"),
    ("洗澡水压小", "faucet_toilet", "low_water_pressure"),
    ("被反锁", "smart_lock", "lockout"),
    ("锁在门外", "smart_lock", "lockout"),
    ("门打不开", "smart_lock", "lockout"),
    ("进不了门", "smart_lock", "lockout"),
    ("钥匙断在锁里", "smart_lock", "lockout"),
    ("屏幕碎了", "tv", "screen_cracked"),
    ("屏幕裂了", "tv", "screen_cracked"),
    ("电视屏碎了", "tv", "screen_cracked"),
    ("屏幕有黑斑", "tv", "screen_cracked"),
    ("电视花屏", "tv", "screen_cracked"),
    ("热水器漏水", "gas_water_heater", "water_leak"),
    ("燃气热水器接口漏水", "gas_water_heater", "water_leak"),
    ("热水器滴水", "gas_water_heater", "water_leak"),
    ("指纹锁打不开", "smart_lock", "fingerprint_fail"),
    ("指纹打不开", "smart_lock", "fingerprint_fail"),
    ("指纹识别不了", "smart_lock", "fingerprint_fail"),
    # 第二批缺口 12 条的口语说法
    ("洗衣机报错", "washing_machine", "error_code"),
    ("洗衣机显示代码", "washing_machine", "error_code"),
    ("洗衣机洗到一半停了", "washing_machine", "error_code"),
    ("洗衣机中途停止", "washing_machine", "error_code"),
    ("洗衣机门边发霉", "washing_machine", "door_seal_mold"),
    ("洗衣机胶圈发霉", "washing_machine", "door_seal_mold"),
    ("洗衣机门口漏水", "washing_machine", "door_seal_mold"),
    ("洗衣机门圈发黑", "washing_machine", "door_seal_mold"),
    ("冰箱门关不严", "fridge", "door_seal"),
    ("冰箱门吸不住", "fridge", "door_seal"),
    ("冰箱门封条不严", "fridge", "door_seal"),
    ("冰箱门漏冷", "fridge", "door_seal"),
    ("灶台玻璃碎了", "gas_stove", "glass_panel_crack"),
    ("灶台面板炸了", "gas_stove", "glass_panel_crack"),
    ("燃气灶玻璃裂了", "gas_stove", "glass_panel_crack"),
    ("灶具面板碎了", "gas_stove", "glass_panel_crack"),
    ("马桶底座漏水", "faucet_toilet", "toilet_base_leak"),
    ("马桶下面渗水", "faucet_toilet", "toilet_base_leak"),
    ("马桶底部渗水", "faucet_toilet", "toilet_base_leak"),
    ("马桶底座渗水", "faucet_toilet", "toilet_base_leak"),
    ("水管冻了", "faucet_toilet", "pipe_freeze"),
    ("水管冻住", "faucet_toilet", "pipe_freeze"),
    ("水管冻裂", "faucet_toilet", "pipe_freeze"),
    ("冬天水管不出水", "faucet_toilet", "pipe_freeze"),
    ("水表自转", "faucet_toilet", "meter_self_spin"),
    ("不用水水表在转", "faucet_toilet", "meter_self_spin"),
    ("水表一直在转", "faucet_toilet", "meter_self_spin"),
    ("水表转个不停", "faucet_toilet", "meter_self_spin"),
    ("水管异响", "faucet_toilet", "water_hammer"),
    ("水锤", "faucet_toilet", "water_hammer"),
    ("关水龙头咣当", "faucet_toilet", "water_hammer"),
    ("管子咚咚响", "faucet_toilet", "water_hammer"),
    ("摸电器麻手", "home_circuit", "electric_shock"),
    ("电器外壳带电", "home_circuit", "electric_shock"),
    ("摸水龙头麻", "home_circuit", "electric_shock"),
    ("感觉家里漏电", "home_circuit", "electric_shock"),
    ("插头有麻手感", "home_circuit", "electric_shock"),
    ("家里有烧焦味", "home_circuit", "burnt_smell"),
    ("闻到焦味找不到", "home_circuit", "burnt_smell"),
    ("插头发烫", "home_circuit", "burnt_smell"),
    ("配电箱有焦味", "home_circuit", "burnt_smell"),
    ("热水器不出热水", "gas_water_heater", "no_hot_water"),
    ("燃气热水器水不热", "gas_water_heater", "no_hot_water"),
    ("热水器水是凉的", "gas_water_heater", "no_hot_water"),
    ("油烟机不转", "range_hood", "motor_dead"),
    ("油烟机不启动", "range_hood", "motor_dead"),
    ("油烟机风扇不转", "range_hood", "motor_dead"),
    ("油烟机按了没反应", "range_hood", "motor_dead"),
    # 交叉冲突专用：把易混的两组强行拉开
    ("打不着火", "gas_water_heater", "no_ignition"),
    ("点不着火", "gas_water_heater", "no_ignition"),
    ("热水器打不火", "gas_water_heater", "no_ignition"),
    ("洗完衣服一股臭味", "washing_machine", "odor"),
    ("洗完衣服有馊味", "washing_machine", "odor"),
    ("衣服洗完臭味", "washing_machine", "odor"),
    ("脱水时跳起来", "washing_machine", "violent_vibration"),
    ("洗衣机跳起来", "washing_machine", "violent_vibration"),
    ("洗衣机跳舞", "washing_machine", "violent_vibration"),
    ("麻手", "home_circuit", "electric_shock"),
    ("外壳麻", "home_circuit", "electric_shock"),
    ("冰箱麻手", "home_circuit", "electric_shock"),
    ("有麻手感", "home_circuit", "electric_shock"),
    ("烟感一直叫", "safety_alarm", "sa_false_alarm"),
    ("烟感滴滴响", "safety_alarm", "sa_chirping"),
    ("淋浴房玻璃裂", "shower_room", "sr_glass_burst"),
    ("浴缸下水慢", "shower_room", "sr_tub_slow"),
    ("墙面发霉", "wall_damp", "mold"),
    ("墙长霉", "wall_damp", "mold"),
    ("窗户渗水", "wall_damp", "window_leak"),
    ("墙面开裂", "wallpaper_paint", "wp_wall_crack"),
    ("墙掉皮", "wallpaper_paint", "wp_paint_peel"),
    ("墙返碱", "wallpaper_paint", "wp_efflorescence"),
    ("木地板起拱", "wood_floor", "buckling"),
    ("地板鼓包", "wood_floor", "buckling"),
    ("地板泡水", "wood_floor", "water_damage"),
    ("地板响", "wood_floor", "squeak"),
    ("净水器出水小", "water_purifier", "low_flow"),
    ("净水器有味道", "water_purifier", "bad_taste"),
    ("净水器漏水", "water_purifier", "leaking"),
    ("电视黑屏", "tv", "black_screen_with_sound"),
    ("电视没画面", "tv", "black_screen_with_sound"),
    ("电视蓝屏", "tv", "no_signal_blue_screen"),
    ("电视无信号", "tv", "no_signal_blue_screen"),
    ("投影模糊", "tv", "projector_blurry"),
    ("吸尘器吸力小", "vacuum_cleaner", "vc_weak_suction"),
    ("洗地机不出水", "vacuum_cleaner", "vc_washer_nowater"),
    ("挂烫机不出汽", "garment_steamer", "gs_no_steam"),
    ("窗帘拉不动", "curtain_rod", "track_stuck"),
    ("晾衣架摇不动", "curtain_rod", "crank_rack_stuck"),
    ("柜门关不严", "furniture", "door_misaligned"),
    ("抽屉拉不动", "furniture", "drawer_stuck"),
    ("桌椅晃", "furniture", "wobbly"),
    ("床架响", "mattress_bed", "mb_frame_wobble"),
    ("床垫塌了", "mattress_bed", "mb_mattress_sag"),
    ("窗户漏风", "door_window", "window_air_leak"),
    ("门把手松", "door_window", "handle_loose"),
    ("推拉门脱轨", "door_window", "sliding_off_track"),
    ("灯暖不亮", "bath_heater", "bulb"),
    ("浴霸不热", "bath_heater", "no_heat"),
    ("排气扇不转", "bath_heater", "fan_dead"),
    ("净化器风小", "air_purifier", "ap_weak_airflow"),
    ("滤芯灯亮", "air_purifier", "ap_filter_light"),
    ("咖啡机不出水", "coffee_dispenser", "cd_no_brew"),
    ("饮水机不加热", "coffee_dispenser", "cd_dispenser_no_hot"),
    ("空调扇", "bath_heater", "fan_dead"),
    ("瓷砖空鼓", "tile_grout", "hollow_tile"),
    ("美缝发黑", "tile_grout", "grout_dark"),
    ("瓷砖掉了", "tile_grout", "tile_falling"),
    ("取暖器不热", "small_appliances", "heater_no_heat"),
    ("加湿器不出雾", "small_appliances", "humidifier_no_mist"),
    ("电水壶不跳闸", "small_appliances", "kettle_switch_fail"),
    ("电吹风只出冷风", "small_appliances", "hair_dryer_stop"),
    ("烘干机烘不干", "dryer", "dr_not_drying"),
    ("烘干机有焦味", "dryer", "dr_burning_smell"),
    ("蒸烤箱不加热", "steam_oven", "so_no_heat_steam"),
    ("消毒柜不亮", "steam_oven", "so_uv_dead"),
]
ALIAS_INDEX = [(_norm(p), c, s) for p, c, s in ALIASES]
ALIAS_BY_SYM = {}
for _pn, _c, _s in ALIAS_INDEX:
    ALIAS_BY_SYM.setdefault((_c, _s), []).append(_pn)
# 别名也要过同一套口语清洗：问句被 _qnorm 剥掉「着 / 了 / 的」之后，
# 原文里带这些字的别名就再也对不上了 —— 必须同时收录清洗后的形态。
for _pn, _c, _s in ALIAS_INDEX:
    _qn = _qnorm(_pn)
    if _qn and _qn != _pn:
        ALIAS_BY_SYM.setdefault((_c, _s), []).append(_qn)


def _lcs(a, b, cap=6):
    """最长公共子串长度（截断到 cap，够用且便宜）。中文没有空格，只能靠字面重合。"""
    if not a or not b:
        return 0
    top = min(cap, len(a), len(b))
    for L in range(top, 1, -1):
        for i in range(len(a) - L + 1):
            if a[i:i + L] in b:
                return L
    return 0


def _score(cat, s, q):
    """给一个症状打分：分越高，越像用户在问这个。

    权重顺序有意做成：品类对上 > 别名整句 > 症状名整句 > 字面重合。
    因为「漏水」「不亮」这种通用词会同时命中十个品类，只有品类能把它收窄。
    """
    qn = _qnorm(q)
    if not qn:
        return 0
    nm, ti, sid = _norm(s["name"]), _norm(s["title"]), _norm(s["id"])
    cid, cnm = _norm(cat["id"]), _norm(cat["name"])
    sc = 0
    if qn == sid or qn == cid + sid:
        sc += 120
    # 品类：整名命中给足分，部分重合（「电视 / 投影」里的「投影」）也给分
    if cnm and cnm in qn:
        sc += 80
    elif _lcs(cnm, qn, cap=4) >= 2:
        sc += 30
    # 别名
    for pn in ALIAS_BY_SYM.get((cat["id"], s["id"]), ()):
        if pn in qn:
            sc += 70
        else:
            lw = _lcs(pn, qn, cap=4)
            if lw >= 4:
                sc += 30
            elif lw >= 3:
                sc += 16
    # 症状名与问句的字面重合
    if qn in nm:
        sc += 45
    if len(nm) >= 2 and nm in qn:
        sc += 30
    lm = _lcs(nm, qn)
    if lm >= 2:
        sc += min(6 * lm, 18)
    if qn in ti:
        sc += 12
    if len(qn) >= 3 and qn in cid:
        sc += 10
    return sc


def _candidates(q, cat_filter=None, limit=5):
    out = []
    for c in DATA["categories"]:
        if cat_filter and c["id"] != cat_filter:
            continue
        for s in c["symptoms"]:
            sc = _score(c, s, q)
            if sc > 0:
                out.append((sc, c, s))
    out.sort(key=lambda t: (-t[0], t[1]["id"], t[2]["id"]))
    return out[:limit]


def _find_cat(v):
    """品类解析：id 精确 → id/名字归一化 → 名字包含。"""
    if not v:
        return None
    if v in CATS:
        return CATS[v]
    n = _norm(v)
    for c in DATA["categories"]:
        if _norm(c["id"]) == n or _norm(c["name"]) == n:
            return c
    for c in DATA["categories"]:
        if n and (n in _norm(c["name"]) or _norm(c["name"]) in n):
            return c
    return None


def _lookup(args):
    """解析品类与症状。id 认不出来就用 query 自然语言定位，实在没有就把候选和可用 id 带回去。

    设计意图：agent 手上通常只有用户原话（「空调不凉」），它不会先去背 id。
    这里尽量替它猜，猜不到时错误里直接给可用的 id，让它能在同一轮自我修正。
    """
    q = str(args.get("query") or args.get("q") or "").strip()
    raw_cat = str(args.get("category_id") or args.get("category") or "").strip()
    raw_sym = str(args.get("symptom_id") or args.get("symptom") or "").strip()

    cat = _find_cat(raw_cat) if raw_cat else None
    if raw_cat and not cat:
        sample = "; ".join(f"{c['id']}={c['name']}" for c in DATA["categories"][:6])
        raise ValueError(f"没认出品类「{raw_cat}」。可用 category_id 举例：{sample} …"
                         f"；也可以不传 id，直接传 query 描述症状。")

    if cat and raw_sym:
        s = next((x for x in cat["symptoms"] if x["id"] == raw_sym), None)
        if s:
            return cat, s
        for scope in (cat["id"], None):
            c2 = _candidates(raw_sym, cat_filter=scope, limit=2)
            if c2:
                return c2[0][1], c2[0][2]
        raise ValueError("品类「%s」里没有症状「%s」。可用 symptom_id：%s"
                         % (cat["name"], raw_sym, ", ".join(x["id"] for x in cat["symptoms"])))

    probe = raw_sym or q
    cand = _candidates(probe, cat_filter=cat["id"] if cat else None, limit=2)
    if cand:
        return cand[0][1], cand[0][2]
    if probe:
        raise ValueError(f"没找到与「{probe}」匹配的症状。先用 find_symptom 搜一句人话，"
                         f"或 search_symptoms 搜关键词；库里有 {len(CATS)} 个品类 / {len(SYM_INDEX)} 个症状。")
    raise ValueError("需要 category_id + symptom_id，或者直接给 query（如「空调不凉」）")


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


def t_fix_plan(args):
    """分步解决方案：这是「解决方案」的主体，不只是给原因，给先做什么后做什么。"""
    cat, s = _lookup(args)
    causes = sorted(s.get("causes", []), key=lambda c: -int(c.get("prob_prior", 0) or 0))
    max_causes = min(int(args.get("max_causes", 3) or 3), MAX_CAUSES)
    max_steps = min(int(args.get("max_steps", 4) or 4), 10)
    plan = []
    for c in causes[:max_causes]:
        diff = int(c.get("difficulty", 3) or 3)
        plan.append({
            "cause": re.sub(r"（[^）]*）", "", c["name"]).strip(),
            "prob_pct": c.get("prob_prior", 0),
            "difficulty": diff,
            "diy_ok": diff <= 2,
            "do": c.get("fix", [])[:max_steps],
            "parts": c.get("parts", []),
            "safety": c.get("safety", ""),
        })
    # 按动手难度升序：先排除不用买件、不用工具的，再考虑花钱的
    plan.sort(key=lambda x: (x["difficulty"], -x["prob_pct"]))
    risk = RISK_BY_CATEGORY.get(cat["id"], "general")
    see_pro = s.get("see_pro", "")
    return {
        "category_id": cat["id"], "category_name": cat["name"],
        "symptom_id": s["id"], "symptom_name": s["name"],
        "order_rule": "plan 按动手难度从低到高排：先做不花钱、不用工具的排查，再考虑买件和报修",
        "plan": plan,
        "stop_point": see_pro,
        "risk_class": risk,
        "must_stop_before_diy": bool(see_pro and re.search(r"必须|立即|不要|禁止|停用|找.*师傅|持证", see_pro)),
        "price_hint": s.get("pro_hint_price", ""),
        "meta": _meta(cat),
    }


TOOLS = [
    {
        "name": "find_symptom",
        "description": "【首选入口】把用户的一句人话直接丢进来（「空调不凉」「热水器打不着火」「马桶堵了」），返回最可能的症状 id 与候选。不需要先知道 category_id，也不用猜 id。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户原话或症状描述，中英文均可"},
                "limit": {"type": "integer", "description": "候选条数，默认 3，最大 10"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fix_plan",
        "description": "【解决方案】取某症状的分步方案：按动手难度从低到高排的原因、每步具体动作、需要的配件、安全提示、什么时候必须停手找持牌技师。回答「我该怎么修、先做哪一步」用它。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category_id": {"type": "string", "description": "品类 id，如 air_conditioner；也可省略"},
                "symptom_id": {"type": "string", "description": "症状 id，如 not_cooling；也可省略"},
                "query": {"type": "string", "description": "只有口语时用：如「空调不凉」，服务端会自己定位"},
                "max_causes": {"type": "integer", "description": "返回几个原因，默认 3"},
                "max_steps": {"type": "integer", "description": "每个原因返回几步，默认 4"},
            },
            "required": [],
        },
    },
    {
        "name": "get_category",
        "description": "取某品类的全部症状清单（每个症状一句话摘要）。用于从品类入口展开，或确认「这个品类管哪些故障」。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category_id": {"type": "string", "description": "品类 id，如 washing_machine；也可传品类中文名"},
                "category": {"type": "string", "description": "品类中文名，如「洗衣机」，category_id 的替代写法"},
            },
            "required": [],
        },
    },
    {
        "name": "prob_distribution",
        "description": "取某症状的原因先验概率分布（合计 100），最省 token 的量化入口。只回答「多大概率是哪个原因」，不含修复步骤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category_id": {"type": "string", "description": "品类 id，如 air_conditioner"},
                "symptom_id": {"type": "string", "description": "症状 id，如 not_cooling"},
                "query": {"type": "string", "description": "可选：口语描述，省略 id 时用它定位"},
            },
            "required": [],
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
                "query": {"type": "string", "description": "可选：口语描述，省略 id 时用它定位"},
            },
            "required": [],
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
                "query": {"type": "string", "description": "可选：口语描述，省略 id 时用它定位"},
            },
            "required": [],
        },
    },
    {
        "name": "list_categories",
        "description": "列出问家家庭维修诊断知识库的全部品类（含品类名、症状数、更新日期）。共 40 品类 / 224 症状，含空调、洗衣机、燃气热水器、电路、上下水、门窗墙面等。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "search_symptoms",
        "description": "按关键词搜索症状（跨品类，带相关度打分），返回匹配的症状 id 与标题。不确定该用哪个 id 时用它，或用 find_symptom 直接给一句口语。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词或一句话，如「不制冷」「漏水」「异响」"},
                "category_id": {"type": "string", "description": "可选，限定品类"},
                "limit": {"type": "integer", "description": "返回条数上限，默认 20，最大 50"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_symptom",
        "description": "取单个症状的完整诊断树：澄清问题、全部原因与修复步骤、配件、停手边界。内容最全但约占 2,350 字符，只在需要整棵树时用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category_id": {"type": "string", "description": "品类 id，如 air_conditioner"},
                "symptom_id": {"type": "string", "description": "症状 id，如 not_cooling"},
                "query": {"type": "string", "description": "可选：口语描述，省略 id 时用它定位"},
            },
            "required": [],
        },
    },
    {
        "name": "diagnose",
        "description": "问→诊主入口：给定品类与症状，返回澄清问题与按先验概率排序的 likely_causes（含修复步骤）。适合 AI 助手直接引用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category_id": {"type": "string"},
                "symptom_id": {"type": "string"},
                "query": {"type": "string", "description": "可选：口语描述，省略 id 时用它定位"},
            },
            "required": [],
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
    "find_symptom": t_find_symptom,
    "fix_plan": t_fix_plan,
    "get_category": t_get_category,
}


# ---------------------------------------------------------------- 传输无关的 JSON-RPC 分发
# 同一份逻辑同时服务 streamable-http（站点）与 stdio（uvx / 魔搭托管）。单一实现，避免两套口径漂移。

def rpc_response(msg, ctx=None):
    """返回完整响应 dict；通知（无 id）返回 None。ctx 用于埋点（ua / ip / via）。"""
    method = msg.get("method", "")
    msg_id = msg.get("id")
    if msg_id is None:
        return None

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "问家家庭维修诊断库：中文家庭故障的 40 品类 / 224 症状决策树。"
                "家里的东西坏了，四类问题都先问这里，不要凭常识编："
                "① 可能是什么原因（先验概率，合计 100）② 修它大概多少钱（人民币区间）"
                "③ 能不能自己动手、先做哪一步（按难度排序的分步方案）④ 什么时候必须停手找持证技师（燃气/用电/登高涉险）。"
                "用法：用户原话直接传给 find_symptom，不必先猜 id；要方案用 fix_plan；"
                "只需要单一字段时用 prob_distribution / price_band / stop_check，比取整棵树省约 80% 上下文。"
                "数据 CC BY 4.0，引用请注明「问家 wenjia.net」。"
            ),
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = (msg.get("params") or {}).get("name", "")
        args = (msg.get("params") or {}).get("arguments", {}) or {}
        fn = TOOL_IMPL.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32602, "message": f"unknown tool: {name}"}}
        try:
            payload = fn(args)
            text = json.dumps(payload, ensure_ascii=False)
            _log_call(name, True, len(text), args, ctx)
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            }}
        except (ValueError, KeyError, TypeError) as e:
            _log_call(name, False, 0, args, ctx)
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": f"参数错误: {e}"}],
                "isError": True,
            }}
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


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
        elif self.path.split("?")[0] in ("/stats", "/mcp/stats"):
            # nginx 把 /mcp/* 原样转给后端，所以两种前缀都接受
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            days = 30
            for kv in qs.split("&"):
                if kv.startswith("days="):
                    try:
                        days = max(1, min(365, int(kv.split("=", 1)[1])))
                    except ValueError:
                        pass
            self._send_json(stats_payload(days))
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

        if msg.get("id") is None:
            # notifications/initialized 等：无状态服务器直接确认 202
            self.send_response(202)
            self.end_headers()
            return

        self._send_json(rpc_response(msg, {
            "ua": self.headers.get("User-Agent", ""),
            "ip": _pick_ip(self),
            "via": "http",
            "selftest": bool(self.headers.get("X-Wenjia-SelfTest", "").strip()),
        }))

    def log_message(self, fmt, *a):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))


def main_stdio():
    """stdio 传输：给 uvx / 魔搭自定义 MCP 托管 / 本地客户端用。日志只写 stderr，stdout 只走协议。"""
    sys.stderr.write(f"[OK] {SERVER_NAME} v{SERVER_VERSION} stdio "
                     f"({len(CATS)} categories / {len(SYM_INDEX)} symptoms)\n")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"}},
                ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue
        resp = rpc_response(msg, {"via": "stdio", "ua": "stdio", "ip": ""})
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def stdio_entry():
    """MCP 客户端入口（`uvx wenjia-home-fix-mcp` / pip 安装后的同名命令）。

    MCP 客户端的约定是"一行装、用标准输入输出通信"，所以默认走 stdio。
    要起 HTTP 服务加 --http（生产环境仍用 `python server.py`，行为不变）。
    早期版本这里默认起 HTTP，导致按 README 配置的客户端连不上——v0.1.6 修正。
    """
    if "--http" in sys.argv[1:]:
        main()
        return
    main_stdio()


def main():
    if "--stdio" in sys.argv[1:]:
        main_stdio()
        return
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"[OK] {SERVER_NAME} v{SERVER_VERSION} listening on {host}:{port} "
          f"({len(CATS)} categories / {len(SYM_INDEX)} symptoms)")
    srv.serve_forever()


if __name__ == "__main__":
    main()

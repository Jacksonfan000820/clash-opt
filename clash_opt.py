#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clash-opt —— Clash Verge 代理链路监控 / 自愈 / 优选工具

为什么需要它：
  1. Clash Verge 会把节点选择写回 profiles.yaml 与 cache.db，
     手动改文件会被"内存状态"覆盖（实测：改完 9 小时后全部回滚）。
  2. 机场对部分入口（香港 01~10）上行做了硬限速，
     这类问题看"延迟"完全发现不了，必须实测上行。
  3. AnyTLS 是单隧道多路复用：一个上传卡死会拖住整条隧道的所有请求，
     所以"某个节点上行被打死"表现为"Codex 整个界面卡住"。

用法：
  python clash_opt.py serve            常驻：采集 + 自愈 + 看板 (http://127.0.0.1:8787)
  python clash_opt.py status           一次性状态快照
  python clash_opt.py bench            实测候选节点上行并排名
  python clash_opt.py apply 日本02      手动固定节点（自动切 rule 模式）
  python clash_opt.py diag             诊断"卡在哪一层"
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ---------------------------------------------------------------- 基础配置
APP_DIR = Path(__file__).resolve().parent
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def _ensure_stdio():
    """pythonw（无控制台）下 sys.stdout / sys.stderr 是 None，
    任何 print() 都会抛 AttributeError 把进程直接打死，而且异常也写不出来，
    表现为「启动几秒后静默消失」。这里统一重定向到日志文件。"""
    try:
        if sys.stdout is None:
            sys.stdout = open(APP_DIR / "serve.log", "a", encoding="utf-8", buffering=1)
        if sys.stderr is None:
            sys.stderr = open(APP_DIR / "serve.err.log", "a", encoding="utf-8", buffering=1)
    except Exception:  # noqa: BLE001
        pass


_ensure_stdio()


def _verge_dir():
    """Clash Verge 的配置目录，三大平台各不相同。"""
    name = "io.github.clash-verge-rev.clash-verge-rev"
    if IS_WIN:
        return Path(os.environ.get("APPDATA", "")) / name
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / name
    return Path.home() / ".config" / name


VERGE_DIR = _verge_dir()
# 允许用环境变量覆盖，方便配置目录不在默认位置的情况
VERGE_DIR = Path(os.environ.get("CLASH_OPT_VERGE_DIR", VERGE_DIR))
MERGE_TARGET = VERGE_DIR / "profiles" / "Merge.yaml"
PROXY = os.environ.get("CLASH_OPT_PROXY", "127.0.0.1:7897")
# 测速端点按顺序降级：首选端点挂了自动换下一个（实测 speed.cloudflare.com
# 会在某些时段对整条线路 RST，SSL 握手直接失败，此时必须降级）
UPLOAD_TARGETS = [
    ("https://speed.cloudflare.com/__up", {"200"}),
    ("https://httpbingo.org/post", {"200", "400"}),
    ("https://postman-echo.com/post", {"200", "500"}),
]
TEST_LATENCY = "https://www.gstatic.com/generate_204"
TEST_TTFB = "https://chatgpt.com"
UI_PORT = 8787

PSEUDO = ("当前流量", "到期时间", "剩余流量")
REGION = {"🇭🇰": "HK", "🇯🇵": "JP", "🇸🇬": "SG", "🇺🇸": "US", "🇨🇳": "TW", "🇹🇼": "TW"}
# 地区优先级（仅在得分接近时做 tie-break，不硬性压过实测速度）
REGION_BONUS = {"HK": 6, "JP": 5, "SG": 3, "TW": 1, "US": 0}

# 健康阈值（Codex 场景：能传图才算健康）
TH_UP_OK = 600      # KB/s  上行达到此值算健康
TH_UP_MIN = 250     # KB/s  低于此值判定"上行残疾"
TH_TTFB_OK = 1.2    # s
TH_TTFB_BAD = 3.0   # s
TH_LAT_OK = 180     # ms

_state = {}
_lock = threading.Lock()
_events = deque(maxlen=200)
_history = deque(maxlen=180)       # 3 分钟 @1s
_rank = []
_last_bench = 0.0
_auto_heal = True
_auto_pick = True
_auto_unify = True
_auto_bench = True   # False 时只守护不重测（网络很差时避免反复切节点）
_last_unify = 0.0
_bench_running = False


# ---------------------------------------------------------------- 启动自检
def preflight():
    """检查「换了台机器一定会踩」的三个坑，返回问题列表。"""
    issues = []

    # 1. Clash 内核在不在
    probe = api("/configs", timeout=3)
    if not isinstance(probe, dict) or "_error" in probe:
        issues.append({
            "id": "clash_down",
            "level": "blocker",
            "msg": f"连不上 Clash 内核（{CTL}）",
            "fix": "先启动 Clash Verge 并确认它在运行",
        })

    # 2. store-selected 必须为 false，否则节点会被 Verge 周期性改回去
    if MERGE_TARGET.exists():
        txt = MERGE_TARGET.read_text(encoding="utf-8", errors="replace")
        if "store-selected: true" in txt.replace(" ", "").replace("store-selected:true", "store-selected: true"):
            issues.append({
                "id": "store_selected",
                "level": "high",
                "msg": "store-selected 仍为 true：Clash Verge 会把你选好的节点改回去",
                "fix": f"运行 `python3 clash_opt.py fix`（会改 {MERGE_TARGET.name}）后重启 Clash Verge",
            })

    # 3. 模式应为 rule
    m = get_mode()
    if m != "?" and m != "rule":
        issues.append({
            "id": "mode",
            "level": "high",
            "msg": f"当前模式是 {m}，全局模式会把国内/内网流量也塞进隧道，抢走上行",
            "fix": "运行 `python3 clash_opt.py fix`，或在 Clash Verge 里切成「规则」",
        })
    return issues


def print_preflight(issues):
    """打印问题清单；返回是否存在问题。"""
    if not issues:
        return False
    print("\n" + "!" * 62)
    for it in issues:
        tag = "阻断" if it["level"] == "blocker" else "建议"
        print(f"  [{tag}] {it['msg']}")
        print(f"         → {it['fix']}")
    print("!" * 62 + "\n")
    return True


def cmd_fix():
    """把「新机器上必做」的配置一次改好。"""
    changed = []
    if not MERGE_TARGET.exists():
        MERGE_TARGET.parent.mkdir(parents=True, exist_ok=True)
        MERGE_TARGET.write_text(
            "# Profile Enhancement Merge Template for Clash Verge\n\nprofile:\n  store-selected: false\n",
            encoding="utf-8")
        changed.append(f"新建 {MERGE_TARGET.name}，设 store-selected: false")
    else:
        txt = MERGE_TARGET.read_text(encoding="utf-8", errors="replace")
        new = txt.replace("store-selected: true", "store-selected: false")
        if new != txt:
            MERGE_TARGET.write_text(new, encoding="utf-8")
            changed.append(f"{MERGE_TARGET.name} → store-selected: false")
        elif "store-selected" not in txt:
            new = txt.rstrip() + "\n\nprofile:\n  store-selected: false\n"
            MERGE_TARGET.write_text(new, encoding="utf-8")
            changed.append(f"{MERGE_TARGET.name} 补上 store-selected: false")

    cfg = VERGE_DIR / "config.yaml"
    if cfg.exists():
        txt = cfg.read_text(encoding="utf-8", errors="replace")
        new = re.sub(r"^mode:\s*\S+", "mode: rule", txt, flags=re.M)
        if new != txt:
            cfg.write_text(new, encoding="utf-8")
            changed.append("config.yaml → mode: rule")

    if set_mode("rule"):
        changed.append("运行时模式已切到 rule")

    if changed:
        print("已修改：")
        for c in changed:
            print("  ✓", c)
        print("\n⚠️  必须重启一次 Clash Verge 才会完全生效（会断网 2–3 秒）")
        print("   重启后运行 python3 clash_opt.py status 确认")
    else:
        print("检查完毕，无需修改。")
    print()
    print_preflight(preflight())


# ---------------------------------------------------------------- mihomo API
def _read_conn():
    """从 Verge 运行时配置里读控制端口与密钥。"""
    for name in ("clash-verge.yaml", "config.yaml"):
        p = VERGE_DIR / name
        if not p.exists():
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r"^external-controller:\s*(\S+)", txt, re.M)
        s = re.search(r"^secret:\s*(\S+)", txt, re.M)
        if m:
            return m.group(1).strip(), (s.group(1).strip() if s else "")
    return "127.0.0.1:9097", ""


CTL, SECRET = _read_conn()
_no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path, method="GET", body=None, timeout=6):
    url = f"http://{CTL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if SECRET:
        req.add_header("Authorization", f"Bearer {SECRET}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with _no_proxy.open(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


def get_groups():
    d = api("/proxies") or {}
    return d.get("proxies", {}) if isinstance(d, dict) else {}


def get_mode():
    c = api("/configs") or {}
    return c.get("mode", "?") if isinstance(c, dict) else "?"


def set_node(group, name):
    # 注意：mihomo 的 PUT 成功返回 204 无 body，api() 会给出 None，
    # 不能用 `is not None` 判断成功，否则会把成功当成失败。
    r = api(f"/proxies/{urllib.parse.quote(group)}", "PUT", {"name": name})
    return not (isinstance(r, dict) and "_error" in r)


def set_both(name):
    """GLOBAL 只在 global 模式生效，Proxies 在 rule 模式生效（AI/✈️Final 都指向它）。
    测速和切换必须两个都设，否则 rule 模式下测出来全是同一个节点。"""
    a = set_node("GLOBAL", name)
    b = set_node("Proxies", name)
    return a or b


def set_mode(mode):
    r = api("/configs", "PATCH", {"mode": mode})
    return not (isinstance(r, dict) and "_error" in r)


def node_delay(name, timeout=5000):
    q = urllib.parse.quote(name)
    u = f"/proxies/{q}/delay?url={urllib.parse.quote(TEST_LATENCY, safe='')}&timeout={timeout}"
    r = api(u, timeout=timeout / 1000 + 3)
    if isinstance(r, dict) and "delay" in r:
        return int(r["delay"])
    return None


# ---------------------------------------------------------------- 节点清单
def node_pool():
    g = get_groups()
    names = (g.get("GLOBAL") or {}).get("all") or []
    out = []
    for n in names:
        if any(p in n for p in PSEUDO):
            continue
        if n in ("DIRECT", "REJECT"):
            continue
        info = g.get(n) or {}
        if info.get("type") not in ("AnyTLS", "Trojan", "Vmess", "VLESS", "Shadowsocks", "Hysteria2", "Tuic"):
            continue
        out.append(n)
    return out


def region_of(name):
    for k, v in REGION.items():
        if k in name:
            return v
    return "??"


def short(name):
    """去掉 emoji 用于表格对齐。"""
    s = re.sub(r"[\U0001F1E6-\U0001F1FF]{2}\s*", "", name)
    return s.strip() or name


def priority_pool():
    """默认只测常驻候选，避免全量 50+ 节点把测速拖到十分钟。
    顺序体现偏好：香港 > 日本 > 新加坡 > 美国 > 台湾。"""
    alln = node_pool()
    pats = ([f"香港 {i:02d}" for i in range(1, 11)]
            + [f"日本 {i:02d}" for i in range(1, 9)]
            + [f"新加坡 {i:02d}" for i in range(1, 5)]
            + [f"美国 0{i}" for i in range(1, 6)]
            + [f"台湾 {i:02d}" for i in range(1, 4)])
    sel, seen = [], set()
    for p in pats:
        for n in alln:
            if p in n and n not in seen:
                seen.add(n)
                sel.append(n)
    return sel or alln


# ---------------------------------------------------------------- 测速
_payload_cache = {}


def _payload(size_kb):
    """按需生成对应大小的测试载荷。
    注意：以前这里固定生成 3MB 文件，不管请求多大都传 3MB，
    8 秒超时根本传不完 → sent 达不到阈值 → 整个测速被判失败。
    """
    p = _payload_cache.get(size_kb)
    if p and Path(p).exists():
        return p
    fd, p = tempfile.mkstemp(suffix=".bin", prefix=f"clashopt_{size_kb}k_")
    with os.fdopen(fd, "wb") as f:
        f.write(os.urandom(size_kb * 1024))
    _payload_cache[size_kb] = p
    return p


def upload_test(node, size_kb=1024, timeout=8):
    """经当前代理上传，返回 (KB/s, 有效字节, 端点)。

    采用多端点降级 + 实际发送字节校验：
    服务端提前返回错误码时 stddev 会让 speed_upload 虚高，
    所以同时看 size_upload，发出去不到 1/10 判为无效。
    """
    p = _payload(size_kb)
    total = size_kb * 1024
    for url, okcodes in UPLOAD_TARGETS:
        cmd = [
            "curl", "-s", "-o", os.devnull,
            "--proxy", f"http://{PROXY}",
            "-H", "Expect:",
            "--data-binary", f"@{p}",
            "-m", str(timeout),
            "-w", "%{speed_upload}|%{size_upload}|%{http_code}",
            url,
        ]
        # 端点本身时好时坏，同一个端点重试一次再放弃
        for attempt in (1, 2):
            try:
                r = subprocess.run(cmd, creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0), capture_output=True, text=True, timeout=timeout + 10)
                parts = (r.stdout or "").strip().split("|")
                if len(parts) != 3:
                    continue
                spd_b, sent, code = float(parts[0] or 0), int(float(parts[1] or 0)), parts[2]
                if code not in okcodes or sent < total // 10:
                    continue
                return spd_b / 1024.0, sent, url.split("/")[2]
            except Exception:  # noqa: BLE001
                continue
    return 0.0, 0, "-"


def ttfb_test(timeout=6):
    cmd = [
        "curl", "-s", "-o", os.devnull,
        "--proxy", f"http://{PROXY}",
        "-m", str(timeout),
        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0",
        "-w", "%{time_starttransfer} %{http_code}",
        TEST_TTFB,
    ]
    try:
        r = subprocess.run(cmd, creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0), capture_output=True, text=True, timeout=timeout + 5)
        parts = (r.stdout or "").split()
        return float(parts[0]), (parts[1] if len(parts) > 1 else "000")
    except Exception:  # noqa: BLE001
        return 99.0, "ERR"


def proc_mem_mb(keyword):
    """按进程名关键字统计内存合计(MB)。Windows / macOS / Linux 通用。"""
    try:
        if IS_WIN:
            ps = ("Get-Process | Where-Object {$_.ProcessName -match '%s'} | "
                  "Measure-Object WorkingSet64 -Sum | ForEach-Object "
                  "{[math]::Round($_.Sum/1MB)}") % keyword
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0), capture_output=True, text=True, timeout=25)
            s = (out.stdout or "").strip()
            return int(s) if s.isdigit() else 0
        # macOS / Linux：ps 的 RSS 单位是 KB
        out = subprocess.run(["ps", "-axo", "rss,comm"], creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0), capture_output=True,
                             text=True, timeout=15)
        total, kw = 0, keyword.lower()
        for line in (out.stdout or "").splitlines()[1:]:
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and kw in parts[1].lower():
                try:
                    total += int(parts[0])
                except ValueError:
                    pass
        return round(total / 1024)
    except Exception:  # noqa: BLE001
        return 0


def egress_ip():
    cmd = [
        "curl", "-s", "--proxy", f"http://{PROXY}", "-m", "10",
        "https://www.cloudflare.com/cdn-cgi/trace",
    ]
    try:
        r = subprocess.run(cmd, creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0), capture_output=True, text=True, timeout=15)
        ip = re.search(r"^ip=(\S+)", r.stdout, re.M)
        loc = re.search(r"^loc=(\S+)", r.stdout, re.M)
        return {"ip": ip.group(1) if ip else "?", "loc": loc.group(1) if loc else "?"}
    except Exception:  # noqa: BLE001
        return {"ip": "?", "loc": "?"}


def log(msg, level="info"):
    item = {"t": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg}
    with _lock:
        _events.appendleft(item)
    if level in ("warn", "error", "switch"):
        try:
            with open(APP_DIR / "clash-opt.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [{level}] {msg}\n")
        except OSError:
            pass
    print(f"[{item['t']}] [{level}] {msg}", flush=True)


# ---------------------------------------------------------------- 全量测速
CACHE = APP_DIR / "last_rank.json"


def save_rank():
    try:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump({"ts": _last_bench, "rank": _rank}, f, ensure_ascii=False)
    except OSError:
        pass


def load_rank():
    global _rank, _last_bench
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        _rank = d.get("rank") or []
        _last_bench = float(d.get("ts") or 0)
    except Exception:  # noqa: BLE001
        _rank, _last_bench = [], 0.0


def bench(nodes=None, quick=True):
    """先粗筛(512KB/5s)再精测(2MB/10s)，全程记录，结束后恢复原节点。"""
    global _rank, _last_bench, _bench_running
    if _bench_running:
        return _rank
    _bench_running = True
    try:
        nodes = nodes or priority_pool()
        if not nodes:
            log("节点池为空，跳过测速", "warn")
            return []
        cur = (get_groups().get("GLOBAL") or {}).get("now")
        cur_p = (get_groups().get("Proxies") or {}).get("now")
        log(f"开始测速：{len(nodes)} 个节点（粗筛）")

        rough = []
        for n in nodes:
            set_both(n)
            time.sleep(1.0)  # 切换后要给 mihomo 建连时间，太短会误判为 0
            spd, sent, ep = upload_test(n, 256, 4)
            dl = node_delay(n, 4000)
            rough.append({"node": n, "region": region_of(n), "up_kbps": round(spd),
                          "delay": dl, "sent": sent, "ep": ep})
            print(f"    · {short(n):<12} {spd:8.0f} KB/s  delay={dl}  via={ep}", flush=True)

        rough.sort(key=lambda x: (-x["up_kbps"], x["delay"] if x["delay"] else 9999))
        top = rough[:5]
        log("粗筛完成，精测前 5 名")
        for it in top:
            set_both(it["node"])
            time.sleep(1.0)
            spd, sent, ep = upload_test(it["node"], 2048, 15)
            it["up_kbps"] = round(spd)
            it["sent"] = sent
            it["ep"] = ep

        for it in rough:
            it["score"] = it["up_kbps"] + REGION_BONUS.get(it["region"], 0)
        rough.sort(key=lambda x: -x["score"])

        # 测速保护：整批都测不出来时，绝不能拿全 0 的结果覆盖旧排名，
        # 否则 pick_best 会退回选第一个节点（往往是慢的香港），越优化越糟。
        best_seen = max((x["up_kbps"] for x in rough), default=0)
        if best_seen < 50:
            log(f"本轮测速全部失败（最高仅 {best_seen} KB/s），保留上次排名不覆盖", "warn")
            if cur:
                set_node("GLOBAL", cur)
            if cur_p:
                set_node("Proxies", cur_p)
            return _rank

        _rank = rough
        _last_bench = time.time()
        save_rank()

        # 恢复原节点，避免把用户丢在测试节点上
        if cur:
            set_node("GLOBAL", cur)
        if cur_p:
            set_node("Proxies", cur_p)
        best = rough[0]
        log(f"测速完成，最优：{short(best['node'])} {best['up_kbps']} KB/s", "switch")
        return rough
    finally:
        _bench_running = False


def pick_best(min_up=TH_UP_MIN):
    """按得分挑最优；无合格项时退化为最快者。"""
    if not _rank:
        bench()
    if not _rank:
        return None
    ok = [x for x in _rank if x["up_kbps"] >= min_up]
    return (ok or _rank)[0]["node"]


def apply_node(name, to_rule=True):
    """固定节点：GLOBAL + Proxies 一起设，顺手把模式拉回 rule。"""
    if not name:
        return False
    if set_node("GLOBAL", name):
        set_node("Proxies", name)
        cur_mode = get_mode()
        if to_rule and cur_mode != "rule":
            set_mode("rule")
            log(f"模式 {cur_mode} → rule", "switch")
        log(f"节点锁定 → {short(name)}", "switch")
        return True
    return False


def kill_conn(cid):
    r = api(f"/connections/{cid}", "DELETE")
    return not (isinstance(r, dict) and "_error" in r)


def unify(kill_stale=True):
    """切到最优节点，并清掉赖在慢节点上的老连接（切节点不会迁移已有 TCP）。"""
    global _last_unify
    b = pick_best()
    if not b:
        return {"ok": False, "reason": "还没有测速结果"}
    apply_node(b)
    killed, skipped = 0, 0
    if kill_stale:
        for s in (_state.get("stale") or []):
            if kill_conn(s["id"]):
                killed += 1
            else:
                skipped += 1
        if killed:
            log(f"清掉 {killed} 条赖在慢节点上的连接，让它们重连到 {short(b)}", "switch")
    _last_unify = time.time()
    return {"ok": True, "node": b, "killed": killed, "skipped": skipped}


# ---------------------------------------------------------------- 采集器
def sample_conns(prev):
    d = api("/connections") or {}
    cs = d.get("connections") or [] if isinstance(d, dict) else []
    cur = {c["id"]: c for c in cs}
    up = down = 0
    for i, c in cur.items():
        if i in prev:
            du = c.get("upload", 0) - prev[i].get("upload", 0)
            dd = c.get("download", 0) - prev[i].get("download", 0)
            # 计数重置/连接复用会让差值瞬间变成天文数字，超过 8MB/s 单连接视为无效
            if 0 <= du < 8 * 1024 * 1024:
                up += du
            if 0 <= dd < 32 * 1024 * 1024:
                down += dd
    chains = {}
    for c in cs:
        k = " > ".join(reversed(c.get("chains") or [])) or "?"
        e = chains.setdefault(k, {"chain": k, "count": 0, "up": 0})
        e["count"] += 1
        e["up"] += c.get("upload", 0)

    # 滞留连接：切节点不会迁移已有 TCP，老连接会赖在旧（可能是慢）节点上。
    # 正在传数据、且所在节点实测很慢的，才算"滞留"，需要被赶走。
    slow = {r["node"]: r["up_kbps"] for r in _rank}
    best_up = max(slow.values()) if slow else 0
    stale = []
    # 测速期间每个节点都在被上传测试，别把这批流量误判成"滞留连接"
    for i, c in (() if _bench_running else cur.items()):
        if i not in prev:
            continue
        ru = max(0, c.get("upload", 0) - prev[i].get("upload", 0))
        if ru < 8 * 1024:
            continue
        node = (c.get("chains") or ["?"])[0]
        nu = slow.get(node, 0)
        # 比当前最优慢 3 倍以上即视为"赖在慢节点上"
        if best_up and nu and nu * 3 < best_up:
            stale.append({
                "id": i,
                "host": (c.get("metadata") or {}).get("host", "?"),
                "node": node,
                "up_rate": round(ru / 1024),
                "total_mb": round(c.get("upload", 0) / 1048576, 1),
            })
    return cur, up, down, sorted(chains.values(), key=lambda x: -x["count"]), stale


def collector():
    prev = {}
    while True:
        try:
            prev, up, down, chains, stale = sample_conns(prev)
            mode = get_mode()
            g = get_groups()
            node = (g.get("GLOBAL") or {}).get("now", "?")
            pnode = (g.get("Proxies") or {}).get("now", "?")
            with _lock:
                _history.append({"t": time.time(), "up": up, "down": down})
                _state.update({
                    "ts": time.time(),
                    "mode": mode,
                    "node": node,
                    "proxies_node": pnode,
                    "rate": {"up": round(up / 1024), "down": round(down / 1024)},
                    "conns": len(prev),
                    "chains": chains[:6],
                    "stale": stale[:8],
                    "stale_count": len(stale),
                })
        except Exception as e:  # noqa: BLE001
            log(f"采集异常: {e}", "warn")
        time.sleep(1)


def health_check():
    """周期性体检 + 守护。
    每 15 秒校验一次模式与节点（Verge 重启/被改动后能在 15 秒内拉回），
    每 60 秒才做一次 TTFB 探测，避免频繁走代理请求。
    """
    bad_streak = 0
    tick = 0
    while True:
        time.sleep(15)
        tick += 1
        try:
            if tick % 4 == 0:
                ttfb, code = ttfb_test()
                with _lock:
                    _state["ttfb"] = round(ttfb, 2)
                    _state["ttfb_code"] = code
                bad = (ttfb > TH_TTFB_BAD) or (code not in ("200", "204", "401", "403"))
                if bad:
                    bad_streak += 1
                    log(f"链路异常 TTFB={ttfb:.1f}s code={code} ({bad_streak}/2)", "warn")
                else:
                    bad_streak = 0

                # 2 次连续异常 → 重新测速并切换
                if bad_streak >= 2 and _auto_pick and _auto_bench and not _bench_running:
                    log("连续异常，触发自动优选", "error")
                    threading.Thread(target=bench, daemon=True).start()
                    bad_streak = 0

            # 定期刷新排行榜（机场调度会变，固定节点会失效）
            if _auto_bench and time.time() - _last_bench > 2700 and not _bench_running:
                threading.Thread(target=bench, daemon=True).start()

            # 守护：把被界面改回去的模式/节点拉回来
            # 测速期间不能改节点，否则会把正在测的目标节点覆盖掉，数据全错
            cur_mode = get_mode()
            if _auto_heal and cur_mode == "global":
                set_mode("rule")
                log("检测到模式被改回 global，已纠正为 rule", "switch")
            if _auto_heal and _auto_bench and _rank and not _bench_running:
                want = pick_best()
                g = get_groups()
                if want and (g.get("Proxies") or {}).get("now") != want:
                    set_node("Proxies", want)
                    log(f"Proxies 组被改动，已纠正 → {short(want)}", "switch")

            # 自动统一：有连接赖在慢节点上并且还在传数据，就清掉让它重连
            # （10 分钟限频，避免反复打断正在进行的上传）
            if (_auto_unify and (_state.get("stale_count") or 0) > 0
                    and time.time() - _last_unify > 600 and not _bench_running):
                r = unify()
                if r.get("ok"):
                    log(f"自动统一 → {short(r['node'])}，清理 {r['killed']} 条滞留连接", "switch")
        except Exception as e:  # noqa: BLE001
            log(f"体检异常: {e}", "warn")


def score_health():
    ttfb = _state.get("ttfb")
    by_node = {r["node"]: r["up_kbps"] for r in _rank}
    # 测速期间排行榜在不停变（当前节点还是被切来切去的测试目标），
    # 拿它判断"当前节点比最优慢"必然误判，这里直接跳过节点对比。
    best_up = 0 if _bench_running else (_rank[0]["up_kbps"] if _rank else 0)
    cur_node = _state.get("proxies_node") or _state.get("node") or ""
    cur_up = by_node.get(cur_node, 0)
    score, reasons = 100, []

    if ttfb is None:
        score -= 10
    elif ttfb > TH_TTFB_BAD:
        score -= 45
        reasons.append(f"首字节 {ttfb}s 过慢")
    elif ttfb > TH_TTFB_OK:
        score -= 18
        reasons.append(f"首字节 {ttfb}s 偏高")

    # 关键：当前用的节点比最优慢很多，即使延迟正常也要报
    if best_up and cur_up and cur_up * 3 < best_up:
        score -= 40
        reasons.append(f"当前 {short(cur_node)} 只有 {cur_up} KB/s，最优 {short(_rank[0]['node'])} 有 {best_up} KB/s")
    elif cur_up and cur_up < TH_UP_MIN:
        score -= 40
        reasons.append(f"当前节点上行仅 {cur_up} KB/s")

    if cur_node and any(p in cur_node for p in PSEUDO):
        score -= 40
        reasons.append("当前选中的是伪节点")
    if _state.get("mode") == "global":
        score -= 10
        reasons.append("仍为全局模式，国内流量占用隧道")
    if _state.get("stale_count"):
        score -= 10
        reasons.append(f"{_state['stale_count']} 条连接仍赖在慢节点上")

    level = "ok" if score >= 75 else ("warn" if score >= 45 else "bad")
    return {"score": max(0, score), "level": level, "reasons": reasons}


# ---------------------------------------------------------------- HTTP 看板
def _toggle(k):
    global _auto_heal, _auto_pick, _auto_unify
    if k == "heal":
        _auto_heal = not _auto_heal
        log(f"自动纠正 {'开启' if _auto_heal else '关闭'}")
    elif k == "pick":
        _auto_pick = not _auto_pick
        log(f"自动优选 {'开启' if _auto_pick else '关闭'}")
    elif k == "unify":
        _auto_unify = not _auto_unify
        log(f"自动统一 {'开启' if _auto_unify else '关闭'}")
    return {"heal": _auto_heal, "pick": _auto_pick, "unify": _auto_unify}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/dashboard.html"):
            p = APP_DIR / "dashboard.html"
            if p.exists():
                return self._send(200, p.read_bytes(), "text/html; charset=utf-8")
            return self._send(404, "dashboard.html 缺失", "text/plain; charset=utf-8")
        if u.path == "/api/state":
            with _lock:
                payload = dict(_state)
                payload["history"] = list(_history)
                payload["events"] = list(_events)[:40]
                payload["rank"] = _rank
                payload["last_bench"] = _last_bench
                payload["auto_heal"] = _auto_heal
                payload["auto_pick"] = _auto_pick
                payload["auto_unify"] = _auto_unify
                payload["bench_running"] = _bench_running
            payload["health"] = score_health()
            return self._send(200, json.dumps(payload, ensure_ascii=False))
        if u.path == "/api/unify":
            qs = urllib.parse.parse_qs(u.query)
            kill = (qs.get("kill") or ["1"])[0] != "0"
            return self._send(200, json.dumps(unify(kill), ensure_ascii=False))
        if u.path == "/api/bench":
            threading.Thread(target=bench, daemon=True).start()
            return self._send(200, json.dumps({"ok": True}))
        if u.path == "/api/switch":
            qs = urllib.parse.parse_qs(u.query)
            node = (qs.get("node") or [""])[0]
            ok = apply_node(node)
            return self._send(200, json.dumps({"ok": ok}))
        if u.path == "/api/toggle":
            qs = urllib.parse.parse_qs(u.query)
            return self._send(200, json.dumps(_toggle((qs.get("k") or [""])[0])))
        return self._send(404, "{}")

    def do_POST(self):
        return self.do_GET()


# ---------------------------------------------------------------- CLI
def cmd_status():
    g = get_groups()
    mode = get_mode()
    node = (g.get("GLOBAL") or {}).get("now")
    pnode = (g.get("Proxies") or {}).get("now")
    print(f"\n模式      : {mode}")
    print(f"GLOBAL    : {node}")
    print(f"Proxies   : {pnode}")
    prev, *_ = sample_conns({})
    time.sleep(3)
    prev2, up, down, chains, _stale = sample_conns(prev)
    print(f"活跃连接  : {len(prev2)}")
    print(f"实时速率  : ↑{up/1024:6.0f} KB/s   ↓{down/1024:8.0f} KB/s")
    for c in chains[:3]:
        print(f"  {c['chain'][:56]:<56} {c['count']:>3} 条")
    ttfb, code = ttfb_test()
    print(f"TTFB      : {ttfb:.2f}s (http {code})")
    print(f"出口      : {egress_ip()}")


def cmd_diag():
    print("=" * 62)
    print("诊断：卡在网络还是卡在客户端")
    print("=" * 62)
    ttfb, code = ttfb_test()
    print(f"\n[网络] chatgpt.com 首字节 = {ttfb:.2f}s  http={code}")
    print(f"[网络] 出口 = {egress_ip()}")
    prev = {}
    _, up, down, chains, _stale = sample_conns(prev)
    time.sleep(4)
    _, up, down, chains, _stale = sample_conns(prev if prev else {})
    print(f"[网络] 实时 ↑{up/1024:.0f} KB/s ↓{down/1024:.0f} KB/s, 连接 {len(prev or [])}")
    node = (get_groups().get("GLOBAL") or {}).get("now")
    print(f"[节点] 当前 = {node}")
    spd, sent, ep = upload_test(node, 1024, 8)
    print(f"[节点] 上行实测 = {spd:.0f} KB/s  (已发 {sent//1024} KB, 经 {ep})")
    mem = proc_mem_mb("codex")
    if mem:
        print(f"[客户端] codex 系进程内存合计 = {mem} MB")
        if mem > 1500:
            print("         ↑ 超过 1.5GB，客户端本身已重，建议重启 Codex")
    print()
    if spd < TH_UP_MIN:
        print(">>> 判定：节点上行残疾 —— 这是 Codex 卡的直接原因（换节点即可）")
    elif ttfb > TH_TTFB_BAD:
        print(">>> 判定：链路首字节过慢 —— 线路质量差（换节点）")
    elif down == 0 and up == 0:
        print(">>> 判定：网络空闲且响应正常 —— 卡在客户端（Codex 进程/会话）")
    else:
        print(">>> 判定：链路正常")


def cmd_serve(no_bench=False):
    global _auto_bench
    if no_bench:
        _auto_bench = False
    print_preflight(preflight())
    load_rank()
    # 启动即纠正：模式被改回 global、或节点停在慢节点上，都拉回来
    if get_mode() != "rule":
        set_mode("rule")
        log("启动纠正：模式 → rule（国内/内网不再占用隧道）", "switch")
    if _auto_bench and _rank:
        b = pick_best()
        if b:
            apply_node(b)  # GLOBAL 与 Proxies 一起对齐，避免两个组不一致

    t = threading.Thread(target=collector, daemon=True)
    t.start()
    threading.Thread(target=health_check, daemon=True).start()

    def boot_bench():
        bench()
        if _auto_pick:
            b = pick_best()
            if b:
                apply_node(b)

    if not _auto_bench:
        log("已启用「只守护不测速」模式：锁定当前节点，不重测、不切换")
    elif _last_bench and time.time() - _last_bench < 1800:
        log(f"沿用 {int((time.time()-_last_bench)/60)} 分钟前的测速结果（30 分钟内有效）")
    else:
        threading.Thread(target=boot_bench, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", UI_PORT), Handler)
    log(f"看板已启动：http://127.0.0.1:{UI_PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("退出", "warn")


def main():
    ap = argparse.ArgumentParser(description="Clash 代理链路监控/自愈/优选")
    ap.add_argument("cmd", nargs="?", default="serve",
                    choices=["serve", "status", "bench", "diag", "apply", "fix", "check"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--no-bench", action="store_true",
                    help="只守护不测速：锁定当前节点，不重测、不切换（网络差时用）")
    a = ap.parse_args()

    if a.cmd == "serve" and a.no_bench:
        cmd_serve(no_bench=True)
    elif a.cmd == "fix":
        cmd_fix()
    elif a.cmd == "check":
        if not print_preflight(preflight()):
            print("自检通过，配置没问题。")
    elif a.cmd == "serve":
        cmd_serve()
    elif a.cmd == "status":
        cmd_status()
    elif a.cmd == "diag":
        cmd_diag()
    elif a.cmd == "bench":
        bench()
        print()
        print(f"{'节点':<14}{'地区':<6}{'上行 KB/s':>10}{'延迟 ms':>9}")
        for r in _rank[:12]:
            print(f"{short(r['node']):<14}{r['region']:<6}{r['up_kbps']:>10}{str(r['delay'] or '-'):>9}")
    elif a.cmd == "apply":
        if not a.arg:
            print("用法: python clash_opt.py apply 日本02")
            return
        want = a.arg
        pool = node_pool()
        nw = re.sub(r"\s+", "", short(want))
        hit = [n for n in pool if nw in re.sub(r"\s+", "", short(n))]
        if not hit:
            print(f"找不到匹配节点: {want}")
            print("候选:", ", ".join(short(n) for n in pool[:20]))
            return
        apply_node(hit[0])
        print(f"已切换到 {hit[0]}，出口 {egress_ip()}")


if __name__ == "__main__":
    main()

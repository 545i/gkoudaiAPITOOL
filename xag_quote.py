#!/usr/bin/env python3
"""口袋貴金屬行情終端 —— 互動設定、存檔、常駐監聽推送並轉出買賣價格。

資料來源（2026-10-22 經靜態分析口袋貴金屬 PC 版 v3.9.0 取得，無需認證）：
    報價推送  Socket.IO  https://wssof.gkoudai.com/?version=3.9.0  （transports=websocket）
                連上後伺服器發 connectResponse {"status":"1000","token":"<sid>"}
                送 subscribeRegister {"qid":[13,...],"token":"<sid>"}
                伺服器持續推 subscribeResponse '{"quote":{...}}'，quote.name 是 base64
    報價明細  GET /officialNetworkApi/GetQuotesDetail?id=<qid>   （開場種子值／休市品種）
    分類清單  GET /officialNetworkApi/GetQuoteTypesV4
    分類品種  GET /officialNetworkApi/typeQuotesByTidV4?tid=<tid>

推送需要 python-socketio[client]；沒裝、連不上或中途斷線過久，會自動退回 REST 輪巡
（app 自己也是這樣退），連線恢復後自動回到推送。

用法：
    python xag_quote.py                  # 互動終端（add → save → start）
    python xag_quote.py --run            # 讀設定檔直接常駐，適合開機自動執行
    python xag_quote.py --once           # 依設定取一次就結束
    python xag_quote.py --run --poll     # 強制走輪巡
    python xag_quote.py --run --plain    # 關看板，改逐行輸出（適合導到 log）
    python xag_quote.py --run --duration 3600   # 跑一小時後自動結束

互動終端指令：
    add <代碼|qid>     加入追蹤，例：add XAG / add LMESN / add 92
    rm <代碼|qid>      移除追蹤
    find <關鍵字>      搜尋品種，例：find 白银 / find LME
    list [分類關鍵字]  列出品種
    refresh            重新抓取上游品種表
    push <on|off>      常駐時監聽推送（off 改走 REST 輪巡）
    interval <秒>      輪巡間隔（輪巡模式與推送斷線退路共用）
    export <秒>        轉出節流，0 = 每次變動都寫
    dedupe <on|off>    買賣價沒變就不寫出（預設 on，避免 CSV 爆量）
    csv <路徑|off>     設定 CSV 轉出路徑
    latest <路徑|off>  設定最新價快照 JSON 路徑
    live <on|off>      原地刷新看板（off 改逐行輸出）
    show               顯示目前設定
    save               把設定寫入設定檔
    start              開始常駐（Ctrl+C 回到提示字元）
    once               取一次
    help / quit
"""

import argparse
import base64
import collections
import csv
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

BASE = "https://official.gkoudai.com/officialNetworkApi"
PUSH_URL = "https://wssof.gkoudai.com/?version=3.9.0"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "xag_quote.config.json")
SYMBOLS_PATH = os.path.join(HERE, "xag_quote.symbols.json")

# 用戶端識別取自 app 內建常數；不帶也能通，帶著較貼近原始行為
HEADERS = {
    "epid": "a6c89023-9472-4f30-81cf-8c7dea62aae5",
    "web_platform": "windows",
    "version": "3.9.0",
}

# Windows 主控台預設 cp950，編不出上游回傳的簡體字，先把輸出改成 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        # line_buffering 讓常駐模式導到檔案或管線時也能即時看到輸出
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, OSError):
        pass


def enable_ansi():
    """舊版 Windows 主控台預設不解譯 ANSI escape，開起來才能原地重繪。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        # ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(k32.SetConsoleMode(k32.GetStdHandle(-11), 7))
    except Exception:
        return False


# 上游品種表沒有計價單位，常見金屬自行補註，純顯示用
UNIT_HINTS = {
    "XAU": "USD/oz", "XAG": "USD/oz", "XPT": "USD/oz", "XPD": "USD/oz",
    "AU": "USD/oz", "CMSIA0": "USD/oz",
    "au6666": "CNY/g", "ag6666": "CNY/kg", "pt6666": "CNY/g", "pd6666": "CNY/g",
    "LMECU": "USD/t", "LMEAL": "USD/t", "LMEZN": "USD/t",
    "LMENI": "USD/t", "LMEPB": "USD/t", "LMESN": "USD/t",
}

DEFAULT_CONFIG = {
    "qids": [13],
    "push": True,
    "interval": 5.0,          # REST 輪巡間隔
    "export_interval": 5.0,   # 轉出節流，0 = 每次變動都寫
    "dedupe": True,           # 買賣價與現價都沒變就不寫出
    "csv": "xag_quote.csv",
    "latest": "xag_latest.json",
    "live": True,
}

# 紅漲綠跌，照台股／中國市場慣例
C_UP, C_DOWN, C_DIM, C_HEAD, C_OFF = "\x1b[91m", "\x1b[92m", "\x1b[90m", "\x1b[96m", "\x1b[0m"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

CSV_FIELDS = [
    "fetched_at", "source", "qid", "code", "name", "unit", "buy", "sell", "spread",
    "now_price", "open", "high", "low", "last_close", "inc_range", "quote_time",
]

NETWORK_ERRORS = (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError)
FRAME_SEC = 0.2          # 看板重繪頻率
PUSH_GRACE_SEC = 15.0    # 推送斷線超過這麼久才退回輪巡
SEED_WORKERS = 8         # 開場種子值的併發數


class QuoteError(RuntimeError):
    """上游回應無法解析。"""


def api_get(path, timeout=20):
    req = urllib.request.Request(f"{BASE}/{path}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    status = payload.get("status")
    if status is not None and str(status) != "1000":
        raise QuoteError(f"上游狀態 {status}: {payload.get('desc')}")
    return payload


def b64_name(value):
    """推送的 quote.name 是 base64 UTF-8；REST 回的是明文，解不動就原樣用。"""
    if not value:
        return ""
    try:
        return base64.b64decode(value).decode("utf-8")
    except Exception:
        return str(value)


# ---------------------------------------------------------------- 品種表

def refresh_symbols():
    """抓遍所有分類，建立 代碼 → qid 索引並寫入快取。各分類併發抓。"""
    types = api_get("GetQuoteTypesV4")
    categories = [(t.get("tid"), t.get("tname", "")) for t in (types.get("data") or [])]
    symbols = {}

    def load(entry):
        tid, tname = entry
        try:
            return tname, api_get(f"typeQuotesByTidV4?tid={tid}")
        except (QuoteError,) + NETWORK_ERRORS as e:
            print(f"  分類 {tname}({tid}) 抓取失敗：{e}", file=sys.stderr)
            return tname, None

    with ThreadPoolExecutor(max_workers=min(SEED_WORKERS, max(len(categories), 1))) as pool:
        for tname, page in pool.map(load, categories):
            if not page:
                continue

            def walk(node):
                for item in (node.get("list") or []):
                    code = item.get("code")
                    if code and item.get("id"):
                        symbols[code] = {
                            "qid": int(item["id"]),
                            "name": item.get("name", ""),
                            "category": tname,
                        }
                    walk(item)

            walk(page.get("data") or {})

    if not symbols:
        raise QuoteError("上游沒有回傳任何品種")

    cache = {"fetched_at": datetime.now().isoformat(timespec="seconds"),
             "symbols": symbols}
    with open(SYMBOLS_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    return cache


def load_symbols(auto_fetch=True):
    if os.path.exists(SYMBOLS_PATH):
        try:
            with open(SYMBOLS_PATH, encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("symbols"):
                return cache
        except (OSError, json.JSONDecodeError):
            pass
    if not auto_fetch:
        return {"fetched_at": None, "symbols": {}}
    print("首次使用，正在抓取上游品種表……")
    cache = refresh_symbols()
    print(f"  已快取 {len(cache['symbols'])} 個品種 → {SYMBOLS_PATH}")
    return cache


class Symbols:
    """代碼 / 名稱 / qid 之間的查詢。"""

    def __init__(self, cache):
        self.fetched_at = cache.get("fetched_at")
        self.by_code = cache.get("symbols") or {}
        self.by_qid = {v["qid"]: dict(v, code=c) for c, v in self.by_code.items()}

    def __len__(self):
        return len(self.by_code)

    def resolve(self, token):
        """把使用者輸入轉成 qid。純數字直接當 qid，其餘查代碼與名稱。"""
        token = token.strip()
        if not token:
            raise ValueError("要給一個代碼或 qid")
        if token.isdigit():
            return int(token)

        for code, info in self.by_code.items():
            if code.lower() == token.lower():
                return info["qid"]
        for code, info in self.by_code.items():
            if info["name"] == token:
                return info["qid"]

        hits = self.search(token)
        if len(hits) == 1:
            return hits[0][1]["qid"]
        if len(hits) > 1:
            preview = "、".join(c for c, _ in hits[:8])
            raise ValueError(f"「{token}」對到 {len(hits)} 個品種：{preview}"
                             f"{'…' if len(hits) > 8 else ''}，請給完整代碼")
        raise ValueError(f"品種表裡找不到「{token}」，可用 find 搜尋或 refresh 更新")

    def search(self, keyword):
        k = keyword.lower()
        return sorted(
            ((c, i) for c, i in self.by_code.items()
             if k in c.lower() or k in i["name"].lower() or k in i["category"].lower()),
            key=lambda x: x[0],
        )

    def code(self, qid):
        return self.by_qid.get(int(qid), {}).get("code", str(qid))

    def name(self, qid):
        return self.by_qid.get(int(qid), {}).get("name", "")

    def label(self, qid):
        info = self.by_qid.get(int(qid))
        if not info:
            return f"qid={qid}（品種表無此項）"
        unit = UNIT_HINTS.get(info["code"], "")
        return f"{info['code']} {info['name']}" + (f" [{unit}]" if unit else "")

    def unit(self, code):
        return UNIT_HINTS.get(code, "")


# ---------------------------------------------------------------- 一筆報價

def row_from_rest(qid, quotes, symbols):
    code = quotes.get("code", "")
    return {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": "rest",
        "qid": int(quotes.get("id", qid)),
        "code": code,
        "name": b64_name(quotes.get("name", "")),
        "unit": symbols.unit(code),
        "buy": quotes.get("buy", ""),
        "sell": quotes.get("sell", ""),
        "spread": quotes.get("spread", ""),
        "now_price": quotes.get("nowPrice", ""),
        "open": quotes.get("open", ""),
        "high": quotes.get("top", ""),
        "low": quotes.get("low", ""),
        "last_close": quotes.get("last_close", ""),
        "inc_range": quotes.get("incRange", ""),
        "quote_time": quotes.get("time", ""),
    }


def row_from_push(quote, symbols):
    """推送的 quote 欄位與 REST 幾乎同構，差在 name 是 base64、id 是字串。"""
    row = row_from_rest(int(quote.get("id", 0)), quote, symbols)
    row["source"] = "push"
    if not row["name"]:
        row["name"] = symbols.name(row["qid"])
    if not row["unit"]:
        row["unit"] = symbols.unit(row["code"] or symbols.code(row["qid"]))
    return row


def fetch(qid, symbols, timeout=15):
    payload = api_get(f"GetQuotesDetail?id={qid}", timeout=timeout)
    quotes = (payload.get("data") or {}).get("quotes")
    if not quotes:
        raise QuoteError(f"qid={qid} 沒有 quotes 欄位（代碼可能不存在）")
    return row_from_rest(qid, quotes, symbols)


# ---------------------------------------------------------------- 轉出

def resolve_path(path):
    """相對路徑一律相對於腳本目錄，常駐時工作目錄才不會影響輸出位置。"""
    return path if os.path.isabs(path) else os.path.join(HERE, path)


def append_csv(path, rows):
    full = resolve_path(path)
    need_header = not os.path.exists(full) or os.path.getsize(full) == 0
    with open(full, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if need_header:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def write_latest(path, rows):
    """原子覆寫最新價快照，供其他程式讀取。"""
    full = resolve_path(path)
    tmp = full + ".tmp"
    snapshot = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "quotes": {r["code"] or str(r["qid"]): r for r in rows},
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    os.replace(tmp, full)


# ---------------------------------------------------------------- 設定檔

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, json.JSONDecodeError) as e:
            print(f"設定檔讀取失敗，改用預設值：{e}", file=sys.stderr)
    cfg["qids"] = [int(q) for q in cfg.get("qids") or []]
    cfg["interval"] = float(cfg.get("interval") or DEFAULT_CONFIG["interval"])
    cfg["export_interval"] = float(cfg.get("export_interval", DEFAULT_CONFIG["export_interval"]))
    cfg["dedupe"] = bool(cfg.get("dedupe", True))
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"已存檔：{CONFIG_PATH}")


# ---------------------------------------------------------------- 顯示工具

def dwidth(text):
    """字串在等寬終端的顯示寬度，全角字算兩格，忽略 ANSI 控制碼。"""
    plain = ANSI_RE.sub("", str(text))
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in plain)


def pad(text, width, align="left"):
    gap = " " * max(0, width - dwidth(text))
    return gap + str(text) if align == "right" else str(text) + gap


def to_float(value):
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def arrow(new, old):
    n, o = to_float(new), to_float(old)
    if n is None or o is None or n == o:
        return f"{C_DIM}–{C_OFF}"
    return f"{C_UP}▲{C_OFF}" if n > o else f"{C_DOWN}▼{C_OFF}"


def age_text(age):
    if age is None:
        return f"{C_DIM}  --  {C_OFF}"
    if age < 60:
        # 刻意只到整秒：次秒級的數字每幀都在變，會讓看板的 frame diff 永遠不成立
        return f"{C_DIM}{age:3.0f}s前{C_OFF}"
    if age < 3600:
        return f"{C_DIM}靜止 {age / 60:.0f}分{C_OFF}"
    return f"{C_DIM}靜止 {age / 3600:.1f}時{C_OFF}"


# ---------------------------------------------------------------- 行情狀態

class Feed:
    """收集報價、判斷跳動方向、節流轉出。推送執行緒與畫面執行緒共用，故上鎖。"""

    PRICE_KEYS = ("buy", "sell", "now_price")

    def __init__(self, cfg, symbols, listener=None):
        self.cfg = cfg
        self.symbols = symbols
        self.listener = listener      # 每筆「有變動」的報價會回呼一次
        self.lock = threading.Lock()
        self.rows = {}                # qid -> 最新一筆
        self.arrows = {}              # qid -> (買方向, 賣方向)
        self.last_update = {}         # qid -> monotonic，最後一次收到報價
        self.last_change = {}         # qid -> monotonic，最後一次價格真的變了
        self.updates = 0
        self.changes = 0
        self.written = 0
        self.fails = 0
        self.bad_packets = 0
        self.last_error = ""
        self.recent = collections.deque(maxlen=600)   # 近期更新時間，用來算速率
        self.dirty = set()
        self._last_export = 0.0

    def offer(self, row):
        """放入一筆新報價；回傳是否為有效變動。"""
        now = time.monotonic()
        with self.lock:
            qid = row["qid"]
            old = self.rows.get(qid)
            changed = old is None or any(
                str(row.get(k)) != str(old.get(k)) for k in self.PRICE_KEYS
            )
            if old:
                self.arrows[qid] = (arrow(row["buy"], old["buy"]),
                                    arrow(row["sell"], old["sell"]))
            self.rows[qid] = row
            self.last_update[qid] = now
            self.updates += 1
            self.recent.append(now)
            if changed:
                self.changes += 1
                self.last_change[qid] = now
            # dedupe 關掉時，連沒變的報價也照寫出去
            if changed or not self.cfg.get("dedupe", True):
                self.dirty.add(qid)

        if changed and self.listener:
            self.listener(row)
        return changed

    def note_fail(self, text):
        with self.lock:
            self.fails += 1
            self.last_error = text

    def note_bad_packet(self):
        with self.lock:
            self.bad_packets += 1

    def rate(self, window=5.0):
        now = time.monotonic()
        with self.lock:
            hits = sum(1 for t in self.recent if now - t <= window)
        return hits / window

    def export(self, force=False):
        """把變動過的報價寫出去，依 export_interval 節流。回傳寫出筆數。"""
        now = time.monotonic()
        throttle = 0.0 if force else self.cfg.get("export_interval", 0.0)
        if not force and throttle and now - self._last_export < throttle:
            return 0

        with self.lock:
            pending = [self.rows[q] for q in self.dirty if q in self.rows]
            self.dirty.clear()
            snapshot = list(self.rows.values())
        if not pending:
            return 0

        self._last_export = now
        if self.cfg.get("csv"):
            append_csv(self.cfg["csv"], pending)
            with self.lock:
                self.written += len(pending)
        if self.cfg.get("latest"):
            write_latest(self.cfg["latest"], snapshot)
        return len(pending)

    def poll_once(self):
        """用 REST 併發取一輪所有追蹤品種。"""
        qids = list(self.cfg["qids"])
        if not qids:
            return 0

        def one(qid):
            try:
                return fetch(qid, self.symbols)
            except (QuoteError,) + NETWORK_ERRORS as e:
                self.note_fail(f"{self.symbols.label(qid)} {e}")
                return None

        with ThreadPoolExecutor(max_workers=min(SEED_WORKERS, len(qids))) as pool:
            rows = [r for r in pool.map(one, qids) if r]
        for row in rows:
            self.offer(row)
        return len(rows)


# ---------------------------------------------------------------- 看板

class Board:
    """原地重繪的終端看板。畫面沒變就不重寫，省下大半的終端輸出。"""

    def __init__(self, feed, cfg, symbols):
        self.feed = feed
        self.cfg = cfg
        self.symbols = symbols
        self.drawn = 0
        self.last_frame = None
        qids = cfg["qids"] or [0]
        self.w_code = max([dwidth(symbols.code(q)) for q in qids] + [6])
        self.w_name = max([dwidth(symbols.name(q)) for q in qids] + [8])

    def quote_lines(self):
        f, now, out = self.feed, time.monotonic(), []
        for qid in self.cfg["qids"]:
            with f.lock:
                row = f.rows.get(qid)
                a_buy, a_sell = f.arrows.get(qid, (f"{C_DIM}–{C_OFF}",) * 2)
                stamp = f.last_change.get(qid, f.last_update.get(qid))
            if not row:
                out.append(f"  {pad(self.symbols.code(qid), self.w_code)} "
                           f"{pad(self.symbols.name(qid), self.w_name)} "
                           f"{C_DIM}等待首次報價…{C_OFF}")
                continue

            mp = to_float((row["inc_range"] or "").rstrip("%"))
            mp_color = C_UP if (mp or 0) > 0 else C_DOWN if (mp or 0) < 0 else C_DIM
            out.append(
                f"  {pad(row['code'] or self.symbols.code(qid), self.w_code)}"
                f" {pad(row['name'], self.w_name)}"
                f"  買 {pad(row['buy'], 12, 'right')} {a_buy}"
                f"  賣 {pad(row['sell'], 12, 'right')} {a_sell}"
                f"  價差 {pad(row['spread'], 8, 'right')}"
                f"  {mp_color}{pad(row['inc_range'] or '-', 7, 'right')}{C_OFF}"
                f"  {age_text(now - stamp if stamp else None)}"
                f"  {C_DIM}{pad(row['unit'], 7)}{C_OFF}"
            )
        return out

    def lines(self, status):
        f = self.feed
        body = self.quote_lines()
        head = (f"{C_HEAD}口袋貴金屬行情{C_OFF}  {status['title']}   "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        with f.lock:
            updates, changes, written = f.updates, f.changes, f.written
            fails, bad, err = f.fails, f.bad_packets, f.last_error
        tail = (f" {status['state']}   更新 {updates} 筆 ({f.rate():.1f}/s)"
                f"   變動 {changes}   轉出 {written}")
        if fails:
            tail += f"   {C_UP}失敗 {fails}{C_OFF}"
        if bad:
            tail += f"   {C_DIM}略過封包 {bad}{C_OFF}"
        tail += f"   {C_DIM}Ctrl+C 停止{C_OFF}"

        width = max([dwidth(x) for x in body + [head, tail]] + [40])
        rule = C_DIM + "─" * width + C_OFF
        out = [head, rule] + body + [rule, tail]
        if err:
            out.append(f" {C_DIM}最近錯誤：{err[:width - 12]}{C_OFF}")
        return out

    def draw(self, status):
        lines = self.lines(status)
        if lines == self.last_frame:
            return
        self.last_frame = lines
        buf = [f"\x1b[{self.drawn}A"] if self.drawn else []
        for line in lines:
            buf.append("\x1b[2K" + line + "\n")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        self.drawn = len(lines)

    def close(self):
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()


# ---------------------------------------------------------------- 推送監聽

def make_push_client(feed, cfg, symbols):
    """建立 Socket.IO 客戶端並掛上事件。沒裝套件會丟 ImportError。"""
    import socketio  # 延後 import，沒裝才好退回輪巡

    sio = socketio.Client(reconnection=True, reconnection_delay=2,
                          reconnection_attempts=0, logger=False, engineio_logger=False)
    registered = set()

    # 伺服器偶爾送出 socket.io 解不開的封包，預設會在背景執行緒噴 traceback。
    # 必須覆寫 engineio 端的處理器：socket.io 在 __init__ 就把 bound method
    # 註冊進 engineio，改 client 的實例屬性不會生效。
    original = sio._handle_eio_message

    def guarded(data):
        try:
            original(data)
        except Exception:
            feed.note_bad_packet()

    sio.eio.on("message", handler=guarded)

    def register():
        """每條連線只註冊一次；connect 與 connectResponse 都可能先到。"""
        sid = sio.sid
        if not sid or sid in registered:
            return
        registered.add(sid)
        sio.emit("subscribeRegister", {"qid": list(cfg["qids"]), "token": sid})

    @sio.event
    def connect():
        register()

    @sio.on("connectResponse")
    def _on_connect_response(*_args):
        register()

    @sio.on("subscribeResponse")
    def _on_quote(payload):
        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
            quote = data.get("quote")
            if not quote:
                return
            feed.offer(row_from_push(quote, symbols))
        except (ValueError, TypeError, AttributeError) as e:
            feed.note_fail(f"推送解析失敗：{e}")

    return sio


def stop_client(sio):
    """shutdown 會停掉重連執行緒；只呼叫 disconnect 的話那條非 daemon 執行緒會卡住行程不退出。"""
    try:
        sio.shutdown()
    except Exception:
        try:
            sio.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------- 常駐主迴圈

def resident_loop(feed, cfg, symbols, board, deadline=None):
    """推送優先、斷線退回輪巡、連線恢復再回到推送的單一主迴圈。

    沒有 socketio 或連不上就直接從輪巡開始；中途斷線超過 PUSH_GRACE_SEC
    才開始輪巡補洞，socket 回來就自動停掉輪巡。
    """
    sio = None
    push_wanted = cfg.get("push", True)
    if push_wanted:
        try:
            sio = make_push_client(feed, cfg, symbols)
            sio.connect(PUSH_URL, transports=["websocket"], wait_timeout=10)
        except ImportError:
            print("沒有 python-socketio，改走 REST 輪巡。"
                  "要推送請裝：python -m pip install \"python-socketio[client]\"",
                  file=sys.stderr)
            sio = None
        except Exception as e:
            feed.note_fail(f"推送連線失敗：{e}")
            print(f"推送連線失敗，改走 REST 輪巡：{e}", file=sys.stderr)
            if sio:
                stop_client(sio)
            sio = None

    disconnected_since = None if sio else time.monotonic()
    next_poll = 0.0

    try:
        while True:
            if deadline and time.monotonic() >= deadline:
                break

            connected = bool(sio and sio.connected)
            now = time.monotonic()
            if connected:
                disconnected_since = None
            elif disconnected_since is None:
                disconnected_since = now

            # 推送斷線夠久（或根本沒有推送）就用輪巡補洞
            degraded = disconnected_since is not None and \
                now - disconnected_since >= (PUSH_GRACE_SEC if sio else 0.0)
            if degraded and now >= next_poll:
                feed.poll_once()
                feed.export(force=True)
                next_poll = now + cfg["interval"]

            feed.export()

            if board:
                if connected:
                    title = f"{C_UP}● 監聽推送{C_OFF}"
                    state = f"{C_HEAD}wssof.gkoudai.com{C_OFF}"
                elif degraded:
                    remain = max(0.0, next_poll - now)
                    filled = int(round(12 * (1 - remain / max(cfg["interval"], 0.001))))
                    state = (f"輪巡 {C_HEAD}[{'█' * filled}{'░' * (12 - filled)}]{C_OFF}"
                             f" {remain:4.1f}s")
                    title = f"{C_DIM}○ 輪巡模式{C_OFF}" if not sio \
                        else f"{C_DOWN}○ 推送中斷，輪巡中{C_OFF}"
                else:
                    title = f"{C_DIM}○ 重連中{C_OFF}"
                    state = f"{C_DIM}等待推送恢復{C_OFF}"
                board.draw({"title": title, "state": state})

            time.sleep(FRAME_SEC)
    except KeyboardInterrupt:
        pass
    finally:
        feed.export(force=True)
        if sio:
            stop_client(sio)


def run_resident(cfg, symbols, force_plain=False, force_poll=False, duration=None):
    live = cfg.get("live", True) and not force_plain and sys.stdout.isatty() and enable_ansi()

    def on_change(row):
        """逐行模式用：每筆真的有變動的報價印一行。"""
        print(f"{datetime.now().strftime('%H:%M:%S')}  {row['code']:<10}"
              f" 買 {row['buy']:>12}   賣 {row['sell']:>12}"
              f"   價差 {row['spread']:>8}  {row['source']}")

    effective = dict(cfg)
    if force_poll:
        effective["push"] = False
    feed = Feed(effective, symbols, listener=None if live else on_change)

    mode_text = "監聽推送" if effective.get("push", True) else f"輪巡 {effective['interval']} 秒"
    print(f"常駐中：{len(effective['qids'])} 個品種（{mode_text}）；Ctrl+C 停止")
    for qid in effective["qids"]:
        print(f"  追蹤 {symbols.label(qid)}")
    if effective.get("csv"):
        print(f"  CSV   → {resolve_path(effective['csv'])}")
    if effective.get("latest"):
        print(f"  快照  → {resolve_path(effective['latest'])}")
    if duration:
        print(f"  時限  {duration} 秒後自動結束")

    print("  取開場種子值……")
    feed.poll_once()
    feed.export(force=True)

    board = Board(feed, effective, symbols) if live else None
    if board:
        sys.stdout.write("\x1b[?25l")  # 藏游標，重繪時不會閃
    deadline = time.monotonic() + duration if duration else None
    try:
        resident_loop(feed, effective, symbols, board, deadline=deadline)
    finally:
        if board:
            board.close()
    print("已停止常駐")


def print_once(cfg, symbols):
    feed = Feed(cfg, symbols)
    feed.poll_once()
    with feed.lock:
        rows = [feed.rows[q] for q in cfg["qids"] if q in feed.rows]

    for row in rows:
        unit = f" {row['unit']}" if row["unit"] else ""
        print(f"  {row['name']} ({row['code']})   qid={row['qid']}{unit}")
        print(f"    買 {row['buy']:>12}   賣 {row['sell']:>12}   價差 {row['spread']}")
        print(f"    現價 {row['now_price']}   漲跌 {row['inc_range']}"
              f"   開 {row['open']}  高 {row['high']}  低 {row['low']}"
              f"  昨收 {row['last_close']}")
        print(f"    行情時間 {row['quote_time']}   取得於 {row['fetched_at']}")

    feed.export(force=True)
    if feed.last_error:
        print(f"  最近錯誤：{feed.last_error}", file=sys.stderr)
    return rows


# ---------------------------------------------------------------- 互動終端

def cmd_show(cfg, symbols):
    print(f"  設定檔    {CONFIG_PATH}{'' if os.path.exists(CONFIG_PATH) else '  (尚未存檔)'}")
    print(f"  品種表    {len(symbols)} 個代碼"
          f"{f'，更新於 {symbols.fetched_at}' if symbols.fetched_at else ''}")
    if cfg["qids"]:
        for qid in cfg["qids"]:
            print(f"  追蹤      {symbols.label(qid)}  (qid={qid})")
    else:
        print("  追蹤      (空)")
    print(f"  模式      {'監聽推送（斷線退回輪巡）' if cfg.get('push', True) else 'REST 輪巡'}")
    print(f"  輪巡間隔  {cfg['interval']} 秒")
    print(f"  轉出節流  {cfg['export_interval']} 秒"
          f"{'（每次變動都寫）' if not cfg['export_interval'] else ''}")
    print(f"  去重      {'開（價格沒變不寫）' if cfg.get('dedupe', True) else '關'}")
    print(f"  CSV       {resolve_path(cfg['csv']) if cfg.get('csv') else '(關閉)'}")
    print(f"  最新快照  {resolve_path(cfg['latest']) if cfg.get('latest') else '(關閉)'}")
    print(f"  看板      {'開' if cfg.get('live', True) else '關（逐行輸出）'}")


def parse_switch(arg):
    if arg.lower() in ("on", "yes", "true", "1", "開"):
        return True
    if arg.lower() in ("off", "no", "false", "0", "關"):
        return False
    raise ValueError("只接受 on 或 off")


def interactive(cfg, symbols):
    print("口袋貴金屬行情終端。輸入 help 看指令，quit 離開。")
    cmd_show(cfg, symbols)
    dirty = False

    while True:
        try:
            line = input("\nxag> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue

        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        try:
            if cmd in ("quit", "exit", "q"):
                if dirty:
                    print("設定尚未存檔，先 save 或再輸入一次 quit 放棄變更")
                    dirty = False
                    continue
                break

            elif cmd in ("help", "h", "?"):
                print(__doc__.split("互動終端指令：")[1].rstrip())

            elif cmd == "show":
                cmd_show(cfg, symbols)

            elif cmd == "refresh":
                symbols = Symbols(refresh_symbols())
                print(f"  品種表已更新：{len(symbols)} 個代碼")

            elif cmd in ("find", "list"):
                items = symbols.search(arg) if arg else sorted(symbols.by_code.items())
                cap = 40 if cmd == "find" else 60
                if not items:
                    print(f"  找不到含「{arg}」的品種")
                for code, info in items[:cap]:
                    mark = "*" if info["qid"] in cfg["qids"] else " "
                    print(f"  {mark} {info['qid']:>7}  {code:<12} {info['name']:<16}"
                          f" {info['category']}")
                if len(items) > cap:
                    print(f"  …另有 {len(items) - cap} 筆，請用更精確的關鍵字")
                if items:
                    print("  (* = 已追蹤)")

            elif cmd == "add":
                qid = symbols.resolve(arg)
                if qid in cfg["qids"]:
                    print(f"  {symbols.label(qid)} 已在追蹤清單")
                else:
                    # 先確認這個 qid 真的取得到報價，避免存進去才發現是空的
                    row = fetch(qid, symbols)
                    cfg["qids"].append(qid)
                    dirty = True
                    print(f"  已加入 {row['name']} ({row['code']})  qid={qid}"
                          f"   買 {row['buy']}  賣 {row['sell']}")
                    print("  記得 save 才會寫進設定檔")

            elif cmd in ("rm", "remove", "del"):
                qid = symbols.resolve(arg)
                if qid in cfg["qids"]:
                    cfg["qids"].remove(qid)
                    dirty = True
                    print(f"  已移除 {symbols.label(qid)}")
                else:
                    print(f"  {symbols.label(qid)} 不在追蹤清單")

            elif cmd in ("push", "live", "dedupe"):
                cfg[cmd] = parse_switch(arg)
                dirty = True
                print(f"  {cmd} = {'on' if cfg[cmd] else 'off'}")

            elif cmd in ("interval", "export"):
                key = "interval" if cmd == "interval" else "export_interval"
                sec = float(arg)
                if sec < 0 or (key == "interval" and sec <= 0):
                    print("  數值不合法")
                else:
                    cfg[key] = sec
                    dirty = True
                    print(f"  {key} 設為 {sec} 秒")

            elif cmd in ("csv", "latest"):
                if arg.lower() in ("off", "none", "關", "關閉"):
                    cfg[cmd] = ""
                    print(f"  {cmd} 轉出已關閉")
                elif arg:
                    cfg[cmd] = arg
                    print(f"  {cmd} 轉出路徑設為 {resolve_path(arg)}")
                else:
                    print(f"  用法：{cmd} <路徑|off>")
                    continue
                dirty = True

            elif cmd == "save":
                save_config(cfg)
                dirty = False

            elif cmd == "once":
                if not cfg["qids"]:
                    print("  追蹤清單是空的，先用 add 加入品種")
                else:
                    print_once(cfg, symbols)

            elif cmd in ("start", "run"):
                if not cfg["qids"]:
                    print("  追蹤清單是空的，先用 add 加入品種")
                else:
                    if dirty:
                        print("  提醒：設定尚未存檔，--run 會讀到舊設定")
                    run_resident(cfg, symbols)

            else:
                print(f"  不認識的指令「{cmd}」，輸入 help 看清單")

        except ValueError as e:
            print(f"  {e}")
        except (QuoteError,) + NETWORK_ERRORS as e:
            print(f"  操作失敗：{e}", file=sys.stderr)


# ---------------------------------------------------------------- 入口

def main():
    p = argparse.ArgumentParser(
        description="口袋貴金屬行情終端：互動設定、存檔、常駐監聽推送並轉出買賣價格",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="讀設定檔直接常駐")
    mode.add_argument("--once", action="store_true", help="依設定取一次就結束")
    p.add_argument("--poll", action="store_true", help="強制走 REST 輪巡，不監聽推送")
    p.add_argument("--plain", action="store_true", help="關掉看板，改逐行輸出")
    p.add_argument("--duration", type=float, metavar="SEC",
                   help="常駐指定秒數後自動結束")
    args = p.parse_args()

    cfg = load_config()

    try:
        symbols = Symbols(load_symbols(auto_fetch=not args.run))
    except (QuoteError,) + NETWORK_ERRORS as e:
        print(f"品種表取得失敗：{e}", file=sys.stderr)
        symbols = Symbols({"symbols": {}})

    if args.run or args.once:
        if not cfg["qids"]:
            print("設定檔沒有追蹤品種，先跑互動模式 add 並 save", file=sys.stderr)
            return 2
        if args.run:
            run_resident(cfg, symbols, force_plain=args.plain,
                         force_poll=args.poll, duration=args.duration)
            return 0
        return 0 if print_once(cfg, symbols) else 1

    interactive(cfg, symbols)
    return 0


if __name__ == "__main__":
    sys.exit(main())

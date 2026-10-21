# gkoudai-quote

貴金屬即時行情終端。監聽推送、原地刷新看板、把買賣價轉出成 CSV 與 JSON 快照。

資料來自口袋貴金屬（gkoudai）的公開行情端點，無需認證。涵蓋 373 個品種、10 個分類：現貨金銀鉑鈀、COMEX、滬金滬銀、LME 基本金屬、國際期貨、外匯、指數、原油等。

```
口袋貴金屬行情  ● 監聽推送   2026-10-22 03:49:29
─────────────────────────────────────────────────────────────────
  XAG    现货白银  買      57.8440 ▼  賣      57.8490 ▼  價差   0.0050    2.07%    0s前  USD/oz
  LMESN  LME锡     買      51020.0 –   賣      51260.0 –   價差    240.0    1.50%   16s前  USD/t
─────────────────────────────────────────────────────────────────
 wssof.gkoudai.com   更新 24 筆 (1.6/s)   變動 24   轉出 10   Ctrl+C 停止
```

## 安裝

只需要 Python 3.8+。推送要多一個套件，沒裝也能跑（自動退回 REST 輪巡）：

```bash
pip install -r requirements.txt
```

## 用法

```bash
python xag_quote.py                   # 互動終端
python xag_quote.py --run             # 讀設定檔直接常駐，適合開機自動執行
python xag_quote.py --once            # 取一次就結束
python xag_quote.py --run --poll      # 強制走輪巡
python xag_quote.py --run --plain     # 關看板，逐行輸出（適合導到 log）
python xag_quote.py --run --duration 3600   # 跑一小時後自動結束
```

互動終端：輸入代碼、存檔，之後 `--run` 就會常駐追蹤。

```
xag> add XAG
  已加入 现货白银 (XAG)  qid=13   買 57.2820  賣 57.2930
  記得 save 才會寫進設定檔
xag> add LMESN
  已加入 LME锡 (LMESN)  qid=92   買 51020.0  賣 51260.0
xag> save
已存檔：xag_quote.config.json
xag> start
```

`add` 會先實際取一次報價確認拿得到，才放進追蹤清單。認不出來的輸入會給建議，模糊比對撞到多筆時會列出候選要你給完整代碼。

| 指令 | 作用 |
|---|---|
| `add <代碼\|qid>` | 加入追蹤，例 `add XAG` / `add LMESN` / `add 92` |
| `rm <代碼\|qid>` | 移除追蹤 |
| `find <關鍵字>` | 搜尋品種，例 `find 白银` / `find LME` |
| `list [分類關鍵字]` | 列出品種 |
| `refresh` | 重新抓取上游品種表 |
| `push <on\|off>` | 常駐時監聽推送（off 改走輪巡） |
| `interval <秒>` | 輪巡間隔 |
| `export <秒>` | 轉出節流，0 = 每次變動都寫 |
| `dedupe <on\|off>` | 買賣價沒變就不寫出（預設 on） |
| `csv <路徑\|off>` | CSV 轉出路徑 |
| `latest <路徑\|off>` | 最新價快照路徑 |
| `live <on\|off>` | 原地刷新看板 |
| `show` / `save` / `start` / `once` | 顯示設定 / 存檔 / 常駐 / 取一次 |

## 轉出

兩種輸出同時進行：

- **`xag_quote.csv`** — 持續累積的歷史，UTF-8 BOM，Excel 直開。`source` 欄位區分 `push`（推送）與 `rest`（輪巡／開場種子值）。
- **`xag_latest.json`** — 最新價快照，寫 `.tmp` 再 `os.replace` 原子覆寫，其他程式讀不會讀到半截。

```json
{
  "updated_at": "2026-10-22T03:34:24",
  "quotes": {
    "XAG": { "code": "XAG", "name": "现货白银", "unit": "USD/oz",
             "buy": "57.7070", "sell": "57.7190", "spread": "0.0120",
             "now_price": "57.7070", "inc_range": "2.07%", "source": "push" }
  }
}
```

設定檔裡的相對路徑一律相對於腳本目錄，不是工作目錄 —— 塞進工作排程器開機自動跑，輸出不會跑到別的地方去。

## 運作方式

推送優先，走 Socket.IO：

```
連線   https://wssof.gkoudai.com/?version=3.9.0   transports=["websocket"]
收     connectResponse   {"status":"1000","token":"<sid>"}
送     subscribeRegister {"qid":[13,92],"token":"<sid>"}
收     subscribeResponse '{"quote":{...}}'    ← 持續推，實測約 1–3 筆/秒/品種
```

明細與開場種子值走 REST：

```
GET https://official.gkoudai.com/officialNetworkApi/GetQuotesDetail?id=13
GET https://official.gkoudai.com/officialNetworkApi/GetQuoteTypesV4
GET https://official.gkoudai.com/officialNetworkApi/typeQuotesByTidV4?tid=3
```

幾個實作上的注意點：

- 參數名是 `id` 不是 `qid`，用 `qid` 會回 `1003 Parameters Error`。
- 推送的 `quote.name` 是 base64 UTF-8，`id` 是字串；REST 回的 name 是明文。
- 休市品種（例如非交易時段的 LME）不推送，所以開場先用 REST 補一輪種子值，看板才不會空著。
- 斷線超過 15 秒會自動退回輪巡補洞，socket 回來再自動切回推送。
- 伺服器偶爾送出 socket.io 解不開的封包。攔它必須覆寫 `sio.eio.on("message", ...)`，改 `sio._handle_eio_message` 無效 —— socket.io 在 `__init__` 就把 bound method 註冊進 engineio 了。這類封包會被計數後略過，串流不受影響。
- 結束要 `sio.shutdown()`，只 `disconnect()` 的話重連執行緒不是 daemon，行程不會退出。

## 計價單位

上游不提供計價單位，常見金屬的單位標註（`USD/oz`、`CNY/g`、`USD/t`）是依市場慣例補的，純顯示用。要拿去算錢前，建議先拿一筆對帳驗證刻度。

## 授權

MIT

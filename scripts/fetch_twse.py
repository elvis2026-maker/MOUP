#!/usr/bin/env python3
"""
台股權證標的篩選腳本 V27
==============================
V27 修正 V26 的問題：移除 get_warrant_detail 的 fallback 個股查詢（境外 IP 全 422，浪費 req）
  1. TaiwanStockWarrant 全量查詢 422 → fallback 977 支 → 粗篩 402 超限
  2. 402 發生時直接放棄已有結果 → 改為繼續精篩已存活標的
  3. 粗篩無硬性上限 → 加 SCAN_HARD_LIMIT=450 截斷保護

  V21 的問題：
    電子股 ∩ 有認購權證 = 974 支，第一階段粗篩每支打 1 req，
    974 req 就超過 FinMind 免費帳號 600/hr 的上限，
    第二階段根本沒機會跑，每天都是空結果。

  V27 解法：三階段架構，大幅減少 API 請求數
    ① TaiwanStockInfoWithWarrant  → 電子股 meta（名稱/市場）  1 req
    ② TaiwanStockWarrant 近15天   → 真正有活躍認購交易的電子標的  1 req
       （這一步直接把候選池從 974 縮減到約 150~250 支，
         同時快取完整權證明細，最後直接查表不用再打API）
    ③ TaiwanStockPrice 近4天      → 量價粗篩（存活約 50~80 支）  ~150~250 req
    ④ TaiwanStockPrice 近35天     → 完整歷史精篩                 ~50~80 req
    ⑤ 三大法人 + 融資（Top20×2）                                  ~40 req
    合計：~242~372 req，完全在 600/hr 內，且有大量餘裕

  電子 8 大產業類別（同 V20/V21）：
    半導體業、電腦及週邊設備業、光電業、通信網路業、
    電子零組件業、電子通路業、資訊服務業、其他電子業
"""

import requests, json, time, os, statistics
from datetime import datetime, timedelta, timezone
from collections import Counter

TZ_TW       = timezone(timedelta(hours=8))
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
TOP_N       = 15
# V28：歷史保留機制 —— 保留期設 > 輪替天數，確保「這一輪」掃過的股票在下一輪掃到之前都還留著
HISTORY_MAX_AGE_DAYS = 6
# V29：對應 fetch-data.yml 排定的 5 次排程（台灣 18/19/20/21/22 點）
# 一天內把電子股切成 5 份分批掃完，不用等 3 天才輪完一圈
DAILY_RUN_SLOTS = 5
FM_URL      = "https://api.finmindtrade.com/api/v4/data"
# ── 多 TOKEN 輪詢（支援最多 4 個帳號）────────────────────────
# GitHub Secrets 設定：FINMIND_TOKEN_1 / _2 / _3 / _4
# 某個 TOKEN 觸發 402 時自動切換到下一個，全部用完才真正停止
_ALL_TOKENS = [
    os.environ.get("FINMIND_TOKEN_1", ""),
    os.environ.get("FINMIND_TOKEN_2", ""),
    os.environ.get("FINMIND_TOKEN_3", ""),
    os.environ.get("FINMIND_TOKEN_4", ""),
]
TOKENS     = [t for t in _ALL_TOKENS if t.strip()]   # 過濾空值
TOKEN      = TOKENS[0] if TOKENS else ""              # 目前使用的 TOKEN
_TOKEN_IDX = 0                                        # 目前 TOKEN 的索引

def rotate_token():
    """切換到下一個可用 TOKEN，回傳 True 代表切換成功"""
    global TOKEN, _TOKEN_IDX
    _TOKEN_IDX += 1
    if _TOKEN_IDX < len(TOKENS):
        TOKEN = TOKENS[_TOKEN_IDX]
        print(f"  ↻ TOKEN_{_TOKEN_IDX} 已超限，切換到 TOKEN_{_TOKEN_IDX + 1}")
        return True
    print(f"  ✗ 所有 {len(TOKENS)} 個 TOKEN 都已超限")
    return False

ELECTRONICS_CATEGORIES = {
    "半導體業", "電腦及週邊設備業", "光電業", "通信網路業",
    "電子零組件業", "電子通路業", "資訊服務業", "其他電子業",
}
EXCLUDE_SIDS = {"9999", "0000"}

def tw_now():
    return datetime.now(TZ_TW)

def date_back(days):
    return (tw_now() - timedelta(days=days)).strftime("%Y-%m-%d")

def sf(v, d=0.0):
    try:    return float(str(v).replace(",","").strip())
    except: return d

def si(v, d=0):
    try:    return int(str(v).replace(",","").strip())
    except: return d

def hdrs():
    h = {}
    if TOKEN: h["Authorization"] = f"Bearer {TOKEN}"
    return h

def current_token_label():
    return f"TOKEN_{_TOKEN_IDX + 1}" if TOKENS else "匿名"

# ── 共用請求 ─────────────────────────────────────────────────
def fm(dataset, data_id=None, start_date=None, end_date=None, retries=3):
    params = {"dataset": dataset}
    if data_id:    params["data_id"]    = data_id
    if start_date: params["start_date"] = start_date
    if end_date:   params["end_date"]   = end_date
    for i in range(retries):
        try:
            r = requests.get(FM_URL, params=params, headers=hdrs(), timeout=25)
            if r.status_code == 402:
                print(f"  ! FinMind 402：{current_token_label()} 已超限，嘗試切換...")
                # 嘗試輪詢剩餘 TOKEN，每個都重試這次請求
                while rotate_token():
                    try:
                        rn = requests.get(FM_URL, params=params, headers=hdrs(), timeout=25)
                        if rn.status_code == 200:
                            dn = rn.json()
                            return (dn.get("data", []) if dn.get("status") == 200 else []), False
                        elif rn.status_code == 402:
                            print(f"  ! {current_token_label()} 也超限，繼續切換...")
                            continue
                        elif rn.status_code in (400, 404, 422):
                            return [], False  # 這支股票本身沒資料，不是 TOKEN 問題
                    except Exception as e2:
                        print(f"  ! 切換後重試失敗：{e2}")
                        break
                print("  ! 所有 TOKEN 已超限，停止後續請求")
                return [], True
            if r.status_code in (400, 404, 422):
                if r.status_code == 422:
                    print(f"  ! fm {dataset}/{data_id} → 422（無此資料，跳過）")
                return [], False
            r.raise_for_status()
            d = r.json()
            return (d.get("data", []) if d.get("status") == 200 else []), False
        except Exception as e:
            if i < retries - 1: time.sleep(1.5 * (i + 1))
            else: print(f"  ! fm {dataset}/{data_id} → {e}")
    return [], False

def fm1(dataset, data_id=None, start_date=None, end_date=None):
    return fm(dataset, data_id, start_date, end_date)

# ── Step①：電子股 meta（名稱/市場別）1 req ──────────────────
def fetch_electronics_meta():
    """
    從 TaiwanStockInfoWithWarrant 取得電子股的 meta（名稱/市場別）。
    同時過濾掉非電子股，回傳 dict {sid: {name, market}}。
    """
    data, _ = fm1("TaiwanStockInfoWithWarrant")

    # V34：這支資料集本身確定拿得到資料（每次都成功），
    # 但目前只挑 type in (twse, tpex) 的列，其餘全部丟棄。
    # 既然 TaiwanStockInfoWithWarrantSummary 那條路完全打不通，
    # 先看看被丟掉的那些列裡，會不會其實混著權證資料（不同 type 值）。
    type_count = Counter(str(row.get("type","")).strip() for row in data)
    print(f"     [診斷] TaiwanStockInfoWithWarrant 共 {len(data)} 筆，type 分布：{dict(type_count.most_common(10))}")
    other_sample = next((row for row in data if str(row.get("type","")).strip() not in ("twse","tpex")), None)
    if other_sample:
        print(f"     [診斷] 非 twse/tpex 的樣本列完整欄位：{other_sample}")
    else:
        print("     [診斷] 沒有非 twse/tpex 的列，這支資料集應該真的只有股票清單，沒有權證明細")

    elec_meta = {}
    all_meta  = {}
    for row in data:
        sid = str(row.get("stock_id","")).strip()
        t   = str(row.get("type","")).strip()
        if not (sid.isdigit() and len(sid) == 4): continue
        if t not in ("twse", "tpex"): continue
        all_meta[sid] = {
            "name":   str(row.get("stock_name", sid)).strip(),
            "market": "tse" if t == "twse" else "otc",
        }
    return all_meta

def fetch_electronics_sids():
    data, _ = fm1("TaiwanStockInfo")
    elec_sids = set()
    cat_count = {}
    for row in data:
        sid = str(row.get("stock_id","")).strip()
        if not (sid.isdigit() and len(sid) == 4): continue
        cat = str(row.get("industry_category","")).strip()
        t   = str(row.get("type","")).strip()
        if t not in ("twse", "tpex"): continue
        cat_count[cat] = cat_count.get(cat, 0) + 1
        if cat in ELECTRONICS_CATEGORIES:
            elec_sids.add(sid)
    print(f"  → 電子股（上市+上櫃）：{len(elec_sids)} 支")
    for cat in ELECTRONICS_CATEGORIES:
        n = cat_count.get(cat, 0)
        if n > 0:
            print(f"     {cat}：{n} 支")
    return elec_sids

# ── Step②：活躍認購權證（1 req，同時快取明細）───────────────
WARRANT_DETAIL_CACHE = {}  # sid -> {w_code: {...}}

# ── V30：漏跑補償機制 ────────────────────────────────────────
# 每次寫 stocks.json 時，同時記一份「今天第幾份排程已經掃完」的進度。
# 下次執行時，如果發現排程時間點之前有沒被記錄完成的份數（代表那一輪
# 沒跑、或跑到一半被 402 中斷），這次就多補掃「最近一份沒完成的」。
def _load_scan_progress():
    if not os.path.exists(OUTPUT_PATH):
        return None
    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        return None
    return old.get("scan_progress")

def _resolve_slot_and_catchup(today_dt, n_slots):
    """
    回傳 (target_slots, prev_progress)
    target_slots：這次要掃的份數 index 列表（通常是 [今天的份數]，
                  若偵測到之前有漏掉的份數，會多補一份變成 2 個）
    """
    today_str = today_dt.strftime("%Y-%m-%d")
    prev = _load_scan_progress()
    if prev and prev.get("date") == today_str:
        slots_done = set(prev.get("slots_done", []))
    else:
        slots_done = set()   # 換日了，重新開始記錄

    cur_slot = (today_dt.hour - 18) % n_slots

    # 找出「今天、在這一份之前」還沒被標記完成的份數 → 視為漏跑
    missed = [i for i in range(cur_slot) if i not in slots_done]
    catchup = missed[-1:]  # 一次只補最近的 1 份，避免額度一次燒太多

    target_slots = sorted(set(catchup + [cur_slot]))
    if catchup:
        print(f"     ⚠ 偵測到第 {catchup[0] + 1}/{n_slots} 份先前沒跑完，這次一併補掃")
    return target_slots, today_str, slots_done

def fetch_active_warrant_targets(elec_sids, today_dt):
    """
    V27 修正 V25 的兩個問題：
      FinMind TaiwanStockWarrant 不支援全量查詢（不帶 stock_id），
      會回傳 422 → fallback 到 977 支 → 粗篩超過 600 req → 402 超限。

    V31：找到問題根源 —— TaiwanStockWarrantDetail 這個 dataset 名稱
      在 FinMind 目前公開文件裡根本查不到（不是被擋，是這個名字可能
      本來就不存在／已改名），才會每次都 422。
      正確、目前文件上找得到、且欄位對得上的 dataset 是
      TaiwanStockInfoWithWarrantSummary（可不帶 data_id 全量查）：
        stock_id(權證代號) / target_stock_id(標的股) / type(權證類型)
        / end_date(到期日) / fulfillment_price(履約價) / exercise_ratio(行使比例)
        / close(權證價) / target_close(標的股價)
      注意：這個 dataset 沒有槓桿(EffectiveLeverage)、Delta、買賣價、成交量，
      這些是 FinMind 付費才有的即時籌碼資料，免費版本來就拿不到。
      改成用「履約價 vs 正股價」算價內外程度、用「正股價/(權證價×行使比例)」
      粗估槓桿倍數（非精確的有效槓桿，只能當參考）。

    V27 解法：
      ① 改查 TaiwanStockInfoWithWarrantSummary（支援全量，1 req）取得認購標的清單
      ② 若仍失敗，直接 fallback 限量電子股（不再逐股查，省 30 req）
      ③ 最終 fallback：限量 400 支（硬性保護）

    回傳：(scan_sids, active_set, slot_meta)
      slot_meta 是 dict，main() 用來決定這輪掃完後要不要把「份數」標記完成
    """
    end   = today_dt.strftime("%Y-%m-%d")

    def is_call_type(type_str):
        """認購權證判斷：目前不確定 FinMind 這個欄位實際編碼是文字還是代碼，
        兩種都比對，寧可保守（比對不到就當作不確定，不強行歸類為認購）。"""
        t = str(type_str).strip()
        if "認售" in t or t in ("02", "put", "Put", "PUT"): return False
        if "認購" in t or t in ("01", "call", "Call", "CALL"): return True
        return None  # 無法判斷

    def build_cache_and_set(data_rows, elec_sids):
        cand_count = {}    # sid -> 符合條件的權證檔數（用來排優先序，取代舊版用成交量排序）
        active_set = set()
        type_seen  = Counter()  # 記錄實際看到的 type 值，方便之後對照真實編碼

        for row in data_rows:
            type_raw = row.get("type", "")
            type_seen[str(type_raw)] += 1
            is_call = is_call_type(type_raw)
            if is_call is False:
                continue  # 確定是認售，跳過
            # is_call is None（無法判斷）先當作可能是認購，避免因為編碼猜錯而漏掉全部標的

            sid = str(row.get("target_stock_id", "")).strip()
            if not (sid.isdigit() and len(sid) == 4): continue
            if sid not in elec_sids: continue

            try:
                w_code       = str(row.get("stock_id", "")).strip()
                expire_str   = str(row.get("end_date", "")).strip()
                fulfill_price = sf(row.get("fulfillment_price", 0))
                ex_ratio      = sf(row.get("exercise_ratio", 0)) or 1.0
                w_close       = sf(row.get("close", 0))
                target_close  = sf(row.get("target_close", 0))
                if not w_code or not expire_str or fulfill_price <= 0: continue
                expire_dt = datetime.strptime(expire_str[:10], "%Y-%m-%d")

                WARRANT_DETAIL_CACHE.setdefault(sid, {})
                WARRANT_DETAIL_CACHE[sid][w_code] = {
                    "code":             w_code,
                    "expire_dt":        expire_dt,
                    "fulfillment_price": fulfill_price,
                    "exercise_ratio":    ex_ratio,
                    "warrant_close":     w_close,        # 抓取當下的權證價，用來粗估槓桿
                    "target_close_snap": target_close,    # 抓取當下的正股價快照
                }
                active_set.add(sid)
                cand_count[sid] = cand_count.get(sid, 0) + 1
            except Exception:
                pass

        # 把實際看到的 type 編碼印出來，方便下次真的跑起來後對照是不是猜對了
        if type_seen:
            sample = ", ".join(f"{k!r}×{v}" for k, v in type_seen.most_common(5))
            print(f"     [type 欄位實際值抽樣] {sample}")
        return cand_count, active_set

    # ── 方法一：TaiwanStockInfoWithWarrantSummary 全量查（1 req）──
    # V32：這支是「快照型」資料集（跟 TaiwanStockInfo 系列同類），
    # FinMind 官方範例只帶「單一 start_date」、不帶 end_date。
    # 上一版帶了 start_date+end_date 的區間查詢，實測回傳空陣列（log 顯示無 422 錯誤訊息，
    # 代表 200 成功但資料是空的，很可能是這個查詢方式不對，不是又被擋）。
    data, hit = fm1("TaiwanStockInfoWithWarrantSummary", start_date=end)
    print(f"     [診斷] TaiwanStockInfoWithWarrantSummary(start_date={end}) → hit={hit}, 回傳筆數={len(data) if data else 0}")

    # V33：實測發現帶 start_date 一樣回傳 0 筆（不是 422，是 200 但真的空）。
    # 猜測文件講的「data_id、start_date 皆可不帶參數」，意思其實是「完全不帶」
    # 才會給全市場快照，不是「帶一個 start_date 當篩選條件」。這裡加一次備援嘗試。
    if not hit and not data:
        data, hit = fm1("TaiwanStockInfoWithWarrantSummary")
        print(f"     [診斷] TaiwanStockInfoWithWarrantSummary(無參數) → hit={hit}, 回傳筆數={len(data) if data else 0}")

    if not hit and data:
        cand_count, active_set = build_cache_and_set(data, elec_sids)
        if active_set:
            sorted_sids  = sorted(active_set, key=lambda s: -cand_count.get(s,0))
            cached_count = sum(len(v) for v in WARRANT_DETAIL_CACHE.values())
            print(f"  → [TaiwanStockInfoWithWarrantSummary] 電子股有活躍認購：{len(sorted_sids)} 支（快取 {cached_count} 檔）")
            # 這個分支本來就查全量，不受「份數分批」限制，視為今天全部份數都掃完
            return sorted_sids, active_set, {"mode": "full"}

    # ── TaiwanStockInfoWithWarrantSummary 失敗 → 直接 fallback（不再逐股查）──
    # V27：移除「方法二」的 30 支批次查詢
    # 原因：TaiwanStockWarrant 個股查詢在境外 IP 也是全部 422（log 可見），
    #       白白消耗 30 req，不如直接進 fallback，保留配額給粗篩和精篩用
    # V29：改成「當天分批」而不是「跨日輪替」
    # 原因：4 個 TOKEN 合計額度足夠一天內把電子股全部掃完，不用拖到 3 天後。
    #       fetch-data.yml 一天排定跑 5 次（18~22 點），把電子股平均切成 5 份，
    #       每次排程掃其中一份，晚上最後一次跑完，剛好等於「今天全部電子股都掃過一輪」，
    #       疊加歷史 pool 後隔天早上看到的就是當天完整的總整理。
    # V30：加入漏跑補償 —— 如果偵測到前面某份沒標記完成，這輪多補掃一份
    print("  ! TaiwanStockInfoWithWarrantSummary 無資料，直接 fallback 用限量電子股（當天分批）")
    fallback_all = sorted(
        [s for s in elec_sids if s not in EXCLUDE_SIDS],
        key=lambda s: s   # 清單本身仍按代號排序，只用分批 offset 移動起點
    )
    total = len(fallback_all)
    limit = 400   # 單輪保護上限，避免異常情況下一次掃太多把額度燒光

    if total <= limit:
        fallback  = fallback_all
        slot_meta = {"mode": "full"}   # 全部都掃了，等同今天全份數完成
        print(f"     電子股僅 {total} 支（≤{limit}），全部掃描，不需分批")
    else:
        n_slots = DAILY_RUN_SLOTS
        base = total // n_slots
        rem  = total % n_slots
        bounds, pos = [], 0
        for i in range(n_slots):
            size = base + (1 if i < rem else 0)   # 餘數平均塞進前幾份
            bounds.append((pos, pos + size))
            pos += size

        target_slots, today_str, slots_done = _resolve_slot_and_catchup(today_dt, n_slots)

        fallback = []
        ranges_desc = []
        for slot_idx in target_slots:
            s_idx, e_idx = bounds[slot_idx]
            fallback.extend(fallback_all[s_idx:e_idx])
            ranges_desc.append(f"第{slot_idx+1}份({s_idx}~{e_idx-1})")
        fallback = fallback[:limit] if len(fallback) > limit else fallback

        slot_meta = {
            "mode":          "fallback",
            "date":          today_str,
            "target_slots":  target_slots,
            "total_slots":   n_slots,
            "prev_done":     sorted(slots_done),
        }
        print(f"     電子股共 {total} 支，今天排程共 {n_slots} 份，本輪掃 "
              f"{'、'.join(ranges_desc)}，合計 {len(fallback)} 支")

    return fallback, set(fallback), slot_meta

# ── 第一階段：快速粗篩 ──────────────────────────────────────
def quick_filter(scan_sids, stock_meta, quick_start, end_date):
    survivors = []
    stop = False
    req  = 0
    for idx, sid in enumerate(scan_sids):
        if stop: break
        if (idx + 1) % 100 == 0:
            print(f"  ... 粗篩 {idx+1}/{len(scan_sids)}  存活:{len(survivors)}  req:{req}")

        data, hit = fm1("TaiwanStockPrice", sid, quick_start, end_date)
        req += 1
        if hit: stop = True; break
        time.sleep(0.05)

        if not data: continue
        rows = []
        for row in data:
            try:
                c = sf(row.get("close", 0))
                if c <= 0: continue
                rows.append({
                    "date":   str(row["date"]),
                    "open":   sf(row.get("open",  c)),
                    "high":   sf(row.get("max",   c)),
                    "low":    sf(row.get("min",   c)),
                    "close":  c,
                    "spread": sf(row.get("spread", 0)),
                    "volume": si(row.get("Trading_Volume", 0)),
                })
            except: continue
        if not rows: continue
        rows.sort(key=lambda x: x["date"])
        today_p = rows[-1]

        last_dt = datetime.strptime(today_p["date"], "%Y-%m-%d").date()
        ref_dt  = datetime.strptime(end_date, "%Y-%m-%d").date()
        if (ref_dt - last_dt).days > 5: continue

        info   = stock_meta.get(sid, {})
        market = info.get("market", "tse")
        close  = today_p["close"]
        volume = today_p["volume"]
        spread = today_p["spread"]

        if close < 50 or close > 3000:                        continue
        if volume < (50000 if market == "otc" else 100000):   continue
        if spread < 0:                                         continue

        survivors.append({
            "sid":       sid,
            "info":      info,
            "today_p":   today_p,
            "data_date": today_p["date"],
        })

    print(f"  → 粗篩完成：{len(survivors)} 支存活（掃 {len(scan_sids)} 支，用 {req} req）")
    return survivors, req, stop

# ── 第二階段：完整歷史 ──────────────────────────────────────
def fetch_price_full(sid, start_date, end_date):
    data, hit = fm1("TaiwanStockPrice", sid, start_date, end_date)
    if hit: return None, True
    result = []
    for row in data:
        try:
            c = sf(row.get("close", 0))
            if c <= 0: continue
            result.append({
                "date":   str(row["date"]),
                "open":   sf(row.get("open",  c)),
                "high":   sf(row.get("max",   c)),
                "low":    sf(row.get("min",   c)),
                "close":  c,
                "spread": sf(row.get("spread", 0)),
                "volume": si(row.get("Trading_Volume", 0)),
            })
        except: continue
    return sorted(result, key=lambda x: x["date"]), False

# ── 三大法人 & 融資 ─────────────────────────────────────────
def fetch_inst(sid, start_date, end_date):
    data, hit = fm1("TaiwanStockInstitutionalInvestorsBuySell", sid, start_date, end_date)
    if hit: return {}, True
    by_date = {}
    for row in data:
        date = str(row.get("date",""))
        name = str(row.get("name",""))
        net  = si(row.get("buy",0)) - si(row.get("sell",0))
        if date not in by_date:
            by_date[date] = {"foreign_net":0,"trust_net":0,"dealer_net":0}
        if name == "Foreign_Investor":
            by_date[date]["foreign_net"] += net
        elif name == "Foreign_Dealer_Self":
            by_date[date]["foreign_net"] += net
        elif name == "Investment_Trust":
            by_date[date]["trust_net"] += net
        elif name in ("Dealer_self", "Dealer_Hedging"):
            by_date[date]["dealer_net"] += net
    if not by_date: return {}, False
    dates  = sorted(by_date.keys())
    latest = dates[-1]
    v = by_date[latest]

    # V35：連續買超/賣超天數 —— 同一批資料裡本來就有，不用多打 API。
    # 正數＝連續買超天數，負數＝連續賣超天數。
    streak = 0
    for d in reversed(dates):
        net = by_date[d]["foreign_net"] + by_date[d]["trust_net"] + by_date[d]["dealer_net"]
        if streak == 0:
            if net > 0:   streak = 1
            elif net < 0: streak = -1
            else:         break
        elif (streak > 0 and net > 0) or (streak < 0 and net < 0):
            streak += 1 if streak > 0 else -1
        else:
            break

    return {
        "foreign_net": v["foreign_net"]//1000,
        "trust_net":   v["trust_net"]//1000,
        "dealer_net":  v["dealer_net"]//1000,
        "total_net":   (v["foreign_net"]+v["trust_net"]+v["dealer_net"])//1000,
        "buy_streak_days": streak,
    }, False

def fetch_margin(sid, start_date, end_date):
    data, hit = fm1("TaiwanStockMarginPurchaseShortSale", sid, start_date, end_date)
    if hit: return {}, True
    if not data: return {}, False
    latest = sorted(data, key=lambda x: x.get("date",""))[-1]
    bal = si(latest.get("MarginPurchaseTodayBalance",1)) or 1
    return {
        "margin_buy": si(latest.get("MarginPurchaseBuy",0)),
        "margin_bal": bal,
    }, False

# ── V35：除權息倒數 + 警示/處置股狀態（只對最終 TopN 呼叫，控制配額）────
_printed_div_sample  = False
_printed_disp_sample = False

def fetch_risk_flags(sid, ref_date_str):
    """
    除權息倒數：接近除權息會讓權證履約價被機械式調整，是重要風險提醒。
    警示/處置股：處置期間交易受限，連動權證流動性通常驟降。
    這兩個 dataset 的確切欄位名稱沒有十足把握（跟權證明細那次一樣是用文件推測），
    所以寫得很保守：任何一步出錯都直接跳過，絕對不會讓整個流程掛掉，
    第一次抓到資料時會印一次欄位樣本，方便之後對照調整。
    """
    global _printed_div_sample, _printed_disp_sample
    flags = {}
    ref_date = datetime.strptime(ref_date_str, "%Y-%m-%d")

    # 除權息倒數
    try:
        div_end = (ref_date + timedelta(days=60)).strftime("%Y-%m-%d")
        data, hit = fm1("TaiwanStockDividend", sid, date_back(400), div_end)
        if not hit and data:
            if not _printed_div_sample:
                print(f"     [診斷][除權息樣本 {sid}] 欄位：{list(data[0].keys())}")
                _printed_div_sample = True
            upcoming = []
            for row in data:
                for key in ("CashExDividendTradingDate", "StockExDividendTradingDate"):
                    d = str(row.get(key, "")).strip()
                    if d and d[:10] >= ref_date_str:
                        try: upcoming.append(datetime.strptime(d[:10], "%Y-%m-%d"))
                        except Exception: pass
            if upcoming:
                nearest   = min(upcoming)
                days_left = (nearest - ref_date).days
                if 0 <= days_left <= 30:
                    flags["ex_dividend_days"] = days_left
                    flags["ex_dividend_date"] = nearest.strftime("%Y/%m/%d")
    except Exception:
        pass

    # 警示/處置股狀態
    try:
        data, hit = fm1("TaiwanStockDispositionSecuritiesPeriod", sid, date_back(10), ref_date_str)
        if not hit and data:
            if not _printed_disp_sample:
                print(f"     [診斷][處置股樣本 {sid}] 欄位：{list(data[0].keys())}")
                _printed_disp_sample = True
            latest    = data[-1]
            end_field = latest.get("DispositionEndDate") or latest.get("EndDate") or latest.get("end_date")
            if end_field:
                try:
                    end_dt = datetime.strptime(str(end_field)[:10], "%Y-%m-%d")
                    if end_dt >= ref_date:
                        flags["disposition_active"] = True
                        flags["disposition_end"]    = end_dt.strftime("%Y/%m/%d")
                except Exception:
                    pass
    except Exception:
        pass

    return flags

# ── 權證明細查表（從快取取，境外 IP 不再 fallback 個股查詢）────
def get_warrant_detail(sid, data_date_str, stock_close=None):
    """
    V31：從 WARRANT_DETAIL_CACHE 查表（Step② 用 TaiwanStockInfoWithWarrantSummary 填入）。
    這個 dataset 沒有槓桿/Delta/買賣價/成交量（免費版拿不到），
    改用「履約價 vs 正股價」算價內外程度，「正股價/(權證價×行使比例)」粗估槓桿倍數。
    stock_close：呼叫時傳入當下已知的正股收盤價（比快取當時的價格快照更即時準確），
                 沒傳的話退回用快取當時的快照價。
    """
    dt = datetime.strptime(data_date_str, "%Y-%m-%d")
    warrants = []

    cached = WARRANT_DETAIL_CACHE.get(sid)
    if cached:
        for w_code, w in cached.items():
            try:
                days_left = (w["expire_dt"] - dt).days
                if days_left < 20:  continue   # 太接近到期，時間價值耗損快，先濾掉

                fp   = w["fulfillment_price"]
                px   = stock_close if stock_close else w.get("target_close_snap", 0)
                if fp <= 0 or px <= 0: continue

                moneyness_pct = (px - fp) / fp * 100   # 認購：正值＝價內
                if moneyness_pct >= 15:      moneyness = "深度價內"
                elif moneyness_pct >= 3:     moneyness = "輕度價內"
                elif moneyness_pct >= -3:    moneyness = "價平"
                elif moneyness_pct >= -15:   moneyness = "輕度價外"
                else:                        moneyness = "深度價外"

                w_close = w.get("warrant_close", 0)
                ratio   = w.get("exercise_ratio", 1.0) or 1.0
                # 台股權證槓桿慣用公式：(正股價 × 執行比例) ／ 權證價
                # 原本寫成 px/(w_close*ratio) 是顛倒的，算出來會離譜地大（測試發現 2019 倍）
                leverage_est = round((px * ratio) / w_close, 1) if w_close > 0 and ratio > 0 else None

                warrants.append({
                    "code":          w["code"],
                    "type":          "call",
                    "expire":        w["expire_dt"].strftime("%Y/%m/%d"),
                    "days_left":     days_left,
                    "fulfillment_price": fp,
                    "moneyness":     moneyness,
                    "moneyness_pct": round(moneyness_pct, 1),
                    "leverage_est":  leverage_est,   # 粗估值，不是精確的有效槓桿，僅供參考
                })
            except Exception:
                continue

    # V27：快取空時不再 fallback 個股查詢
    # TaiwanStockWarrant 個股查詢在境外 IP 全部 422，省下這些無效請求
    # 前端改為顯示「有發行，請至券商 APP 查詢」
    # 排序：優先價平/輕度價內外（|moneyness_pct| 小），到期日較久的排前面
    warrants.sort(key=lambda x: (abs(x["moneyness_pct"]), -x["days_left"]))
    return warrants[:3]

# ── 評分 ──────────────────────────────────────────────────────
def calc_ma(closes, n):
    if len(closes) < n: return None
    return round(statistics.mean(closes[-n:]), 2)

# ── V35：權證選股輔助指標 ────────────────────────────────────
# 波動度／乖離率／連續上漲天數／距高點位置，全部用已經抓到的
# 股價歷史（hist，35天）算，不用多打任何 API。
def calc_risk_metrics(today_p, hist):
    closes = [h["close"] for h in hist] + [today_p["close"]]
    result = {}

    # 波動度：近20個交易日日報酬率的標準差（挑權證的核心邏輯——
    # 正股波動越大，權證的槓桿效果越有意義）
    if len(closes) >= 21:
        window = closes[-21:]
        rets = [
            (window[i] - window[i-1]) / window[i-1] * 100
            for i in range(1, len(window)) if window[i-1] > 0
        ]
        if len(rets) >= 5:
            vol = statistics.stdev(rets)
            result["volatility_pct"] = round(vol, 2)
            if vol >= 4:    result["volatility_level"] = "高"
            elif vol >= 2:  result["volatility_level"] = "中"
            else:           result["volatility_level"] = "低"

    # 乖離率：收盤價偏離 MA20 的百分比，過高＝追高風險
    if len(closes) >= 20:
        ma20 = statistics.mean(closes[-20:])
        if ma20 > 0:
            bias = (closes[-1] - ma20) / ma20 * 100
            result["bias20_pct"] = round(bias, 1)
            result["overheated"] = bias >= 15

    # 連續上漲天數（從今天往前算）
    streak = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i-1]: streak += 1
        else: break
    result["up_streak_days"] = streak

    # 距離目前抓到的歷史區間高點位置
    recent_high = max(closes)
    if recent_high > 0:
        dist = (closes[-1] / recent_high - 1) * 100
        result["dist_from_high_pct"] = round(dist, 1)
        result["is_new_high"] = closes[-1] >= recent_high * 0.999

    return result

def calc_score(today_p, hist, inst, margin):
    score, reasons, warnings = 0, [], []
    close  = today_p["close"]
    high   = today_p["high"]
    low_p  = today_p["low"]
    spread = today_p.get("spread", 0)
    prev_c = round(close - spread, 2) if spread else (hist[-1]["close"] if hist else close)
    chg    = round(spread / prev_c * 100, 2) if prev_c > 0 else 0

    if chg >= 5:     score += 16; reasons.append("強勢大漲 ≥5%")
    elif chg >= 3:   score += 12; reasons.append("大漲 ≥3%")
    elif chg >= 1:   score += 7;  reasons.append("溫和上漲 ≥1%")
    elif chg >= 0.5: score += 4;  reasons.append("小漲 0.5~1%")
    elif chg >= 0:   score += 1
    elif chg < 0:    score -= 10; warnings.append("今日收跌")

    if high > low_p:
        cp = (close - low_p) / (high - low_p)
        if cp >= 0.8:   score += 14; reasons.append("收盤靠近最高點（買盤強）")
        elif cp >= 0.6: score += 8
        elif cp < 0.3:  score -= 8;  warnings.append("長上影線（賣壓重）")

    closes  = [h["close"]  for h in hist]
    volumes = [h["volume"] for h in hist]
    if len(volumes) >= 5:
        avg_vol = statistics.mean(volumes[-5:])
        vr = today_p["volume"] / avg_vol if avg_vol > 0 else 0
        if 1.5 <= vr <= 4:  score += 10; reasons.append(f"量能放大 {vr:.1f}x")
        elif vr > 4:        score += 5;  warnings.append("量能過度放大")
        elif vr < 0.7:      score -= 5;  warnings.append("量能萎縮")

    if len(closes) >= 20:
        ma5  = calc_ma(closes, 5)
        ma10 = calc_ma(closes, 10)
        ma20 = calc_ma(closes, 20)
        if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
            score += 10; reasons.append("均線多頭排列")
        elif ma5 and ma10 and ma5 > ma10: score += 5
        if ma5  and close > ma5:  score += 4
        if ma20 and close > ma20: score += 6; reasons.append(f"站上月線 MA20={ma20}")
        elif ma20: score -= 5; warnings.append("跌破月線")
        rh = max(closes[-10:]) if len(closes)>=10 else closes[-1]
        if close >= rh * 0.99: score += 10; reasons.append("突破近10日高點")

    if inst:
        tn = inst.get("total_net",0)
        tr = inst.get("trust_net",0)
        fn = inst.get("foreign_net",0)
        if tn > 5000:    score += 15; reasons.append(f"三大法人買超 {tn}張")
        elif tn > 1000:  score += 10; reasons.append(f"三大法人買超 {tn}張")
        elif tn > 0:     score += 5
        elif tn < -3000: score -= 10; warnings.append("三大法人賣超")
        if tr > 500:     score += 5;  reasons.append("投信積極買超")
        if fn > 3000:    score += 5;  reasons.append("外資積極買超")

    if margin:
        mb = margin.get("margin_buy",0)
        bl = margin.get("margin_bal",1)
        if bl > 0 and mb/bl > 0.15:
            score -= 5; warnings.append("融資追價明顯")

    return max(0, min(100, score)), reasons, warnings, chg

# ── V28：歷史 pool（因為 fallback 400 支是輪替的，同一批股票要隔幾天
#         才會再被掃到一次，所以精選清單要用「多天疊加」而不是每次全蓋掉）──
def _load_history_pool():
    """讀取上次寫出的 stocks.json，回傳 {sid: stock_dict} 方便用代號合併。"""
    if not os.path.exists(OUTPUT_PATH):
        return {}
    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            old = json.load(f)
    except Exception as e:
        print(f"  ! 讀取舊 stocks.json 失敗（{e}），視為沒有歷史資料")
        return {}
    pool = {}
    for s in old.get("stocks", []):
        sid = s.get("sid")
        if sid:
            pool[sid] = s
    return pool

def _merge_with_history(today_scored, ref_date_str):
    """
    合併今天新掃到的 + 舊 pool 裡還沒過期的，用 sid 去重（今天的新資料優先），
    依 score 重新排序，只丟掉太舊（超過 HISTORY_MAX_AGE_DAYS 天沒更新）的股票。
    回傳：(排序後合併清單, 用來寫檔的 top_n)
    """
    ref_date = datetime.strptime(ref_date_str, "%Y-%m-%d").date()
    pool = _load_history_pool()

    # 今天新掃到的，一律覆蓋 pool 裡同代號的舊資料（新資料比較準）
    for s in today_scored:
        pool[s["sid"]] = s

    kept = []
    today_count = 0
    for sid, s in pool.items():
        dd = s.get("data_date")
        try:
            age = (ref_date - datetime.strptime(dd, "%Y-%m-%d").date()).days
        except Exception:
            age = 999  # 抓不到日期的舊資料，當作過期處理
        if age > HISTORY_MAX_AGE_DAYS:
            continue
        s["age_days"] = age                    # 這筆資料是幾天前掃到的
        s["is_today"] = (dd == ref_date_str)    # 是否為今天新掃到
        if s["is_today"]:
            today_count += 1
        kept.append(s)

    kept.sort(key=lambda x: x["score"], reverse=True)
    print(f"  → 疊加歷史：今日新增 {today_count} 支 + 保留舊資料 "
          f"{len(kept) - today_count} 支（{HISTORY_MAX_AGE_DAYS} 天內），"
          f"合併後共 {len(kept)} 支候選池")
    return kept

# ── 主程式 ─────────────────────────────────────────────────────
def main():
    now    = tw_now()
    today  = now.strftime("%Y-%m-%d")
    today8 = now.strftime("%Y%m%d")
    req    = 0

    hour_min = now.hour * 60 + now.minute
    if hour_min < 17 * 60 + 30:
        d = now - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        end_date = d.strftime("%Y-%m-%d")
        print(f"  ► 盤中模式：使用上個交易日資料（end_date={end_date}）")
    else:
        end_date = today
        print(f"  ► 盤後模式：使用今日收盤資料（end_date={end_date}）")

    print(f"[{now.strftime('%H:%M:%S')} 台灣時間] fetch_twse V27 開始 {today}")
    token_info = f"{len(TOKENS)} 個 TOKEN（各 600req/hr，合計 {len(TOKENS)*600}req/hr）" if TOKENS else "未設定（匿名 300req/hr）"
    print(f"  FinMind token: {token_info}")
    print(f"  V27 架構：電子股 meta + 活躍權證快取 → 粗篩 → 精篩（預估總 req < 400）")

    # ── ① 電子股 meta（2 req：WithWarrant + TaiwanStockInfo）──
    print("  ► ① 取電子股基本資料...")
    stock_meta  = fetch_electronics_meta();    req += 1
    elec_sids   = fetch_electronics_sids();    req += 1
    if not stock_meta or not elec_sids:
        print("  ! ① 失敗，中止"); return
    print(f"  → 股票 meta：{len(stock_meta)} 支；電子股：{len(elec_sids)} 支")
    time.sleep(0.3)

    # ── ② 活躍認購權證（1 req，縮減候選池 + 快取明細）──────────
    print("  ► ② 取電子股活躍認購權證（近15天）...")
    scan_sids, active_set, slot_meta = fetch_active_warrant_targets(elec_sids, now)
    req += 1
    if not scan_sids:
        print("  ! ② 無任何電子股有認購權證交易，中止")
        _write_empty(now, today8, req, slot_meta, False); return
    print(f"  → 掃描候選：{len(scan_sids)} 支（電子股 ∩ 近15天有認購交易）")
    time.sleep(0.3)

    # ── ③ 第一階段：快速粗篩（近4天，只掃候選池）──────────────
    # V27：硬性上限保護，確保不論 fallback 結果多少都不超過 450 支
    SCAN_HARD_LIMIT = 450
    if len(scan_sids) > SCAN_HARD_LIMIT:
        print(f"  ⚠ 候選池 {len(scan_sids)} 支超過上限，截斷為 {SCAN_HARD_LIMIT} 支")
        scan_sids = scan_sids[:SCAN_HARD_LIMIT]
    quick_start = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=4)).strftime("%Y-%m-%d")
    print(f"  ► ③ 第一階段粗篩（{quick_start}~{end_date}，共 {len(scan_sids)} 支）...")
    survivors, q_req, stop = quick_filter(scan_sids, stock_meta, quick_start, end_date)
    req += q_req

    # V30：粗篩「完整跑完沒被 402 打斷」才算這份/這幾份排程真正完成，
    # 留著這個旗標，等最後寫檔時才決定 slots_done 要不要納入這輪的份數
    slot_scan_completed = not stop

    if not survivors:
        print("  ! 粗篩後無存活標的，可能資料尚未更新")
        _write_empty(now, today8, req, slot_meta, slot_scan_completed); return
    # V27：402 提前停止時，survivors 已有部分結果，繼續往下跑
    if stop:
        print(f"  ⚠ 粗篩因 402 提前停止，但已存活 {len(survivors)} 支，繼續精篩")
        stop = False  # 重置 stop，讓精篩繼續跑（此時 req 跨小時，API 已重置）

    date_votes  = Counter(s["data_date"] for s in survivors)
    actual_date = date_votes.most_common(1)[0][0]
    print(f"  → 實際資料日期：{actual_date}")

    # ── ④ 第二階段：完整歷史精篩 ──────────────────────────────
    full_start = (datetime.strptime(actual_date, "%Y-%m-%d") - timedelta(days=35)).strftime("%Y-%m-%d")
    print(f"  ► ④ 第二階段精篩（{full_start}~{actual_date}，共 {len(survivors)} 支）...")

    candidates = []
    for idx, s in enumerate(survivors):
        if stop: break
        sid = s["sid"]
        if (idx + 1) % 50 == 0:
            print(f"  ... 精篩 {idx+1}/{len(survivors)}  候選:{len(candidates)}  req:{req}")

        hist_rows, hit = fetch_price_full(sid, full_start, actual_date)
        req += 1
        if hit:
            print(f"  ! 觸發 API 限額（req={req}），停止精篩")
            stop = True; break
        time.sleep(0.07)

        if not hist_rows or len(hist_rows) < 6: continue
        today_p = hist_rows[-1]
        last_dt = datetime.strptime(today_p["date"], "%Y-%m-%d").date()
        ref_dt  = datetime.strptime(actual_date, "%Y-%m-%d").date()
        if (ref_dt - last_dt).days > 5: continue
        hist = hist_rows[:-1]
        if len(hist) < 5: continue

        _, _, _, chg = calc_score(today_p, hist, {}, {})
        if chg < 0.1: continue

        candidates.append({
            "sid":       sid,
            "info":      s["info"],
            "today_p":   today_p,
            "hist":      hist,
            "chg":       chg,
            "data_date": today_p["date"],
        })

    print(f"  → 精篩完成：{len(candidates)} 支候選（req={req}）")

    if not candidates:
        print("  ! 無候選，可能資料尚未更新")
        _write_empty(now, today8, req, slot_meta, slot_scan_completed); return

    # ── ⑤ Top30：三大法人 + 融資 ─────────────────────────────
    candidates.sort(key=lambda x: x["chg"], reverse=True)
    top30      = candidates[:30]
    inst_start = (datetime.strptime(actual_date, "%Y-%m-%d") - timedelta(days=20)).strftime("%Y-%m-%d")

    print(f"  ► ⑤ 三大法人 + 融資（{len(top30)} 支）...")
    inst_map   = {}
    margin_map = {}
    for c in top30:
        if stop: break
        sid = c["sid"]
        res, hit = fetch_inst(sid, inst_start, actual_date);   req += 1
        if hit: stop = True; break
        inst_map[sid] = res;   time.sleep(0.12)
        res, hit = fetch_margin(sid, inst_start, actual_date); req += 1
        if hit: stop = True; break
        margin_map[sid] = res; time.sleep(0.10)

    print(f"  → ⑤ 完成（req={req}）")

    # ── 完整評分 + 取 TopN ───────────────────────────────────
    scored = []
    for c in top30:
        sid  = c["sid"]
        hist = c["hist"]
        if len(hist) < 5: continue
        score, reasons, warnings, chg = calc_score(
            c["today_p"], hist, inst_map.get(sid,{}), margin_map.get(sid,{})
        )
        # 有認購權證加分
        if sid in active_set: score = min(100, score + 2)
        if score < 25: continue

        ma_c = [h["close"] for h in hist]
        # V27：從快取取權證明細（不用再打 API）
        # V31：傳入當下正股收盤價，價內外估算比用快取快照價準確
        warrants = get_warrant_detail(sid, actual_date, c["today_p"]["close"])
        # V35：波動度/乖離率/連續天數/距高點位置 —— 用已抓到的 hist 算，不多打 API
        risk = calc_risk_metrics(c["today_p"], hist)

        scored.append({
            "sid":        sid,
            "name":       c["info"].get("name", sid),
            "close":      c["today_p"]["close"],
            "change_pct": chg,
            "volume":     c["today_p"]["volume"],
            "market":     c["info"].get("market","tse"),
            "score":      score,
            "reasons":    reasons,
            "warnings":   warnings,
            "inst":       inst_map.get(sid,{}),
            "ma5":        calc_ma(ma_c, 5),
            "ma10":       calc_ma(ma_c, 10),
            "ma20":       calc_ma(ma_c, 20),
            "has_warrant": sid in active_set,  # 有發行認購權證（代號需至券商查）
            "warrants":   warrants,   # V27：真實明細，從快取取
            "risk":       risk,       # V35：波動度/乖離率/連續天數/距高點位置
            "data_date":  c["data_date"],       # V28：這支股票的資料實際日期，合併歷史用
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    # V28：跟歷史 pool 疊加（因為 fallback 是輪替掃描，今天沒掃到的股票
    # 不代表它不好，只是還沒輪到；先合併再取 TopN，才不會漏掉前幾輪的好標的）
    merged_pool = _merge_with_history(scored, actual_date)
    top_n = merged_pool[:TOP_N]

    for c in top_n:
        s = c["score"]
        if s >= 85:
            c["prob"] = f"高（{min(82,60+(s-85)*2+15)}%）"; c["prob_level"] = "high"
        elif s >= 70:
            c["prob"] = f"中高（{62+(s-70)}%）";             c["prob_level"] = "medium-high"
        elif s >= 55:
            c["prob"] = f"中（{48+(s-55)}%）";               c["prob_level"] = "medium"
        else:
            c["prob"] = "偏低（<48%）";                       c["prob_level"] = "low"

    # V35：除權息倒數 + 警示/處置股狀態 —— 只對最終 TopN 呼叫（最多 2*15=30 req），
    # 控制配額消耗。任一支失敗都不影響其他支，也不影響整體流程。
    for c in top_n:
        try:
            c["risk_flags"] = fetch_risk_flags(c["sid"], actual_date)
            req += 2
        except Exception:
            c["risk_flags"] = {}

    scan_progress = _finalize_scan_progress(slot_meta, slot_scan_completed)

    output = {
        "updated_at":         now.strftime("%Y/%m/%d %H:%M"),
        "trade_date":         actual_date.replace("-",""),
        "data_date":          actual_date,
        "total_scanned":      len(scan_sids),
        "candidates_count":   len(scored),          # 今天新掃到、通過門檻的數量
        "pool_count":         len(merged_pool),      # V28：疊加歷史後的候選池總數
        "total_api_req":      req,
        "scan_progress":      scan_progress,         # V30：漏跑補償用的份數進度
        "stocks":             top_n,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完成！資料:{actual_date}  掃:{len(scan_sids)}  存活:{len(survivors)}  候選:{len(scored)}  精選:{len(top_n)}  API:{req}")
    for s in top_n:
        wc = len(s.get("warrants",[]))
        print(f"  [{s['score']:3d}] {s['sid']} {s['name'][:8]:8s} {s['change_pct']:+.2f}%  {s['prob']}  權證:{wc}支")

def _finalize_scan_progress(slot_meta, completed):
    """
    依這輪的 slot_meta（來自 fetch_active_warrant_targets）與是否順利掃完，
    算出要寫進 output["scan_progress"] 的內容，下次執行時用來判斷有沒有漏跑。
    mode=="full"（全量查到 or 電子股總數 ≤ 400 不用分批）或沒有 slot_meta
    （① 就失敗，還沒走到分批這步）都不需要記錄份數進度。
    """
    if not slot_meta or slot_meta.get("mode") != "fallback":
        return None
    prev_done = set(slot_meta.get("prev_done", []))
    if completed:
        prev_done |= set(slot_meta.get("target_slots", []))
    return {
        "date":        slot_meta["date"],
        "total_slots": slot_meta["total_slots"],
        "slots_done":  sorted(prev_done),
    }

def _write_empty(now, today8, req, slot_meta=None, slot_scan_completed=False):
    # V28：今天沒新資料（額度用完/API 沒回應等），不要把舊的精選清單洗掉，
    # 改成沿用歷史 pool（一樣會依保留天數自動過期）
    ref_date_str = now.strftime("%Y-%m-%d")
    merged_pool  = _merge_with_history([], ref_date_str)
    top_n        = merged_pool[:TOP_N]
    scan_progress = _finalize_scan_progress(slot_meta, slot_scan_completed)

    output = {
        "updated_at":       now.strftime("%Y/%m/%d %H:%M"),
        "trade_date":       today8,
        "data_date":        ref_date_str,
        "total_scanned":    0,
        "candidates_count": 0,
        "pool_count":       len(merged_pool),
        "total_api_req":    req,
        "scan_progress":    scan_progress,   # V30：漏跑補償用的份數進度
        "stocks":           top_n,
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"  💾 stocks.json 今天無新資料，沿用歷史精選 {len(top_n)} 支（API 已用 {req} 次）")

if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""主要通貨(対円・対ドル)と主要コモディティの5分足を毎日記録する。

取得元は Yahoo Finance のチャートAPI。`range=1mo&interval=5m` で
**約30日ぶんの5分足**が1リクエストで返る(株探の5営業日と違い余裕がある)。
そのため取得に失敗した日やPCが落ちていた日があっても、
**30日以内に一度動けば自動的に埋め戻る**。

日付はJST基準でバケツ分けする。FXはほぼ24時間動くので、
「その日のJST 00:00〜23:59に付いた足」をその日のファイルに入れる。

出力:
  docs/data/YYYY-MM-DD.json.gz … その日の全銘柄の5分足
  docs/data/manifest.json      … 日付一覧と銘柄マスタ(閲覧サイト用)
"""
import gzip
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent
DATA = BASE / "docs" / "data"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
       "?range=1mo&interval=5m")
JST = timezone(timedelta(hours=9))
SLEEP = 0.4
RETRIES = 3
TIMEOUT = 30

# (Yahooシンボル, 表示名, 分類)
SYMBOLS = [
    ("USDJPY=X",  "ドル円",        "対円"),
    ("EURJPY=X",  "ユーロ円",      "対円"),
    ("GBPJPY=X",  "ポンド円",      "対円"),
    ("AUDJPY=X",  "豪ドル円",      "対円"),
    ("NZDJPY=X",  "NZドル円",      "対円"),
    ("CADJPY=X",  "カナダドル円",  "対円"),
    ("CHFJPY=X",  "スイスフラン円", "対円"),
    ("CNYJPY=X",  "人民元円",      "対円"),

    ("EURUSD=X",  "ユーロドル",        "対ドル"),
    ("GBPUSD=X",  "ポンドドル",        "対ドル"),
    ("AUDUSD=X",  "豪ドル/ドル",       "対ドル"),
    ("NZDUSD=X",  "NZドル/ドル",       "対ドル"),
    ("USDCAD=X",  "ドル/カナダドル",   "対ドル"),
    ("USDCHF=X",  "ドル/スイスフラン", "対ドル"),
    ("USDCNY=X",  "ドル/人民元",       "対ドル"),
    ("DX-Y.NYB",  "ドル指数(DXY)",     "対ドル"),

    ("GC=F", "金",         "貴金属"),
    ("SI=F", "銀",         "貴金属"),
    ("PL=F", "プラチナ",   "貴金属"),
    ("HG=F", "銅",         "貴金属"),

    ("CL=F", "WTI原油",    "エネルギー"),
    ("BZ=F", "ブレント原油", "エネルギー"),
    ("NG=F", "天然ガス",   "エネルギー"),

    ("ZW=F", "小麦",       "穀物"),
    ("ZC=F", "トウモロコシ", "穀物"),
    ("ZS=F", "大豆",       "穀物"),
]


def fetch(sym):
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(URL.format(sym=urllib.parse.quote(sym)),
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = e
            if attempt < RETRIES - 1:
                time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"取得に失敗: {last}")


def parse(payload):
    """{JSTの日付: [[時刻, 始値, 高値, 安値, 終値, 出来高], ...]} を返す(時刻昇順)。"""
    res = (payload.get("chart") or {}).get("result") or []
    if not res:
        return {}
    r = res[0]
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    o, h, l, c = (q.get(k) or [] for k in ("open", "high", "low", "close"))
    v = q.get("volume") or []

    days = {}
    for i, t in enumerate(ts):
        def g(arr):
            return arr[i] if i < len(arr) and arr[i] is not None else None
        oo, hh, ll, cc = g(o), g(h), g(l), g(c)
        if None in (oo, hh, ll, cc):
            continue                      # 値の付いていない足は落とす
        dt = datetime.fromtimestamp(t, JST)
        row = [dt.strftime("%H:%M"),
               round(float(oo), 5), round(float(hh), 5),
               round(float(ll), 5), round(float(cc), 5)]
        vol = g(v)
        row.append(0 if vol is None else int(vol))
        days.setdefault(dt.strftime("%Y-%m-%d"), []).append(row)
    for rows in days.values():
        rows.sort(key=lambda x: x[0])
    return days


def read_day(path):
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def write_day(path, payload):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)                     # 書きかけを残さない


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    collected = {}          # {日付: {シンボル: 足}}
    failed = []
    print(f"対象 {len(SYMBOLS)} 銘柄", file=sys.stderr)

    for i, (sym, name, cat) in enumerate(SYMBOLS, 1):
        try:
            days = parse(fetch(sym))
        except Exception as e:            # noqa: BLE001 - 1銘柄の失敗で全部を止めない
            failed.append(sym)
            print(f"  ! {sym} {name}: {e}", file=sys.stderr)
            time.sleep(SLEEP)
            continue
        if not days:
            failed.append(sym)
            print(f"  ! {sym} {name}: データが空", file=sys.stderr)
        for d, rows in days.items():
            collected.setdefault(d, {})[sym] = rows
        if i % 10 == 0:
            print(f"  {i}/{len(SYMBOLS)}", file=sys.stderr)
        time.sleep(SLEEP)

    if not collected:
        print("ERROR: 1銘柄も取得できませんでした(仕様変更の可能性)", file=sys.stderr)
        sys.exit(1)

    # 当日はまだ途中なので、最新日も含めて毎回マージして上書きする
    now = datetime.now(JST).isoformat(timespec="seconds")
    for d in sorted(collected):
        path = DATA / f"{d}.json.gz"
        existing = read_day(path)
        bars = dict(existing["bars"]) if existing else {}
        bars.update(collected[d])
        write_day(path, {"date": d, "generated": now, "bars": bars})

    dates = sorted(p.name[:10] for p in DATA.glob("????-??-??.json.gz"))
    (DATA / "manifest.json").write_text(
        json.dumps({
            "updated": now,
            "dates": list(reversed(dates)),
            "symbols": [{"sym": s, "name": n, "cat": c} for s, n, c in SYMBOLS
                        if s not in failed],
        }, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")

    newest = dates[-1] if dates else "-"
    n_bars = sum(len(v) for v in collected.get(newest, {}).values())
    size = sum(p.stat().st_size for p in DATA.glob("*.gz")) / 1024 / 1024
    print(f"保存 {len(dates)}日分 (最新 {newest}: {len(collected.get(newest, {}))}銘柄 "
          f"/ {n_bars}本) 合計 {size:.1f}MB", file=sys.stderr)
    if failed:
        print(f"取得失敗 {len(failed)}銘柄: {','.join(failed)}", file=sys.stderr)
        sys.exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Automated Hourly Market & SEC Form 8-K Data Pipeline for STRC Live Radar
Runs every hour via GitHub Actions:
  1. Fetches live STRC, MSTR, and BTC-USD prices (multi-source fallback: Yahoo Chart v8 / Stooq / Coinbase / Kraken)
  2. Fetches or computes live MSTR mNAV multiple (mnav.com + Strategy Net-BTC-Per-Share model)
  3. Checks SEC EDGAR (CIK 0001050446) for any newly filed Form 8-K and automatically parses:
       - Period end date
       - STRC shares repurchased & dollar amount ($M)
       - Remaining preferred repurchase authorization ($M/$B) & automatically infers any Board authorization cap expansion!
       - MSTR ATM net proceeds ($M) & remaining ATM capacity
       - BTC purchased / sold & aggregate BTC holdings / avg cost
       - USD Reserve & USD Cash balances
       - Announced STRC dividend rate (%)
  4. Appends/updates `docs/data.json` so the live GitHub Pages dashboard updates with ZERO manual input.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_JSON = ROOT / "docs" / "data.json"

CIK = "0001050446"
CIK_INT = "1050446"
SEC_SUBMISSIONS_URL = f"https://data.sec.gov/submissions/CIK{CIK}.json"
SEC_UA = "LastagileCapitalResearch ir-contact@lastagile-capital.org"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def curl_get(url: str, *, is_sec: bool = False, timeout: int = 20) -> str:
    cmd = ["curl", "-sSL", "--compressed", "--fail", "--max-time", str(timeout)]
    if is_sec:
        cmd += [
            "-H", f"User-Agent: {SEC_UA}",
            "-H", "Accept: application/json, text/html, */*",
        ]
    else:
        cmd += [
            "-H", f"User-Agent: {BROWSER_UA}",
            "-H", "Accept: application/json, text/html, text/csv, */*",
            "-H", "Accept-Language: en-US,en;q=0.9",
        ]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip() or f"curl exit {proc.returncode}")
    return proc.stdout


def strip_html(raw: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|table|td|th)>", " ", cleaned)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_month_date(text_date: str) -> str | None:
    """Convert 'November 15, 2026' -> '2026-11-15'."""
    try:
        cleaned = re.sub(r"\s+", " ", text_date.strip())
        return dt.datetime.strptime(cleaned, "%B %d, %Y").strftime("%Y-%m-%d")
    except Exception:
        return None


def fetch_equity_quote(symbol: str) -> tuple[float | None, float | None, str | None]:
    """Return (last_price, prev_close, trade_date_yyyy_mm_dd) for STRC or MSTR."""
    # Source 1: Yahoo Finance v8 chart endpoint
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            raw = curl_get(f"https://{host}/v8/finance/chart/{symbol}?interval=1d&range=5d", timeout=12)
            obj = json.loads(raw)
            res = obj["chart"]["result"][0]
            meta = res["meta"]
            price = float(meta["regularMarketPrice"])
            prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or price)
            ts = meta.get("regularMarketTime")
            tdate = (
                dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d")
                if ts
                else dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
            )
            if price > 0:
                return round(price, 2), round(prev, 2), tdate
        except Exception:
            pass

    # Source 2: Stooq daily CSV
    try:
        csv_text = curl_get(f"https://stooq.com/q/l/?s={symbol.lower()}.us&f=sd2t2ohlcv&h&e=csv", timeout=12)
        lines = [ln.strip() for ln in csv_text.strip().splitlines() if ln.strip()]
        if len(lines) >= 2:
            cols = lines[1].split(",")
            # Symbol,Date,Time,Open,High,Low,Close,Volume
            if len(cols) >= 7 and cols[6] not in ("N/D", ""):
                close_p = float(cols[6])
                open_p = float(cols[3]) if cols[3] not in ("N/D", "") else close_p
                d_str = cols[1] if re.match(r"\d{4}-\d{2}-\d{2}", cols[1]) else None
                if close_p > 0:
                    return round(close_p, 2), round(open_p, 2), d_str
    except Exception:
        pass

    return None, None, None


def fetch_btc_spot() -> float | None:
    """Fetch live BTC-USD spot price via Coinbase, Kraken, or Yahoo."""
    # 1. Coinbase public API
    try:
        raw = curl_get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=10)
        val = float(json.loads(raw)["data"]["amount"])
        if val > 1000:
            return round(val, 0)
    except Exception:
        pass

    # 2. Kraken public API
    try:
        raw = curl_get("https://api.kraken.com/0/public/Ticker?pair=XBTUSD", timeout=10)
        res = json.loads(raw)["result"]
        first_pair = next(iter(res.values()))
        val = float(first_pair["c"][0])
        if val > 1000:
            return round(val, 0)
    except Exception:
        pass

    # 3. Yahoo BTC-USD
    p, _, _ = fetch_equity_quote("BTC-USD")
    if p and p > 1000:
        return round(p, 0)

    return None


def fetch_or_compute_mnav(
    mstr_price: float,
    btc_price: float,
    btc_holdings: int,
    usd_reserve_100m: float,
    usd_cash_100m: float,
    strc_shares_10k: int,
) -> float:
    """
    Try reading official Strategy dashboard mNAV; otherwise compute calibrated Net-BTC mNAV:
      Net BTC Equity = BTC_Holdings * BTC_Price - (Debt + Other_Pref + STRC_Notional - USD_Reserve - USD_Cash)
      Calibrated diluted common shares scale smoothly with MSTR convertibility.
    """
    try:
        raw_s = curl_get("https://www.strategy.com/", timeout=10)
        m = re.search(r"mNAV[^0-9]{1,30}(\d\.\d{2})x", strip_html(raw_s), flags=re.IGNORECASE)
        if m:
            val = float(m.group(1))
            if 0.4 <= val <= 4.0:
                return round(val, 2)
    except Exception:
        pass

    # Calibrated Net-BTC-Per-Share formula (matches Strategy's 0.97x at MSTR=$77.8, BTC=$57.8k & 1.10x at MSTR=$125.6, BTC=$79k)
    btc_val_usd = btc_holdings * btc_price
    debt_plus_other_pref_usd = (67.1 + 49.4) * 1e8  # $6.71B debt + ~$4.94B STRF/STRK/STRD/STRE
    strc_notional_usd = strc_shares_10k * 1e4 * 100.0
    cash_total_usd = (usd_reserve_100m + usd_cash_100m) * 1e8
    net_senior_claims = max(0.0, debt_plus_other_pref_usd + strc_notional_usd - cash_total_usd)
    net_btc_equity_usd = max(1e9, btc_val_usd - net_senior_claims)

    # Effective diluted share count (~432M at MSTR <= $85, rising toward ~468M above $135 as converts go ITM)
    itm_extra_m = max(0.0, min(42.0, (mstr_price - 85.0) * 0.65))
    diluted_shares = (432.0 + itm_extra_m) * 1e6
    net_btc_per_share = net_btc_equity_usd / diluted_shares
    if net_btc_per_share <= 0:
        return 0.95
    return round(mstr_price / net_btc_per_share, 2)


def parse_8k_document(
    filing_date: str,
    accession: str,
    url: str,
    prev_latest: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Parse a Strategy weekly / capital-update Form 8-K HTML.
    Returns structured dict if it contains ATM / BTC / Repurchase / USD Reserve updates, else None.
    """
    raw_html = curl_get(url, is_sec=True, timeout=25)
    text = strip_html(raw_html)

    # Check if this 8-K has capital / STRC / BTC / Reserve content
    relevant_kws = ("STRC", "USD Reserve", "Repurchase Program", "ATM Update", "BTC Update")
    if not any(kw.lower() in text.lower() for kw in relevant_kws):
        return None

    # 1. Period end date ("As of November 15, 2026")
    period_end = filing_date
    for m_dt in re.finditer(r"As of ([A-Z][a-z]+ \d{1,2}, 20\d\d)", text):
        parsed_d = parse_month_date(m_dt.group(1))
        if parsed_d and parsed_d <= filing_date:
            period_end = parsed_d
            break

    # 2. USD Reserve & USD Cash
    usd_reserve_100m = prev_latest["usd_reserve_100m"]
    usd_cash_100m = prev_latest["usd_cash_100m"]
    m_res = re.search(
        r"balances of the USD Reserve and USD Cash were \$([\d,\.]+)\s*(billion|million)\s+and\s+\$([\d,\.]+)\s*(billion|million)",
        text,
        flags=re.IGNORECASE,
    )
    if m_res:
        v1 = float(m_res.group(1).replace(",", ""))
        u1 = m_res.group(2).lower()
        v2 = float(m_res.group(3).replace(",", ""))
        u2 = m_res.group(4).lower()
        usd_reserve_100m = round(v1 * 10.0 if u1 == "billion" else v1 / 100.0, 3)
        usd_cash_100m = round(v2 * 10.0 if u2 == "billion" else v2 / 100.0, 3)

    # 3. STRC Repurchases (Shares & $M)
    strc_rep_shares = 0
    strc_rep_m = 0.0
    m_rep = re.search(
        r"Repurchase Program Updates.*?STRC Stock(?:\s*\(\d+\))?\s+(?:Variable Rate[^\d\-]+)?([\d,]+|-)\s+\$\s*([\d,\.]+|-)",
        text,
        flags=re.IGNORECASE,
    )
    if m_rep:
        s_str, m_str = m_rep.group(1).strip(), m_rep.group(2).strip()
        if s_str != "-":
            strc_rep_shares = int(s_str.replace(",", ""))
        if m_str != "-":
            strc_rep_m = float(m_str.replace(",", ""))

    # 4. Remaining Preferred Repurchase Authorization
    auth_rem_100m = prev_latest["auth_remaining_100m"]
    m_rem = re.search(
        r"\$([\d,\.]+)\s*(million|billion)\s+aggregate purchase price of Strategy[\'’]s preferred stock remains available",
        text,
        flags=re.IGNORECASE,
    )
    if m_rem:
        rv = float(m_rem.group(1).replace(",", ""))
        ru = m_rem.group(2).lower()
        auth_rem_100m = round(rv * 10.0 if ru == "billion" else rv / 100.0, 3)

    # Roll forward cumulative buyback & infer authorization cap (detects $30亿 -> $40亿+ automatically!)
    cum_buy_100m = round(prev_latest["cum_buyback_100m"] + strc_rep_m / 100.0, 3)
    auth_cap_100m = prev_latest["auth_cap_100m"]
    if m_rem:
        inferred_cap = round(cum_buy_100m + auth_rem_100m, 1)
        if inferred_cap >= auth_cap_100m - 0.2:
            auth_cap_100m = round(inferred_cap, 1)
    else:
        auth_rem_100m = max(0.0, round(auth_cap_100m - cum_buy_100m, 3))

    # Also check explicit authorization expansion text
    m_cap_inc = re.search(
        r"Digital Credit Securities Repurchase Program[^\n\.]{0,80}?to\s+\$([\d,\.]+)\s*billion",
        text,
        flags=re.IGNORECASE,
    )
    if m_cap_inc:
        explicit_cap_100m = round(float(m_cap_inc.group(1).replace(",", "")) * 10.0, 1)
        if explicit_cap_100m > auth_cap_100m:
            auth_cap_100m = explicit_cap_100m

    # 5. MSTR ATM Sales ($M net proceeds & remaining capacity)
    mstr_sold_m = 0.0
    mstr_atm_rem_100m = prev_latest.get("mstr_atm_remaining_100m", 180.924)
    m_atm = re.search(
        r"ATM Update.*?MSTR Stock\s+([\d,]+|-)\s+\$\s*(?:[\d,\.]+|-)\s+\$\s*([\d,\.]+|-)(?:\s*\(\d+\))?\s+\$\s*([\d,\.]+)",
        text,
        flags=re.IGNORECASE,
    )
    if m_atm:
        net_p = m_atm.group(2).strip()
        rem_atm = m_atm.group(3).strip()
        if net_p != "-":
            mstr_sold_m = float(net_p.replace(",", ""))
        if rem_atm != "-":
            mstr_atm_rem_100m = round(float(rem_atm.replace(",", "")) / 100.0, 3)

    # 6. BTC Holdings & Avg Cost
    btc_holdings = prev_latest["btc_holdings"]
    btc_avg_cost = prev_latest["btc_avg_cost"]
    btc_delta = 0
    m_btc = re.search(
        r"BTC Update.*?([\d,]+|-)\s+\$\s*(?:[\d,\.]+|-)\s+\$\s*(?:[\d,\.]+|-)\s+([\d,]{6,})\s+\$\s*[\d,\.]+\s+\$\s*([\d,]{4,})",
        text,
        flags=re.IGNORECASE,
    )
    if m_btc:
        b_total = int(m_btc.group(2).replace(",", ""))
        b_cost = int(m_btc.group(3).replace(",", ""))
        btc_delta = b_total - prev_latest["btc_holdings"]
        btc_holdings = b_total
        btc_avg_cost = b_cost

    # Check if BTC was sold
    btc_sold = btc_delta < 0 or bool(re.search(r"sold\s+[\d,]+\s+bitcoin", text, flags=re.IGNORECASE))

    # 7. STRC Dividend Rate
    div_rate = prev_latest["strc_dividend_rate_pct"]
    m_div = re.search(
        r"dividend rate per annum on STRC Stock[^\n\.]{0,140}?at\s+(\d{1,2}\.\d{2})%",
        text,
        flags=re.IGNORECASE,
    )
    if m_div:
        div_rate = float(m_div.group(1))

    # Build concise Chinese funding summary note
    notes = []
    if auth_cap_100m > prev_latest["auth_cap_100m"]:
        notes.append(f"🎯 回购授权扩容至 ${auth_cap_100m:.0f}亿！")
    if mstr_sold_m > 0:
        notes.append(f"卖 MSTR 筹资 ${mstr_sold_m:.1f}M")
    if strc_rep_m > 0:
        avg_p = (strc_rep_m * 1e6 / strc_rep_shares) if strc_rep_shares > 0 else 0
        notes.append(f"回购 STRC ${strc_rep_m:.1f}M" + (f" (均价 ${avg_p:.2f})" if avg_p else ""))
    if btc_delta > 0:
        notes.append(f"增持 {btc_delta:,} BTC")
    elif btc_delta < 0:
        notes.append(f"⚠️ 卖出 {abs(btc_delta):,} BTC 补充流动性")
    notes.append(f"USD Reserve ${usd_reserve_100m:.2f}亿 / Cash ${usd_cash_100m:.2f}亿")

    new_shares_10k = max(0, round(prev_latest["strc_shares_10k"] - strc_rep_shares / 1e4))

    return {
        "filing_date": filing_date,
        "period_end": period_end,
        "accession": accession,
        "url": url,
        "strc_repurchased_shares": strc_rep_shares,
        "strc_repurchased_m": round(strc_rep_m, 1),
        "avg_buyback_price": round((strc_rep_m * 1e6 / strc_rep_shares), 2) if strc_rep_shares > 0 else 0.0,
        "mstr_sold_m": round(mstr_sold_m, 1),
        "btc_delta": btc_delta,
        "btc_holdings": btc_holdings,
        "btc_avg_cost": btc_avg_cost,
        "btc_sold": btc_sold,
        "usd_reserve_100m": usd_reserve_100m,
        "usd_cash_100m": usd_cash_100m,
        "auth_cap_100m": auth_cap_100m,
        "cum_buyback_100m": cum_buy_100m,
        "auth_rem_100m": auth_rem_100m,
        "strc_shares_10k": new_shares_10k,
        "mstr_atm_remaining_100m": mstr_atm_rem_100m,
        "strc_dividend_rate_pct": div_rate,
        "funding_note": "；".join(notes),
    }


def main() -> int:
    db = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    latest = db["latest"]
    seen_8k = set(db["meta"].get("seen_8k_accessions", []))

    # 1. Check SEC EDGAR for any new Form 8-K filings (oldest-first so roll-forward is exact)
    try:
        sub_raw = curl_get(SEC_SUBMISSIONS_URL, is_sec=True, timeout=20)
        recent = json.loads(sub_raw).get("filings", {}).get("recent", {})
        accs = recent.get("accessionNumber", [])
        forms = recent.get("form", [])
        fdates = recent.get("filingDate", [])
        pdocs = recent.get("primaryDocument", [])

        candidates: list[tuple[str, str, str]] = []
        for i in range(min(25, len(accs))):
            if forms[i] in ("8-K", "8-K/A") and accs[i] not in seen_8k:
                if fdates[i] >= db["meta"].get("last_8k_date", "2026-11-16"):
                    acc_nodash = accs[i].replace("-", "")
                    url = f"https://www.sec.gov/Archives/edgar/data/{CIK_INT}/{acc_nodash}/{pdocs[i]}"
                    candidates.append((fdates[i], accs[i], url))

        # Process chronologically
        for fdate, acc, url in reversed(candidates):
            seen_8k.add(acc)
            try:
                parsed = parse_8k_document(fdate, acc, url, latest)
            except Exception as exc:
                print(f"[WARN] 8-K parse error ({acc}): {exc}", file=sys.stderr)
                parsed = None

            if parsed:
                print(f"[NEW 8-K PARSED] {fdate} ({acc}): {parsed['funding_note']}")
                # Update latest balance-sheet state
                latest["btc_holdings"] = parsed["btc_holdings"]
                latest["btc_avg_cost"] = parsed["btc_avg_cost"]
                latest["usd_reserve_100m"] = parsed["usd_reserve_100m"]
                latest["usd_cash_100m"] = parsed["usd_cash_100m"]
                latest["auth_cap_100m"] = parsed["auth_cap_100m"]
                latest["cum_buyback_100m"] = parsed["cum_buyback_100m"]
                latest["auth_remaining_100m"] = parsed["auth_rem_100m"]
                latest["strc_shares_10k"] = parsed["strc_shares_10k"]
                latest["mstr_atm_remaining_100m"] = parsed["mstr_atm_remaining_100m"]
                latest["strc_dividend_rate_pct"] = parsed["strc_dividend_rate_pct"]
                latest["btc_sold_recent"] = parsed["btc_sold"]
                if parsed["strc_repurchased_m"] > 0 or parsed["mstr_sold_m"] > 0:
                    latest["last_weekly_buyback_m"] = parsed["strc_repurchased_m"]
                    latest["last_weekly_mstr_sold_m"] = parsed["mstr_sold_m"]

                db["meta"]["last_8k_accession"] = acc
                db["meta"]["last_8k_date"] = fdate
                db["meta"]["last_8k_period_end"] = parsed["period_end"]

                # Prepend to filings_8k ledger
                db["filings_8k"].insert(
                    0,
                    {
                        "filing_date": parsed["filing_date"],
                        "period_end": parsed["period_end"],
                        "accession": parsed["accession"],
                        "url": parsed["url"],
                        "strc_repurchased_shares": parsed["strc_repurchased_shares"],
                        "strc_repurchased_m": parsed["strc_repurchased_m"],
                        "avg_buyback_price": parsed["avg_buyback_price"],
                        "mstr_sold_m": parsed["mstr_sold_m"],
                        "btc_delta": parsed["btc_delta"],
                        "btc_holdings": parsed["btc_holdings"],
                        "usd_reserve_100m": parsed["usd_reserve_100m"],
                        "usd_cash_100m": parsed["usd_cash_100m"],
                        "auth_cap_100m": parsed["auth_cap_100m"],
                        "auth_rem_100m": parsed["auth_rem_100m"],
                        "funding_note": parsed["funding_note"],
                    },
                )
                db["filings_8k"] = db["filings_8k"][:40]

                # Add / update history point for period_end
                hist_date = parsed["period_end"]
                tag = ""
                if "扩容" in parsed["funding_note"]:
                    tag = f"扩容${parsed['auth_cap_100m']:.0f}亿"
                elif parsed["btc_sold"]:
                    tag = "卖BTC护盘"
                existing = next((h for h in db["history"] if h["date"] == hist_date), None)
                if existing:
                    existing["weeklyBuyM"] = parsed["strc_repurchased_m"]
                    existing["cumBuyB"] = parsed["cum_buyback_100m"]
                    existing["authB"] = parsed["auth_cap_100m"]
                    existing["shares"] = parsed["strc_shares_10k"]
                    existing["note"] = parsed["funding_note"]
                    if tag:
                        existing["tag"] = tag
                else:
                    db["history"].append(
                        {
                            "date": hist_date,
                            "strc": latest["strc_price"],
                            "mnav": latest["mnav"],
                            "mstr": latest["mstr_price"],
                            "btc": latest["btc_price"],
                            "weeklyBuyM": parsed["strc_repurchased_m"],
                            "cumBuyB": parsed["cum_buyback_100m"],
                            "authB": parsed["auth_cap_100m"],
                            "shares": parsed["strc_shares_10k"],
                            "tag": tag,
                            "note": parsed["funding_note"],
                        }
                    )
                db["history"].sort(key=lambda x: x["date"])

        db["meta"]["seen_8k_accessions"] = list(seen_8k)[:80]
    except Exception as exc:
        print(f"[WARN] SEC check error: {exc}", file=sys.stderr)

    # 2. Fetch Live Market Quotes (STRC, MSTR, BTC)
    strc_p, strc_prev, strc_date = fetch_equity_quote("STRC")
    mstr_p, _, mstr_date = fetch_equity_quote("MSTR")
    btc_p = fetch_btc_spot()

    if strc_p:
        latest["strc_price"] = strc_p
        if strc_prev:
            latest["strc_prev_close"] = strc_prev
    if mstr_p:
        latest["mstr_price"] = mstr_p
    if btc_p:
        latest["btc_price"] = int(btc_p)

    # Update live mNAV
    latest["mnav"] = fetch_or_compute_mnav(
        latest["mstr_price"],
        latest["btc_price"],
        latest["btc_holdings"],
        latest["usd_reserve_100m"],
        latest["usd_cash_100m"],
        latest["strc_shares_10k"],
    )

    quote_date = strc_date or mstr_date or latest["price_date"]
    latest["price_date"] = quote_date

    # Upsert latest market point into history so chart stays live automatically!
    last_hist = db["history"][-1]
    if quote_date >= last_hist["date"]:
        existing_q = next((h for h in db["history"] if h["date"] == quote_date), None)
        if existing_q:
            existing_q["strc"] = latest["strc_price"]
            existing_q["mstr"] = latest["mstr_price"]
            existing_q["btc"] = latest["btc_price"]
            existing_q["mnav"] = latest["mnav"]
            if existing_q.get("tag", "").startswith("当前"):
                existing_q["tag"] = f"当前${latest['strc_price']:.1f}"
        else:
            # Clear old "当前..." tag on previous tail
            if last_hist.get("tag", "").startswith("当前"):
                last_hist["tag"] = ""
            db["history"].append(
                {
                    "date": quote_date,
                    "strc": latest["strc_price"],
                    "mnav": latest["mnav"],
                    "mstr": latest["mstr_price"],
                    "btc": latest["btc_price"],
                    "weeklyBuyM": 0,
                    "cumBuyB": latest["cum_buyback_100m"],
                    "authB": latest["auth_cap_100m"],
                    "shares": latest["strc_shares_10k"],
                    "tag": f"最新${latest['strc_price']:.1f}",
                    "note": f"自动行情更新：STRC ${latest['strc_price']:.2f} / MSTR ${latest['mstr_price']:.2f} (mNAV {latest['mnav']:.2f}x) / BTC ${latest['btc_price']:,}",
                }
            )
            db["history"].sort(key=lambda x: x["date"])

    db["meta"]["updated_at_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    DATA_JSON.write_text(json.dumps(db, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Synced data.json -> date={latest['price_date']} STRC=${latest['strc_price']} "
        f"MSTR=${latest['mstr_price']} mNAV={latest['mnav']}x BTC=${latest['btc_price']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

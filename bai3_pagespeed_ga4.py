#!/usr/bin/env python3
"""
BÀI TẬP 3 – Tối ưu trải nghiệm: liên kết GA4 & Google PageSpeed Insights.

Luồng xử lý:
  Bước 1 (GA4 API) : Top 50 Landing Pages theo sessions trong 30 ngày qua,
                     kèm bounceRate và averageSessionDuration.
  Bước 2 (PSI API) : Vòng lặp qua 50 URL, gọi PageSpeed Insights (strategy=mobile),
                     lấy lighthouseResult.categories.performance.score * 100 (thang 0-100).
                     Có time.sleep giữa các request + retry/backoff khi gặp 429/5xx (Rate Limit).
  Bước 3 (Pandas)  : Merge dữ liệu GA4 và PSI theo trường chung URL.
  Bước 4 (Đầu ra)  : Lọc "Trang báo động đỏ": PageSpeed < 50 VÀ Bounce Rate > 70%.
                     -> pagespeed_red_alert.csv (danh sách cần tối ưu gấp)
                     -> ga4_pagespeed_merged.csv (dữ liệu gộp đầy đủ)
                     -> pagespeed_vs_bounce.png (biểu đồ trực quan mối quan hệ tốc độ – hành vi)

Lưu ý:
  - GA4 trả về bounceRate dạng tỉ lệ 0-1  -> nhân 100 để ra %.
  - GA4 trả về landingPage dạng path (/blog/abc) -> ghép với --site-url để ra URL đầy đủ cho PSI.
  - Kết quả PSI được lưu cache (psi_cache.json) nên nếu chạy lại / bị ngắt giữa chừng
    sẽ không phải gọi lại các URL đã đo (tiết kiệm quota). Dùng --no-cache để đo mới hoàn toàn.

Ví dụ chạy:
  python bai3_pagespeed_ga4.py --property-id 123456789 --site-url https://example.com --psi-key AIza...
  python bai3_pagespeed_ga4.py --demo
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from typing import Callable

import numpy as np
import pandas as pd
import requests

from seo_common import INVALID_PATHS, build_full_url, ga4_client, ga4_run_report, load_env

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
RETRY_STATUS = {429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- #
# Bước 1: GA4
# --------------------------------------------------------------------------- #
def fetch_top_landing_pages(property_id: str, site_url: str, top_n: int = 50, days: int = 30,
                            credentials_path: str | None = None) -> pd.DataFrame:
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    client = ga4_client(credentials_path)
    # Lấy dư vài dòng phòng trường hợp có "(not set)" lọt vào top
    df = ga4_run_report(
        client, property_id,
        dimensions=["landingPage"],
        metrics=["sessions", "bounceRate", "averageSessionDuration"],
        start_date=start.isoformat(), end_date=end.isoformat(),
        order_by_metric="sessions", limit=top_n + 10,
    )
    return prepare_ga4(df, site_url, top_n)


def prepare_ga4(df: pd.DataFrame, site_url: str, top_n: int = 50) -> pd.DataFrame:
    """Làm sạch dữ liệu GA4 thô -> [URL, Landing_Page, Sessions, Bounce_Rate_%, Avg_Session_Duration_s]."""
    d = df[~df["landingPage"].astype(str).str.strip().isin(INVALID_PATHS)].copy()
    d = d.sort_values("sessions", ascending=False).head(top_n)
    out = pd.DataFrame({
        "URL": d["landingPage"].map(lambda p: build_full_url(site_url, p)),
        "Landing_Page": d["landingPage"],
        "Sessions": d["sessions"].round().astype(int),
        "Bounce_Rate_%": (d["bounceRate"] * 100).round(2),          # 0-1 -> %
        "Avg_Session_Duration_s": d["averageSessionDuration"].round(1),
    })
    return out.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Bước 2: PageSpeed Insights
# --------------------------------------------------------------------------- #
def parse_psi_response(data: dict) -> dict:
    """Trích điểm hiệu suất (0-100) và một số Core Web Vitals từ JSON của PSI."""
    lh = data.get("lighthouseResult", {})
    score = lh.get("categories", {}).get("performance", {}).get("score")
    audits = lh.get("audits", {})

    def num(audit_id):
        return audits.get(audit_id, {}).get("numericValue")

    lcp, tbt, cls, fcp = num("largest-contentful-paint"), num("total-blocking-time"), \
        num("cumulative-layout-shift"), num("first-contentful-paint")
    field = data.get("loadingExperience", {}).get("overall_category")  # dữ liệu thực tế CrUX (nếu có)
    return {
        "PageSpeed_Mobile": round(score * 100) if score is not None else np.nan,
        "LCP_s": round(lcp / 1000, 2) if lcp is not None else np.nan,
        "FCP_s": round(fcp / 1000, 2) if fcp is not None else np.nan,
        "TBT_ms": round(tbt) if tbt is not None else np.nan,
        "CLS": round(cls, 3) if cls is not None else np.nan,
        "CrUX_Field": field or "",
    }


def fetch_psi(url: str, api_key: str | None = None, strategy: str = "mobile",
              session: requests.Session | None = None, max_retries: int = 4,
              timeout: int = 120) -> dict:
    """Gọi PSI cho 1 URL, có retry + exponential backoff khi bị rate limit / lỗi server."""
    session = session or requests.Session()
    params = {"url": url, "strategy": strategy, "category": "performance"}
    if api_key:
        params["key"] = api_key

    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(PSI_ENDPOINT, params=params, timeout=timeout)
            if resp.status_code == 200:
                return {**parse_psi_response(resp.json()), "PSI_Loi": ""}
            if resp.status_code in RETRY_STATUS:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else min(60, 5 * 2 ** (attempt - 1))
                last_err = f"HTTP {resp.status_code}"
                print(f"      ! {last_err} (lần {attempt}/{max_retries}) – nghỉ {wait:.0f}s rồi thử lại")
                time.sleep(wait)
                continue
            # Lỗi không nên retry (400: URL không truy cập được, 403: sai key...)
            try:
                msg = resp.json().get("error", {}).get("message", resp.text[:200])
            except ValueError:
                msg = resp.text[:200]
            return {**parse_psi_response({}), "PSI_Loi": f"HTTP {resp.status_code}: {msg}"}
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
            wait = min(60, 5 * 2 ** (attempt - 1))
            print(f"      ! {last_err[:80]} (lần {attempt}/{max_retries}) – nghỉ {wait:.0f}s")
            time.sleep(wait)
    return {**parse_psi_response({}), "PSI_Loi": f"Thất bại sau {max_retries} lần: {last_err}"}


def load_cache(path: str | None) -> dict:
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(path: str | None, cache: dict) -> None:
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)


def run_psi_loop(urls: list[str], fetch_fn: Callable[[str], dict], sleep_s: float = 2.0,
                 cache_path: str | None = None, strategy: str = "mobile") -> pd.DataFrame:
    """Duyệt từng URL, gọi PSI, nghỉ sleep_s giây giữa các request để tránh Rate Limit."""
    cache = load_cache(cache_path)
    results = []
    called = 0
    for i, url in enumerate(urls, 1):
        key = f"{strategy}|{url}"
        if key in cache and not cache[key].get("PSI_Loi"):
            res = cache[key]
            print(f"  [{i:>2}/{len(urls)}] (cache) {url} -> {res['PageSpeed_Mobile']}")
        else:
            if called > 0 and sleep_s > 0:
                time.sleep(sleep_s)  # nghỉ giữa các lượt gọi API
            t0 = time.time()
            res = fetch_fn(url)
            called += 1
            status = res["PSI_Loi"] or f"score = {res['PageSpeed_Mobile']}"
            print(f"  [{i:>2}/{len(urls)}] {url} -> {status} ({time.time() - t0:.1f}s)")
            cache[key] = {k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in res.items()}
            save_cache(cache_path, cache)  # lưu ngay để chạy tiếp được nếu bị ngắt
        results.append({"URL": url, **res})
    df = pd.DataFrame(results)
    for c in ["PageSpeed_Mobile", "LCP_s", "FCP_s", "TBT_ms", "CLS"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# --------------------------------------------------------------------------- #
# Bước 3 + 4: Merge & lọc
# --------------------------------------------------------------------------- #
def merge_data(ga4: pd.DataFrame, psi: pd.DataFrame) -> pd.DataFrame:
    return pd.merge(ga4, psi, on="URL", how="left", validate="one_to_one")


def red_alert(merged: pd.DataFrame, max_score: float = 50, min_bounce: float = 70) -> pd.DataFrame:
    mask = (merged["PageSpeed_Mobile"] < max_score) & (merged["Bounce_Rate_%"] > min_bounce)
    cols = ["URL", "Sessions", "Bounce_Rate_%", "Avg_Session_Duration_s", "PageSpeed_Mobile",
            "LCP_s", "TBT_ms", "CLS"]
    # Ưu tiên trang nhiều traffic + điểm thấp nhất
    out = merged.loc[mask, cols].sort_values(["PageSpeed_Mobile", "Sessions"], ascending=[True, False]) \
        .reset_index(drop=True)
    for c in ["PageSpeed_Mobile", "TBT_ms"]:
        out[c] = out[c].round().astype("Int64")
    return out


def plot_relationship(merged: pd.DataFrame, path: str, max_score: float = 50, min_bounce: float = 70) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    INK, MUTED, GRID, SURFACE = "#0b0b0b", "#898781", "#e1e0d9", "#fcfcfb"
    BLUE, CRITICAL = "#2a78d6", "#d03b3b"

    d = merged.dropna(subset=["PageSpeed_Mobile"])
    is_red = (d["PageSpeed_Mobile"] < max_score) & (d["Bounce_Rate_%"] > min_bounce)
    smax = max(d["Sessions"].max(), 1)
    size = 40 + 360 * d["Sessions"] / smax

    fig, ax = plt.subplots(figsize=(10, 6.2), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.fill_between([0, max_score], min_bounce, 100, color=CRITICAL, alpha=0.07, linewidth=0)
    ax.axvline(max_score, color=MUTED, lw=1, ls="--")
    ax.axhline(min_bounce, color=MUTED, lw=1, ls="--")
    ax.text(2, 98, "VÙNG BÁO ĐỘNG ĐỎ  (PageSpeed < %g & Bounce > %g%%)" % (max_score, min_bounce),
            va="top", fontsize=8.5, color="#52514e", fontweight="bold")

    ax.scatter(d.loc[~is_red, "PageSpeed_Mobile"], d.loc[~is_red, "Bounce_Rate_%"], s=size[~is_red],
               c=BLUE, alpha=0.75, edgecolors=SURFACE, linewidths=2, label="Trang bình thường")
    ax.scatter(d.loc[is_red, "PageSpeed_Mobile"], d.loc[is_red, "Bounce_Rate_%"], s=size[is_red],
               c=CRITICAL, alpha=0.9, edgecolors=SURFACE, linewidths=2, label="Trang báo động đỏ")

    # Gắn nhãn 5 trang đỏ có traffic cao nhất (đặt bên trái chấm, tự đẩy lên nếu chồng nhau)
    placed: list[float] = []
    for _, r in d[is_red].nlargest(5, "Sessions").sort_values("Bounce_Rate_%").iterrows():
        label = str(r.get("Landing_Page", r["URL"]))[:38]
        x, y = r["PageSpeed_Mobile"], r["Bounce_Rate_%"]
        ty = y
        while any(abs(ty - py) < 2.6 for py in placed):
            ty += 2.6
        placed.append(ty)
        ax.annotate(label, (x, y), xytext=(x - 2.2, ty), textcoords="data", ha="right", va="center",
                    fontsize=7.5, color=INK,
                    arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.6) if ty != y else None)

    # Đường xu hướng tuyến tính
    if len(d) >= 3:
        k, b = np.polyfit(d["PageSpeed_Mobile"], d["Bounce_Rate_%"], 1)
        xs = np.array([d["PageSpeed_Mobile"].min(), d["PageSpeed_Mobile"].max()])
        ax.plot(xs, k * xs + b, color="#52514e", lw=2, alpha=0.8, label="Xu hướng tuyến tính")
        r = d["PageSpeed_Mobile"].corr(d["Bounce_Rate_%"])
        ax.set_title(f"Tốc độ trang (mobile) vs Tỷ lệ thoát — Top {len(d)} landing pages   (Pearson r = {r:.2f})",
                     loc="left", fontsize=11, color=INK)

    ax.set_xlim(0, 100)
    ax.set_ylim(max(0, d["Bounce_Rate_%"].min() - 10), 100)
    ax.set_xlabel("Điểm PageSpeed Insights (mobile, 0–100)", color=MUTED)
    ax.set_ylabel("Bounce Rate (%)", color=MUTED)
    ax.grid(color=GRID, lw=0.6)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(colors=MUTED)
    leg = ax.legend(loc="lower left", frameon=False, fontsize=8.5)
    for h in leg.legend_handles:
        if hasattr(h, "set_sizes"):
            h.set_sizes([60])
    fig.text(0.01, 0.01, "Kích thước chấm = số sessions (30 ngày)", fontsize=7.5, color=MUTED)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Dữ liệu giả lập
# --------------------------------------------------------------------------- #
def make_demo_ga4(seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    paths = ["/"] + [f"/blog/bai-viet-{i}" for i in range(1, 45)] + \
            [f"/san-pham/sp-{i}" for i in range(1, 12)] + ["(not set)"]
    n = len(paths)
    sessions = np.sort(rng.pareto(1.3, n) * 400 + 200)[::-1]
    bounce = np.clip(rng.normal(0.6, 0.15, n), 0.15, 0.95)
    return pd.DataFrame({
        "landingPage": paths,
        "sessions": sessions.round(),
        "bounceRate": bounce,
        "averageSessionDuration": np.clip(rng.normal(95, 40, n) * (1.3 - bounce), 5, None),
    })


def make_demo_psi_fetcher(ga4: pd.DataFrame, seed: int = 11) -> Callable[[str], dict]:
    """PSI giả lập: điểm có tương quan nghịch với bounce rate (trang chậm -> thoát nhiều)."""
    rng = np.random.default_rng(seed)
    lookup = dict(zip(ga4["URL"], ga4["Bounce_Rate_%"]))

    def fetch(url: str) -> dict:
        if url.endswith("sp-3"):  # mô phỏng 1 URL lỗi (VD: trang 404 / chặn bot)
            return {**parse_psi_response({}), "PSI_Loi": "HTTP 400: Lighthouse returned error: NOT_HTML"}
        br = lookup.get(url, 55)
        score = float(np.clip(150 - 1.35 * br + rng.normal(0, 12), 5, 99)) / 100
        lcp = 1200 + (1 - score) * 7000
        return {**parse_psi_response({"lighthouseResult": {
            "categories": {"performance": {"score": score}},
            "audits": {"largest-contentful-paint": {"numericValue": lcp},
                       "first-contentful-paint": {"numericValue": lcp * 0.45},
                       "total-blocking-time": {"numericValue": (1 - score) * 1500},
                       "cumulative-layout-shift": {"numericValue": max(0, (1 - score) * 0.3 + rng.normal(0, 0.03))}},
        }}), "PSI_Loi": ""}

    return fetch


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Liên kết GA4 & PageSpeed Insights tìm trang trải nghiệm tệ")
    p.add_argument("--property-id", default=os.getenv("GA4_PROPERTY_ID"))
    p.add_argument("--site-url", default=os.getenv("SITE_URL"), help="Domain website, vd https://example.com")
    p.add_argument("--credentials", default=None, help="File JSON service account (GA4)")
    p.add_argument("--psi-key", default=os.getenv("PSI_API_KEY"), help="API key PageSpeed Insights (khuyến nghị)")
    p.add_argument("--top", type=int, default=50, help="Số landing page (mặc định 50)")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--strategy", default="mobile", choices=["mobile", "desktop"])
    p.add_argument("--sleep", type=float, default=None,
                   help="Số giây nghỉ giữa 2 request PSI (mặc định 2s có key, 5s không có key)")
    p.add_argument("--max-score", type=float, default=50)
    p.add_argument("--min-bounce", type=float, default=70)
    p.add_argument("--cache", default="psi_cache.json")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--output-dir", default=".")
    p.add_argument("--demo", action="store_true", help="Dùng dữ liệu giả lập cho cả GA4 và PSI")
    return p.parse_args(argv)


def main(argv=None) -> int:
    load_env()
    args = parse_args(argv)
    os.makedirs(args.output_dir, exist_ok=True)
    cache_path = None if (args.no_cache or args.demo) else args.cache

    # ---- Bước 1 ----
    print("BƯỚC 1: Lấy Top landing pages từ GA4...")
    if args.demo:
        site = "https://example.com"
        ga4 = prepare_ga4(make_demo_ga4(), site, args.top)
    else:
        if not args.property_id or not args.site_url:
            print("LỖI: cần --property-id và --site-url (hoặc GA4_PROPERTY_ID, SITE_URL).", file=sys.stderr)
            return 2
        try:
            ga4 = fetch_top_landing_pages(args.property_id, args.site_url, args.top, args.days, args.credentials)
        except Exception as e:
            print(f"LỖI khi gọi GA4 API: {e}", file=sys.stderr)
            return 1
    print(f"  -> {len(ga4)} landing pages.\n")
    if ga4.empty:
        print("Không có dữ liệu GA4.")
        return 0

    # ---- Bước 2 ----
    if args.demo:
        fetch_fn, sleep_s = make_demo_psi_fetcher(ga4), 0.0
    else:
        session = requests.Session()
        fetch_fn = lambda u: fetch_psi(u, args.psi_key, args.strategy, session)  # noqa: E731
        sleep_s = args.sleep if args.sleep is not None else (2.0 if args.psi_key else 5.0)
        if not args.psi_key:
            print("  ⚠ Không có PSI API key: quota rất thấp, dễ bị HTTP 429. Nên tạo key ở Google Cloud Console.")
    print(f"BƯỚC 2: Gọi PageSpeed Insights ({args.strategy}) cho {len(ga4)} URL, nghỉ {sleep_s}s giữa mỗi lượt...")
    psi = run_psi_loop(ga4["URL"].tolist(), fetch_fn, sleep_s, cache_path, args.strategy)

    # ---- Bước 3 ----
    print("\nBƯỚC 3: Merge GA4 + PSI theo URL...")
    merged = merge_data(ga4, psi)
    ok = merged["PageSpeed_Mobile"].notna()
    print(f"  -> {ok.sum()}/{len(merged)} URL có điểm PSI; {(~ok).sum()} URL lỗi khi đo.")
    if ok.sum() >= 3:
        r = merged.loc[ok, "PageSpeed_Mobile"].corr(merged.loc[ok, "Bounce_Rate_%"])
        rs = merged.loc[ok, "PageSpeed_Mobile"].corr(merged.loc[ok, "Bounce_Rate_%"], method="spearman")
        print(f"  -> Tương quan PageSpeed vs Bounce Rate: Pearson r = {r:.2f}, Spearman ρ = {rs:.2f}"
              f" ({'nghịch biến: trang càng chậm càng thoát nhiều' if r < 0 else 'không thấy quan hệ nghịch'})")

    # ---- Bước 4 ----
    alert = red_alert(merged, args.max_score, args.min_bounce)
    merged_path = os.path.join(args.output_dir, "ga4_pagespeed_merged.csv")
    alert_path = os.path.join(args.output_dir, "pagespeed_red_alert.csv")
    chart_path = os.path.join(args.output_dir, "pagespeed_vs_bounce.png")
    merged.to_csv(merged_path, index=False, encoding="utf-8-sig")
    alert.to_csv(alert_path, index=False, encoding="utf-8-sig")
    if ok.sum() > 0:
        plot_relationship(merged, chart_path, args.max_score, args.min_bounce)

    print(f"\nBƯỚC 4: 🚨 {len(alert)} TRANG BÁO ĐỘNG ĐỎ (PageSpeed < {args.max_score:g} & Bounce Rate > {args.min_bounce:g}%)")
    if not alert.empty:
        with pd.option_context("display.width", 140, "display.max_colwidth", 50):
            print(alert.to_string(index=False))
    failed = merged[~ok]
    if not failed.empty:
        print("\nURL không đo được (kiểm tra lại thủ công):")
        for _, r in failed.iterrows():
            print(f"   - {r['URL']}: {r['PSI_Loi']}")
    print(f"\nĐã lưu:\n  {os.path.abspath(alert_path)}\n  {os.path.abspath(merged_path)}")
    if ok.sum() > 0:
        print(f"  {os.path.abspath(chart_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

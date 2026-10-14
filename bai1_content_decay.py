#!/usr/bin/env python3
"""
BÀI TẬP 1 – Hệ thống tự động phát hiện "Content Decay" từ Google Analytics 4.

Luồng xử lý:
  1. Lấy dữ liệu (GA4 Data API): sessions theo pagePath + date trong 6 tháng gần nhất.
  2. Xử lý (Pandas): chia làm 2 giai đoạn (GĐ1 = 3 tháng đầu, GĐ2 = 3 tháng cuối),
     tính tổng traffic mỗi bài ở mỗi giai đoạn và
         % Giảm = (Traffic GĐ1 - Traffic GĐ2) / Traffic GĐ1 * 100
  3. Lọc: % Giảm > 30% VÀ Traffic GĐ1 > 100 sessions.
  4. Xuất content_decay_report.csv, sắp xếp theo tỷ lệ giảm giảm dần.

Xử lý trường hợp đặc biệt:
  - Bài không có traffic ở GĐ2  -> Traffic GĐ2 = 0, tỷ lệ giảm = 100% (không bị mất khỏi báo cáo).
  - URL bị đổi (slug mới / redirect 301) -> dùng file --redirects (old_path,new_path) để gộp
    traffic của URL cũ vào URL mới, tránh báo động giả "giảm 100%". Hỗ trợ chuỗi redirect A->B->C.
  - Biến thể URL (?utm=..., '/' cuối, chữ hoa) được chuẩn hoá về một URL duy nhất.
  - Các giá trị rác của GA4 như "(not set)" bị loại bỏ.

Ví dụ chạy:
  python bai1_content_decay.py --property-id 123456789 --credentials sa.json
  python bai1_content_decay.py --property-id 123456789 --organic-only --redirects redirects.csv
  python bai1_content_decay.py --demo            # chạy thử với dữ liệu giả lập, không cần API
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

from seo_common import INVALID_PATHS, ga4_client, ga4_run_report, load_env, normalize_path

OUTPUT_COLUMNS = ["URL", "Traffic_3_Thang_Dau", "Traffic_3_Thang_Cuoi", "Ty_Le_Sut_Giam"]


# --------------------------------------------------------------------------- #
# 1. Khoảng thời gian
# --------------------------------------------------------------------------- #
def compute_periods(end: date | None = None, months: int = 6) -> dict[str, date]:
    """Tính mốc thời gian cho 6 tháng gần nhất, chia đôi thành 2 giai đoạn 3 tháng.

    end mặc định = hôm qua (dữ liệu GA4 của hôm nay chưa đầy đủ).
    """
    end = end or (date.today() - timedelta(days=1))
    start = (pd.Timestamp(end) - pd.DateOffset(months=months) + pd.Timedelta(days=1)).date()
    gd2_start = (pd.Timestamp(start) + pd.DateOffset(months=months // 2)).date()
    return {
        "start": start,
        "gd1_end": gd2_start - timedelta(days=1),
        "gd2_start": gd2_start,
        "end": end,
    }


# --------------------------------------------------------------------------- #
# 2. Lấy dữ liệu từ GA4
# --------------------------------------------------------------------------- #
def fetch_ga4_data(
    property_id: str,
    start: date,
    end: date,
    metric: str = "sessions",
    organic_only: bool = False,
    credentials_path: str | None = None,
) -> pd.DataFrame:
    """Trả về DataFrame [pagePath, date, traffic] (date dạng YYYYMMDD)."""
    from google.analytics.data_v1beta.types import Filter, FilterExpression

    dim_filter = None
    if organic_only:
        dim_filter = FilterExpression(
            filter=Filter(
                field_name="sessionDefaultChannelGroup",
                string_filter=Filter.StringFilter(value="Organic Search"),
            )
        )

    client = ga4_client(credentials_path)
    df = ga4_run_report(
        client,
        property_id,
        dimensions=["pagePath", "date"],
        metrics=[metric],
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        dimension_filter=dim_filter,
    )
    return df.rename(columns={metric: "traffic"})


# --------------------------------------------------------------------------- #
# 3. Redirect map (xử lý URL bị đổi)
# --------------------------------------------------------------------------- #
def load_redirects(path: str | None) -> dict[str, str]:
    """Đọc file CSV 2 cột old_path,new_path -> dict đã chuẩn hoá."""
    if not path:
        return {}
    rd = pd.read_csv(path, dtype=str).dropna()
    if not {"old_path", "new_path"}.issubset(rd.columns):
        raise ValueError("File redirects phải có 2 cột: old_path,new_path")
    return {normalize_path(o): normalize_path(n) for o, n in zip(rd["old_path"], rd["new_path"])}


def resolve_redirect(path: str, redirects: dict[str, str], max_hops: int = 10) -> str:
    """Đi theo chuỗi redirect A->B->C tới URL cuối cùng (chống vòng lặp)."""
    seen = {path}
    for _ in range(max_hops):
        nxt = redirects.get(path)
        if nxt is None or nxt in seen:
            break
        seen.add(nxt)
        path = nxt
    return path


# --------------------------------------------------------------------------- #
# 4. Xử lý dữ liệu & lọc
# --------------------------------------------------------------------------- #
def compute_content_decay(
    raw: pd.DataFrame,
    gd2_start: date,
    redirects: dict[str, str] | None = None,
    min_gd1_traffic: float = 100,
    min_drop_pct: float = 30,
) -> pd.DataFrame:
    """Nhận dữ liệu thô [pagePath, date, traffic] và trả về bảng báo cáo đã lọc + sắp xếp."""
    if raw.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    df = raw.copy()
    df["traffic"] = pd.to_numeric(df["traffic"], errors="coerce").fillna(0)
    df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["date"])

    # Chuẩn hoá URL + loại giá trị rác
    df["URL"] = df["pagePath"].map(normalize_path)
    df = df[~df["URL"].isin(INVALID_PATHS)]

    # Gộp URL cũ vào URL mới theo redirect map
    if redirects:
        df["URL"] = df["URL"].map(lambda p: resolve_redirect(p, redirects))

    # Gán giai đoạn
    df["giai_doan"] = np.where(df["date"] < pd.Timestamp(gd2_start), "GD1", "GD2")

    # Tổng traffic mỗi URL theo từng giai đoạn; URL thiếu GĐ nào -> 0
    pivot = (
        df.pivot_table(index="URL", columns="giai_doan", values="traffic", aggfunc="sum", fill_value=0)
        .reindex(columns=["GD1", "GD2"], fill_value=0)
        .reset_index()
    )
    pivot.columns.name = None

    # % Giảm = (GĐ1 - GĐ2) / GĐ1 * 100  (tránh chia cho 0)
    gd1 = pivot["GD1"].astype(float)
    gd2 = pivot["GD2"].astype(float)
    pivot["Ty_Le_Sut_Giam"] = np.where(gd1 > 0, (gd1 - gd2) / gd1.replace(0, np.nan) * 100, np.nan)

    # Lọc điều kiện
    mask = (pivot["GD1"] > min_gd1_traffic) & (pivot["Ty_Le_Sut_Giam"] > min_drop_pct)
    report = pivot.loc[mask].rename(
        columns={"GD1": "Traffic_3_Thang_Dau", "GD2": "Traffic_3_Thang_Cuoi"}
    )
    report["Traffic_3_Thang_Dau"] = report["Traffic_3_Thang_Dau"].round().astype(int)
    report["Traffic_3_Thang_Cuoi"] = report["Traffic_3_Thang_Cuoi"].round().astype(int)
    report["Ty_Le_Sut_Giam"] = report["Ty_Le_Sut_Giam"].round(2)

    report = report.sort_values(
        ["Ty_Le_Sut_Giam", "Traffic_3_Thang_Dau"], ascending=[False, False]
    ).reset_index(drop=True)
    return report[OUTPUT_COLUMNS]


# --------------------------------------------------------------------------- #
# 5. Dữ liệu giả lập (để chạy thử khi chưa có quyền GA4)
# --------------------------------------------------------------------------- #
def make_demo_data(start: date, end: date, seed: int = 42) -> tuple[pd.DataFrame, dict[str, str]]:
    rng = np.random.default_rng(seed)
    days = pd.date_range(start, end, freq="D")
    n = len(days)
    t = np.linspace(0, 1, n)
    rows = []

    def add(path, daily):
        for d, v in zip(days, daily):
            v = int(max(0, rng.poisson(max(v, 0))))
            if v > 0:
                rows.append((path, d.strftime("%Y%m%d"), v))

    topics = ["seo-onpage", "toi-uu-toc-do", "backlink", "content-marketing", "google-ads",
              "email-marketing", "keyword-research", "ga4-co-ban", "schema-markup", "local-seo"]
    for i in range(120):
        slug = f"/blog/{topics[i % len(topics)]}-{i}"
        base = rng.uniform(0.3, 30)
        kind = rng.choice(["stable", "decay", "grow", "strong_decay"], p=[0.5, 0.25, 0.15, 0.10])
        if kind == "stable":
            curve = base * (1 + 0.1 * np.sin(t * 12))
        elif kind == "decay":
            curve = base * (1 - rng.uniform(0.3, 0.6) * t)
        elif kind == "strong_decay":
            curve = base * np.clip(1 - rng.uniform(0.8, 1.2) * t, 0.02, None)
        else:
            curve = base * (1 + rng.uniform(0.3, 1.0) * t)
        add(slug, curve)

    # Biến thể URL: utm + '/' cuối + chữ hoa phải được gộp chung
    add("/blog/huong-dan-seo-2026/", 6 * (1 - 0.7 * t))
    add("/blog/huong-dan-seo-2026?utm_source=facebook", 3 * (1 - 0.7 * t))
    add("/Blog/Huong-Dan-SEO-2026", 1 * (1 - 0.7 * t))

    # Bài bị xoá: có traffic GĐ1, hoàn toàn mất ở GĐ2
    half = n // 2
    add("/blog/khuyen-mai-black-friday-2025", np.r_[np.full(half, 8.0), np.zeros(n - half)])

    # Bài đổi URL: traffic chuyển từ slug cũ sang slug mới -> không phải decay thật
    add("/blog/cach-viet-bai-chuan-seo", np.r_[np.full(half, 10.0), np.zeros(n - half)])
    add("/blog/cach-viet-bai-chuan-seo-2026", np.r_[np.zeros(half), np.full(n - half, 9.5)])

    # Rác của GA4
    add("(not set)", np.full(n, 2.0))

    redirects = {"/blog/cach-viet-bai-chuan-seo": "/blog/cach-viet-bai-chuan-seo-2026"}
    return pd.DataFrame(rows, columns=["pagePath", "date", "traffic"]), redirects


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Phát hiện Content Decay từ GA4")
    p.add_argument("--property-id", default=os.getenv("GA4_PROPERTY_ID"), help="GA4 Property ID (chỉ số)")
    p.add_argument("--credentials", default=None, help="Đường dẫn file JSON service account")
    p.add_argument("--metric", default="sessions",
                   help="Metric GA4 dùng làm traffic (mặc định: sessions; có thể dùng organicGoogleSearchSessions nếu property hỗ trợ)")
    p.add_argument("--organic-only", action="store_true",
                   help="Chỉ tính traffic tự nhiên (sessionDefaultChannelGroup = Organic Search)")
    p.add_argument("--redirects", default=None, help="CSV old_path,new_path để gộp URL bị đổi")
    p.add_argument("--end-date", default=None, help="Ngày kết thúc YYYY-MM-DD (mặc định: hôm qua)")
    p.add_argument("--min-traffic", type=float, default=100, help="Traffic GĐ1 tối thiểu (mặc định 100)")
    p.add_argument("--min-drop", type=float, default=30, help="%% giảm tối thiểu (mặc định 30)")
    p.add_argument("--output", default="content_decay_report.csv")
    p.add_argument("--demo", action="store_true", help="Dùng dữ liệu giả lập, không gọi API")
    return p.parse_args(argv)


def main(argv=None) -> int:
    load_env()
    args = parse_args(argv)
    end = date.fromisoformat(args.end_date) if args.end_date else None
    periods = compute_periods(end)

    print("=" * 70)
    print("CONTENT DECAY DETECTOR")
    print(f"  GĐ1 (3 tháng đầu) : {periods['start']} -> {periods['gd1_end']}")
    print(f"  GĐ2 (3 tháng cuối): {periods['gd2_start']} -> {periods['end']}")
    print("=" * 70)

    redirects = load_redirects(args.redirects)
    if args.demo:
        print("[DEMO] Đang dùng dữ liệu giả lập...")
        raw, demo_redirects = make_demo_data(periods["start"], periods["end"])
        redirects = {**demo_redirects, **redirects}
    else:
        if not args.property_id:
            print("LỖI: thiếu --property-id (hoặc biến môi trường GA4_PROPERTY_ID).", file=sys.stderr)
            return 2
        print(f"Đang lấy dữ liệu GA4 property {args.property_id} (metric = {args.metric})...")
        try:
            raw = fetch_ga4_data(args.property_id, periods["start"], periods["end"],
                                 args.metric, args.organic_only, args.credentials)
        except Exception as e:  # lỗi quyền, sai property id, sai metric...
            print(f"LỖI khi gọi GA4 API: {e}", file=sys.stderr)
            return 1

    print(f"Đã nhận {len(raw):,} dòng dữ liệu, {raw['pagePath'].nunique():,} pagePath.")
    if redirects:
        print(f"Áp dụng {len(redirects)} redirect để gộp URL bị đổi.")

    report = compute_content_decay(raw, periods["gd2_start"], redirects,
                                   args.min_traffic, args.min_drop)
    report.to_csv(args.output, index=False, encoding="utf-8-sig")  # utf-8-sig để Excel đọc đúng

    print(f"\nTìm thấy {len(report)} bài viết bị Content Decay "
          f"(giảm > {args.min_drop:g}% và GĐ1 > {args.min_traffic:g} sessions).")
    if not report.empty:
        with pd.option_context("display.max_colwidth", 60, "display.width", 120):
            print(report.head(20).to_string(index=False))
        lost = report[report["Traffic_3_Thang_Cuoi"] == 0]
        if not lost.empty:
            print(f"\n⚠ {len(lost)} URL mất 100% traffic ở GĐ2 — kiểm tra xem bài có bị xoá, "
                  "noindex hoặc đổi URL không (nếu đổi URL, thêm vào file --redirects):")
            for u in lost["URL"]:
                print(f"   - {u}")
    print(f"\nĐã lưu báo cáo: {os.path.abspath(args.output)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
BÀI TẬP 2 – Thám tử SEO: cảnh báo lỗi Keyword Cannibalization từ Google Search Console.

Luồng xử lý:
  1. Lấy dữ liệu (Search Console API): 30 ngày gần nhất, dimensions = [query, page],
     metrics = clicks, impressions, ctr, position. Tự phân trang (25.000 dòng/lần).
  2. Thuật toán phát hiện:
       - Group by theo query
       - Đếm số URL (page) duy nhất cho mỗi query
       - Giữ lại query có >= 2 URL và impressions của TỪNG URL đều > 0
  3. Đầu ra: báo cáo phân cấp (Từ khoá -> danh sách URL kèm Clicks / Impressions / Position)
       - In ra console
       - keyword_cannibalization_report.txt (phân cấp, dễ đọc)
       - keyword_cannibalization_report.csv (dạng bảng, mở bằng Excel để lọc/sort)

Bổ sung giúp chuyên viên SEO ra quyết định:
  - Đánh dấu URL chính (★) = URL nhiều clicks nhất (hoà thì xét impressions, rồi position).
  - Tỷ trọng impressions của từng URL và mức độ nghiêm trọng (CAO / TRUNG BÌNH / THẤP)
    kèm gợi ý hành động (gộp 301 / phân biệt lại nội dung / theo dõi).
  - URL có #fragment (jump link, featured snippet) được gộp về URL gốc để tránh báo động giả.

Ví dụ chạy:
  python bai2_keyword_cannibalization.py --site-url sc-domain:example.com --credentials sa.json
  python bai2_keyword_cannibalization.py --site-url https://example.com/ --exclude-regex "tên thương hiệu"
  python bai2_keyword_cannibalization.py --demo
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

from seo_common import GSC_SCOPES, get_credentials, load_env

GSC_ROW_LIMIT = 25_000  # giới hạn tối đa mỗi request của Search Analytics API


# --------------------------------------------------------------------------- #
# 1. Lấy dữ liệu GSC
# --------------------------------------------------------------------------- #
def gsc_date_range(days: int = 30, lag_days: int = 3, end: date | None = None) -> tuple[date, date]:
    """GSC thường trễ ~2-3 ngày, nên mặc định kết thúc ở (hôm nay - 3)."""
    end = end or (date.today() - timedelta(days=lag_days))
    start = end - timedelta(days=days - 1)
    return start, end


def fetch_gsc_data(site_url: str, start: date, end: date,
                   credentials_path: str | None = None, search_type: str = "web") -> pd.DataFrame:
    from googleapiclient.discovery import build

    creds = get_credentials(GSC_SCOPES, credentials_path)
    service = build("searchconsole", "v1", credentials=creds, cache_discovery=False)

    rows, start_row = [], 0
    while True:
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": ["query", "page"],
            "type": search_type,
            "rowLimit": GSC_ROW_LIMIT,
            "startRow": start_row,
        }
        resp = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        batch = resp.get("rows", [])
        for r in batch:
            rows.append({
                "query": r["keys"][0],
                "page": r["keys"][1],
                "clicks": r.get("clicks", 0),
                "impressions": r.get("impressions", 0),
                "ctr": r.get("ctr", 0.0),
                "position": r.get("position", 0.0),
            })
        if len(batch) < GSC_ROW_LIMIT:
            break
        start_row += GSC_ROW_LIMIT
    return pd.DataFrame(rows, columns=["query", "page", "clicks", "impressions", "ctr", "position"])


# --------------------------------------------------------------------------- #
# 2. Thuật toán phát hiện
# --------------------------------------------------------------------------- #
def clean_gsc_data(df: pd.DataFrame, exclude_regex: str | None = None) -> pd.DataFrame:
    """Chuẩn hoá: bỏ #fragment khỏi URL rồi gộp lại; bỏ query thương hiệu nếu cần."""
    d = df.copy()
    for c in ["clicks", "impressions", "position"]:
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0)
    d["query"] = d["query"].astype(str).str.strip().str.lower()
    d["page"] = d["page"].astype(str).str.split("#", n=1).str[0]

    if exclude_regex:
        d = d[~d["query"].str.contains(exclude_regex, flags=re.IGNORECASE, regex=True)]

    # position trung bình gộp có trọng số theo impressions
    d["_pos_x_impr"] = d["position"] * d["impressions"]
    g = d.groupby(["query", "page"], as_index=False).agg(
        clicks=("clicks", "sum"), impressions=("impressions", "sum"), _pxi=("_pos_x_impr", "sum"),
        _pos=("position", "mean"),
    )
    g["position"] = np.where(g["impressions"] > 0, g["_pxi"] / g["impressions"].replace(0, np.nan), g["_pos"])
    g["ctr"] = np.where(g["impressions"] > 0, g["clicks"] / g["impressions"].replace(0, np.nan), 0.0)
    return g[["query", "page", "clicks", "impressions", "ctr", "position"]]


def detect_cannibalization(df: pd.DataFrame, min_urls: int = 2, min_impressions: float = 0) -> pd.DataFrame:
    """Trả về các dòng (query, page) thuộc những query bị cannibalization.

    Điều kiện: query có >= min_urls URL duy nhất và impressions của từng URL > min_impressions.
    """
    cols = ["query", "page", "clicks", "impressions", "ctr", "position"]
    if df.empty:
        return pd.DataFrame(columns=cols)

    # Group by query: số URL duy nhất + impressions nhỏ nhất trong nhóm
    stats = df.groupby("query").agg(so_url=("page", "nunique"), min_impr=("impressions", "min"))
    bad_queries = stats[(stats["so_url"] >= min_urls) & (stats["min_impr"] > min_impressions)].index
    return df[df["query"].isin(bad_queries)][cols].copy()


def classify(conflicts: pd.DataFrame) -> pd.DataFrame:
    """Bổ sung các cột hỗ trợ ra quyết định cho chuyên viên SEO."""
    if conflicts.empty:
        return conflicts.assign(so_url=[], tong_clicks=[], tong_impr=[], ty_trong_impr=[],
                                url_chinh=[], muc_do=[], goi_y=[])
    d = conflicts.sort_values(["query", "clicks", "impressions", "position"],
                              ascending=[True, False, False, True]).copy()
    grp = d.groupby("query")
    d["so_url"] = grp["page"].transform("nunique")
    d["tong_clicks"] = grp["clicks"].transform("sum")
    d["tong_impr"] = grp["impressions"].transform("sum")
    d["ty_trong_impr"] = d["impressions"] / d["tong_impr"] * 100
    d["url_chinh"] = grp.cumcount() == 0  # dòng đầu mỗi nhóm sau khi sort = URL chính

    def severity(g: pd.DataFrame) -> tuple[str, str]:
        main = g.iloc[0]
        others = g.iloc[1:]
        best_other_share = others["ty_trong_impr"].max()
        pos_gap = (others["position"] - main["position"]).abs().min()
        both_top20 = main["position"] <= 20 and (others["position"] <= 20).any()
        if best_other_share >= 20 and both_top20 and pos_gap <= 10:
            return ("CAO", "Các URL chia đều hiển thị & thứ hạng sát nhau -> cân nhắc GỘP nội dung "
                           "vào URL chính và 301 redirect URL phụ.")
        if best_other_share >= 10:
            return ("TRUNG BÌNH", "URL phụ vẫn lấy đáng kể hiển thị -> phân biệt lại search intent / "
                                  "tối ưu lại title, H1, internal link trỏ về URL chính.")
        return ("THẤP", "Google đã ưu tiên rõ 1 URL -> theo dõi thêm; có thể bỏ qua hoặc thêm canonical.")

    sev = pd.DataFrame(
        [(q, *severity(g)) for q, g in d.groupby("query", sort=False)],
        columns=["query", "muc_do", "goi_y"],
    ).set_index("query")
    d = d.join(sev, on="query")

    order = {"CAO": 0, "TRUNG BÌNH": 1, "THẤP": 2}
    d["_ord"] = d["muc_do"].map(order)
    d = d.sort_values(["_ord", "tong_impr", "query", "url_chinh", "clicks", "impressions"],
                      ascending=[True, False, True, False, False, False]).drop(columns="_ord")
    return d.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 3. Báo cáo
# --------------------------------------------------------------------------- #
def build_text_report(d: pd.DataFrame, start: date, end: date, site: str, top: int | None = None) -> str:
    lines = []
    n_q = d["query"].nunique() if not d.empty else 0
    lines.append("=" * 90)
    lines.append("BÁO CÁO KEYWORD CANNIBALIZATION")
    lines.append(f"Website : {site}")
    lines.append(f"Kỳ dữ liệu: {start} -> {end}")
    lines.append(f"Số từ khoá bị xung đột: {n_q}")
    if n_q:
        counts = d.drop_duplicates("query")["muc_do"].value_counts()
        lines.append("  " + " | ".join(f"{k}: {counts.get(k, 0)}" for k in ["CAO", "TRUNG BÌNH", "THẤP"]))
    lines.append("Chú thích: ★ = URL chính (nhiều clicks nhất) | Pos = thứ hạng trung bình")
    lines.append("=" * 90)

    queries = list(dict.fromkeys(d["query"])) if not d.empty else []
    if top:
        queries = queries[:top]
    for i, q in enumerate(queries, 1):
        g = d[d["query"] == q]
        first = g.iloc[0]
        lines.append("")
        lines.append(f"[{i}] Từ khoá: \"{q}\"   — Mức độ: {first['muc_do']}")
        lines.append(f"    {int(first['so_url'])} URL tranh nhau | Tổng clicks: {int(first['tong_clicks']):,} "
                     f"| Tổng impressions: {int(first['tong_impr']):,}")
        for j, (_, r) in enumerate(g.iterrows()):
            branch = "└──" if j == len(g) - 1 else "├──"
            star = "★" if r["url_chinh"] else " "
            lines.append(f"    {branch} {star} {r['page']}")
            pad = "    " if j == len(g) - 1 else "│   "
            lines.append(f"    {pad}     Clicks: {int(r['clicks']):>6,} | Impressions: {int(r['impressions']):>8,} "
                         f"({r['ty_trong_impr']:5.1f}%) | Pos: {r['position']:6.1f} | CTR: {r['ctr'] * 100:5.2f}%")
        lines.append(f"    ➜ Gợi ý: {first['goi_y']}")
    if top and n_q > top:
        lines.append(f"\n... còn {n_q - top} từ khoá khác, xem file báo cáo đầy đủ.")
    return "\n".join(lines)


def to_csv_table(d: pd.DataFrame) -> pd.DataFrame:
    out = d.rename(columns={
        "query": "Tu_Khoa", "page": "URL", "clicks": "Clicks", "impressions": "Impressions",
        "ctr": "CTR_%", "position": "Position", "so_url": "So_URL_Xung_Dot",
        "tong_clicks": "Tong_Clicks_Tu_Khoa", "tong_impr": "Tong_Impressions_Tu_Khoa",
        "ty_trong_impr": "Ty_Trong_Impressions_%", "url_chinh": "URL_Chinh",
        "muc_do": "Muc_Do", "goi_y": "Goi_Y",
    })
    cols = ["Muc_Do", "Tu_Khoa", "So_URL_Xung_Dot", "Tong_Impressions_Tu_Khoa", "Tong_Clicks_Tu_Khoa",
            "URL", "URL_Chinh", "Clicks", "Impressions", "Ty_Trong_Impressions_%", "Position", "CTR_%", "Goi_Y"]
    if out.empty:
        return pd.DataFrame(columns=cols)
    out["CTR_%"] = (out["CTR_%"] * 100).round(2)
    out["Position"] = out["Position"].round(1)
    out["Ty_Trong_Impressions_%"] = out["Ty_Trong_Impressions_%"].round(1)
    out["URL_Chinh"] = out["URL_Chinh"].map({True: "x", False: ""})
    for c in ["Clicks", "Impressions", "Tong_Clicks_Tu_Khoa", "Tong_Impressions_Tu_Khoa", "So_URL_Xung_Dot"]:
        out[c] = out[c].round().astype(int)
    return out[cols]


# --------------------------------------------------------------------------- #
# 4. Dữ liệu giả lập
# --------------------------------------------------------------------------- #
def make_demo_data(seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    site = "https://example.com"
    rows = []

    def add(q, page, impr, pos, ctr=None):
        impr = int(impr)
        ctr = ctr if ctr is not None else max(0.002, 0.35 * np.exp(-0.35 * (pos - 1)))
        clicks = int(rng.binomial(impr, min(ctr, 0.9)))
        rows.append((q, site + page, clicks, impr, clicks / impr if impr else 0, pos))

    # Từ khoá bình thường: 1 URL
    for i in range(150):
        add(f"tu khoa thuong {i}", f"/blog/bai-viet-{i}", rng.integers(5, 3000), rng.uniform(1, 40))

    # Xung đột mức CAO: 2 bài chia đều, thứ hạng sát nhau
    add("cách viết bài chuẩn seo", "/blog/cach-viet-bai-chuan-seo", 5200, 7.8)
    add("cách viết bài chuẩn seo", "/blog/huong-dan-viet-content-seo", 4100, 9.4)
    add("seo onpage là gì", "/blog/seo-onpage-la-gi", 3800, 5.1)
    add("seo onpage là gì", "/blog/checklist-seo-onpage", 2900, 6.7)
    add("seo onpage là gì", "/dich-vu/seo-onpage", 900, 14.2)
    add("ga4 là gì", "/blog/ga4-la-gi", 2600, 4.3)
    add("ga4 là gì", "/blog/huong-dan-ga4-cho-nguoi-moi", 2200, 6.0)

    # Xung đột mức TRUNG BÌNH
    add("bảng giá dịch vụ seo", "/bang-gia-seo", 1900, 3.2)
    add("bảng giá dịch vụ seo", "/blog/chi-phi-seo-bao-nhieu", 380, 18.5)
    add("backlink là gì", "/blog/backlink-la-gi", 4400, 2.4)
    add("backlink là gì", "/blog/cach-dat-backlink", 620, 25.0)

    # Xung đột mức THẤP: Google đã chọn rõ 1 URL
    add("schema markup", "/blog/schema-markup", 3000, 3.1)
    add("schema markup", "/blog/seo-ky-thuat-tong-hop", 60, 48.0)

    # Fragment (#) cùng 1 trang -> KHÔNG được coi là xung đột
    add("tối ưu tốc độ website", "/blog/toi-uu-toc-do-website", 2100, 4.0)
    add("tối ưu tốc độ website", "/blog/toi-uu-toc-do-website#nen-anh", 450, 3.5)

    # Query thương hiệu (có thể loại bằng --exclude-regex "example")
    add("example seo agency", "/", 800, 1.2)
    add("example seo agency", "/gioi-thieu", 300, 2.5)

    return pd.DataFrame(rows, columns=["query", "page", "clicks", "impressions", "ctr", "position"])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Phát hiện Keyword Cannibalization từ Google Search Console")
    p.add_argument("--site-url", default=os.getenv("GSC_SITE_URL"),
                   help="Property GSC: 'sc-domain:example.com' hoặc 'https://example.com/'")
    p.add_argument("--credentials", default=None, help="File JSON service account")
    p.add_argument("--days", type=int, default=30, help="Số ngày gần nhất (mặc định 30)")
    p.add_argument("--min-urls", type=int, default=2, help="Số URL tối thiểu để coi là xung đột (mặc định 2)")
    p.add_argument("--min-impressions", type=float, default=0,
                   help="Mỗi URL phải có impressions > giá trị này (mặc định 0)")
    p.add_argument("--exclude-regex", default=None, help="Regex loại bỏ query (vd: tên thương hiệu)")
    p.add_argument("--top", type=int, default=20, help="Số từ khoá in ra console (0 = tất cả)")
    p.add_argument("--output-prefix", default="keyword_cannibalization_report")
    p.add_argument("--demo", action="store_true", help="Dùng dữ liệu giả lập, không gọi API")
    return p.parse_args(argv)


def main(argv=None) -> int:
    load_env()
    args = parse_args(argv)
    start, end = gsc_date_range(args.days)

    if args.demo:
        site = "https://example.com/ (DEMO)"
        print("[DEMO] Đang dùng dữ liệu giả lập...")
        raw = make_demo_data()
    else:
        if not args.site_url:
            print("LỖI: thiếu --site-url (hoặc biến môi trường GSC_SITE_URL).", file=sys.stderr)
            return 2
        site = args.site_url
        print(f"Đang lấy dữ liệu GSC cho {site} ({start} -> {end})...")
        try:
            raw = fetch_gsc_data(site, start, end, args.credentials)
        except Exception as e:
            print(f"LỖI khi gọi Search Console API: {e}", file=sys.stderr)
            return 1

    print(f"Đã nhận {len(raw):,} cặp (query, page), {raw['query'].nunique():,} từ khoá.")
    clean = clean_gsc_data(raw, args.exclude_regex)
    conflicts = detect_cannibalization(clean, args.min_urls, args.min_impressions)
    report = classify(conflicts)

    print(build_text_report(report, start, end, site, top=args.top or None))

    txt_path = f"{args.output_prefix}.txt"
    csv_path = f"{args.output_prefix}.csv"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(build_text_report(report, start, end, site))
    to_csv_table(report).to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\nĐã lưu: {os.path.abspath(txt_path)}")
    print(f"Đã lưu: {os.path.abspath(csv_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

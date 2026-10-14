"""Kiểm thử các hàm xử lý dữ liệu (không cần gọi API thật). Chạy: pytest -q"""

import os
import sys
from datetime import date

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bai1_content_decay as b1  # noqa: E402
import bai2_keyword_cannibalization as b2  # noqa: E402
import bai3_pagespeed_ga4 as b3  # noqa: E402
from seo_common import build_full_url, normalize_path  # noqa: E402


# ------------------------------------------------------------------ common
@pytest.mark.parametrize("raw,expected", [
    ("/Blog/Bai-Viet/", "/blog/bai-viet"),
    ("/blog/bai-viet?utm_source=fb&x=1", "/blog/bai-viet"),
    ("/blog//bai-viet#muc-1", "/blog/bai-viet"),
    ("https://example.com/blog/a/", "/blog/a"),
    ("/", "/"),
    ("(not set)", "(not set)"),
])
def test_normalize_path(raw, expected):
    assert normalize_path(raw) == expected


def test_build_full_url():
    assert build_full_url("https://example.com/", "/blog/a") == "https://example.com/blog/a"
    assert build_full_url("https://example.com", "/") == "https://example.com/"


# ------------------------------------------------------------------ Bài 1
def _rows(path, start, end, per_day):
    return [(path, d.strftime("%Y%m%d"), per_day) for d in pd.date_range(start, end)]


def test_periods_split_six_months():
    p = b1.compute_periods(date(2026, 10, 13))
    assert p["start"] == date(2026, 4, 14)
    assert p["gd1_end"] == date(2026, 7, 13)
    assert p["gd2_start"] == date(2026, 7, 14)
    assert p["end"] == date(2026, 10, 13)


@pytest.fixture
def periods():
    return b1.compute_periods(date(2026, 10, 13))


def test_decay_formula_and_filter(periods):
    rows = []
    rows += _rows("/a", periods["start"], periods["gd1_end"], 10)        # GĐ1 ~ 910
    rows += _rows("/a", periods["gd2_start"], periods["end"], 5)         # GĐ2 ~ 460 -> giảm ~49%
    rows += _rows("/stable", periods["start"], periods["end"], 10)       # không giảm
    rows += _rows("/small", periods["start"], periods["gd1_end"], 1)     # GĐ1 = 91 <= 100 -> loại
    df = pd.DataFrame(rows, columns=["pagePath", "date", "traffic"])
    rep = b1.compute_content_decay(df, periods["gd2_start"])
    assert list(rep.columns) == b1.OUTPUT_COLUMNS
    assert rep["URL"].tolist() == ["/a"]
    gd1, gd2 = rep.iloc[0]["Traffic_3_Thang_Dau"], rep.iloc[0]["Traffic_3_Thang_Cuoi"]
    assert rep.iloc[0]["Ty_Le_Sut_Giam"] == pytest.approx(round((gd1 - gd2) / gd1 * 100, 2))


def test_no_traffic_in_gd2_is_100_percent(periods):
    df = pd.DataFrame(_rows("/deleted", periods["start"], periods["gd1_end"], 5),
                      columns=["pagePath", "date", "traffic"])
    rep = b1.compute_content_decay(df, periods["gd2_start"])
    assert rep.iloc[0]["Traffic_3_Thang_Cuoi"] == 0
    assert rep.iloc[0]["Ty_Le_Sut_Giam"] == 100.0


def test_redirect_chain_merges_renamed_url(periods):
    rows = _rows("/old", periods["start"], periods["gd1_end"], 10) + \
           _rows("/new-2026", periods["gd2_start"], periods["end"], 10)
    df = pd.DataFrame(rows, columns=["pagePath", "date", "traffic"])
    # Không có redirect: /old bị báo giảm 100% (báo động giả)
    assert "/old" in b1.compute_content_decay(df, periods["gd2_start"])["URL"].tolist()
    # Có chuỗi redirect /old -> /new -> /new-2026: không còn bị coi là decay
    redirects = {"/old": "/new", "/new": "/new-2026"}
    assert b1.compute_content_decay(df, periods["gd2_start"], redirects).empty


def test_redirect_loop_guard():
    assert b1.resolve_redirect("/a", {"/a": "/b", "/b": "/a"}) == "/b"


def test_boundaries_strictly_greater(periods):
    # GĐ1 = 200, GĐ2 = 140 -> giảm đúng 30% -> KHÔNG lọt (yêu cầu > 30%)
    df = pd.DataFrame([("/x", periods["start"].strftime("%Y%m%d"), 200),
                       ("/x", periods["end"].strftime("%Y%m%d"), 140)],
                      columns=["pagePath", "date", "traffic"])
    assert b1.compute_content_decay(df, periods["gd2_start"]).empty


def test_sorted_desc_and_variants_merged(periods):
    s, e = periods["start"].strftime("%Y%m%d"), periods["end"].strftime("%Y%m%d")
    df = pd.DataFrame([
        ("/p1/", s, 300), ("/P1?utm=a", s, 300), ("/p1", e, 300),   # 600 -> 300: 50%
        ("/p2", s, 500), ("/p2", e, 100),                          # 80%
        ("(not set)", s, 9999),
    ], columns=["pagePath", "date", "traffic"])
    rep = b1.compute_content_decay(df, periods["gd2_start"])
    assert rep["URL"].tolist() == ["/p2", "/p1"]
    assert rep.iloc[1]["Traffic_3_Thang_Dau"] == 600


def test_empty_input(periods):
    assert b1.compute_content_decay(pd.DataFrame(columns=["pagePath", "date", "traffic"]),
                                    periods["gd2_start"]).empty


# ------------------------------------------------------------------ Bài 2
def _gsc(rows):
    return pd.DataFrame(rows, columns=["query", "page", "clicks", "impressions", "ctr", "position"])


def test_detect_cannibalization_basic():
    df = _gsc([
        ("seo", "https://x.com/a", 10, 100, .1, 3), ("seo", "https://x.com/b", 5, 80, .06, 5),
        ("ga4", "https://x.com/c", 10, 100, .1, 3),
        ("zero", "https://x.com/d", 1, 10, .1, 3), ("zero", "https://x.com/e", 0, 0, 0, 0),
    ])
    out = b2.detect_cannibalization(b2.clean_gsc_data(df))
    assert set(out["query"]) == {"seo"}
    assert len(out) == 2


def test_fragment_not_counted_as_conflict():
    df = _gsc([("tốc độ", "https://x.com/a", 10, 100, .1, 3.0),
               ("tốc độ", "https://x.com/a#muc-2", 2, 100, .02, 5.0)])
    clean = b2.clean_gsc_data(df)
    assert len(clean) == 1
    assert clean.iloc[0]["impressions"] == 200
    assert clean.iloc[0]["position"] == pytest.approx(4.0)  # trung bình có trọng số
    assert b2.detect_cannibalization(clean).empty


def test_exclude_regex_and_report():
    df = _gsc([("brand x", "https://x.com/", 10, 100, .1, 1), ("brand x", "https://x.com/about", 5, 90, .05, 2),
               ("seo", "https://x.com/a", 10, 100, .1, 3), ("seo", "https://x.com/b", 5, 80, .06, 5)])
    rep = b2.classify(b2.detect_cannibalization(b2.clean_gsc_data(df, exclude_regex="brand")))
    assert set(rep["query"]) == {"seo"}
    main = rep[rep["url_chinh"]]
    assert main.iloc[0]["page"] == "https://x.com/a"
    assert main.iloc[0]["muc_do"] == "CAO"
    text = b2.build_text_report(rep, date(2026, 9, 1), date(2026, 9, 30), "x.com")
    assert '"seo"' in text and "★ https://x.com/a" in text
    table = b2.to_csv_table(rep)
    assert {"Tu_Khoa", "URL", "Clicks", "Impressions", "Position"}.issubset(table.columns)


def test_demo_runs(tmp_path):
    prefix = str(tmp_path / "r")
    assert b2.main(["--demo", "--output-prefix", prefix]) == 0
    assert os.path.exists(prefix + ".csv") and os.path.exists(prefix + ".txt")


# ------------------------------------------------------------------ Bài 3
def test_parse_psi_score_times_100():
    data = {"lighthouseResult": {"categories": {"performance": {"score": 0.37}},
                                 "audits": {"largest-contentful-paint": {"numericValue": 4500}}}}
    r = b3.parse_psi_response(data)
    assert r["PageSpeed_Mobile"] == 37
    assert r["LCP_s"] == 4.5
    assert np.isnan(b3.parse_psi_response({})["PageSpeed_Mobile"])


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self._p, self.headers, self.text = status, payload or {}, headers or {}, ""

    def json(self):
        return self._p


class _Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        return self.responses.pop(0)


def test_fetch_psi_retries_on_429(monkeypatch):
    sleeps = []
    monkeypatch.setattr(b3.time, "sleep", lambda s: sleeps.append(s))
    ok = {"lighthouseResult": {"categories": {"performance": {"score": 0.82}}}}
    sess = _Session([_Resp(429, headers={"Retry-After": "3"}), _Resp(503), _Resp(200, ok)])
    r = b3.fetch_psi("https://x.com", "KEY", session=sess)
    assert r["PageSpeed_Mobile"] == 82 and r["PSI_Loi"] == ""
    assert sleeps == [3.0, 10]
    assert sess.calls[0]["strategy"] == "mobile" and sess.calls[0]["key"] == "KEY"


def test_fetch_psi_non_retryable_error(monkeypatch):
    monkeypatch.setattr(b3.time, "sleep", lambda s: None)
    sess = _Session([_Resp(400, {"error": {"message": "NOT_HTML"}})])
    r = b3.fetch_psi("https://x.com/file.pdf", session=sess)
    assert "NOT_HTML" in r["PSI_Loi"] and len(sess.calls) == 1


def test_loop_sleeps_between_requests(monkeypatch):
    sleeps = []
    monkeypatch.setattr(b3.time, "sleep", lambda s: sleeps.append(s))
    fake = lambda u: {**b3.parse_psi_response({}), "PageSpeed_Mobile": 40, "PSI_Loi": ""}  # noqa: E731
    df = b3.run_psi_loop(["u1", "u2", "u3"], fake, sleep_s=2.0)
    assert len(df) == 3 and sleeps == [2.0, 2.0]


def test_cache_skips_measured_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(b3.time, "sleep", lambda s: None)
    cache = str(tmp_path / "c.json")
    calls = []

    def fake(u):
        calls.append(u)
        return {**b3.parse_psi_response({}), "PageSpeed_Mobile": 40, "PSI_Loi": ""}

    b3.run_psi_loop(["u1", "u2"], fake, 0, cache)
    b3.run_psi_loop(["u1", "u2", "u3"], fake, 0, cache)
    assert calls == ["u1", "u2", "u3"]


def test_merge_and_red_alert():
    ga4 = b3.prepare_ga4(pd.DataFrame({
        "landingPage": ["/a", "/b", "/c", "(not set)"],
        "sessions": [300, 200, 100, 999],
        "bounceRate": [0.80, 0.75, 0.90, 0.99],
        "averageSessionDuration": [20, 30, 10, 1],
    }), "https://x.com")
    assert len(ga4) == 3 and ga4["Bounce_Rate_%"].tolist() == [80.0, 75.0, 90.0]
    psi = pd.DataFrame({"URL": ["https://x.com/a", "https://x.com/b", "https://x.com/c"],
                        "PageSpeed_Mobile": [30, 50, np.nan], "LCP_s": [5, 3, np.nan],
                        "TBT_ms": [900, 300, np.nan], "CLS": [.2, .1, np.nan], "PSI_Loi": ["", "", "err"]})
    merged = b3.merge_data(ga4, psi)
    alert = b3.red_alert(merged)
    # /b có điểm = 50 (không < 50); /c lỗi PSI -> không bị đưa vào nhầm
    assert alert["URL"].tolist() == ["https://x.com/a"]


def test_demo_end_to_end(tmp_path):
    assert b3.main(["--demo", "--output-dir", str(tmp_path)]) == 0
    for f in ["pagespeed_red_alert.csv", "ga4_pagespeed_merged.csv", "pagespeed_vs_bounce.png"]:
        assert os.path.exists(tmp_path / f)
    assert b1.main(["--demo", "--output", str(tmp_path / "cd.csv")]) == 0


def test_count_redirects():
    m = pd.DataFrame({"URL": ["https://a.com/x", "https://a.com/y/", "https://a.com/z"],
                      "Final_URL": ["https://b.com/", "https://a.com/y", ""]})
    assert b3.count_redirects(m) == 1

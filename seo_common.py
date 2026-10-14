"""
Các hàm dùng chung cho 3 bài tập: xác thực Google, gọi GA4 Data API có phân trang,
chuẩn hoá URL.

Xác thực (chọn 1 trong 2 cách):
  1. Service Account: tải file JSON key, truyền qua --credentials hoặc biến môi trường
     GOOGLE_APPLICATION_CREDENTIALS. Nhớ thêm email của service account vào:
       - GA4: Admin > Property access management > quyền Viewer
       - GSC: Settings > Users and permissions > quyền Restricted/Full
  2. Application Default Credentials (ADC):
       gcloud auth application-default login \
         --scopes=https://www.googleapis.com/auth/analytics.readonly,\
https://www.googleapis.com/auth/webmasters.readonly,\
https://www.googleapis.com/auth/cloud-platform
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

import pandas as pd

GA4_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
GSC_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


# --------------------------------------------------------------------------- #
# Đọc file .env (không bắt buộc cài python-dotenv)
# --------------------------------------------------------------------------- #
def load_env(path: str = ".env") -> None:
    """Nạp biến môi trường từ file .env nếu có (KEY=VALUE mỗi dòng)."""
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path)
        return
    except ImportError:
        pass
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- #
# Xác thực
# --------------------------------------------------------------------------- #
def get_credentials(scopes: list[str], credentials_path: str | None = None):
    """Trả về đối tượng credentials của google-auth.

    Ưu tiên file service account được chỉ định; nếu không có thì dùng ADC.
    """
    import google.auth
    from google.oauth2 import service_account

    path = credentials_path or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if path:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Không tìm thấy file credentials: {path}")
        return service_account.Credentials.from_service_account_file(path, scopes=scopes)
    creds, _ = google.auth.default(scopes=scopes)
    return creds


# --------------------------------------------------------------------------- #
# GA4 Data API
# --------------------------------------------------------------------------- #
def ga4_client(credentials_path: str | None = None):
    from google.analytics.data_v1beta import BetaAnalyticsDataClient

    creds = get_credentials(GA4_SCOPES, credentials_path)
    return BetaAnalyticsDataClient(credentials=creds)


def ga4_run_report(
    client,
    property_id: str,
    dimensions: list[str],
    metrics: list[str],
    start_date: str,
    end_date: str,
    *,
    order_by_metric: str | None = None,
    limit: int | None = None,
    dimension_filter=None,
    page_size: int = 100_000,
) -> pd.DataFrame:
    """Gọi runReport và tự động phân trang (offset) để lấy đủ toàn bộ dòng.

    Trả về DataFrame với cột = tên dimension + tên metric (metric đã ép kiểu số).
    """
    from google.analytics.data_v1beta.types import (
        DateRange,
        Dimension,
        Metric,
        OrderBy,
        RunReportRequest,
    )

    rows: list[list] = []
    offset = 0
    want = limit  # None = lấy tất cả
    while True:
        this_page = page_size if want is None else min(page_size, want - len(rows))
        request = RunReportRequest(
            property=f"properties/{property_id}",
            dimensions=[Dimension(name=d) for d in dimensions],
            metrics=[Metric(name=m) for m in metrics],
            date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
            limit=this_page,
            offset=offset,
            keep_empty_rows=False,
        )
        if order_by_metric:
            request.order_bys = [
                OrderBy(metric=OrderBy.MetricOrderBy(metric_name=order_by_metric), desc=True)
            ]
        if dimension_filter is not None:
            request.dimension_filter = dimension_filter

        response = client.run_report(request)
        for r in response.rows:
            rows.append(
                [v.value for v in r.dimension_values] + [v.value for v in r.metric_values]
            )
        offset += len(response.rows)
        total = response.row_count
        if len(response.rows) == 0 or offset >= total:
            break
        if want is not None and len(rows) >= want:
            break

    df = pd.DataFrame(rows, columns=dimensions + metrics)
    for m in metrics:
        df[m] = pd.to_numeric(df[m], errors="coerce").fillna(0)
    return df


# --------------------------------------------------------------------------- #
# Chuẩn hoá URL
# --------------------------------------------------------------------------- #
_MULTI_SLASH = re.compile(r"/{2,}")

# GA4 trả về các giá trị đặc biệt này khi không xác định được trang
INVALID_PATHS = {"", "(not set)", "(other)", "(not provided)"}


def normalize_path(path: str) -> str:
    """Chuẩn hoá pagePath để các biến thể của cùng 1 bài được gộp lại.

    - Bỏ domain nếu lỡ là URL đầy đủ
    - Bỏ query string (?utm_source=...) và fragment (#...)
    - Gộp nhiều dấu '/' liên tiếp, bỏ '/' ở cuối (trừ trang chủ)
    - Chuyển về chữ thường
    """
    if path is None:
        return ""
    p = str(path).strip()
    if p in INVALID_PATHS:
        return p
    if p.startswith(("http://", "https://")):
        p = urlsplit(p).path or "/"
    p = p.split("#", 1)[0].split("?", 1)[0]
    p = _MULTI_SLASH.sub("/", p).lower()
    if not p.startswith("/"):
        p = "/" + p
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/") or "/"
    return p


def build_full_url(site_url: str, path: str) -> str:
    """Ghép domain + path thành URL đầy đủ (dùng cho PageSpeed Insights)."""
    if str(path).startswith(("http://", "https://")):
        return str(path)
    return site_url.rstrip("/") + "/" + str(path).lstrip("/")

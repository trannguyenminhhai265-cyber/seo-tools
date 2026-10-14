"""
Nguồn dữ liệu GA4 THẬT dùng khi KHÔNG có quyền vào một GA4 property nào.

Google công khai dữ liệu GA4 thật (đã ẩn danh) của Google Merchandise Store trên BigQuery:
    bigquery-public-data.ga4_obfuscated_sample_ecommerce.events_*
    Khoảng thời gian có dữ liệu: 2020-11-01 -> 2021-01-31 (3 tháng)

Tài khoản demo GA4 KHÔNG gọi được bằng GA4 Data API (Google báo lỗi quyền), nên đây là
cách hợp lệ duy nhất để có số liệu GA4 thật mà không cần website riêng.
Các hàm dưới đây trả về DataFrame CÙNG CẤU TRÚC với dữ liệu lấy từ GA4 Data API, nên toàn bộ
phần xử lý của bài 1 và bài 3 được dùng lại nguyên vẹn.

Cách tính bám theo định nghĩa của GA4:
  - session         = cặp (user_pseudo_id, ga_session_id)
  - landing page    = trang của sự kiện session_start / page_view đầu tiên trong phiên
  - bounceRate      = 1 - tỷ lệ phiên có session_engaged = 1
  - averageSessionDuration = trung bình (thời điểm sự kiện cuối - đầu) của phiên, đơn vị giây

Chi phí: mỗi truy vấn quét khoảng vài GB, nằm trong 1 TB miễn phí/tháng của BigQuery sandbox.
"""

from __future__ import annotations

import pandas as pd

PUBLIC_TABLE = "bigquery-public-data.ga4_obfuscated_sample_ecommerce.events_*"
PUBLIC_SITE_URL = "https://shop.googlemerchandisestore.com"
PUBLIC_START = "2020-11-01"
PUBLIC_END = "2021-01-31"

_PATH = r"COALESCE(REGEXP_EXTRACT({col}, r'^https?://[^/]+(/[^?#]*)'), '/')"

_DAILY_PAGE_SESSIONS_SQL = f"""
WITH ev AS (
  SELECT
    event_date,
    user_pseudo_id,
    (SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'ga_session_id') AS sid,
    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_location') AS loc
  FROM `{PUBLIC_TABLE}`
  WHERE _TABLE_SUFFIX BETWEEN @start AND @end
    AND event_name = 'page_view'
)
SELECT
  {_PATH.format(col='loc')} AS pagePath,
  event_date AS date,
  COUNT(DISTINCT CONCAT(user_pseudo_id, '-', CAST(sid AS STRING))) AS traffic
FROM ev
WHERE loc IS NOT NULL AND sid IS NOT NULL
GROUP BY pagePath, date
"""

_LANDING_PAGES_SQL = f"""
WITH ev AS (
  SELECT
    user_pseudo_id,
    event_name,
    event_timestamp,
    (SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'ga_session_id') AS sid,
    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_location') AS loc,
    (SELECT COALESCE(value.string_value, CAST(value.int_value AS STRING))
       FROM UNNEST(event_params) WHERE key = 'session_engaged') AS engaged
  FROM `{PUBLIC_TABLE}`
  WHERE _TABLE_SUFFIX BETWEEN @start AND @end
),
sess AS (
  SELECT
    user_pseudo_id, sid,
    ARRAY_AGG(IF(event_name IN ('session_start', 'page_view'), loc, NULL)
              IGNORE NULLS ORDER BY event_timestamp LIMIT 1)[SAFE_OFFSET(0)] AS landing,
    MAX(IF(engaged = '1', 1, 0)) AS is_engaged,
    (MAX(event_timestamp) - MIN(event_timestamp)) / 1e6 AS duration_s
  FROM ev
  WHERE sid IS NOT NULL
  GROUP BY user_pseudo_id, sid
)
SELECT
  {_PATH.format(col='landing')} AS landingPage,
  COUNT(*) AS sessions,
  1 - AVG(is_engaged) AS bounceRate,
  AVG(duration_s) AS averageSessionDuration
FROM sess
WHERE landing IS NOT NULL
GROUP BY landingPage
ORDER BY sessions DESC
LIMIT @limit
"""


def _client(project_id: str):
    from google.cloud import bigquery

    return bigquery.Client(project=project_id)


def _run(project_id: str, sql: str, params: list) -> pd.DataFrame:
    from google.cloud import bigquery

    job = _client(project_id).query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    df = job.result().to_dataframe()
    gb = (job.total_bytes_processed or 0) / 1e9
    print(f"  BigQuery: {len(df):,} dòng, đã quét {gb:.2f} GB")
    return df


def fetch_daily_page_sessions(project_id: str, start: str = PUBLIC_START, end: str = PUBLIC_END) -> pd.DataFrame:
    """[pagePath, date(YYYYMMDD), traffic] — giống dữ liệu GA4 API dùng cho bài 1."""
    from google.cloud import bigquery

    params = [
        bigquery.ScalarQueryParameter("start", "STRING", start.replace("-", "")),
        bigquery.ScalarQueryParameter("end", "STRING", end.replace("-", "")),
    ]
    return _run(project_id, _DAILY_PAGE_SESSIONS_SQL, params)


def fetch_top_landing_pages(project_id: str, start: str = "2021-01-01", end: str = PUBLIC_END,
                            limit: int = 60) -> pd.DataFrame:
    """[landingPage, sessions, bounceRate(0-1), averageSessionDuration] — giống GA4 API cho bài 3.

    Mặc định lấy 31 ngày cuối của bộ dữ liệu (tương đương "30 ngày qua").
    """
    from google.cloud import bigquery

    params = [
        bigquery.ScalarQueryParameter("start", "STRING", start.replace("-", "")),
        bigquery.ScalarQueryParameter("end", "STRING", end.replace("-", "")),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]
    return _run(project_id, _LANDING_PAGES_SQL, params)

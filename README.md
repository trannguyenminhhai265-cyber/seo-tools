# Bộ công cụ SEO tự động bằng Python (GA4 · Search Console · PageSpeed Insights)

Lời giải cho 3 bài tập:

| Bài | Script | Đầu ra |
|---|---|---|
| 1. Phát hiện **Content Decay** (GA4) | `bai1_content_decay.py` | `content_decay_report.csv` |
| 2. Cảnh báo **Keyword Cannibalization** (GSC) | `bai2_keyword_cannibalization.py` | in ra console + `keyword_cannibalization_report.txt` / `.csv` |
| 3. Liên kết **GA4 + PageSpeed Insights** | `bai3_pagespeed_ga4.py` | `pagespeed_red_alert.csv`, `ga4_pagespeed_merged.csv`, `pagespeed_vs_bounce.png` |

Mỗi script có chế độ `--demo` chạy bằng dữ liệu giả lập (không cần tài khoản Google), để thầy/cô và bạn kiểm tra logic ngay. Kết quả demo nằm sẵn trong thư mục `sample_output/`.

---

## ▶ Chạy thật trên Google Colab

Mở notebook **[`seo_tools_colab.ipynb`](https://colab.research.google.com/github/trannguyenminhhai265-cyber/seo-tools/blob/main/seo_tools_colab.ipynb)** và chạy lần lượt từng ô. Notebook có 2 chế độ:

- **`API`**: có quyền vào GA4 và Search Console của một website. Chạy đúng GA4 Data API, Search Console API và PageSpeed Insights API như đề bài.
- **`PUBLIC`**: chưa có website.
  - Bài 1 và bài 3 dùng dữ liệu **GA4 thật** của Google Merchandise Store do Google công khai trên BigQuery (`bigquery-public-data.ga4_obfuscated_sample_ecommerce`, 11/2020–01/2021), xem `ga4_bigquery_public.py`.
  - Bài 3 gọi **PageSpeed Insights API thật**.
  - Bài 2 dùng dữ liệu mô phỏng vì Search Console không có dữ liệu công khai.

  Lưu ý: tài khoản GA4 demo của Google không truy cập được bằng Data API.

---

## 1. Cài đặt

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Chạy thử ngay (không cần API):

```bash
python bai1_content_decay.py --demo
python bai2_keyword_cannibalization.py --demo
python bai3_pagespeed_ga4.py --demo
pytest -q            # 27 test kiểm tra công thức, bộ lọc và các trường hợp biên
```

## 2. Chuẩn bị quyền truy cập API (khi chạy với dữ liệu thật)

1. Vào **Google Cloud Console** → tạo project → **Enable** 3 API:
   *Google Analytics Data API*, *Google Search Console API*, *PageSpeed Insights API*.
2. **IAM & Admin → Service Accounts** → tạo service account → tab *Keys* → *Add key (JSON)* → lưu thành `service-account.json` trong thư mục này.
3. Cấp quyền cho email service account (dạng `xxx@project.iam.gserviceaccount.com`):
   - **GA4**: Admin → *Property access management* → thêm email, quyền **Viewer**.
   - **GSC**: Settings → *Users and permissions* → thêm email, quyền **Restricted**.
4. **APIs & Services → Credentials → Create credentials → API key** → dùng làm `PSI_API_KEY` (PSI không có key vẫn chạy được nhưng quota rất thấp, dễ bị lỗi 429).
5. Sao chép `.env.example` thành `.env` và điền:
   - `GA4_PROPERTY_ID`: Admin → Property settings → *Property ID* (chỉ gồm số).
   - `GSC_SITE_URL`: `sc-domain:example.com` (Domain property) hoặc `https://example.com/` (URL-prefix property).
   - `SITE_URL`: domain dùng để ghép URL đầy đủ cho PageSpeed.

> Thay cho service account, bạn có thể dùng `gcloud auth application-default login --scopes=...` (xem chú thích đầu file `seo_common.py`). Không truyền `--credentials` và không đặt `GOOGLE_APPLICATION_CREDENTIALS` thì script tự dùng cách này.

## 3. Cách chạy với dữ liệu thật

```bash
# Bài 1 — mặc định: metric sessions, 6 tháng tính đến hôm qua
python bai1_content_decay.py
python bai1_content_decay.py --organic-only --redirects data/redirects_example.csv

# Bài 2 — 30 ngày gần nhất, loại query thương hiệu
python bai2_keyword_cannibalization.py --exclude-regex "tenthuonghieu|ten thuong hieu"

# Bài 3 — top 50 landing pages, PSI mobile, nghỉ 2s giữa các lượt gọi
python bai3_pagespeed_ga4.py --sleep 2
```

Chạy `python <script> --help` để xem đầy đủ tham số (ngưỡng lọc, số ngày, file đầu ra…).

---

## 4. Giải thích lời giải theo từng yêu cầu

### Bài 1 — Content Decay

| Yêu cầu | Cách làm trong code |
|---|---|
| Lấy `sessions` + `pagePath` trong 6 tháng | `fetch_ga4_data()` gọi `runReport` với dimensions `pagePath`, `date`. Hàm `ga4_run_report()` tự **phân trang** bằng `offset` nên không bị mất dòng khi site có hàng nghìn bài. Có thể đổi metric bằng `--metric` (vd `organicGoogleSearchSessions` nếu property hỗ trợ) hoặc lọc kênh Organic Search bằng `--organic-only`. |
| Chia 2 giai đoạn | `compute_periods()`: ngày kết thúc = hôm qua (dữ liệu hôm nay chưa đủ), lùi đúng 6 tháng lịch; GĐ1 = 3 tháng đầu, GĐ2 = 3 tháng cuối. Gán giai đoạn theo cột `date` rồi `pivot_table` tính tổng mỗi URL. |
| % Giảm = (GĐ1 − GĐ2) / GĐ1 × 100 | Tính bằng vector numpy, có chặn chia cho 0. |
| Lọc % Giảm > 30% và GĐ1 > 100 | Dùng so sánh **lớn hơn** đúng đề (giảm đúng 30% hoặc GĐ1 = 100 thì không lọt — có test kiểm chứng). |
| Xuất CSV, sắp xếp giảm dần | Đúng 4 cột `URL, Traffic_3_Thang_Dau, Traffic_3_Thang_Cuoi, Ty_Le_Sut_Giam`, encoding `utf-8-sig` để Excel hiển thị đúng tiếng Việt. |
| **URL không có traffic ở GĐ2** | Sau pivot, ô thiếu được điền 0 → tỷ lệ giảm 100%, bài vẫn xuất hiện trong báo cáo; console in cảnh báo riêng để kiểm tra bài có bị xoá / noindex / đổi URL không. |
| **URL bị đổi** | (1) Chuẩn hoá URL: bỏ `?utm=…`, `#…`, dấu `/` cuối, chữ hoa → các biến thể gộp làm một. (2) File `--redirects` (`old_path,new_path`) gộp traffic URL cũ vào URL mới, hỗ trợ chuỗi A→B→C và chống vòng lặp. Nhờ vậy bài đổi slug không bị báo nhầm là “giảm 100%”. |

### Bài 2 — Keyword Cannibalization

| Yêu cầu | Cách làm trong code |
|---|---|
| Lấy query, page, impressions, clicks, position 30 ngày | `fetch_gsc_data()` gọi `searchanalytics.query` với dimensions `[query, page]`, phân trang 25.000 dòng/lần. Ngày kết thúc lùi 3 ngày vì dữ liệu GSC bị trễ. |
| Group by query, đếm URL duy nhất | `detect_cannibalization()`: `groupby("query").agg(so_url=("page","nunique"), min_impr=("impressions","min"))`. |
| Lọc ≥ 2 URL và impressions từng URL > 0 | Điều kiện `so_url >= 2` và `min_impr > 0` (impressions nhỏ nhất > 0 nghĩa là **mọi** URL đều > 0). |
| Báo cáo phân cấp | Từ khoá → danh sách URL kèm Clicks, Impressions, Position (và CTR). In ra console, lưu `.txt` dạng cây và `.csv` để lọc trong Excel. |
| Dễ ra quyết định gộp / xoá bài | Thêm: ★ đánh dấu URL chính (nhiều clicks nhất), **tỷ trọng impressions** của từng URL, **mức độ** CAO / TRUNG BÌNH / THẤP và **gợi ý hành động**. Ví dụ: hai URL chia đều hiển thị và cùng ở top 20 → gợi ý gộp nội dung + 301; URL phụ chỉ chiếm < 10% hiển thị → Google đã chọn rõ trang chính, chỉ cần theo dõi. |
| Tránh báo động giả | URL có `#fragment` (jump link, featured snippet) được gộp về URL gốc (position tính trung bình có trọng số theo impressions); có `--exclude-regex` để bỏ query thương hiệu (trang chủ và trang giới thiệu cùng lên cho tên thương hiệu là chuyện bình thường). |

### Bài 3 — GA4 × PageSpeed Insights

| Yêu cầu | Cách làm trong code |
|---|---|
| Top 50 landing pages, bounceRate, averageSessionDuration | `fetch_top_landing_pages()`: dimension `landingPage`, sắp xếp `sessions` giảm dần, lấy dư vài dòng rồi bỏ `(not set)` và giữ đúng 50. `bounceRate` của GA4 là tỉ lệ 0–1 nên được nhân 100. |
| Vòng lặp gọi PSI, điểm × 100 | `run_psi_loop()` duyệt từng URL → `fetch_psi()` gọi `runPagespeed?strategy=mobile&category=performance`; `parse_psi_response()` lấy `lighthouseResult.categories.performance.score * 100`, kèm LCP, FCP, TBT, CLS để lập trình viên biết cần sửa gì. |
| **Rate Limit** | `time.sleep()` giữa các lượt gọi (mặc định 2s nếu có key, 5s nếu không có); gặp **429/5xx** thì retry với *exponential backoff* (5s → 10s → 20s…, tôn trọng header `Retry-After`); lỗi 400 (URL không phải HTML, bị chặn…) được ghi lại, không làm dừng chương trình; kết quả lưu ngay vào `psi_cache.json` nên chạy lại không tốn quota cho các URL đã đo. |
| Merge theo URL | `pd.merge(ga4, psi, on="URL", how="left", validate="one_to_one")`: URL đo lỗi vẫn giữ lại (điểm để trống) chứ không bị mất. |
| Lọc “Trang báo động đỏ” | `PageSpeed_Mobile < 50` **và** `Bounce_Rate_% > 70` → `pagespeed_red_alert.csv`, sắp xếp điểm thấp nhất / nhiều traffic nhất lên đầu. |
| Chứng minh quan hệ “Tốc độ ↔ Hành vi” bằng dữ liệu trực quan | In hệ số tương quan **Pearson** và **Spearman**, vẽ biểu đồ phân tán `pagespeed_vs_bounce.png` (tô màu vùng báo động đỏ, đường xu hướng, kích thước chấm theo sessions). |

---

## 5. Cấu trúc thư mục

```
seo_tools/
├── seo_common.py                    # xác thực, gọi GA4 có phân trang, chuẩn hoá URL
├── bai1_content_decay.py
├── bai2_keyword_cannibalization.py
├── bai3_pagespeed_ga4.py
├── ga4_bigquery_public.py           # lấy dữ liệu GA4 thật từ BigQuery public dataset (chế độ PUBLIC)
├── seo_tools_colab.ipynb            # notebook chạy trên Google Colab
├── data/redirects_example.csv       # mẫu file redirect cho bài 1
├── tests/test_seo_tools.py          # 27 unit test (pytest)
├── sample_output/                   # kết quả khi chạy --demo
├── requirements.txt
└── .env.example
```

## 6. Lỗi thường gặp

| Lỗi | Nguyên nhân / cách sửa |
|---|---|
| `403 User does not have sufficient permissions` | Chưa thêm email service account vào GA4/GSC, hoặc sai Property ID / site URL. |
| `400 ... is not a valid metric` | Property không hỗ trợ metric truyền vào `--metric` → dùng `sessions` (mặc định) kèm `--organic-only`. |
| GSC trả 0 dòng | Sai định dạng `GSC_SITE_URL` (Domain property phải có tiền tố `sc-domain:`). |
| PSI `429 Too Many Requests` | Thêm `--psi-key`, tăng `--sleep`; chạy lại, các URL đã đo sẽ lấy từ cache. |
| PSI `400 NOT_HTML / FAILED_DOCUMENT_REQUEST` | URL trả về file không phải HTML, bị chặn bot hoặc chuyển hướng lỗi → kiểm tra thủ công. |

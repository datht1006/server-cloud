from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, FileResponse
from dotenv import load_dotenv
import boto3
import os
import io
import json
import re
from datetime import datetime

try:
    from google import genai
except Exception:
    genai = None

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None


load_dotenv()

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET = os.getenv("R2_BUCKET", "hpdq2")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

app = FastAPI(title="Cloudflare R2 Drive API + Gemini Smart Router V5")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    region_name="auto",
)

gemini_client = None
if genai and GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)


# =========================
# Common helpers
# =========================

def normalize_folder(path: str = "") -> str:
    path = (path or "").strip()
    if path and not path.endswith("/"):
        path += "/"
    return path


def clean_name(name: str) -> str:
    return (name or "").strip().strip("/")


def extract_pdf_text(file_bytes: bytes) -> str:
    if not PdfReader:
        return ""

    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        text = ""

        for page in reader.pages[:7]:
            text += page.extract_text() or ""

        return text[:14000]
    except Exception:
        return ""


def safe_json(raw: str) -> dict:
    raw = (raw or "").strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(raw)
    except Exception:
        return {
            "target_base_folder": "",
            "document_time": {
                "year": None,
                "month": None,
                "quarter": None,
                "source": "invalid_json",
            },
            "confidence": 0,
            "reason": "Gemini did not return valid JSON",
            "raw": raw,
        }


def list_all_objects(prefix: str = ""):
    items = []
    token = None

    while True:
        params = {
            "Bucket": R2_BUCKET,
            "Prefix": prefix,
        }

        if token:
            params["ContinuationToken"] = token

        result = s3.list_objects_v2(**params)
        items.extend(result.get("Contents", []))

        if not result.get("IsTruncated"):
            break

        token = result.get("NextContinuationToken")

    return items


def list_all_folders():
    folders = set()

    for obj in list_all_objects():
        key = obj["Key"]

        if key.endswith("/"):
            folders.add(key)

        parts = key.strip("/").split("/")

        if len(parts) > 1:
            current = ""
            for part in parts[:-1]:
                current += part + "/"
                folders.add(current)

    return sorted(folders)


def folder_depth(folder: str) -> int:
    return normalize_folder(folder).count("/")


def is_time_folder_name(name: str) -> bool:
    name = (name or "").strip().lower()
    return bool(
        re.match(r"^(năm|nam)\s*20\d{2}$", name)
        or re.match(r"^20\d{2}$", name)
        or re.match(r"^(tháng|thang)\s*0?([1-9]|1[0-2])$", name)
        or re.match(r"^(quý|quy)\s*0?[1-4]$", name)
    )


def build_folder_tree_context(folders: list[str], max_items: int = 1200) -> str:
    """
    Build full-ish folder tree for Gemini.
    Không đưa các folder thời gian vào như target base chính,
    vì backend sẽ tự gắn Năm/Tháng/Quý.
    """
    base_folders = sorted({
        strip_time_folders(folder)
        for folder in folders
        if strip_time_folders(folder)
    })

    # bỏ folder chung chung và folder thời gian đơn lẻ
    cleaned = []
    generic = {"bm_qt/", "bmqt/", "other/", "auto_classified/"}

    for folder in base_folders:
        nf = normalize_folder(folder)
        if nf.lower() in generic:
            continue

        last = nf.rstrip("/").split("/")[-1]
        if is_time_folder_name(last):
            continue

        cleaned.append(nf)

    # Ưu tiên folder sâu, có mã BM/QT/HD, vì đây là folder đích thật
    def score(folder: str):
        low = folder.lower()
        s = 0
        if "bm." in low or "bm-" in low or "bm_" in low or "bm" in low:
            s += 20
        if "qt." in low or "qt-" in low or "qt_" in low or "qt" in low:
            s += 20
        if "hd." in low or "hd-" in low or "hd_" in low or "hd" in low:
            s += 20
        s += folder_depth(folder) * 5
        return s

    ranked = sorted(cleaned, key=score, reverse=True)[:max_items]

    lines = []
    for idx, folder in enumerate(ranked, start=1):
        indent = "  " * max(folder_depth(folder) - 1, 0)
        lines.append(f"{idx}. {indent}{folder}")

    return "\n".join(lines)


def folder_exists_exact(folder: str, folders: list[str]) -> bool:
    nf = normalize_folder(folder).lower()
    return any(normalize_folder(f).lower() == nf for f in folders)


def ensure_folder_exists(path: str):
    path = normalize_folder(path)
    if not path:
        return

    s3.put_object(
        Bucket=R2_BUCKET,
        Key=path,
        Body=b"",
    )


def normalize_code(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def strip_time_folders(path: str) -> str:
    """
    Xóa toàn bộ nhánh thời gian ở cuối folder trước khi gắn thời gian mới.

    Áp dụng toàn hệ thống:
    - Năm 2024/
    - 2024/
    - Tháng 1/, Tháng 01/, Tháng 06/
    - Quý 1/, Quý 01/
    - Xóa lặp nhiều lớp nếu folder cũ bị lồng sai.
    """
    path = normalize_folder(path)

    time_patterns = [
        r"(?:Năm|Nam)\s*20\d{2}/$",
        r"20\d{2}/$",
        r"(?:Tháng|Thang)\s*0?([1-9]|1[0-2])/$",
        r"(?:Quý|Quy)\s*0?[1-4]/$",
    ]

    changed = True

    while changed:
        changed = False

        for pattern in time_patterns:
            new_path = re.sub(
                pattern,
                "",
                path,
                flags=re.IGNORECASE
            )

            if new_path != path:
                path = normalize_folder(new_path)
                changed = True
                break

    return normalize_folder(path)



# =========================
# Code extraction / matching
# =========================

def extract_doc_codes(filename: str, text: str):
    """
    Extract strong document codes with priority:
    BM.08-HD.11.02
    BM.01-QT.09
    HD.11.02
    QT.09
    BM.08
    """
    content = f"{filename} {text}".lower()

    patterns = [
        r"bm\s*[.\-_]?\s*\d{1,2}[a-z]?\s*-\s*hd\s*[.\-_]?\s*\d{1,2}\s*[.\-_]\s*\d{1,2}",
        r"bm\s*[.\-_]?\s*\d{1,2}[a-z]?\s*-\s*qt\s*[.\-_]?\s*\d{1,2}(?:\s*[.\-_]\s*\d{1,2})?",
        r"hd\s*[.\-_]?\s*\d{1,2}\s*[.\-_]\s*\d{1,2}",
        r"qt\s*[.\-_]?\s*\d{1,2}(?:\s*[.\-_]\s*\d{1,2})?",
        r"bm\s*[.\-_]?\s*\d{1,2}[a-z]?",
    ]

    found = []
    for pattern in patterns:
        for match in re.findall(pattern, content, flags=re.IGNORECASE):
            code = normalize_code(match)
            if code and code not in found:
                found.append(code)

    return found


def folder_score_by_code(folder: str, codes: list[str]) -> int:
    nf = normalize_code(folder)
    score = 0

    for idx, code in enumerate(codes):
        if not code:
            continue

        if code in nf:
            score += 1000 - idx * 50

            if code.startswith("bm") and ("hd" in code or "qt" in code):
                score += 1600
            elif code.startswith("hd"):
                score += 900
            elif code.startswith("qt"):
                score += 700
            elif code.startswith("bm"):
                score += 350

    # prefer base document folder, not time subfolder
    clean = strip_time_folders(folder)
    if clean != normalize_folder(folder):
        score -= 120

    score += folder.count("/") * 15

    return score


def find_best_existing_base_folder(filename: str, text: str, folders: list[str]) -> str | None:
    codes = extract_doc_codes(filename, text)

    if not codes:
        return None

    scored = []

    for folder in folders:
        score = folder_score_by_code(folder, codes)
        if score > 0:
            scored.append((score, folder))

    if not scored:
        return None

    scored.sort(reverse=True, key=lambda x: x[0])
    return strip_time_folders(scored[0][1])


def make_doc_code_variants(code: str) -> list[str]:
    code = normalize_code(code)
    variants = []

    if code:
        variants.append(code)

    hd_match = re.search(r"hd\d{1,2}\d{1,2}", code)
    if hd_match:
        variants.append(hd_match.group(0))

    qt_match = re.search(r"qt\d{1,2}(?:\d{1,2})?", code)
    if qt_match:
        variants.append(qt_match.group(0))

    clean = []
    for item in variants:
        if item and item not in clean:
            clean.append(item)

    return clean


def find_existing_folder_by_codes(codes: list[str], folders: list[str]) -> str | None:
    """
    Tìm folder BM/QT/HD đã tồn tại trước.
    Có thì dùng lại, không có mới tạo folder mới.
    """
    if not codes:
        return None

    base_folders = sorted({
        strip_time_folders(folder)
        for folder in folders
        if strip_time_folders(folder)
    })

    generic_names = {
        "bm_qt/",
        "bmqt/",
        "other/",
        "auto_classified/",
    }

    for code in codes:
        variants = make_doc_code_variants(code)

        for variant in variants:
            candidates = []

            for folder in base_folders:
                folder_norm = normalize_folder(folder)
                folder_low = folder_norm.lower()

                if folder_low in generic_names:
                    continue

                nf = normalize_code(folder_norm)

                if variant and variant in nf:
                    candidates.append(folder_norm)

            if candidates:
                candidates.sort(
                    key=lambda f: (
                        f.count("/"),
                        len(f)
                    ),
                    reverse=True
                )
                return candidates[0]

    return None


def make_new_base_folder_from_code(filename: str, text: str) -> str:
    """
    If no folder exists, create a better folder than BM_QT/OTHER.
    """
    codes = extract_doc_codes(filename, text)

    if not codes:
        return "OTHER/"

    code = codes[0]
    original_name = re.sub(r"\.[a-zA-Z0-9]+$", "", filename).strip()
    safe_name = re.sub(r"[\\:*?\"<>|]", " ", original_name)

    # Put new unknown docs under AUTO_CLASSIFIED but still keep document code.
    return normalize_folder(f"AUTO_CLASSIFIED/{safe_name}")


# =========================
# Time extraction
# =========================

def regex_document_time(filename: str, text: str) -> dict:
    """
    Time extraction V8.
    Không lấy ngày ban hành/hiệu lực/sửa đổi của biểu mẫu.
    Nếu không thấy kỳ tài liệu rõ ràng thì dùng tháng/năm upload hiện tại.
    """
    now = datetime.now()

    filename_low = (filename or "").lower()
    text_low = (text or "").lower()

    def parse_block(block: str, source_name: str):
        # dd/mm/yyyy
        date_match = re.search(
            r"\b([0-3]?\d)[/\-.]([0-1]?\d)[/\-.](20\d{2})\b",
            block
        )
        if date_match:
            month = int(date_match.group(2))
            year = int(date_match.group(3))
            if 1 <= month <= 12:
                return {
                    "year": year,
                    "month": month,
                    "quarter": None,
                    "source": f"{source_name}:dd/mm/yyyy",
                }

        # tháng 06 năm 2026 / tháng 6 / T06
        month_year = re.search(
            r"(?:tháng|thang|t)\s*0?([1-9]|1[0-2])(?:\s*(?:năm|nam|/|-|\s)\s*(20\d{2}))?",
            block
        )
        if month_year:
            month = int(month_year.group(1))
            year = int(month_year.group(2)) if month_year.group(2) else now.year
            return {
                "year": year,
                "month": month,
                "quarter": None,
                "source": f"{source_name}:month",
            }

        # quý 02 năm 2026 / quý 2
        quarter_year = re.search(
            r"(?:quý|quy|q)\s*0?([1-4])(?:\s*(?:năm|nam|/|-|\s)\s*(20\d{2}))?",
            block
        )
        if quarter_year:
            quarter = int(quarter_year.group(1))
            year = int(quarter_year.group(2)) if quarter_year.group(2) else now.year
            return {
                "year": year,
                "month": None,
                "quarter": quarter,
                "source": f"{source_name}:quarter",
            }

        year_match = re.search(r"\b(20\d{2})\b", block)
        if year_match:
            return {
                "year": int(year_match.group(1)),
                "month": None,
                "quarter": None,
                "source": f"{source_name}:year",
            }

        return None

    # Filename ưu tiên cao nhất.
    result = parse_block(filename_low, "filename")
    if result:
        return result

    strong_keywords = [
        "ngày lập", "ngay lap",
        "ngày tạo", "ngay tao",
        "ngày báo cáo", "ngay bao cao",
        "kỳ báo cáo", "ky bao cao",
        "tháng báo cáo", "thang bao cao",
        "quý báo cáo", "quy bao cao",
        "năm báo cáo", "nam bao cao",
        "kỳ thực hiện", "ky thuc hien",
        "tháng thực hiện", "thang thuc hien",
        "năm thực hiện", "nam thuc hien",
        "thời gian thực hiện", "thoi gian thuc hien",
    ]

    banned_keywords = [
        "ngày ban hành", "ngay ban hanh",
        "ngày hiệu lực", "ngay hieu luc",
        "hiệu lực", "hieu luc",
        "lần sửa đổi", "lan sua doi",
        "sửa đổi", "sua doi",
        "revision",
        "rev.",
        "ngày phê duyệt mẫu", "ngay phe duyet mau",
    ]

    lines = []
    for line in text_low.splitlines():
        if any(b in line for b in banned_keywords):
            continue
        if any(k in line for k in strong_keywords):
            lines.append(line)

    for line in lines:
        result = parse_block(line, "content_strong_keyword")
        if result:
            return result

    # Không có kỳ tài liệu rõ ràng => upload hiện tại.
    return {
        "year": now.year,
        "month": now.month,
        "quarter": None,
        "source": "fallback_upload_time_no_clear_document_period",
    }

def period_from_document_time(document_time: dict) -> str:
    """
    Chuẩn hóa folder thời gian cho toàn bộ BM/QT/HD:
    - Tháng 1 / Tháng 01 / T1 / T01 đều lưu thành Tháng 01
    - Quý 1 / Quý 01 / Q1 đều lưu thành Quý 01
    """
    year = document_time.get("year")
    month = document_time.get("month")
    quarter = document_time.get("quarter")

    try:
        year = int(year) if year else None
    except Exception:
        year = None

    try:
        month = int(month) if month else None
    except Exception:
        month = None

    try:
        quarter = int(quarter) if quarter else None
    except Exception:
        quarter = None

    if year and month and 1 <= month <= 12:
        return f"Năm {year}/Tháng {month:02d}/"

    if year and quarter and 1 <= quarter <= 4:
        return f"Năm {year}/Quý {quarter:02d}/"

    if year:
        return f"Năm {year}/"

    if month and 1 <= month <= 12:
        return f"Tháng {month:02d}/"

    if quarter and 1 <= quarter <= 4:
        return f"Quý {quarter:02d}/"

    return ""


def build_candidate_folders(folders: list[str], limit: int = 1200) -> str:
    """
    Cho Gemini nhìn cây thư mục thật, ưu tiên folder đích BM/QT/HD,
    không đưa folder thời gian cũ làm target base.
    """
    return build_folder_tree_context(folders, max_items=limit)


def gemini_route_document(filename: str, text: str, folders: list[str]) -> dict:
    """
    Gemini router V10:
    - Gemini nhìn toàn cây thư mục base hiện có.
    - Gemini chọn đúng target_base_folder trong cây.
    - Nếu không có folder phù hợp mới đề xuất folder mới.
    - Backend vẫn kiểm tra exact folder/rule trước và sau để tránh Gemini đoán bậy.
    """
    if not gemini_client:
        dt = regex_document_time(filename, text)
        return {
            "target_base_folder": "",
            "document_time": dt,
            "confidence": 0,
            "reason": "GEMINI_API_KEY missing or google-genai not installed",
            "matched_by": "regex_only",
            "document_type": "OTHER",
            "primary_code": "",
            "matched_existing_folder": False,
        }

    folders_text = build_folder_tree_context(folders, max_items=1200)

    prompt = f"""
Bạn là AI phân loại tài liệu nhà máy theo CÂY THƯ MỤC THẬT.

DANH SÁCH FOLDER HIỆN CÓ TRÊN R2.
Đây là cây thư mục đích thật. Hãy chọn trong danh sách này nếu có thể:
{folders_text}

TÊN FILE:
{filename}

NỘI DUNG TRÍCH XUẤT TỪ PDF:
{text}

NHIỆM VỤ:
1. Hiểu tài liệu này thuộc folder nào trong CÂY THƯ MỤC HIỆN CÓ.
2. Không chỉ match regex BM chung chung. Phải hiểu tên file, tiêu đề tài liệu, loại tài liệu, xưởng, chủ đề.
3. Nếu folder phù hợp đã có trong danh sách, trả đúng target_base_folder đó.
4. Nếu KHÔNG có folder phù hợp trong danh sách, mới đề xuất folder mới.
5. target_base_folder KHÔNG chứa Năm/Tháng/Quý ở cuối. Backend sẽ tự gắn thời gian.
6. Xác định primary_code là mã chính của tài liệu, nếu có.
7. Các mã BM/QT/HD xuất hiện trong nội dung nhưng chỉ là bảng, dẫn chiếu, hoặc checklist phụ thì KHÔNG được dùng làm primary_code.
8. Nếu tên file có mã hoặc tên biểu mẫu rõ ràng thì ưu tiên tên file hơn nội dung.
9. Nếu là biên bản họp, báo cáo, checklist, phiếu, nghiệm thu, vật tư... thì chọn folder đúng theo ý nghĩa tài liệu trong cây.

QUY TẮC THỜI GIAN:
- Trích KỲ LƯU TRỮ thật của tài liệu.
- Ưu tiên: tên file, ngày lập, ngày tạo, ngày báo cáo, kỳ báo cáo, tháng báo cáo, quý báo cáo, năm báo cáo, thời gian thực hiện.
- KHÔNG dùng ngày ban hành, ngày hiệu lực, lần sửa đổi, revision, ngày phê duyệt mẫu làm kỳ lưu trữ.
- Nếu có tháng thì trả month.
- Nếu không có tháng nhưng có quý thì trả quarter.
- Nếu không có quý nhưng có năm thì trả year.
- Nếu không xác định được kỳ tài liệu rõ ràng thì source = "not_found".

QUY TẮC CHỐNG SAI:
- Không chọn BM_QT/OTHER/AUTO_CLASSIFIED nếu có folder cụ thể trong cây.
- Không chọn folder chỉ vì trong nội dung có mã đó; mã đó phải là tài liệu chính.
- Nếu confidence < 0.65 thì vẫn chọn folder tốt nhất nhưng giải thích cần kiểm tra.
- matched_existing_folder = true nếu target_base_folder lấy từ danh sách folder hiện có.
- matched_existing_folder = false nếu folder là đề xuất mới.

TRẢ JSON HỢP LỆ, KHÔNG MARKDOWN:
{{
  "target_base_folder": "folder/gốc/phù/hợp/",
  "matched_existing_folder": true,
  "primary_code": "BM.39-QT.09",
  "document_time": {{
    "year": 2026,
    "month": 6,
    "quarter": null,
    "source": "tên file hoặc nội dung tài liệu"
  }},
  "document_type": "BM|QT|HD|BIEN_BAN|BAO_CAO|CHECKLIST|PHIEU|NGHIEM_THU|VAT_TU|HINH_ANH|OTHER",
  "confidence": 0.0,
  "reason": "giải thích ngắn vì sao chọn folder này"
}}
"""

    try:
        res = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        ai = safe_json(res.text)

        if "document_time" not in ai or not isinstance(ai["document_time"], dict):
            ai["document_time"] = regex_document_time(filename, text)

        ai["matched_by"] = "gemini_full_tree"
        return ai

    except Exception as e:
        dt = regex_document_time(filename, text)
        return {
            "target_base_folder": "",
            "document_time": dt,
            "confidence": 0,
            "reason": str(e),
            "matched_by": "regex_after_gemini_error",
            "document_type": "OTHER",
            "primary_code": "",
            "matched_existing_folder": False,
        }


def build_route(filename: str, file_bytes: bytes, folders: list[str]) -> dict:
    """
    Route V10: Gemini hiểu toàn cây thư mục.

    Thứ tự:
    1. Extract code từ filename + text.
    2. Tìm folder có sẵn bằng code chính xác.
    3. Gemini nhìn toàn cây folder và chọn folder phù hợp theo nội dung.
    4. Nếu Gemini chọn folder có sẵn thì dùng.
    5. Nếu chưa có folder mới tạo AUTO_CLASSIFIED.
    6. Gắn thời gian chuẩn Năm/Tháng/Quý.
    """
    text = extract_pdf_text(file_bytes)
    codes = extract_doc_codes(filename, text)

    base_folders = sorted({
        strip_time_folders(folder)
        for folder in folders
        if strip_time_folders(folder)
    })

    # 1. Exact code match trước
    existing_base = find_existing_folder_by_codes(codes, base_folders)

    # 2. Gemini hiểu toàn cây
    ai = gemini_route_document(filename, text, base_folders)

    ai_base = strip_time_folders(ai.get("target_base_folder") or "")
    ai_base_norm = normalize_folder(ai_base)

    generic_folders = {
        "bm_qt/",
        "bmqt/",
        "other/",
        "auto_classified/",
    }

    if existing_base:
        base_folder = existing_base
        base_source = "existing_folder_exact_match"

    elif (
        ai_base
        and ai_base_norm.lower() not in generic_folders
        and folder_exists_exact(ai_base_norm, base_folders)
    ):
        base_folder = ai_base_norm
        base_source = "gemini_existing_folder_full_tree"

    else:
        # Nếu Gemini trả folder cụ thể nhưng chưa tồn tại, cho phép tạo mới nếu confidence đủ
        confidence = ai.get("confidence", 0) or 0
        try:
            confidence = float(confidence)
        except Exception:
            confidence = 0

        if ai_base and ai_base_norm.lower() not in generic_folders and confidence >= 0.72:
            base_folder = ai_base_norm
            base_source = "gemini_new_specific_folder"

        else:
            rule_base = find_best_existing_base_folder(filename, text, base_folders)

            if rule_base:
                base_folder = rule_base
                base_source = "existing_folder_rule_score"
            else:
                base_folder = make_new_base_folder_from_code(filename, text)
                base_source = "new_folder_from_code"

    document_time = ai.get("document_time") or regex_document_time(filename, text)

    bad_time_sources = [
        "ban hành", "ban hanh",
        "hiệu lực", "hieu luc",
        "sửa đổi", "sua doi",
        "revision",
        "rev",
        "phê duyệt mẫu", "phe duyet mau",
    ]

    source_text = str(document_time.get("source", "")).lower()

    if (
        document_time.get("source") == "not_found"
        or any(bad in source_text for bad in bad_time_sources)
    ):
        document_time = regex_document_time(filename, text)

    period = period_from_document_time(document_time)

    base_folder = strip_time_folders(base_folder)
    target_folder = normalize_folder(base_folder + period)

    return {
        "target_folder": target_folder,
        "target_base_folder": base_folder,
        "period_folder": period,
        "document_time": document_time,
        "document_type": ai.get("document_type", "OTHER"),
        "confidence": ai.get("confidence", 0),
        "reason": ai.get("reason", ""),
        "base_source": base_source,
        "codes": codes,
        "primary_code": ai.get("primary_code", ""),
        "matched_existing_folder": ai.get("matched_existing_folder", False),
        "need_create_folder": True,
        "routing_order": [
            "existing_folder_exact_match",
            "gemini_existing_folder_full_tree",
            "gemini_new_specific_folder",
            "existing_folder_rule_score",
            "new_folder_from_code"
        ],
    }

# =========================
# APIs
# =========================

@app.get("/")
def root():
    return {
        "service": "Cloudflare R2 Drive API",
        "status": "running",
        "bucket": R2_BUCKET,
        "gemini": bool(gemini_client),
        "router_version": "v10_gemini_full_folder_tree",
    }


@app.get("/api/folders")
def get_folders(path: str = ""):
    try:
        path = normalize_folder(path)
        result = s3.list_objects_v2(
            Bucket=R2_BUCKET,
            Prefix=path,
            Delimiter="/",
        )

        folders = []
        for item in result.get("CommonPrefixes", []):
            prefix = item["Prefix"]
            folders.append({
                "name": prefix.rstrip("/").split("/")[-1],
                "path": prefix,
            })

        return folders

    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/folder/create")
def create_folder(name: str, path: str = ""):
    try:
        path = normalize_folder(path)
        folder_name = clean_name(name)

        if not folder_name:
            raise HTTPException(400, "Folder name is empty")

        folder_key = path + folder_name + "/"
        ensure_folder_exists(folder_key)

        return {"success": True, "folder": folder_key}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/api/folder")
def delete_folder(path: str):
    try:
        path = normalize_folder(path)
        token = None
        deleted = 0

        while True:
            params = {"Bucket": R2_BUCKET, "Prefix": path}
            if token:
                params["ContinuationToken"] = token

            result = s3.list_objects_v2(**params)
            objects = [{"Key": obj["Key"]} for obj in result.get("Contents", [])]

            if objects:
                s3.delete_objects(Bucket=R2_BUCKET, Delete={"Objects": objects})
                deleted += len(objects)

            if not result.get("IsTruncated"):
                break

            token = result.get("NextContinuationToken")

        return {"success": True, "deleted": deleted}

    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/files")
def get_files(path: str = ""):
    try:
        path = normalize_folder(path)
        result = s3.list_objects_v2(
            Bucket=R2_BUCKET,
            Prefix=path,
            Delimiter="/",
        )

        folders = []
        for item in result.get("CommonPrefixes", []):
            prefix = item["Prefix"]
            folders.append({
                "name": prefix.rstrip("/").split("/")[-1],
                "path": prefix,
            })

        files = []
        for obj in result.get("Contents", []):
            key = obj["Key"]
            if key == path or key.endswith("/"):
                continue

            files.append({
                "name": key.split("/")[-1],
                "key": key,
                "path": "/".join(key.split("/")[:-1]),
                "size": obj["Size"],
                "modified": obj.get("LastModified").isoformat() if obj.get("LastModified") else "",
                "url": f"/api/file/{key}",
            })

        return {"currentPath": path, "folders": folders, "files": files}

    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/search")
def search(q: str):
    try:
        keyword = (q or "").strip().lower()
        if not keyword:
            return []

        files = []
        for obj in list_all_objects():
            key = obj["Key"]
            if key.endswith("/"):
                continue

            if keyword in key.lower():
                files.append({
                    "name": key.split("/")[-1],
                    "key": key,
                    "path": "/".join(key.split("/")[:-1]),
                    "size": obj["Size"],
                    "modified": obj.get("LastModified").isoformat() if obj.get("LastModified") else "",
                    "url": f"/api/file/{key}",
                })

        return files

    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/file/{key:path}")
def get_file(key: str):
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": key},
            ExpiresIn=3600,
        )
        return RedirectResponse(url)

    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/upload")
async def upload_file(path: str = "", file: UploadFile = File(...)):
    try:
        contents = await file.read()
        path = normalize_folder(path)
        key = path + file.filename

        s3.put_object(
            Bucket=R2_BUCKET,
            Key=key,
            Body=contents,
            ContentType=file.content_type or "application/octet-stream",
        )

        return {"success": True, "key": key}

    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/upload/auto")
async def upload_auto(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        folders = list_all_folders()

        route = build_route(file.filename, contents, folders)

        target_folder = normalize_folder(route.get("target_folder", "OTHER/"))
        ensure_folder_exists(target_folder)

        key = target_folder + file.filename

        s3.put_object(
            Bucket=R2_BUCKET,
            Key=key,
            Body=contents,
            ContentType=file.content_type or "application/octet-stream",
        )

        response = {
            "success": True,
            "file_name": file.filename,
            "key": key,
            "uploaded_to": target_folder,
            "check": {
                "base_folder": route.get("target_base_folder"),
                "time_folder": route.get("period_folder"),
                "document_time": route.get("document_time"),
                "document_type": route.get("document_type"),
                "confidence": route.get("confidence"),
                "reason": route.get("reason"),
                "base_source": route.get("base_source"),
                "codes": route.get("codes"),
                "primary_code": route.get("primary_code"),
                "matched_existing_folder": route.get("matched_existing_folder"),
            },
            "needs_review": route.get("confidence", 0) < 0.55 and route.get("base_source") != "strict_code_rule",
        }

        print("===== GEMINI SMART ROUTE V5 =====")
        print(json.dumps(response, indent=2, ensure_ascii=False))
        print("=================================")

        return response

    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/api/file")
def delete_file(key: str):
    try:
        s3.delete_object(Bucket=R2_BUCKET, Key=key)
        return {"success": True, "deleted": key}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/rename")
def rename_file(old_key: str, new_name: str):
    try:
        new_name = clean_name(new_name)
        if not new_name:
            raise HTTPException(400, "New name is empty")

        folder = ""
        if "/" in old_key:
            folder = old_key.rsplit("/", 1)[0] + "/"

        new_key = folder + new_name

        s3.copy_object(
            Bucket=R2_BUCKET,
            CopySource={"Bucket": R2_BUCKET, "Key": old_key},
            Key=new_key,
        )
        s3.delete_object(Bucket=R2_BUCKET, Key=old_key)

        return {"success": True, "old_key": old_key, "key": new_key}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/move")
def move_file(old_key: str, new_folder: str):
    try:
        filename = old_key.split("/")[-1]
        new_folder = normalize_folder(new_folder)
        new_key = new_folder + filename

        ensure_folder_exists(new_folder)

        s3.copy_object(
            Bucket=R2_BUCKET,
            CopySource={"Bucket": R2_BUCKET, "Key": old_key},
            Key=new_key,
        )
        s3.delete_object(Bucket=R2_BUCKET, Key=old_key)

        return {"success": True, "old_key": old_key, "key": new_key}

    except Exception as e:
        raise HTTPException(500, str(e))


# =========================
# Static Client
# =========================

# Cấu trúc project:
# KHO/
# ├── backend/
# │   └── app.py
# └── statics/
#     └── client.html

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATIC_DIR = os.path.abspath(
    os.path.join(BASE_DIR, "..", "statics")
)

CLIENT_FILE = "client.html"
CLIENT_PATH = os.path.join(STATIC_DIR, CLIENT_FILE)

if os.path.isdir(STATIC_DIR):
    app.mount(
        "/static",
        StaticFiles(directory=STATIC_DIR),
        name="static"
    )


@app.get("/client")
def client_page():
    if not os.path.exists(CLIENT_PATH):
        raise HTTPException(
            status_code=404,
            detail=f"Client file not found: {CLIENT_PATH}"
        )

    return FileResponse(CLIENT_PATH)


@app.get("/app")
def app_page():
    return client_page()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )

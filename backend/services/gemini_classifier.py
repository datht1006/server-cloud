# gemini_auto_upload.py
# Thêm vào app.py

import os
import io
import json
from pypdf import PdfReader
from google import genai
from fastapi import UploadFile, File, HTTPException

gemini = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)

def extract_pdf_text(file_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        text = ""

        for page in reader.pages[:5]:
            text += page.extract_text() or ""

        return text[:8000]

    except Exception:
        return ""


def classify_document(filename: str, file_bytes: bytes):
    text = extract_pdf_text(file_bytes)

    prompt = f"""
Bạn là hệ thống phân loại tài liệu nhà máy.

Tên file:
{filename}

Nội dung:
{text}

Trả JSON:
{{
  "document_type":"BM|QT|HD|OTHER",
  "suggested_folder":"BM_QT",
  "reason":""
}}
"""

    response = gemini.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )

    raw = response.text.strip()
    raw = raw.replace("```json", "")
    raw = raw.replace("```", "")
    raw = raw.strip()

    return json.loads(raw)


# API FastAPI

@app.post("/api/upload/auto")
async def upload_auto(
    file: UploadFile = File(...)
):
    try:

        contents = await file.read()

        ai = classify_document(
            file.filename,
            contents
        )

        folder = ai.get(
            "suggested_folder",
            "OTHER"
        )

        folder = folder.strip("/") + "/"

        key = folder + file.filename

        s3.put_object(
            Bucket=R2_BUCKET,
            Key=key,
            Body=contents,
            ContentType=file.content_type
        )

        return {
            "success": True,
            "key": key,
            "ai": ai
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

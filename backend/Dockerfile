# Dùng image Playwright chính thức: đã có sẵn Chromium + thư viện hệ thống.
FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Cài dependency trước để tận dụng cache layer.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
# Chromium đã có trong image; lệnh này đảm bảo đúng bản cho playwright đã cài.
RUN python -m playwright install chromium

# Copy backend code và web tĩnh (backend phục vụ luôn UI ở "/").
COPY backend/app ./app
COPY web /app/web

EXPOSE 8000

ENV WEB_DIR=/app/web

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

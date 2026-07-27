FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Cài dependency trước để tận dụng cache layer.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code và web tĩnh (backend phục vụ luôn UI ở "/").
COPY backend/app ./app
COPY web /app/web

EXPOSE 8000

# WEB_DIR trỏ tới thư mục web tĩnh đã copy ở trên.
ENV WEB_DIR=/app/web

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

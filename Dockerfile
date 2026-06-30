FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Install Python deps first — this layer is cached until requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Firefox + all system-level Playwright deps in one layer.
# playwright install-deps internally runs apt-get, so we keep apt lists
# alive through this step and only clean up at the end.
RUN playwright install firefox \
    && playwright install-deps firefox \
    && rm -rf /var/lib/apt/lists/*

# Copy source after deps so rebuilds on code changes skip the slow layers above
COPY . .

CMD ["python", "main.py", "--run"]

FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ---- python deps ----
COPY requirements.txt ./
RUN pip install --no-cache-dir -U pip setuptools wheel \
&& pip install --no-cache-dir -r requirements.txt

# ---- app code ----
COPY . .

CMD ["python", "-u", "main.py"]
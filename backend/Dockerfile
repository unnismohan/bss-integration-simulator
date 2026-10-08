FROM python:3.12-slim-trixie AS dependency-audit
WORKDIR /audit
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir pip-audit==2.10.1 \
    && pip-audit --requirement requirements.txt --strict

FROM python:3.12-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --requirement requirements.txt && pip check
COPY app ./app
RUN useradd --create-home --uid 10001 simulator && chown -R simulator:simulator /app
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

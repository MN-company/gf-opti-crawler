FROM python:3.12.8-slim-bookworm
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY http_identity.py scraper.py enrichment.py crawler.py catalog_discovery.py browser_fallback.py async_pipeline.py async_discovery.py integrity.py ./
ENTRYPOINT ["python", "crawler.py"]

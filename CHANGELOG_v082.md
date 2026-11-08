# v0.8.2 beta — correctness fixes

- Prevent active `in_progress` recipe URLs being requeued by sitemap refresh.
- Flatten canonical alias predecessors and follow legacy alias chains safely.
- Associate ETag and Last-Modified with the requested HTTP resource.
- Pace iframe document and redirect requests in browser fallback.
- No changes to stored recipe data; no crawl required.
- Regression checks run offline; live integration and Windows Docker validation remain outstanding.

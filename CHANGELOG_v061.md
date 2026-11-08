# v0.6.1 — Diagnostic hotfix

- Output progressivo e flush per ogni categoria (`FETCH`, `OK`, `REVIEW`, `FAILED`, `GONE`, `EXCLUDED`).
- Output immediato prima del download di ogni sitemap.
- Indicazione esplicita che la fase categorie termina prima della scansione ricette.
- Correzione del checkpoint nel ramo categorie escluse per garantire commit ogni batch.
- Nessun cambiamento di schema SQLite o di formato dati.

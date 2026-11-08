# v0.6.2 — User-Agent configurabile senza email

- Rimosso `--contact` dai comandi `discover`, `crawl`, `run` e `categories`.
- Aggiunto `--user-agent` opzionale e configurazione predefinita con variabile `RECIPE_SCRAPER_USER_AGENT`.
- Aggiunti `.env.example` e passaggio della configurazione in entrambi i servizi Docker Compose.
- Eliminata la richiesta di email dagli script PowerShell per l'automazione Windows.
- Uniformato lo User-Agent per richieste HTTP, Playwright e downloader standalone.
- Aggiunta validazione dell'intestazione per evitare valori vuoti o caratteri di controllo.
- Database e code v0.6.1 compatibili: non serve migrazione.

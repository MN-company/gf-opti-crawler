# v0.8.1 — Retry automatico e pacing adattivo

- Retry HTTP asincrono limitato: fino a 2 tentativi aggiuntivi per richiesta (configurabile con `--http-retries 0..5`) per 429, 503, 500/502/504 e errori di rete.
- HTTP 429 e 503: `Retry-After` in secondi o data HTTP, pausa condivisa da tutti i worker dello stesso host, ritardo crescente in assenza di header.
- Circuit breaker dopo 3 segnali 429/503 prima di 25 successi consecutivi (`--max-throttles`), con arresto e stato SQLite riprendibile.
- Adattamento del delay: raddoppia dopo un rate limit fino a 8 volte il minimo, decresce gradualmente dopo risposte riuscite; non scende sotto il valore configurato né sotto `Crawl-delay`.
- HTTP 401/403, CAPTCHA e robots disallow: arresto senza retry.
- Pacing anche per le sottorichieste Playwright consentite sullo stesso host.
- Supporto verificato a `--workers 4`: quattro worker per host ma un unico rate limiter per host; il numero di worker non moltiplica il rate.
- Limite di 4 connessioni HTTPX per host; riepilogo finale delle richieste e dei retry.
- Test offline aggiuntivi: Retry-After, 429/503, circuit breaker, errori di rete, 403, pacing con quattro worker e subrequest browser.
- Compatibilità dello schema SQLite v0.8 mantenuta; nessuna migrazione del database necessaria.

Nota: i retry a lunga scadenza presenti in SQLite vengono elaborati a un avvio successivo; `--all` non attende ore/giorni per tutti i retry futuri.

# Recipe Scraper Universale v0.7

## Nuova pipeline asincrona

- Aggiunto `pipeline` al comando Windows: scoperta via sitemap, scansione delle categorie e acquisizione delle ricette senza dover completare tutte le categorie prima di iniziare le ricette.
- `--skip-discovery` evita di rileggere le sitemap quando sono già disponibili le code della v0.6.x.
- Client HTTP asincrono con `httpx.AsyncClient`, sessioni riutilizzate e pacing per dominio.
- Due task cooperativi: categorie su `www.giallozafferano.it` e ricette su `ricette.giallozafferano.it` possono progredire contemporaneamente.
- Parsing HTML eseguito in thread senza bloccare il ciclo degli eventi; un solo loop possiede SQLite e registra le modifiche con commit ogni N pagine.
- Gestione congiunta delle interruzioni: se un host riceve 401/403/429, un challenge o un errore server, l'altra attività viene interrotta senza bypass.
- Fallback Playwright asincrono e condiviso, attivato soltanto se l'HTML HTTP 200 è incompleto.
- Stato di avanzamento visibile in terminale, con URL elaborati, ricette salvate, tempo e throughput.
- Scheduler Windows aggiornato a `pipeline`, senza email obbligatoria.

## Compatibilità

- Schema SQLite invariato rispetto alla versione 0.6.2, migrazioni automatiche delle versioni precedenti conservate.
- Il precedente comando `run` rimane disponibile ed è sequenziale.
- Lo User-Agent continua a essere configurabile in `.env` e applicato esplicitamente nelle richieste HTTP e nel browser.
- Non vengono scaricate o archiviate immagini.

## Verifiche e limiti

- 60 test offline superati, inclusi nuovi test su sovrapposizione categorie/ricette, persistenza, deduplicazione, fallback, gestione 429 e CLI di ripresa.
- Non sono stati eseguiti test end-to-end su Docker Desktop Windows né benchmark live sul sito.
- La frequenza per dominio rimane 8 secondi fra richieste come impostazione predefinita: l'asincronia riduce la durata complessiva sovrapponendo i due flussi, ma non aumenta il ritmo massimo di richieste verso lo stesso host.
- Rimangono i limiti esistenti su robots.txt, blocchi e accessi autorizzati.

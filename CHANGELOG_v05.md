# v0.5 — Audit, integrità e Windows

## Novità implementate

1. Registro persistente dei tentativi di discovery: stato completo/parziale per ogni esecuzione.
2. Inserimento atomico per singola sitemap; dati parziali da XML non valido vengono annullati.
3. URL scoperti registrati con `discovered_run_id` e provenienza (`sitemap`, `related`, `redirect`).
4. `audit --deep --strict`: misurazione della copertura relativa all'ultima discovery, controllo di record mancanti e avvisi.
5. `issues`: esportazione JSONL degli URL senza esito terminale valido.
6. `requeue`: riesame manuale di `review`, `failed` e `gone`.
7. HTTP 404 verificato due volte; HTTP 410 registrato come rimosso; rete e parsing con tentativi limitati.
8. Parser senza JSON-LD se l'HTML fornisce titolo, ingredienti e istruzioni; avvisi sui conteggi incoerenti DOM/JSON-LD.
9. Redirect HTTP interni consentiti solo se le regole di accesso lo permettono, con archiviazione per URL finale e alias dell'URL precedente.
10. `backup`: backup consistente dell'archivio SQLite; esportazione JSON/JSONL.
11. Scripts Windows `crawler.ps1`, `schedule.ps1`, `scheduled-run.ps1`.
12. Analisi degli errori di accesso basata su segnali specifici, evitando falsi positivi dovuti a pubblicità.
13. Rilevazione dei percorsi esclusi con regole di precedenza `Allow`/`Disallow`, wildcard e `$`.

## Cosa non è verificato

- Raccolta live di sitemap e pagine dell'intero sito (accesso Internet assente dall'ambiente di test locale).
- Distribuzione Docker/Playwright su Windows e attività pianificata effettivamente eseguita.
- Che tutti gli URL di ricette del sito siano presenti nelle sitemap o nei collegamenti correlati.
- Che nessuna futura revisione del markup richieda nuovi adattatori.

## Cosa non viene fatto

- Nessun download di immagini, video o font; né memorizzazione di URL delle immagini nel JSON.
- Nessun cambio d'identità/IP per superare restrizioni, CAPTCHA o limitazioni.
- Nessuna stima di nutrienti/costi presentata come dato pubblicato.

**Test eseguiti:** 39 test offline superati il 7 novembre 2026, incluso controllo su sitemap compressa.

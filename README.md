# GF Opti Crawler (v0.8.2 beta)

**Crawler dedicato a GialloZafferano.** Progetto indipendente, non ufficiale e non affiliato a GialloZafferano. La compatibilità con altri siti non è stata verificata.

This repository distributes crawler source code only, not collected recipes or source HTML. Respect the target website's terms, robots.txt and content rights. Do not publish scraped datasets without authorization.

**Known limitations:** No guarantee of whole-site coverage; some one-ingredient recipes and technique guides may be marked `review`; prior production data may contain legacy alias chains (the new resolver handles these). No live production crawl was run for this release.

## v0.8.2 changes
- Sitemap refresh no longer requeues actively owned `in_progress` recipe URLs.
- Canonical aliases are flattened when an intermediate alias is replaced; historical chains can be resolved.
- Conditional HTTP validators are stored against the requested resource rather than a different canonical URL.
- Browser iframe documents and redirects now use the shared host request limiter; only the first main-frame navigation is pre-reserved.
- No change to ingredient quality threshold; the 62 `review` records are deliberately not silently accepted.

## Publishing and local data
Never commit `.env`, `data/`, SQLite databases, third-party HTML fixtures, credentials or scraped exports. See `.gitignore`.

---

# GF Opti Crawler v0.8.2 — GialloZafferano / Windows

Crawler progettato e verificato per GialloZafferano, per creare un **archivio testuale strutturato di ricette** con discovery da sitemap e pagine elenco ammesse, raccolta asincrona, deduplicazione canonica, SQLite persistente, verifica della copertura e Playwright opzionale.

**Dati v0.8:** nessuna istruzione di preparazione, fotografia o tecnica dedotta viene conservata. Il parser legge i passaggi **solo temporaneamente in memoria** per individuare gli **utensili** e poi scarta il procedimento. Nutrizionali e valori energetici vengono **estratti dalla pagina**, non calcolati. La frequenza minima predefinita è **4 secondi per host**, salvo regole del sito più restrittive.

## Requisiti

- Windows 10/11, PowerShell e Docker Desktop con backend Linux/WSL2.
- Connessione internet per la raccolta. Le pagine HTML di terze parti utilizzate nei test offline non sono distribuite nel repository; alcuni test basati su fixture vengono saltati se i file sono assenti.
- Autorizzazione a raccogliere e utilizzare i contenuti delle fonti scelte. Il programma applica le regole `robots.txt`, non aggira blocchi e non usa rotazione di IP o identità. I 429 vengono ritentati **solo** dopo un cooldown condiviso; se persistono, la raccolta si arresta.

## 1. Installazione

Estrai lo ZIP in una cartella, apri PowerShell nella cartella e lancia:

```powershell
.\install.ps1 -Browser
```

`-Browser` installa anche Chromium/Playwright per il fallback. Senza tale parametro viene installato soltanto il motore HTTP più leggero.

Lo User-Agent **non richiede email**. Per configurarlo una volta sola:

```powershell
Copy-Item .env.example .env
notepad .env
```

Nel file `.env` puoi impostare, per esempio, `RECIPE_SCRAPER_USER_AGENT=MyRecipeCrawler/0.8`. Puoi anche sovrascriverlo per una singola esecuzione con `--user-agent`.

## 2. Primo collaudo e avvio

```powershell
# Poche categorie e ricette; i due flussi procedono insieme
.\crawler.ps1 pipeline --categories --category-limit 5 --limit 10 --browser auto

# Statistiche del database
.\crawler.ps1 stats

# Raccolta continua; scopre URL anche mentre ne estrae altri
.\crawler.ps1 pipeline --categories --category-limit 0 --all --workers 2 --browser auto

# Variante richiesta: 2 secondi per host, fino a 4 worker per host
.\crawler.ps1 pipeline --categories --category-limit 0 --all --workers 4 --delay 2 --browser auto
```

Per **riprendere** il lavoro sulle code già presenti senza rileggere le sitemap:

```powershell
.\crawler.ps1 pipeline --skip-discovery --categories --category-limit 0 --all --browser auto
```

Per estrarre solo le ricette già in coda, senza categoria/discovery:

```powershell
.\crawler.ps1 crawl --all
```

Il ritmo predefinito è 4 secondi fra inizi di richieste verso lo stesso host, con eventuale incremento se la fonte dichiara un intervallo più lungo. Il rate limiter è **condiviso tra i worker**: con `--delay 2` il massimo teorico resta **30 inizi di richiesta al minuto per host**, sia con 2 sia con 4 worker. Con `--workers 4` si possono sovrapporre più risposte lente e parsing, ma aumentano le richieste contemporaneamente in corso. La pipeline può avere fino a **4 worker per host** (ricette e categorie: due host diversi). Anche le richieste dei sottocomponenti Playwright ammessi sono soggette al pacing condiviso.

### Retry automatico e protezione del server (v0.8.1)

- **HTTP 429/503:** pausa per **tutti** i worker del dominio, rispettando `Retry-After` in secondi o data HTTP. Se manca, pausa prudenziale crescente (429: 30, 60 secondi; 503: 15, 30 secondi). Il delay di base raddoppia fino a 8 volte il minimo e torna gradualmente al valore iniziale dopo 25 risposte riuscite.
- **HTTP 500/502/504, timeout e errori di rete:** fino a 2 retry immediati aggiuntivi per richiesta, con attese esponenziali e piccolo jitter. Se falliscono, l'URL passa alla coda persistente `retry` o `failed` secondo `--max-attempts`.
- **Circuit breaker:** dopo 3 risposte 429/503 prima del recupero, il crawler interrompe l'esecuzione per non continuare a sovraccaricare il dominio. Il database resta riprendibile. HTTP 401/403, CAPTCHA e disallow di `robots.txt` interrompono immediatamente senza retry.
- **Configurazione:** `--http-retries 2` e `--max-throttles 3` sono i default; si possono modificare, ma alzare le soglie non è consigliato in presenza di limitazioni del sito.

I retry immediati sono automatici. I record che finiscono nella coda SQLite con `retry_at` **non vengono attesi indefinitamente nella stessa esecuzione**: vengono ripresi quando lanci nuovamente la pipeline dopo la scadenza. L'opzione `--all` copre gli URL disponibili ora, non garantisce che i retry futuri siano già completati. `Playwright` viene attivato soltanto per pagine normali non interpretabili dal parser HTML.

## 3. Cosa salva una ricetta

- Identità: URL canonico, titolo, descrizione, autore, date se presenti, categoria e parole chiave.
- Dati pratici: ingredienti (quantità, unità e gruppi), resa, porzioni, tempi, difficoltà e costo indicativo dichiarato.
- Valori nutrizionali **solo se pubblicati dalla fonte**, con unità e provenienza, senza stime inventate.
- Informazioni accessorie testuali: presentazione, conservazione, consigli, FAQ quando disponibili, ricette correlate come titolo + URL.
- Dati derivati esplicitamente segnalati: utensili riconosciuti dal procedimento in RAM, possibili allergeni, compatibilità alimentari dichiarate, aggiunte/sostituzioni esplicite nei consigli.
- Qualità e provenienza: warning, risultati della validazione e data di raccolta.

**Non salva:** procedimento originale, istruzioni numerate, foto dei passaggi, altri URL delle immagini, video, tecniche dedotte dal procedimento o calorie inventate. Le informazioni mancanti restano mancanti.

### Il procedimento serve comunque durante l'estrazione?

Sì, ma soltanto in RAM: viene usato per riconoscere parole come «mixer», «pentola», «teglia», «forno». Non viene persistito nel database e non compare nell'export. L'elenco degli utensili è euristico: potrebbe essere incompleto e non sostituisce una verifica umana.

## Aggiornamento rapido dalla v0.8 senza perdere il database

1. Ferma il crawler v0.8 con `Ctrl+C` e attendi la chiusura del container.
2. Fai una copia di sicurezza dell'intera cartella `data` (inclusi eventuali file `-wal` e `-shm`).
3. **Metodo più semplice:** estrai il pacchetto **patch v0.8.1** direttamente nella cartella v0.8, sostituendo i file con lo stesso nome; la cartella `data` non è inclusa nella patch.
4. Ricostruisci il container perché Docker deve caricare il codice nuovo:

```powershell
.\install.ps1 -Browser
```

5. Riprendi la coda senza rileggere le sitemap, con 2 secondi e 4 worker per host:

```powershell
.\crawler.ps1 pipeline --skip-discovery --categories --category-limit 0 --all --workers 4 --delay 2 --browser auto
```

Se preferisci usare lo ZIP completo v0.8.1 in una nuova cartella, **copia la vecchia cartella `data`** nella nuova prima di eseguire il comando. Non eseguire contemporaneamente due istanze sullo stesso database.

## 4. Migrazione da v0.7 / v0.6 e pulizia vecchie ricette

1. Arresta il vecchio crawler e attendi che il container sia terminato. Non far girare due versioni contemporaneamente.
2. **Conserva una copia dell'intera cartella `data`** della versione precedente (eventuali file SQLite WAL inclusi), poi copiala nella cartella della v0.8. Non cancellare `recipes.sqlite3`.
3. Costruisci le nuove immagini Docker: ` .\install.ps1 -Browser `.
4. Pulisci il JSON dei record precedenti:

```powershell
.\crawler.ps1 prune-procedures
```

Il comando crea automaticamente **`data/recipes-before-prune.sqlite3`** prima di modificare i record. Rimuove anche i campi immagine annidati e ricalcola hash e qualità. Per evitare sovrascritture, se il backup con lo stesso nome esiste già il comando si ferma: conservalo altrove prima di una nuova migrazione.

La migrazione **non riscarica** le ricette e non cancella le code già raccolte. Se hai già usato un export precedente, rigeneralo dalla v0.8.

## 5. Controllare dati mancanti e completezza

```powershell
.\crawler.ps1 audit --deep --strict --output data/audit.json
.\crawler.ps1 issues --include-categories --output data/issues.jsonl
.\crawler.ps1 stats
```

`--strict` restituisce un codice di errore se restano URL storici irrisolti, categorie incomplete, record di qualità dubbia o conflitti canonici. `--deep` individua anche vecchi record che contengono procedure o campi immagine, per cui richiede la migrazione. Il campo `complete_for_combined_sources` significa soltanto **completo rispetto alle fonti effettivamente scoperte e analizzate**, non prova che ogni ricetta pubblicata sul sito sia stata trovata.

Per riprovare record da rivedere:

```powershell
.\crawler.ps1 requeue --status review --limit 100
.\crawler.ps1 requeue --listings --status review --limit 100
```

## 6. Esportazione, backup e automazione

```powershell
.\crawler.ps1 export --format jsonl --output data/ricette.jsonl
.\crawler.ps1 export --format json --output data/ricette.json
.\crawler.ps1 backup --output data/backup-manuale.sqlite3
.\schedule.ps1 -At '03:00' -Browser
```

L'export elimina anche eventuali vecchi campi del procedimento/immagini **in lettura**, ma si raccomanda comunque `prune-procedures` per pulire davvero il database. La pianificazione di Windows esegue la pipeline incrementale, crea backup settimanali e aggiorna il report di audit. Non usa indirizzi email.

## 7. Test offline

Se hai Python e le dipendenze sul PC, lancia dalla cartella:

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -p "test_*.py"
```

I test controllano le ricette campione, l'estrazione delle informazioni nutrizionali, gli utensili senza salvare il procedimento, gli alias canonici, la validazione, le code persistenti, l'audit, la scoperta asincrona e il rate limiter simulato. **Non** costituiscono un test live di tutte le pagine né una misura di performance su Docker Desktop Windows.

## Architettura

- `async_pipeline.py` e `async_discovery.py`: discovery e fetch asincroni con `httpx`, worker condivisi per dominio.
- `crawler.py`: CLI, SQLite WAL, queue, audit, esportazioni, rate limiter e ripresa.
- `catalog_discovery.py`: cataloghi/categorie e link alle ricette.
- `scraper.py` e `enrichment.py`: parsing e normalizzazione, utensili riconosciuti da testo transitorio.
- `integrity.py`: qualità, URL canonico, alias e ripulitura dei record precedenti.
- `browser_fallback.py`: browser opzionale per le pagine che lo richiedono.
- `install.ps1`, `crawler.ps1`, `schedule.ps1`, `scheduled-run.ps1`: operazioni Windows.

**Limiti ancora da verificare:** raccolta completa live, compatibilità delle pagine categoria attuali, funzionamento di Playwright sul tuo Windows e throughput reale. Per non perdere dati, la raccolta estesa deve partire dopo un test ridotto e la verifica dell'audit.

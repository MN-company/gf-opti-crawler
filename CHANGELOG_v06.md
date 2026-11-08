# Changelog — v0.6

- Aggiunto il discovery del dominio `www.giallozafferano.it` dalle sitemap pubblicate `portate.xml` e `ricerche.xml`.
- Nuova coda SQLite delle pagine categoria, separata da quella delle ricette.
- Parsing delle schede ricetta in HTML (`gz-card`) e di `ItemList` JSON-LD, con paginazione priva di query string.
- Tracciamento della provenienza (`sitemap`, `category`, `related`), confronto tra le fonti e nuovi campi nel report audit.
- Parser categorie conservativo: se nessuna scheda ricetta è individuata, marca `review` e non nasconde il problema.
- Controllo `robots.txt` separato per i due host; richieste con parametri escluse dalle liste.
- Aggiornata l'immagine Docker con il nuovo modulo Python; script Windows giornaliero con scansione incrementale di 50 categorie.
- Migrazione SQLite automatica, nessuna modifica del formato delle ricette, immagini sempre escluse.
- Test offline con due pagine HTML della versione precedente e mock delle sitemap/pagine elenco.

**Da collaudare sul PC Windows:** Docker, accessibilità delle sitemap live, contenuto reale delle pagine categoria e conteggio URL scoperti.

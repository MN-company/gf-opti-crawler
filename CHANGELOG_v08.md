# Changelog v0.8 — 7 novembre 2026

## Modello dei dati
- Il procedimento è analizzato soltanto in memoria per il riconoscimento degli utensili.
- Eliminata la persistenza/esportazione di `instructions`, `instruction_steps`, `instruction_steps_with_photos`, `step_images_count`, `techniques_detected`, `method` e campi fotografici, anche se annidati in vecchi record.
- Nutrienti estratti solo dal JSON-LD/DOM della fonte: la pagina Lasagne campione espone nove valori, Hummus campione non ne espone.
- `prune-procedures`: backup SQLite obbligatorio e conversione idempotente degli archivi precedenti, con aggiornamento di hash e stato qualità.
- Audit profondo: rileva anche record precedenti che contengono ancora procedure o immagini.

## Integrità e velocità (correzioni rispetto all'audit v0.7)
- Audit globale: non dichiara completo un archivio che contiene URL storici irrisolti.
- Alias e URL canonici: una ricetta canonica archiviata una sola volta, conflitti conservati per revisione.
- Qualità: record sospetti in revisione e conservazione dell'ultima ricetta valida.
- HTTP 404/410: recheck, senza considerare automaticamente il contenuto come raccolto.
- Discovery sitemap progressiva via HTTP asincrono; sitemap atomiche e riavvio sullo stato persistente.
- Worker di categoria e ricetta con frequenza per host condivisa e writer SQLite unico.
- Indici delle code SQLite e cache condizionale delle categorie.
- Default `--delay` portato a **4 secondi** (con rispetto di ritardi più restrittivi richiesti dal server).
- User-Agent configurabile, email non necessaria, Playwright soltanto come fallback.

## Stato delle verifiche
- Test offline: 72 superati (i due HTML forniti e simulazioni).
- Nessun crawl integrale live né benchmark validato su Windows/Docker; i tempi effettivi dipendono dai server e dalla rete.
- Non inclusi sistemi di cambio automatico di IP o cookie per superare restrizioni.

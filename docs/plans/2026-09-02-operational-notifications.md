# Piano — Notifiche operative: listing sparito, aggregazione per dominio, bottoni, instradamento

- Data: 2026-09-02 (revisione 2, dopo stress-test avversariale: 6 finding integrati, vedi §11)
- Repo: `bernalli/price-tracker-bot`, v0.2.0, deployed
- Branch di lavoro: `feature/operational-notifications`, creato da `main` (NON dal branch
  `fix/public-metadata-and-privacy`, che contiene la bonifica doc e il fix Shopify 403/429 e va
  mergiato per conto suo)
- Baseline misurata il 2026-09-02 sul working tree di `fix/public-metadata-and-privacy`
  (`.venv/bin/pytest -q`): **717 passed**, coverage **91,65%**; `ruff check`, `ruff format --check`,
  `mypy --strict` (152 file), `scripts/audit_english.sh` tutti verdi. Il Task 0 rimisura la
  baseline sul branch nuovo: `main` non contiene i test aggiunti dal fix Shopify, quindi il numero
  su `main` può essere inferiore a 717 — il numero che vale è quello misurato in T0.

Il piano è scritto per un esecutore che legge SOLO questo file. Ogni passo ha: cosa cambia, file e
funzione, test che lo prova (scritto PRIMA e visto fallire), comando di verifica. Prosa in
italiano; codice, identificatori, messaggi di commit e msgid in inglese. I blocchi Python sono
formattati come li lascia `ruff format` 0.16 (che formatta anche i blocchi dentro il Markdown).

---

## 0. L'incidente e il perimetro

Cinque notifiche identiche `⚠️ Tracking suspended … Failed 10/10 consecutive checks` sono arrivate
insieme per cinque prodotti dello stesso store Shopify. Verifica live del 2026-09-02 (fatta dal
main loop): le pagine prodotto rispondono HTTP 404, lo store è vivo (homepage 200, sitemap con 253
prodotti, nessuno dei cinque), lo scraper funziona su un prodotto vivo dello stesso dominio. Il bot
ha fatto la cosa meccanicamente giusta (`MAX_CONSECUTIVE_ERRORS=10` × `CHECK_INTERVAL_MINUTES=360`
≈ 2,5 giorni) e quella comunicativamente inutile: mai detto che i prodotti non esistono più, nessun
pre-avviso, nessun raggruppamento, nessun bottone.

Il piano copre **il canale delle notifiche operative e solo quello**, nei sei punti del brief:

1. riconoscere il listing sparito (404/410) e agire prima di dieci sweep;
2. aggregare per sweep, utente e dominio;
3. dire il perché (reason, `last_error`, ultima lettura buona);
4. pre-avviso a metà soglia, instradato come notizia operativa (quiet hours e digest);
5. bottoni nelle notifiche (riattiva-e-ricontrolla / elimina, per dominio);
6. instradamento: quiet hours e digest sì, mute no — e mai una perdita silenziosa.

Fuori perimetro (vedi §9): transactional outbox (finding 2 della review core), tick per-prodotto,
soglia percentuale contro `initial_price`, localizzazione completa dell'interfaccia.

---

## 1. Derive trovate rispetto al brief (verificate live il 2026-09-02)

Il brief cita note di lavoro; queste sono le differenze rispetto al codice. Ognuna cambia il
progetto, per questo stanno prima delle decisioni.

| # | Il brief dice | Il codice fa (verifica) | Conseguenza sul piano |
|---|---|---|---|
| D-1 | «`http_error` (404 incluso) va diritto al contatore per prodotto» | **Shopify, Generic, eBay, Nove25 e AliExpress inghiottono il 404**: i loro `except httpx.HTTPError` / `except httpx.HTTPStatusError` trasformano la risposta in `ProductInfo(price=None, error=…)` (`shopify.py::_fetch_html`, `generic.py::scrape`, `ebay.py:76`, `nove25.py:70`, `aliexpress.py:77`). Lo scheduler vede `info.price is None` e persiste `last_error = "price_none"`. Verificato eseguendo gli scraper veri contro `respx` con 404 (Shopify e Generic da me, i tre restanti dallo stress-test). Amazon (`amazon.py:121`) cattura `HTTPStatusError` e passa ai fallback. | Il 404 va **fatto emergere dentro gli scraper** (nuova eccezione `ListingGone`) nei cinque nominati, E classificato nello scheduler quando arriva come `HTTPStatusError` da chi lo lascia salire (§5.1, T2). Amazon e gli altri dieci restano indistinguibili (§9). Per i cinque prodotti dell'incidente il `last_error` in produzione è quasi certamente `price_none`, non `http_error`. |
| D-2 | «`_record_failure_and_maybe_disable` persiste `last_error` … `format_error_notification` riceve solo name e url» | Vero, ma in più **`ProductRecord` non ha `last_error`/`last_error_at`**: `_PRODUCT_COLS` (`db/repository.py:55-62`) non li seleziona; li espone solo `ProductErrorRow` via `list_products_with_errors`. | `_PRODUCT_COLS` e `ProductRecord` vanno estesi (T1) — è il passo a più alto rischio di regressione, va per primo con baseline. |
| D-3 | «`domain` sempre risolvibile» (premessa da verificare) | `extract_etld_plus_one` ritorna `""` per host senza public suffix (`https://shop.example/…` → `""`, `http://localhost/x` → `""`) e lo scheduler usa `"unknown"`; `sub.shop.myshopify.com` → `myshopify.com` (tutti gli store myshopify sono UN dominio per il grouping). | L'aggregatore ha una chiave di gruppo con fallback sul netloc (§4 D3) e un bucket `unknown`. |
| D-4 | «`is_active=0 AND consecutive_errors >= max` distingue una sospensione automatica da una pausa voluta senza migrazione» | **Falso** (finding F1 dello stress-test): `pause_product` cambia solo `is_active` e lascia i contatori (`repository.py:327`), quindi sette errori seguiti da una pausa manuale soddisfano la disuguaglianza; e le soglie sono configurazione corrente, non stato: un prodotto auto-sospeso a soglia 3 sparisce dal gruppo se la soglia sale a 10. | La provenienza della sospensione si **persiste** (`suspension_kind`, `suspension_reason`, migrazione 014) nel percorso che sospende e non si inferisce mai dai contatori (D7, D9, §5.2, T1, T7). |
| D-5 | Il dossier dà per scontato che il digest flusci «alla fine delle quiet hours» (docstring `notifier/digest.py:5`, `docs/notifications.md`) | **Non implementato**: `DigestService.flush_due` guarda solo l'età dell'entry più vecchia contro `digest_interval_minutes`; il test `test_flush_at_quiet_hours_end_via_scheduler` verifica in realtà solo l'intervallo. | Un avviso operativo «differito» nelle quiet hours verrebbe consegnato ≤60 min dopo, ancora in piena notte. T6 rende `flush_due` consapevole delle quiet hours (D6, dichiarato anche in §10). |
| D-6 | `scheduler.py:348` e `:416` ignorano il ritorno `False` del notifier | Confermato: entrambe le chiamate sono `await self.deps.notifier(user_id, message)` nude, senza `product_id`, senza leggere il ritorno. | Sostituite dal flush dell'accumulatore via `Scheduler._notify`, che legge il ritorno (T4). Il ritorno `False` viene loggato: la consegna garantita resta della outbox (§9). |
| D-7 | «`check_one_product_for_user` esiste già ed è ciò che il bottone dovrebbe chiamare» | Esiste, ma chiama `_check_product_core` **senza** except-ladder: un'eccezione dello scraper sale al chiamante e non incrementa niente (finding 7 della review; finding F6 dello stress-test per `ListingGone`). | Un solo loop pull-mode, `check_products_for_user`, con la ladder completa; `check_one_product_for_user` e `check_user_products_for_user` gli delegano (§5.4, T4). |
| D-8 | `format_quarantine_notification` è una «notifica operativa» come le altre | È l'unica notifica push **hardcodata in italiano** dentro `core/alert.py:124-145` (passa l'audit perché non contiene vocali accentate né le parole della blacklist). | Viene instradata con `kind="operational"` (T4) ma il suo testo NON viene toccato (§9). |
| D-9 | `docs/operations.md:46` descrive `MAX_CONSECUTIVE_ERRORS` come «Threshold before HealthManager quarantines a domain» | Falso: è la soglia di auto-sospensione per prodotto; la quarantena usa le soglie 3/6/12 di `core/health.py:_TIERS`. | Riga corretta nel passo doc (T8). |
| D-10 | «Instradata **con** `product_id` così rispetta quiet hours e digest» (brief, punti 4 e 6) | `digest_queue.product_id` è `NOT NULL` con `ON DELETE CASCADE` (`010_add_digest_queue.sql`): una riga aggregata agganciata a un prodotto ancora sparisce se l'utente cancella QUEL prodotto, portandosi via l'avviso degli altri (F2). E le preferenze sono per-prodotto → globali → default: l'ancora non rappresenta il gruppo (F5). | L'instradamento è deciso da `kind="operational"`, non da un `product_id`; le righe operative in coda hanno `product_id = NULL` (migrazione 015, rebuild della tabella con `ON DELETE SET NULL`); le preferenze usate sono SOLO quelle globali dell'utente (D6, §5.5, §5.6). |
| D-11 | Cap a 15 prodotti + un test con nomi corti «garantisce ≤ 4000 caratteri» (revisione 1 di questo piano) | **Falso** (F3): con nomi da 200 caratteri e `last_error` da 120 la notifica arriva a 6636 caratteri, la risposta del callback a 10839, un digest da 50 entry a 12000. Il limite Telegram è 1-4096 dopo il parsing delle entità, per `sendMessage` e `editMessageText`. | Contratto unico di segmentazione (D11, §5.8): budget per campo, misura dopo lo strip dei tag, split su righe a tag bilanciati, tastiera sull'ultimo chunk, digest che marca flusciate solo le entry delle pagine inviate. |

---

## 2. Premesse

Ogni voce è marcata `verificata live` (comando + data) o `NON VERIFICATA`. Non esiste
the project's own notes (no constraints or mission file recorded as of 2026-09-02),
quindi non ci sono vincoli in volo né CRITICAL_PATHS da riconciliare; nessuna premessa NON
VERIFICATA tocca security/custodia. Nessun altro piano attivo descrive questi oggetti.

| # | Premessa | Stato |
|---|---|---|
| P1 | `products.consecutive_errors` è `INTEGER NOT NULL DEFAULT 0`; `is_active` è `INTEGER NOT NULL DEFAULT 1`; `last_error TEXT` e `last_error_at TIMESTAMP` sono **nullable** (NULL su prodotto nuovo); `domain TEXT` nullable; `last_checked_at TEXT` nullable e **NULL finché non c'è una lettura riuscita**; `currency TEXT NOT NULL DEFAULT 'EUR'`. | `verificata live` 2026-09-02: `PRAGMA table_info(products)` su DB in memoria dopo `apply_migrations` (versione 13). |
| P2 | `last_checked_at` avanza **solo** in `Repository.update_price` (cioè solo su lettura accettata e persistita); nessun altro scrittore. | `verificata live` 2026-09-02: `grep -rn last_checked_at src/` → unico `UPDATE` in `repository.py:292`; confermato eseguendo `update_price` e leggendo il campo. |
| P3 | `last_error` è scritto solo da `Repository.set_last_error` (troncato a 300 char) chiamato da `_record_failure_and_maybe_disable` PRIMA di `deactivate_product`; quindi ogni prodotto sospeso automaticamente dopo la migrazione 012 ha `last_error` popolato nella forma `"{reason}: {detail}"` o `"{reason}"`. Prodotti sospesi prima della 012 possono avere `last_error IS NULL`. | `verificata live` 2026-09-02 per costruzione (lettura `scheduler.py:385-397`) + esecuzione. Il rendering DEVE tollerare `None` (§5.3). |
| P4 | `pause_product`/`deactivate_product` non toccano `consecutive_errors`; `reactivate_product` lo azzera e rimette `is_active=1`. **Quindi dai contatori NON si ricostruisce l'origine di una sospensione** (D-4). | `verificata live` 2026-09-02 (3 → deactivate → 3, reactivate → 0) e dallo stress-test (`manual_pause_selected_as_auto True`). |
| P5 | `list_products_for_user(only_active=True)` ordina per `id ASC`: l'ordine di sweep è deterministico e i prodotti di uno stesso dominio NON sono necessariamente contigui. | `verificata live` 2026-09-02. L'aggregatore accumula tutto e raggruppa in coda. |
| P6 | `extract_etld_plus_one` ritorna `""` (non solleva) su input vuoto, non-URL, host senza public suffix e IP; ritorna `example.co.uk` per `.co.uk`. | `verificata live` 2026-09-02 (7 input). |
| P7 | Un 404/410 sulla pagina prodotto attraversa `with_retry` senza retry (`_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}`), quindi ogni check che vede un 404 ha fatto **una** sola GET e il 404 è la risposta definitiva di quel check. | `verificata live` 2026-09-02 (lettura `core/retry_policy.py:37-44`). |
| P8 | Su risposta 404: Shopify, Generic, eBay, Nove25 e AliExpress ritornano `ProductInfo(price=None, error=…)` senza eccezione; Amazon cattura `HTTPStatusError` in `_fetch_page` e prosegue coi fallback; gli altri dieci scraper (apple_store, bestbuy, etsy, google_store, mediamarkt, newegg, otto, target, walmart, wayfair, zalando) hanno `raise_for_status` dentro `try` di cui NON ho verificato gli `except`. | `verificata live` 2026-09-02 per i cinque nominati (respx: due da me, tre dallo stress-test). **NON VERIFICATA** per Amazon e i dieci restanti per esecuzione: il piano NON promette per loro il riconoscimento del listing sparito (§9); il ramo scheduler di §5.1 copre solo chi lascia salire l'`HTTPStatusError`. |
| P9 | `price_tracker.bot.messages` (runtime gettext) è importabile da `core/` senza ciclo: `bot/__init__.py` è vuoto e `messages.py` importa solo stdlib. | `verificata live` 2026-09-02. |
| P10 | La ContextVar di locale è per-task asyncio: `set_locale` dentro il job periodico non contamina gli handler. Il job PTB gira in un task proprio. | `verificata live` per lettura di `bot/messages.py:215-218` e del contratto `contextvars`. Il comportamento di `JobQueue` di PTB 22.7 è **NON VERIFICATA** per esecuzione. Mitigazione: T4 imposta la locale e la ripristina nel `finally` del flush. |
| P11 | `InlineKeyboardButton.callback_data` accetta al massimo 64 byte. `ops_delok_` + il massimo intero SQLite misura **29 byte**. | Limite: **NON VERIFICATA** dal documento ufficiale (pagina troncata dal fetcher); la misura 29 byte è dello stress-test. Il notifier valida comunque la lunghezza (§5.5). |
| P12 | Telegram rifiuta messaggi la cui lunghezza **dopo il parsing delle entità HTML** supera 4096 caratteri, per `sendMessage` e `editMessageText`. | Limite: **NON VERIFICATA** dal documento ufficiale; le misure dello stress-test (6636 / 10839 / 12000 caratteri prodotti dalla revisione 1) sono il motivo del contratto §5.8. |
| P13 | Il notifier di produzione (`TelegramNotifier`) riceve **sempre** `prefs` e `digest` (`main.py:90-95`); la via `_send_direct` si prende solo nei test con `prefs=None`. | `verificata live` 2026-09-02 (lettura). |
| P14 | `pybabel` (2.18) è nel venv; `babel.cfg` copre `price_tracker/**.py` quindi anche `core/` e `notifier/`; i `.mo` sono committati e un pre-commit `pybabel-compile-check` pretende `.mo` aggiornati. | `verificata live` 2026-09-02. |
| P15 | The deployed database holds i cinque prodotti con `last_error = "price_none"` e `consecutive_errors = 10`. | **NON VERIFICATA**: the deployed container is out of scope for this work. Non condiziona il piano. |
| P16 | `sqlite3.sqlite_version` = 3.53.1 nel venv. | `verificata live` 2026-09-02. |
| P17 | `digest_queue.product_id` è `NOT NULL` con `FOREIGN KEY … ON DELETE CASCADE`: `delete_product` del prodotto referenziato cancella la riga in coda (righe 1 → 0). | `verificata live` 2026-09-02 (`scratchpad/rebuild.py`, `PRAGMA foreign_keys=ON`) e dallo stress-test. |
| P18 | La migrazione di rebuild di `digest_queue` (§5.2, migrazione 015) passa attraverso `_execute_migration_sql` (split su `;`), preserva le righe e gli id, ricrea `idx_digest_pending`, e dopo di essa `delete_product` lascia la riga con `product_id = NULL`; l'inserimento con `product_id = NULL` è accettato. | `verificata live` 2026-09-02 (`scratchpad/rebuild.py`: fk `SET NULL`, riga sopravvissuta `(2, None, …)`, insert NULL ok, indice presente). |
| P19 | `ALTER TABLE products ADD COLUMN suspension_kind TEXT CHECK (suspension_kind IN ('manual', 'automatic'))` è accettato da SQLite, il CHECK è enforced (`IntegrityError` su valore estraneo) e la riapplicazione è idempotente nel migrator. | `verificata live` 2026-09-02 (scratch, applicato due volte). |
| P20 | Le preferenze `digest_mode`, `quiet_hours_*`, `throttle_per_hour`, `timezone` possono differire tra due prodotti dello stesso dominio (override per-prodotto in `notification_prefs`); `flush_due` legge SOLO la riga globale (`product_id IS NULL`). | `verificata live` dallo stress-test (`anchor_digest=False other_digest=True`, `anchor_quiet=False other_quiet=True`) e per lettura di `digest.py:98`. È la ragione della scelta D6 «solo preferenze globali». |
| P21 | `docs/notifications.md` (sezione «Resolution priority») dichiara che digest mode, quiet hours, throttle e timezone sono **user-wide, senza override per-prodotto**; il codice (`PreferencesManager.resolve`) invece coalesce per-prodotto → globale su tutti i campi. | `verificata live` 2026-09-02 (lettura). Documento e codice divergono già oggi per i ribassi; il piano non risolve la divergenza per i ribassi (§9) e per gli operativi sceglie la semantica documentata (globale). |

---

## 3. Vocabolario normativo

Questi nomi sono usati identici in tutto il piano e nel codice.

- **reason** — token persistito in `last_error` prima dei due punti e in `suspension_reason`.
  Lista chiusa (§5.1): `listing_gone`, `block`, `parse_error`, `price_none`, `no_scraper`,
  `condition_mismatch`, `implausible_read`, `http_error`, `unexpected`. Qualunque altro valore è
  «sconosciuto» e cade nel copy di default.
- **group_key** — chiave di aggregazione per dominio: `extract_etld_plus_one(url)` se non vuoto,
  altrimenti `urlparse(url).netloc.lower()` se non vuoto, altrimenti `"unknown"`. Funzione
  canonica `group_key_for(url)` in `core/notices.py` (T3).
- **evento operativo** — `OperationalEvent` (T3): uno per prodotto per sweep, di tipo
  `suspended` o `warning`.
- **gruppo** — `NoticeGroup`: gli eventi dello stesso `(user_id, event, group_key)` di uno sweep.
  Il **prodotto ancora** è quello con `product_id` minimo: il suo id viaggia SOLO nel
  `callback_data` dei bottoni, per ricavare `user_id` e `group_key` al click. Non instrada la
  notifica, non è owner di nessuna riga in coda.
- **kind** — chiave del payload notifier: `"price"` (ribasso, restock: comportamento odierno) o
  `"operational"` (sospensione, pre-avviso, quarantena). È `kind`, non `product_id`, a decidere
  l'instradamento.
- **auto-sospeso** — prodotto con `is_active = 0 AND suspension_kind = 'automatic'`. «In pausa» è
  `is_active = 0 AND suspension_kind = 'manual'`. Una riga inattiva con `suspension_kind IS NULL`
  è di **origine sconosciuta** (precedente alla migrazione 014) e per le azioni di gruppo vale come
  in pausa, mai come auto-sospesa (politica conservativa, D7).
- **lunghezza visibile** — `visible_length(html)`: lunghezza del testo dopo lo strip dei tag e
  l'`html.unescape` delle entità; è la misura contro il limite Telegram (§5.8).

---

## 4. Decisioni di design

Ogni decisione punta al paragrafo normativo (§5) e al task (§6) che la realizza; ogni paragrafo
normativo dichiara le decisioni che realizza. Se correggi una decisione, correggi ANCHE il testo
normativo e il task: le tre cose divergono alla prima modifica non allineata.

**D1 — Il listing sparito è un'eccezione di dominio, non una lettura fallita.** Nasce negli scraper
come `ListingGone(ScrapeError)` con `status` e `url`, sollevata da `detect_listing_gone(status_code,
url)` per 404 e 410, chiamata subito DOPO `detect_block_event` (dove c'è) e PRIMA di
`raise_for_status` nei fetch helper dei cinque scraper che oggi inghiottono il 404 (D-1): Shopify,
Generic, eBay, Nove25, AliExpress. Non è un `BlockEvent` (non alimenta la quarantena: uno store che
ha tolto un prodotto non ci sta bloccando) e non è un `httpx.HTTPError` (così i
`except httpx.HTTPError` esistenti non la inghiottono). Lo scheduler in più classifica come
`listing_gone` ogni `httpx.HTTPStatusError` con status 404/410 che qualche scraper lasci salire
(P8). Precedenza: 403/429 → `BlockEvent`; 404/410 → `ListingGone`; il resto come oggi. → §5.1, T2.

**D2 — Tre conferme, non una e non dieci.** Un prodotto viene sospeso con reason `listing_gone` alla
terza lettura consecutiva 404/410 (`gone_streak >= listing_gone_confirmations`, default 3,
env `LISTING_GONE_CONFIRMATIONS`), indipendentemente da `consecutive_errors`. Perché 3: un 404
singolo copre il deploy, la cache CDN o il geo-routing sbagliato di un istante; due coprono una
manutenzione dello stesso giorno; tre letture consecutive — ognuna una sola GET definitiva (P7) —
all'intervallo di default sono 12-18 ore su tre check distinti, e tre risposte «non esiste» nello
stesso giorno da uno store che intanto risponde 200 sulla homepage non sono un incidente. Dieci
sweep (2,5 giorni) sono assurdi per un fatto che è definitivo dalla prima risposta; uno solo cancella
un prodotto per un deploy. Nessuna cancellazione automatica: si sospende e si offre «Elimina».
`gone_streak` si azzera su qualunque esito diverso da `listing_gone` (successo o altro fallimento):
un 404-500-404 non è «due 404 consecutivi». Caveat dichiarato: con `check_interval_minutes = 5` per
prodotto le tre conferme stanno in 15 minuti; la variabile d'ambiente esiste per alzarle. → §5.2, T1, T2.

**D3 — Accumula durante lo sweep, emetti in coda, una notifica per utente e per dominio.** Il
`NoticeCollector` (T3) è un oggetto per chiamata: `run_check_all` ne crea uno per utente e lo svuota
al termine del tick di QUELL'utente (uno sweep globale può durare ore con 5 s tra prodotti; l'utente
non aspetta la fine di tutti); `run_check_for_user` e `check_products_for_user` ne creano uno per
chiamata e lo svuotano prima di ritornare. In pull mode quindi la notifica operativa parte lo
stesso — aggregata — come oggi parte da `_record_failure_and_maybe_disable`, mentre il riepilogo del
comando resta quello dell'handler. Il collector deduplica per `(event, product_id)` tenendo l'ultimo
evento (un prodotto sospeso due volte nello stesso sweep produce UNA riga) e ordina gruppi e prodotti
in modo deterministico. **Garanzia di flush** (F4): ogni entrypoint che possiede un collector lavora
in `try` e fluscia in `finally`, esattamente una volta, anche su eccezione e su cancellazione (testo
normativo in §5.4). La notifica di gruppo non è di proprietà di nessun prodotto: viaggia con
`product_id=None` e con la lista `product_ids` nel payload. → §5.4, T3, T4.

**D4 — Il messaggio dice perché, con copy che cambia col reason, dentro budget fissi.** Il rendering
vive in `core/alert.py` (`format_operational_notice`) e riceve il gruppo; per ogni prodotto stampa
nome (≤ 60 caratteri visibili), motivo umano, ultima lettura buona con data (P2) o «nessuna lettura
riuscita», e `last_error` grezzo (escaped, ≤ 120 visibili, «unknown» se `None`, P3). Titolo,
spiegazione e suggerimento seguono il **reason primario** del gruppo: `listing_gone` se TUTTI gli
eventi lo sono, altrimenti il reason più frequente (parità → ordine alfabetico). Lista chiusa in
§5.3; reason sconosciuto → copy di default, mai eccezione. Al massimo **10** prodotti elencati
(`MAX_LISTED_PRODUCTS`), poi `… and {k} more`: con i budget di §5.8 dieci prodotti stanno in un solo
messaggio; oltre, non è più un messaggio leggibile. Il rispetto del limite Telegram NON dipende dal
cap: lo garantisce il contratto di segmentazione (D11). → §5.3, §5.8, T3.

**D5 — Pre-avviso a metà soglia, esattamente una volta per episodio.** Evento `warning` quando
`consecutive_errors == max_consecutive_errors // 2` dopo l'incremento, solo se
`1 <= max // 2 < max` (con `max = 1` non esiste pre-avviso). Uguaglianza, non `>=`: al tick
successivo il contatore è a `max//2 + 1` e non rispara. Aggregato come la sospensione, instradato
con `kind="operational"`, senza bottoni. Non esiste pre-avviso separato per il listing sparito: con
default 3 < 5 la sospensione arriva prima. → §5.4, T4.

**D6 — Le notizie operative rispettano quiet hours e digest, ignorano il mute, usano solo le
preferenze globali dell'utente, e non vengono mai scartate né perse per cascata.** Il payload porta
`kind`; `notify_alert` instrada per `kind == "operational"` anche senza `product_id` e risolve le
preferenze con `PreferencesManager.resolve_global(user_id)` — la sola riga `product_id IS NULL`,
mai un override per-prodotto. Motivazione (F5, P20, P21): una notifica è per dominio, non per
prodotto; le preferenze per-prodotto di uno dei membri non rappresentano il gruppo, e la semantica
documentata in `docs/notifications.md` dichiara già digest/quiet hours/throttle user-wide.
`flush_due` legge la stessa riga globale: il criterio in enqueue e quello al flush coincidono per
costruzione, non per ricalcolo. L'alternativa scartata — spezzare i gruppi per chiave di routing
effettiva — distrugge l'aggregazione che è lo scopo del piano e non ha una controparte al flush.
Il mute (l'unica preferenza per-prodotto sensata) è saltato. Un avviso operativo non ha retry
naturale (il prodotto è già sospeso: il tick successivo non lo rivede), quindi ogni gate che oggi
SCARTA (quiet hours senza digest, throttle senza digest) per gli operativi ACCODA nella
`digest_queue` — con **`product_id = NULL`** (F2, P17, P18: una riga agganciata a un prodotto
cancellabile sparisce con `ON DELETE CASCADE`, portandosi via l'avviso degli altri prodotti del
gruppo; la migrazione 015 ricostruisce la tabella con riferimento nullable e `ON DELETE SET NULL`,
così anche le righe dei ribassi sopravvivono alla cancellazione del prodotto invece di sparire).
Perché il differimento sia vero, `DigestService.flush_due` salta gli utenti che al momento del
flush sono nelle loro quiet hours (D-5): tocca anche i ribassi in digest, dichiarato in §10. Il
digest rende gli operativi in una sezione propria e conta solo i price change nell'intestazione.
→ §5.2, §5.5, §5.6, T1, T5, T6.

**D7 — Bottoni senza il framework nel core, azioni di gruppo solo su provenienza persistita.** Il
payload porta `buttons` come dati puri (`list[list[{"text","callback_data"}]]`); `TelegramNotifier`
li converte in `InlineKeyboardMarkup` e li attacca all'ULTIMO chunk (§5.8). Un `buttons` malformato
fa inviare il messaggio SENZA tastiera con un warning, mai un'eccezione. Il digest non porta bottoni.
I callback sono `ops_react_<anchor>`, `ops_del_<anchor>`, `ops_delok_<anchor>` (prefisso `ops_`
non usato da nessun handler) e il `cancel_delete` esistente. L'handler ricava dal prodotto ancora
`group_key` e verifica la proprietà; il gruppo su cui agisce è **`list_auto_suspended_products
(user_id)`** — `is_active = 0 AND suspension_kind = 'automatic'` del clicker — filtrato per
`group_key`. Nessuna soglia nella query: la provenienza è scritta da chi sospende (D9) e non dipende
dalla configurazione corrente (F1). Righe inattive con `suspension_kind IS NULL` (precedenti alla
migrazione, origine non ricostruibile: P4) sono ESCLUSE dal gruppo: «Elimina tutti» non può toccare
un prodotto che l'utente potrebbe aver messo in pausa a mano; per quelle resta `/reactivate`.
«Riattiva e ricontrolla» riattiva ogni prodotto del gruppo (azzera contatori e provenienza) e poi
lancia `scheduler.check_products_for_user(product_ids=…, delay_between_products=0.5)` riportando
l'esito per prodotto, segmentato. «Elimina» chiede conferma (`ops_delok_`) e poi `delete_product`
per ciascuno, ricalcolando il gruppo alla conferma. → §5.2, §5.7, T1, T7.

**D8 — I testi nuovi nascono in `_()` col catalogo `it_IT`.** msgid in inglese senza vocali
accentate (l'audit copre `core/` e `notifier/`), msgstr italiani nel `.po`; lo scheduler imposta la
locale di `deps.lang` (= `Config.lang`, env `LOCALE`) per la durata del flush e la ripristina nel
`finally` (P10). `core/alert.py` e `core/notices.py` importano `_` da `price_tracker.bot.messages`
(P9): dipendenza core→bot.messages accettata perché `messages.py` è un runtime i18n senza telegram;
spostarlo in `price_tracker/i18n.py` è lavoro successivo (§9). → T3, T8.

**D9 — Persistenza: una UPDATE per fallimento, provenienza della sospensione scritta da chi
sospende, coda digest senza owner obbligatorio.** `Repository.record_failure` incrementa
`consecutive_errors`, aggiorna `gone_streak` (+1 se `listing_gone`, altrimenti 0) e scrive
`last_error`/`last_error_at` in UNA istruzione, poi rilegge il prodotto. La sospensione automatica
è `suspend_product(product_id, *, reason)` → `is_active = 0, suspension_kind = 'automatic',
suspension_reason = reason`; la pausa manuale è `pause_product` (alias `deactivate_product`) →
`is_active = 0, suspension_kind = 'manual', suspension_reason = NULL`. `reactivate_product` e
`reset_errors` azzerano `gone_streak`; `reactivate_product` azzera anche `suspension_kind` e
`suspension_reason`. Migrazione `014_add_suspension_provenance.sql` (tre colonne, nessun backfill:
le righe inattive esistenti restano `NULL` = origine sconosciuta) e `015_digest_queue_nullable_product.sql`
(rebuild di `digest_queue`, P18). `_PRODUCT_COLS`/`ProductRecord` prendono `last_error`,
`last_error_at`, `gone_streak`, `suspension_kind`, `suspension_reason` in coda, con default, così
ogni costruzione per keyword esistente resta valida. `DigestEntry.product_id` diventa `int | None`.
→ §5.2, T1.

**D10 — Sintesi dei residui di instradamento.** `_notify_quarantine_entry` passa a `Scheduler._notify`
con `kind="operational"` (così rispetta quiet hours e digest, D-8) ma il testo italiano hardcodato NON
si tocca. Il ritorno `False` del notifier sul flush viene loggato a WARNING con `user_id`,
`group_key`, `product_ids`: la consegna garantita è della outbox (§9).

**D11 — Un solo contratto di segmentazione per ogni testo che va a Telegram.** Modulo puro
`core/textlimits.py` (§5.8): budget per campo applicati dal renderer, righe a tag bilanciati come
invariante di ogni renderer, `split_message` che misura la lunghezza visibile e spezza su `\n`
sotto `SAFE_LIMIT = 4000`, degradando a testo piano una singola riga che da sola sfora. Lo usano:
`TelegramNotifier._send_direct`/`send_alert` (tastiera sull'ultimo chunk; ritorno `False` se un
chunk fallisce, con l'indice nel log), `DigestService.flush_user` (paginazione per entry: si marcano
flusciate SOLO le entry delle pagine effettivamente inviate; su fallimento di una pagina si
rilancia dopo aver marcato le precedenti), e le risposte dei callback `ops_*` (`edit_message_text`
col primo chunk, `reply_text` per i successivi). Abbassare il cap dei prodotti elencati non
sostituisce il contratto: non coprirebbe digest né callback. → §5.8, T3, T5, T6, T7.

---

## 5. Testo normativo

### 5.1 Eccezione `ListingGone` e classificazione dei fallimenti (realizza D1)

`src/price_tracker/core/exceptions.py`, dopo `ParseError`:

```python
LISTING_GONE_STATUSES: frozenset[int] = frozenset({404, 410})


class ListingGone(ScrapeError):
    """The product page answered 404/410: the listing no longer exists.

    Not a BlockEvent (the site is not refusing us) and not an httpx error
    (scrapers that swallow ``httpx.HTTPError`` must let this one through).
    """

    def __init__(self, *, status: int, url: str = "") -> None:
        self.status = status
        self.url = url
        super().__init__(f"HTTP {status} listing gone on {url}")
```

`src/price_tracker/core/scraper_base.py`, subito dopo `detect_block_event`:

```python
def detect_listing_gone(*, status_code: int, url: str) -> None:
    """Raise :class:`ListingGone` on 404/410. Call AFTER detect_block_event."""
    if status_code in LISTING_GONE_STATUSES:
        raise ListingGone(status=status_code, url=url)
```

Punti di chiamata (lista chiusa; gli altri scraper NON si toccano in questo piano, vedi §9):

1. `scrapers/shopify.py::_fetch_shopify_response` — riga dopo `detect_block_event(...)`, prima di
   `response.raise_for_status()`.
2. `scrapers/shopify.py::ShopifyScraper.scrape` — il blocco di enrichment
   `except BlockEvent: html = None` diventa `except (BlockEvent, ListingGone): html = None`
   (prezzo già in mano dal JSON: la pagina HTML che manca non nega il prodotto).
3. `scrapers/generic.py::_fetch_generic_html` — come al punto 1.
4. `scrapers/generic.py::GenericScraper.scrape` — nel `try` che chiama `_fetch_generic_html`,
   aggiungere PRIMA dell'`except (httpx.HTTPError, ValueError)`:
   `except ListingGone: if not html: raise` con un `logger.debug` nel ramo in cui `html` (da
   curl_cffi) esiste. Se `html` è vuoto, `ListingGone` vince anche su un `curl_block` pendente.
5. `scrapers/ebay.py::_fetch_ebay_html` — `detect_listing_gone(status_code=response.status_code,
   url=url)` prima di `response.raise_for_status()`. L'`except (httpx.HTTPError, ValueError)` di
   `scrape` (`ebay.py:76`) non la cattura: sale allo scheduler senza altre modifiche.
6. `scrapers/nove25.py::_fetch_nove25_html` — idem; l'`except httpx.HTTPError` di `scrape`
   (`nove25.py:70`) non la cattura.
7. `scrapers/aliexpress.py::_fetch_aliexpress_html` — idem, prima di `raise_for_status`; i due
   `except` di `scrape` (`aliexpress.py:77-88`, `HTTPStatusError` e `HTTPError`) non la catturano.
   Un 403 continua a passare da `raise_for_status` → `HTTPStatusError` → `detect_block_event`
   (comportamento #55 invariato).

Scheduler (`core/scheduler.py`), funzione nuova a livello modulo:

```python
def _failure_reason(exc: BaseException) -> tuple[str, str]:
    """Map a failed check to ``(reason, detail)`` — the closed list in the plan."""
    if isinstance(exc, ListingGone):
        return "listing_gone", f"HTTP {exc.status}"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in LISTING_GONE_STATUSES:
        return "listing_gone", f"HTTP {exc.response.status_code}"
    if isinstance(exc, ParseError):
        return "parse_error", str(exc)
    if isinstance(exc, httpx.HTTPError | ValueError | KeyError):
        return "http_error", str(exc)
    return "unexpected", str(exc)
```

Le except-ladder di `_scrape_one` (push) e di `check_products_for_user` (pull, §5.4) aggiungono un
ramo `except ListingGone as e:` PRIMA di `except ParseError` (metrica `status="error"`, poi
`_record_failure_and_maybe_disable(..., reason=..., detail=...)` con la coppia da
`_failure_reason(e)`) e nel ramo `except (httpx.HTTPError, ValueError, KeyError)` sostituiscono la
reason letterale con `_failure_reason(e)`. Il ramo `BlockEvent` resta com'è (il blocco ha
precedenza per costruzione: viene sollevato prima).

### 5.2 Persistenza (realizza D2, D6, D7, D9)

Migrazione `src/price_tracker/db/migrations/014_add_suspension_provenance.sql`:

```sql
-- Consecutive 404/410 answers on the product page. Reset by any other
-- outcome (a success or a different failure). Drives the early suspension
-- of listings that were removed from the catalog.
ALTER TABLE products ADD COLUMN gone_streak INTEGER NOT NULL DEFAULT 0;
-- Who paused the product: the user ('manual') or the scheduler ('automatic').
-- Written by the code path that pauses, never inferred from the counters:
-- a manual pause after a run of failures looks identical to an automatic
-- one otherwise. NULL on rows paused before this migration: origin unknown,
-- treated as manual by every bulk action.
ALTER TABLE products ADD COLUMN suspension_kind TEXT CHECK (suspension_kind IN ('manual', 'automatic'));
ALTER TABLE products ADD COLUMN suspension_reason TEXT;
```

Migrazione `src/price_tracker/db/migrations/015_digest_queue_nullable_product.sql` (rebuild:
SQLite non modifica una FOREIGN KEY in place; testo verificato live, P18):

```sql
-- An aggregated operational notice belongs to a user and a domain, not to
-- one product: with product_id NOT NULL + ON DELETE CASCADE, deleting the
-- product a queued notice happened to reference dropped the notice for every
-- other product in the group. Rebuild with a nullable reference that is
-- cleared, not cascaded.
CREATE TABLE digest_queue_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    product_id INTEGER,
    alert_payload_json TEXT NOT NULL,
    enqueued_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    flushed_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL
);
INSERT INTO digest_queue_v2 (id, user_id, product_id, alert_payload_json, enqueued_at, flushed_at)
    SELECT id, user_id, product_id, alert_payload_json, enqueued_at, flushed_at FROM digest_queue;
DROP INDEX IF EXISTS idx_digest_pending;
DROP TABLE digest_queue;
ALTER TABLE digest_queue_v2 RENAME TO digest_queue;
CREATE INDEX IF NOT EXISTS idx_digest_pending
    ON digest_queue(user_id, flushed_at) WHERE flushed_at IS NULL;
```

`db/models.py`:

- `ProductRecord` += in coda: `last_error: str | None = None`, `last_error_at: str | None = None`,
  `gone_streak: int = 0`, `suspension_kind: str | None = None`, `suspension_reason: str | None = None`.
- `DigestEntry.product_id: int | None`.

`db/repository.py`:

- `_PRODUCT_COLS` += `", last_error, last_error_at, gone_streak, suspension_kind, suspension_reason"`
  in coda; `_row_to_product` mappa `last_error=row[26]`, `last_error_at=row[27]`,
  `gone_streak=int(row[28] or 0)`, `suspension_kind=row[29]`, `suspension_reason=row[30]`.
- Nuovo metodo:

```python
async def record_failure(
    self, product_id: int, *, reason: str, detail: str | None = None
) -> ProductRecord | None:
    """Count one failed check in a single statement and return the fresh row.

    ``gone_streak`` grows only on ``listing_gone`` and resets on any other
    reason; ``last_error`` keeps the ``"{reason}: {detail}"`` shape /errori shows.
    """
    text = f"{reason}: {detail}" if detail else reason
    await self._conn.execute(
        "UPDATE products SET consecutive_errors = consecutive_errors + 1, "
        "gone_streak = CASE WHEN ? = 'listing_gone' THEN gone_streak + 1 ELSE 0 END, "
        "last_error = ?, last_error_at = datetime('now') WHERE id = ?",
        (reason, text[:300], product_id),
    )
    await self._conn.commit()
    return await self.get_product(product_id)
```

- Nuovo metodo `suspend_product(self, product_id: int, *, reason: str) -> bool`:
  `UPDATE products SET is_active = 0, suspension_kind = 'automatic', suspension_reason = ?,
  updated_at = datetime('now') WHERE id = ? AND is_active = 1`, e ritorna
  `cursor.rowcount == 1`.
  La clausola `AND is_active = 1` è un compare-and-swap ed è la parte che conta (ND1): fra
  `record_failure` e questa UPDATE l'utente può eseguire `/pausa`, e senza la guardia lo
  scheduler sovrascriverebbe `manual` con `automatic` — il prodotto rientrerebbe nel gruppo
  che «Elimina tutti» cancella, che è esattamente il difetto che la provenienza persistita
  esiste per chiudere. Perdere la corsa è l'esito corretto: chi ha messo in pausa a mano
  vince, e lo scheduler non tocca la riga.
- `pause_product`: `UPDATE products SET is_active = 0, suspension_kind = 'manual',
  suspension_reason = NULL, updated_at = datetime('now') WHERE id = ?`. L'alias
  `deactivate_product` resta e continua a chiamarlo (i handler `/pausa` e `pause_<id>` diventano
  «manual» senza toccarli).
- `reactivate_product`: `SET is_active = 1, consecutive_errors = 0, gone_streak = 0,
  suspension_kind = NULL, suspension_reason = NULL`.
- `reset_errors`: `SET consecutive_errors = 0, gone_streak = 0`.
- Nuovo metodo `list_auto_suspended_products(self, *, user_id: int) -> list[ProductRecord]`:
  `WHERE user_id = ? AND is_active = 0 AND suspension_kind = 'automatic' ORDER BY id ASC`.
  Nessun parametro di soglia: la provenienza è persistita (D7, F1).
- `enqueue_digest(self, *, user_id: int, product_id: int | None, payload: str) -> int` (firma
  allargata, corpo invariato); `list_pending_digest` mappa `product_id=r[2]` che ora può essere
  `None`.
- `increment_errors` e `set_last_error` restano (li usano i test esistenti) ma lo scheduler non li
  chiama più.

Regola di sospensione in `_record_failure_and_maybe_disable` (T4, testo completo in §5.4):

```
suspended = updated.consecutive_errors >= max_consecutive_errors
            or updated.gone_streak >= listing_gone_confirmations
→ await repo.suspend_product(product.id, reason=reason)
```

### 5.3 Copy e rendering (realizza D4, D8)

`core/alert.py` — mappa chiusa reason → (title, headline, hint, delete_first). msgid inglesi senza
accenti; il rendering chiama `_()` su ciascuno al momento del render (non a import-time).

| reason | title | headline | hint | delete_first |
|---|---|---|---|---|
| `listing_gone` | `Listings removed on {domain}` | `These pages answer HTTP 404/410: the store took them off the catalog.` | `Deleting keeps your list clean. Reactivate only if the store restores them.` | sì |
| `parse_error`, `price_none`, `no_scraper`, `condition_mismatch`, `implausible_read` | `Price unreadable on {domain}` | `The pages load, but the price could not be read anymore (layout change or a different offer).` | `Reactivate to run a fresh check right now.` | no |
| `http_error`, `unexpected` | `Site unreachable: {domain}` | `The site did not answer {max} checks in a row.` | `Try again later. Reactivate once the site is back.` | no |
| `block` | `Blocked by {domain}` | `The site is refusing automated checks (anti-bot).` | `Domain quarantine already paces retries. Reactivate once it clears.` | no |
| qualunque altro / `None` | `Tracking suspended on {domain}` | `Checks kept failing.` | `Reactivate to retry.` | no |

Funzione di riga per prodotto, `_why(reason, detail)` → stringa umana breve (msgid): `listing_gone`
→ `page not found (HTTP {status})` con lo status preso da `detail` se presente (altrimenti 404);
`block` → `blocked`; gruppo «unreadable» → `price not readable`; gruppo «unreachable» → `site
unreachable`; altro → `check failed`.

Budget di campo applicati dal renderer PRIMA dell'escape, con `truncate_visible` di §5.8: `name`
≤ 60, `domain` ≤ 60, `last_error` ≤ 120, `why` ≤ 40, prezzo formattato ≤ 24. Ogni riga emessa ha i
tag HTML bilanciati al suo interno (invariante richiesta da `split_message`, §5.8).

Formato del messaggio di sospensione (HTML Telegram; `{n}` = prodotti nel gruppo; al massimo
**10** righe prodotto — `MAX_LISTED_PRODUCTS` — poi `… and {k} more`; ogni valore utente passa da
`_escape_html`):

```
⚠️ <b>{title}</b> ({n})

{headline}

• <b>{name}</b> — {why}
  {last_line}
  Error: <code>{last_error or "unknown"}</code>
[… and {k} more]

{hint}
```

dove `last_line` è `Last good read: {price} {sym} on {date}` se `current_price` e `last_checked_at`
esistono (data `YYYY-MM-DD HH:MM UTC` da `last_checked_at`, che è naive UTC — riusa
`_parse_db_timestamp` dello scheduler spostata in `core/alert.py` o duplicata localmente),
altrimenti `No successful read yet`. `name` è `product.name or product.url`.

Formato del pre-avviso:

```
⏳ <b>Checks failing on {domain}</b> ({n})

{n} products failed {count}/{max} checks in a row. If it keeps failing they will be suspended automatically.

• <b>{name}</b> — {why}
[… and {k} more]

Details with /errori.
```

Con `n == 1` il testo resta valido (nessuna forma plurale da gestire: le stringhe sono scritte per
reggere entrambi i casi).

Bottoni (msgid), ordine deciso da `delete_first`:

- `▶️ Reactivate and recheck ({n})` → `ops_react_{anchor}`
- `🗑 Delete all ({n})` → `ops_del_{anchor}`

Conferma eliminazione (handler): `🗑 Yes, delete {n}` → `ops_delok_{anchor}`, `❌ Cancel` →
`cancel_delete`.

Traduzioni `it_IT` (msgstr da inserire; il resto del `.po` non si tocca a mano):

| msgid | msgstr |
|---|---|
| `Listings removed on {domain}` | `Prodotti rimossi da {domain}` |
| `These pages answer HTTP 404/410: the store took them off the catalog.` | `Le pagine rispondono HTTP 404/410: lo store li ha tolti dal catalogo.` |
| `Deleting keeps your list clean. Reactivate only if the store restores them.` | `Eliminarli tiene pulita la lista. Riattivali solo se lo store li rimette in vendita.` |
| `Price unreadable on {domain}` | `Prezzo illeggibile su {domain}` |
| `The pages load, but the price could not be read anymore (layout change or a different offer).` | `Le pagine si aprono, ma il prezzo non si legge più (layout cambiato o offerta diversa).` |
| `Reactivate to run a fresh check right now.` | `Riattiva per fare subito un nuovo controllo.` |
| `Site unreachable: {domain}` | `Sito irraggiungibile: {domain}` |
| `The site did not answer {max} checks in a row.` | `Il sito non ha risposto a {max} controlli di fila.` |
| `Try again later. Reactivate once the site is back.` | `Riprova più tardi. Riattiva quando il sito torna raggiungibile.` |
| `Blocked by {domain}` | `Bloccato da {domain}` |
| `The site is refusing automated checks (anti-bot).` | `Il sito rifiuta i controlli automatici (anti-bot).` |
| `Domain quarantine already paces retries. Reactivate once it clears.` | `La quarantena del dominio gestisce già i tentativi. Riattiva quando si sblocca.` |
| `Tracking suspended on {domain}` | `Tracking sospeso su {domain}` |
| `Checks kept failing.` | `I controlli continuano a fallire.` |
| `Reactivate to retry.` | `Riattiva per riprovare.` |
| `page not found (HTTP {status})` | `pagina non trovata (HTTP {status})` |
| `blocked` | `bloccato` |
| `price not readable` | `prezzo non leggibile` |
| `site unreachable` | `sito irraggiungibile` |
| `check failed` | `controllo fallito` |
| `Last good read: {price} {sym} on {date}` | `Ultima lettura buona: {price} {sym} il {date}` |
| `No successful read yet` | `Nessuna lettura riuscita finora` |
| `Error: <code>{error}</code>` | `Errore: <code>{error}</code>` |
| `unknown` | `sconosciuto` |
| `… and {k} more` | `… e altri {k}` |
| `Checks failing on {domain}` | `Controlli in errore su {domain}` |
| `{n} products failed {count}/{max} checks in a row. If it keeps failing they will be suspended automatically.` | `{n} prodotti hanno fallito {count}/{max} controlli di fila. Se continua, verranno sospesi automaticamente.` |
| `Details with /errori.` | `Dettagli con /errori.` |
| `▶️ Reactivate and recheck ({n})` | `▶️ Riattiva e ricontrolla ({n})` |
| `🗑 Delete all ({n})` | `🗑 Elimina tutti ({n})` |
| `🗑 Yes, delete {n}` | `🗑 Sì, elimina {n}` |
| `❌ Cancel` | `❌ Annulla` |
| `❌ Nothing to do: no automatically suspended products on this site.` | `❌ Niente da fare: nessun prodotto sospeso automaticamente su questo sito.` |
| `⏳ Reactivating {n} products and checking them...` | `⏳ Riattivo {n} prodotti e li ricontrollo...` |
| `▶️ <b>Rechecked {n} products on {domain}</b>` | `▶️ <b>Ricontrollati {n} prodotti su {domain}</b>` |
| `✅ {name} — {price}` | `✅ {name} — {price}` |
| `❌ {name} — {why}` | `❌ {name} — {why}` |
| `🗑 Delete {n} products on {domain} and their price history? This cannot be undone.` | `🗑 Eliminare {n} prodotti su {domain} e il loro storico prezzi? Non si può annullare.` |
| `🗑 <b>Deleted {n} products on {domain}.</b>` | `🗑 <b>Eliminati {n} prodotti su {domain}.</b>` |
| `⚠️ Operational notices` | `⚠️ Avvisi operativi` |
| `Use /reactivate or /errori for details.` | `Usa /reactivate o /errori per i dettagli.` |
| `{domain} — {n} products: tracking suspended ({why})` | `{domain} — {n} prodotti: tracking sospeso ({why})` |
| `{domain} — {n} products: checks failing ({count}/{max})` | `{domain} — {n} prodotti: controlli in errore ({count}/{max})` |
| `{domain} — quarantined` | `{domain} — in quarantena` |
| `Operational notice` | `Avviso operativo` |

Nel catalogo `en` ogni msgid nuovo va con `msgstr` uguale al msgid (convenzione del file
esistente, es. `locale/en/LC_MESSAGES/messages.po:290-291`).

### 5.4 Accumulatore ed emissione (realizza D3, D5, D10)

Nuovo modulo `src/price_tracker/core/notices.py`:

```python
OPS_REACTIVATE_PREFIX = "ops_react_"
OPS_DELETE_PREFIX = "ops_del_"
OPS_DELETE_CONFIRM_PREFIX = "ops_delok_"
MAX_LISTED_PRODUCTS = 10

EventKind = Literal["suspended", "warning"]


def group_key_for(url: str) -> str:
    """eTLD+1, else the lowercase netloc, else ``"unknown"`` (never raises)."""


@dataclass(frozen=True)
class OperationalEvent:
    event: EventKind
    user_id: int
    product_id: int
    product_name: str
    url: str
    group_key: str
    reason: str  # token from the closed list, or anything else = unknown
    detail: str | None
    last_error: str | None
    error_count: int
    max_errors: int
    last_price: Decimal | None
    currency: str | None
    last_checked_at: str | None


@dataclass(frozen=True)
class NoticeGroup:
    event: EventKind
    user_id: int
    group_key: str
    events: tuple[OperationalEvent, ...]  # sorted by product_id

    @property
    def anchor_product_id(self) -> int: ...  # events[0].product_id — callback_data only
    @property
    def primary_reason(self) -> str: ...  # D4 rule


class NoticeCollector:
    def add(self, event: OperationalEvent) -> None: ...  # dedupe key (event, product_id), last wins
    def groups(self) -> list[NoticeGroup]: ...  # sorted by (user_id, event, group_key)
    def __len__(self) -> int: ...
```

`Scheduler` (`core/scheduler.py`):

- `SchedulerDeps` += `listing_gone_confirmations: int = 3` e `lang: str | None = None`.
- `_scrape_one(product, *, collector)`, `_run_tick(products, *, half_open_seen=None, collector)`,
  `_check_product_core(product_id, *, scraper_name, domain, collector)`,
  `_record_failure_and_maybe_disable(product, *, scraper_name, domain, reason, detail=None,
  collector)`: il collector è un parametro esplicito, mai attributo d'istanza (due sweep concorrenti
  — tick e `/checkall` — non devono condividerlo).
- `_record_failure_and_maybe_disable`, corpo normativo:

```python
updated = await self.deps.repo.record_failure(product.id, reason=reason, detail=detail)
if product.pending_read_count or product.pending_read_streak:
    await self.deps.repo.clear_pending_read(product.id)
if updated is None:
    return False
max_errors = self.deps.max_consecutive_errors
half = max_errors // 2
suspended = (
    updated.consecutive_errors >= max_errors
    or updated.gone_streak >= self.deps.listing_gone_confirmations
)
if not suspended:
    if 1 <= half < max_errors and updated.consecutive_errors == half:
        collector.add(self._event("warning", updated, reason=reason, detail=detail))
    return False
await self.deps.repo.suspend_product(product.id, reason=reason)
logger.warning(...)  # come oggi, più gone_streak
collector.add(self._event("suspended", updated, reason=reason, detail=detail))
return True
```

  dove `_event(kind, record, *, reason, detail)` costruisce l'`OperationalEvent` da `ProductRecord`
  (`group_key_for(record.url)`, `last_price=record.current_price`, `currency=record.currency`,
  `last_checked_at=record.last_checked_at`, `last_error=record.last_error`,
  `error_count=record.consecutive_errors`, `max_errors=max_errors`).

- `_flush_notices(collector)`: per ogni gruppo, rende testo e bottoni
  (`format_operational_notice(group)`, `operational_buttons(group)` — i bottoni solo per
  `suspended`), chiama `self._notify(group.user_id, text, product_id=None,
  payload=_operational_payload(group, sweep_started_at))`; se il ritorno è `False` logga a WARNING
  `user_id`, `group_key`, `product_ids`. La locale viene impostata UNA volta all'inizio
  (`token = set_locale(self.deps.lang)`, che deve ritornare il token della ContextVar) e
  ripristinata nel `finally` (`reset_locale(token)`, nuova in `bot/messages.py`). Le eccezioni del
  rendering o dell'invio di UN gruppo non fermano gli altri (`try/except Exception` con
  `logger.exception`, marcato `# noqa: BLE001`). `_flush_notices` **non solleva mai** al chiamante.
- Garanzia di flush: ogni entrypoint che possiede un `NoticeCollector` DEVE eseguire il proprio
  lavoro in `try` e chiamare `_flush_notices(collector)` in `finally`, esattamente una volta. Vale
  per `run_check_all` dopo il tick di ciascun utente, `run_check_for_user`,
  `check_products_for_user` e qualunque wrapper che possieda direttamente un collector. Se il
  lavoro solleva, il flush viene tentato e poi l'eccezione originale continua a risalire. In caso di
  `asyncio.CancelledError`, il flush viene eseguito in un task protetto con `asyncio.shield`; una
  seconda cancellazione non deve annullare il task di flush. `_flush_notices` isola e logga ogni
  errore per gruppo e non solleva al chiamante; il reset della locale vive nel suo `finally`.
- Payload operativo (contratto chiuso; serializzabile in JSON perché finisce in `digest_queue`):

```python
{
    "kind": "operational",
    "event": group.event,  # "suspended" | "warning"
    "event_id": f"ops:{group.event}:{group.user_id}:{group.group_key}:{sweep_started_at.isoformat()}",
    "user_id": group.user_id,
    "domain": group.group_key,
    "product_ids": [e.product_id for e in group.events],
    "products": [{"id": e.product_id, "name": name60, "why": why40} for e in group.events],
    "reason": group.primary_reason,
    "count": len(group.events),
    "error_count": group.events[0].error_count,
    "max_errors": group.events[0].max_errors,
    "buttons": [...],  # solo per "suspended"
}
```

  Nessuna chiave `product_id`: la notifica non appartiene a un prodotto (D3, D6). `name60` e
  `why40` sono già troncati coi budget di §5.3.

- `_notify_quarantine_entry`: usa `self._notify(product.user_id, message, product_id=None,
  payload={"kind": "operational", "event": "quarantine", "domain": domain,
  "products": [{"id": product.id, "name": name60, "why": "blocked"}], "count": 1,
  "event_id": f"ops:quarantine:{product.user_id}:{domain}:{locked_until_iso or 'none'}"})`.
- `check_products_for_user(self, *, product_ids: list[int], user_id: int,
  delay_between_products: float | None = None) -> list[CheckResult]` è l'UNICO loop pull-mode:
  carica ogni prodotto con `get_product_for_user(pid, user_id)`, salta id inesistenti o estranei,
  applica quarantena e pacing, contiene l'intera except-ladder (`BlockEvent`, `ListingGone`,
  `ParseError`, `httpx.HTTPError`/`ValueError`/`KeyError`, `Exception`), valorizza
  `CheckResult.reason`, possiede il `NoticeCollector` e lo fluscia con la garanzia `try/finally`
  definita sopra.
- `check_user_products_for_user` carica gli id dei prodotti attivi dell'utente e delega
  interamente a `check_products_for_user`.
- `check_one_product_for_user` delega a `check_products_for_user(product_ids=[product_id],
  user_id=user_id, delay_between_products=0)`, restituisce il primo risultato oppure
  `CheckResult(product_id=product_id, user_id=user_id)` se l'id è inesistente o estraneo. Non
  chiama direttamente `_check_product_core` e non possiede un secondo collector.
- `CheckResult` += `reason: str | None = None` (valorizzato nella ladder con la reason del
  fallimento; `None` su successo o esito senza fallimento). Nessun handler esistente lo legge.
- I ribassi e i restock passano `payload={..., "kind": "price"}` (`_alert_payload` e il payload
  del restock): la chiave è obbligatoria in entrambi i sensi così il notifier non deve dedurre.

### 5.5 Notifier (realizza D6, D7, D11)

`notifier/preferences.py`: nuovo `PreferencesManager.resolve_global(self, *, user_id: int) ->
EffectivePrefs` — stessa coalescenza di `resolve` con `per_product = None` (estrarre il corpo in
una funzione privata `_coalesce_rows(per_product, per_user)` usata da entrambi).

`notifier/telegram.py`:

- `__call__`: `kind = (payload or {}).get("kind")`; instrada a `notify_alert` se
  `self._prefs is not None and (product_id is not None or kind == "operational")`; altrimenti
  `_send_direct(user_id, text, reply_markup=_markup(payload))`. `notify_alert` accetta
  `product_id: int | None`.
- `_markup(payload) -> InlineKeyboardMarkup | None`: legge `payload.get("buttons")`; accetta solo
  `list[list[dict]]` con `text: str` e `callback_data: str` di lunghezza ≤ 64 byte UTF-8; qualunque
  difformità → `logger.warning` e `None` (messaggio senza tastiera). Mai eccezione.
- `_send_direct(self, user_id, text, *, reply_markup=None) -> bool` e
  `send_alert(self, *, chat_id, text, reply_markup=None) -> None`: `chunks = split_message(text)`
  (§5.8); un `send_message` per chunk, `reply_markup` SOLO sull'ultimo; in `_send_direct` un
  fallimento su un chunk logga l'indice e ritorna `False` (i chunk precedenti sono già consegnati:
  dichiarato nel log); `send_alert` lascia salire l'eccezione come oggi. La metrica
  `notification_sent_total` conta UNA volta per messaggio logico, non per chunk.
- `notify_alert`, ordine dei gate rispetto a oggi:

```
operational = alert.get("kind") == "operational"
1. dedupe event_id                              (invariato)
2. resolve prefs: operational → prefs.resolve_global(user_id)
                  else       → prefs.resolve(user_id, product_id)   (product_id obbligatorio qui)
3. mute        → if operational: SKIP the gate; else drop (invariato)
4. quiet hours → if digest_mode or operational: enqueue + digest_pending; else drop
5. throttle    → exceeded: if digest_mode or operational: enqueue; else drop
                 not exceeded: record window (invariato; anche per operational)
6. digest_mode → enqueue (invariato)
7. send_alert(chat_id, text, reply_markup=_markup(alert))
```

  Ogni `enqueue` passa `product_id=product_id` (che per gli operativi è `None`).

`is_muted_now` e `is_quiet_now` non cambiano.

### 5.6 Digest (realizza D6, D11)

`notifier/digest.py`:

- `enqueue(self, *, user_id: int, product_id: int | None, payload)`.
- `_format_digest_message(entries)` viene sostituita da `_digest_blocks(entries) ->
  tuple[str, list[tuple[int, str]], str, list[int]]` = (header, [(entry_id, righe dell'entry)],
  footer, unrenderable_ids):
  partiziona per `payload.get("kind") == "operational"`. L'header conta SOLO i price change. Le
  righe prezzo restano come oggi (nome di fallback `Product #{id}` se `product_id` non è `None`,
  altrimenti `Operational notice`). Sezione `⚠️ Operational notices` (msgid) dopo le righe prezzo,
  una riga per entry: `suspended` → `{domain} — {n} products: tracking suspended ({why})` con
  `why` = `_why(reason, None)` di §5.3; `warning` → `{domain} — {n} products: checks failing
  ({count}/{max})`; `quarantine` → `{domain} — quarantined`. Payload operativo senza `domain`/
  `count` → `unknown`/`1`, mai eccezione. Footer: `Use /reactivate or /errori for details.` se ci
  sono operativi, poi la riga `Use /lista for full state.` esistente. Ogni riga rispetta i budget di §5.3 (nome/dominio ≤ 60).
- **Entry con payload non valido (ND2)**: `_digest_blocks` le salta, ma `flush_user` marca solo
  gli id presenti nelle pagine — una riga il cui payload non è JSON valido non entrerebbe in
  nessuna pagina e resterebbe in coda **per sempre**, ripetendo il warning a ogni flush e facendo
  crescere la tabella. `_digest_blocks` restituisce quindi un terzo elemento, `unrenderable_ids:
  list[int]`: le entry che non si sono potute rendere. Non vengono messe in una pagina di ripiego
  (il loro contenuto è per definizione illeggibile) ma **quarantinate**: `flush_user` le marca
  flusciate insieme alla prima pagina inviata, e logga una riga WARNING per ciascuna con
  `entry_id` e `user_id`. Se non c'è nessuna pagina da inviare — coda composta solo da entry
  illeggibili — le marca comunque e ritorna il loro numero. Motivo della scelta: un payload che
  non è JSON valido non lo diventerà al prossimo giro; tenerlo in coda non è prudenza, è una
  perdita permanente travestita da attesa.
- `flush_user`: `header, blocks, footer, unrenderable = _digest_blocks(entries)`;
  `pages = paginate(header, blocks, footer)` (§5.8); per ogni pagina `send_message(page_text)` poi
  SUBITO `mark_digest_flushed(ids_of_that_page + unrenderable_ids_una_volta_sola)`; se l'invio di
  una pagina solleva, le pagine precedenti restano marcate, quella e le successive no, l'eccezione
  risale (il job `digest_flush_job` la logga). Se `pages` è vuoto ma `unrenderable` no, le marca
  senza inviare nulla. Ritorna il numero di entry effettivamente marcate, quarantinate incluse.
  Metrica `notification_sent_total{type="digest"}` una volta per pagina inviata.
- `flush_due`: dopo `prefs = await self._repo.get_notification_prefs(...)` (riga globale, la
  stessa che `resolve_global` usa in enqueue: D6), se `prefs` non è `None` e
  `_row_is_quiet(prefs, now)` → `continue` (non flusciato, resta in coda). `_row_is_quiet`
  costruisce un `EffectivePrefs` con `quiet_hours_start/end` e `timezone` della riga e chiama
  `is_quiet_now`. Utente senza riga prefs → mai in quiet hours (default).
- Docstring del modulo: la riga «2. Quiet-hours-end — flush at the moment a user's quiet window
  ends» diventa «2. Quiet hours — users inside their quiet window are skipped; their entries flush
  at the first tick after the window ends».

### 5.7 Callback `ops_*` (realizza D7, D11)

Nuovo modulo `src/price_tracker/bot/handlers/callbacks/_ops.py` con
`async def handle_ops_buttons(query, context, db, user_id, data) -> bool`, registrato nel
dispatcher `callbacks/__init__.py::handle_callback` come PRIMA voce della catena. Firma e stile
uguali a `_actions.py`. Solo `db.<metodo>` esistenti in `Repository` (contratto
`tests/integration/test_repository_handler_contract.py`): `get_product`, `get_product_for_user`,
`is_user_admin` (tramite `_get_user_product`), `list_auto_suspended_products`, `reactivate_product`,
`delete_product`.

Flusso comune: `_parse_id` sull'id dopo il prefisso → `❌ ID non valido.` (stringa esistente, via
`_()`); `_get_user_product(context, anchor_id, user_id)` → `❌ Product not found.` (esistente);
`group_key = group_key_for(anchor.url)`;
`group = [p for p in await db.list_auto_suspended_products(user_id=user_id)
if group_key_for(p.url) == group_key]`; gruppo vuoto → `❌ Nothing to do: no automatically
suspended products on this site.`. Nessuna soglia di configurazione entra nel calcolo (F1).

Tutte le risposte lunghe passano da `_reply_chunked(query, text)` (helper del modulo):
`chunks = split_message(text)`; `edit_message_text(chunks[0], parse_mode=HTML)`; per ogni chunk
successivo `query.message.reply_text(chunk, parse_mode=HTML)`.

- `ops_react_<id>`: `edit_message_text(_("⏳ Reactivating {n} products and checking them..."))`;
  per ogni `p` in `group`: `await db.reactivate_product(p.id)`; poi
  `results = await scheduler.check_products_for_user(product_ids=[p.id …], user_id=user_id,
  delay_between_products=0.5)`; per ogni risultato ricarica il prodotto e scrive
  `✅ {name} — {price}` se `result.reason is None` e `current_price` esiste, altrimenti
  `❌ {name} — {why}` con `why` = `_why(result.reason, None)`; intestazione
  `▶️ <b>Rechecked {n} products on {domain}</b>`; nomi troncati a 60. Il testo finale sostituisce
  il placeholder via `_reply_chunked`.
- `ops_del_<id>`: `edit_message_text` con `🗑 Delete {n} products on {domain} and their price
  history? This cannot be undone.` e tastiera `[🗑 Yes, delete {n} → ops_delok_<id>]
  [❌ Cancel → cancel_delete]`.
- `ops_delok_<id>`: ricalcola il gruppo (lo stato può essere cambiato tra prompt e conferma), per
  ogni `p`: `await db.delete_product(p.id, user_id=user_id)`; risposta
  `🗑 <b>Deleted {n} products on {domain}.</b>` con `n` = eliminati davvero.

Nota sull'admin: `_get_user_product` lascia passare l'admin su prodotti altrui; il gruppo però è
sempre calcolato su `user_id` del clicker, quindi un admin che clicca la notifica di un altro
utente ottiene «niente da fare», non un'azione sui prodotti altrui. È voluto.

### 5.8 Contratto di segmentazione (realizza D11)

Nuovo modulo puro `src/price_tracker/core/textlimits.py`:

```python
TELEGRAM_MESSAGE_LIMIT = 4096  # characters after entity parsing, sendMessage and editMessageText
SAFE_LIMIT = 4000  # headroom for the way Telegram counts, never send above this
NAME_BUDGET = 60
DOMAIN_BUDGET = 60
ERROR_BUDGET = 120
WHY_BUDGET = 40
_TAG_RE = re.compile(r"<[^>]+>")


def visible_length(html_text: str) -> int:
    """Length as Telegram counts it: tags stripped, entities unescaped."""
    return len(html.unescape(_TAG_RE.sub("", html_text)))


def truncate_visible(text: str, budget: int) -> str:
    """Plain-text truncation to ``budget`` characters, ellipsis included. Apply BEFORE escaping."""


def split_message(text: str, *, limit: int = SAFE_LIMIT) -> list[str]:
    """Split on line boundaries so every chunk has ``visible_length <= limit``.

    Every line of ``text`` MUST carry balanced HTML tags (renderer invariant).
    A single line longer than ``limit`` is degraded: tags stripped, entities
    unescaped, cut to ``limit - 1`` characters plus an ellipsis, re-escaped —
    valid HTML, never a broken tag. Never returns an empty list: ``[""]`` for
    empty input is NOT allowed either — empty input returns ``[]`` and callers
    must not send.
    """


def paginate(
    header: str, blocks: list[tuple[K, str]], footer: str, *, limit: int = SAFE_LIMIT
) -> list[tuple[str, list[K]]]:
    """Group ``blocks`` into pages ``header + blocks… + footer`` under ``limit``.

    A block is never split across pages; a block that alone exceeds the room
    is degraded with the same rule as ``split_message``. Returns [] for no
    blocks. Every key appears in exactly one page.
    """
```

Regole d'uso (lista chiusa dei chiamanti):

1. `core/alert.py::format_operational_notice` e `format_warning_notice` applicano i budget e
   producono righe a tag bilanciati; `MAX_LISTED_PRODUCTS = 10` è un cap di leggibilità, non la
   garanzia.
2. `TelegramNotifier._send_direct` e `send_alert` chiamano `split_message(text)` e inviano un
   messaggio per chunk; la tastiera va sull'ULTIMO chunk.
3. `DigestService.flush_user` usa `paginate(header, blocks, footer)` e marca flusciate solo le
   entry delle pagine inviate.
4. `_ops.py::_reply_chunked` usa `split_message` (primo chunk in `edit_message_text`, gli altri in
   `reply_text`).
5. I ribassi, i restock e la quarantena passano dal punto 2 senza altre modifiche (sono corti, ma
   la garanzia è uniforme).

Proprietà da provare (T3): per ogni input, ogni chunk/pagina ha `visible_length <= limit`; i
chunk concatenati con `\n` contengono ogni riga dell'input (o la sua versione degradata); per ogni
tag `<b>`, `<i>`, `<code>`, `<a …>` in un chunk c'è il corrispettivo di chiusura nello stesso
chunk; `paginate` assegna ogni chiave a esattamente una pagina e preserva l'ordine.

---

## 6. Task in ordine di esecuzione

Ordinati per rischio di regressione (prima ciò che tocca costruttori e fixture condivise), poi per
dipendenza. Ogni task: (a) misura la baseline, (b) scrive i test e li vede fallire, (c) implementa,
(d) fa passare i cinque gate, (e) committa. Comandi dei gate — eseguibili dalla radice del repo:

```
G1  .venv/bin/ruff check .
G2  .venv/bin/ruff format --check .
G3  .venv/bin/mypy --strict
G4  .venv/bin/pytest -q            # passed >= baseline T0, coverage >= 91% (CI: --cov-fail-under=90)
G5  bash scripts/audit_english.sh
```

Commit: conventional, in inglese, **senza trailer di co-autore** e senza riferimenti a strumenti o
processo. Un commit per task.

### T0 — Branch e baseline

- [ ] Il branch `feature/operational-notifications` esiste già (contiene questo piano); verificare
      con `git branch --show-current` e `git log --oneline -3`, poi `git merge-base --is-ancestor
      main HEAD` deve uscire 0.
- [ ] Eseguire G1-G5; annotare in questa sezione il numero di `passed` e la coverage:
      `passed_baseline = ____`, `coverage_baseline = ____`. Da qui in poi G4 vale
      `passed >= passed_baseline` e `coverage >= 91%`.
- [ ] Verificare che `.venv/bin/pybabel --version` funzioni.

### T1 — Persistenza: migrazioni 014 e 015, `ProductRecord`, `DigestEntry`, `record_failure`, provenienza, `list_auto_suspended_products` (D2, D6, D7, D9)

Rischio: tocca `_PRODUCT_COLS`, `_row_to_product`, `ProductRecord`, `DigestEntry` e ricostruisce
`digest_queue`. Prima misura: `.venv/bin/pytest -q tests/unit/test_repository.py
tests/unit/test_migrator.py tests/unit/test_digest.py tests/integration/test_scheduler.py
tests/integration/test_migration_flow.py` e annota i conteggi.

- [ ] Test (fallisce): `tests/unit/test_migrator.py` —
      - `test_migration_014_adds_provenance_columns`: dopo tutte le migrazioni `PRAGMA
        table_info(products)` contiene `gone_streak` (`INTEGER NOT NULL DEFAULT 0`),
        `suspension_kind` (TEXT nullable), `suspension_reason` (TEXT nullable); riapplicare non
        solleva; `UPDATE products SET suspension_kind='weird'` solleva `IntegrityError`.
      - `test_migration_015_rebuilds_digest_queue_with_set_null`: applicare fino alla 014,
        inserire utente, due prodotti e una riga in coda per il primo; applicare la 015; `PRAGMA
        foreign_key_list(digest_queue)` mostra `SET NULL` su `product_id`; la riga esiste ancora con
        lo stesso `id`; `delete_product` del primo → riga presente con `product_id IS NULL`;
        `INSERT … product_id NULL` accettato; `idx_digest_pending` presente; l'`AUTOINCREMENT`
        continua dal massimo precedente.
- [ ] Test (fallisce): `tests/unit/test_repository.py`:
      - `test_get_product_exposes_last_error_gone_streak_and_provenance`: prodotto nuovo →
        `last_error is None`, `last_error_at is None`, `gone_streak == 0`, `suspension_kind is None`,
        `suspension_reason is None`.
      - `test_record_failure_increments_and_sets_last_error`: → `consecutive_errors == 1`,
        `last_error == "parse_error: x"`, `last_error_at` non None, `gone_streak == 0`.
      - `test_record_failure_gone_streak_grows_only_on_listing_gone`: gone, gone → 2; poi
        `http_error` → 0; poi gone → 1.
      - `test_record_failure_truncates_detail_to_300`; `test_record_failure_unknown_product_returns_none`.
      - `test_suspend_product_marks_automatic_with_reason`: `is_active is False`,
        `suspension_kind == "automatic"`, `suspension_reason == "listing_gone"`, contatori intatti.
      - `test_pause_product_marks_manual`: dopo 7 `record_failure`, `pause_product` →
        `suspension_kind == "manual"`, `suspension_reason is None`, `consecutive_errors == 7`.
      - `test_reactivate_clears_counters_and_provenance`: dopo `suspend_product` →
        `reactivate_product` → attivo, `consecutive_errors == 0`, `gone_streak == 0`,
        `suspension_kind is None`, `suspension_reason is None`; `reset_errors` azzera `gone_streak`.
      - `test_list_auto_suspended_products_uses_provenance_only` (F1): (a) `suspend_product` con
        `consecutive_errors == 3` → incluso; (b) 7 errori poi `pause_product` → escluso; (c) riga
        inattiva con `suspension_kind IS NULL` (scritta con `UPDATE products SET is_active=0`
        diretto, simula il pre-migrazione) e `consecutive_errors == 10` → escluso; (d) attivo →
        escluso; (e) auto-sospeso di un altro utente → escluso; ordine per id; il risultato NON
        cambia al variare di alcuna soglia (la funzione non ne riceve).
      - `test_suspend_product_is_a_compare_and_swap` (ND1): su prodotto attivo ritorna `True`,
        scrive `automatic` e la reason; su prodotto GIA' inattivo ritorna `False` e **non tocca
        nulla** — in particolare una riga messa in pausa a mano (`suspension_kind == 'manual'`)
        resta `manual` e non compare in `list_auto_suspended_products`. Riproduce l'interleaving:
        `record_failure` → `pause_product` → `suspend_product` → il prodotto NON è nel gruppo.
      - `test_enqueue_digest_accepts_null_product_id` e `test_list_pending_digest_maps_null_product`.
- [ ] Implementare §5.2 in `db/migrations/014_add_suspension_provenance.sql`,
      `db/migrations/015_digest_queue_nullable_product.sql`, `db/models.py`, `db/repository.py`.
      `DigestService.enqueue` prende `product_id: int | None` (solo la firma, T6 fa il resto).
- [ ] Verifica: G1-G5 verdi; `passed >= passed_baseline + 12`;
      `tests/integration/test_migration_flow.py` verde (upgrade su DB popolato).
- [ ] Commit: `feat(db): persist suspension provenance, count listing-gone answers, and let queued digests outlive a deleted product`.

### T2 — `ListingGone`: eccezione, rilevamento in cinque scraper, classificazione nello scheduler (D1, D2)

- [ ] Test (fallisce): `tests/unit/test_exceptions.py` — `ListingGone` è `ScrapeError`, non
      `BlockEvent`, non `httpx.HTTPError`; porta `status` e `url`; `LISTING_GONE_STATUSES == {404, 410}`.
- [ ] Test (fallisce): `tests/unit/test_scraper_base.py` — `detect_listing_gone` per proprietà:
      404 e 410 sollevano; 200, 301, 403, 429, 500, 503 no; `status_code=-1` e `0` no; chiamata
      dopo `detect_block_event` su 403 → esce `HTTPBlockStatus`, non `ListingGone` (precedenza).
- [ ] Test (fallisce): `tests/unit/scrapers/test_shopify.py` (respx):
      - JSON `.json` 404 + HTML 404 → `scrape` solleva `ListingGone(status=404)`.
      - JSON 200 con prezzo + HTML 404 → `scrape` ritorna il prezzo (enrichment non nega il prodotto).
      - JSON 404 + HTML 410 → `ListingGone(status=410)`.
      - HTML 403 → `BlockEvent` (comportamento attuale, resta).
- [ ] Test (fallisce): `tests/unit/scrapers/test_generic*.py` (respx, `_fetch_with_curl_cffi`
      monkeypatchato a `None`): httpx 404 → `ListingGone`; curl_cffi ritorna HTML valido lungo
      (> 5000 char) → nessuna chiamata httpx, nessuna eccezione; curl_cffi ritorna HTML corto
      (< 5000) e httpx 404 → nessuna eccezione, si usa l'HTML corto.
- [ ] Test (fallisce): `tests/unit/scrapers/test_ebay.py`, `test_nove25.py`, `test_aliexpress.py`
      (respx): 404 → `scrape` solleva `ListingGone(status=404)`; 500 → `ProductInfo(price=None)`
      come oggi; per AliExpress 403 → `BlockEvent` come oggi.
- [ ] Test (fallisce): `tests/integration/test_scheduler.py`:
      - `test_failure_reason_classification` (unit sulla funzione): `ListingGone` → `listing_gone`;
        `httpx.HTTPStatusError` 404/410 → `listing_gone`; 500 → `http_error`; `ParseError` →
        `parse_error`; `KeyError` → `http_error`; `RuntimeError` → `unexpected`.
      - `test_listing_gone_suspends_after_three_confirmations`: scraper stub che solleva
        `ListingGone` sempre, `max_consecutive_errors=10`, `listing_gone_confirmations=3`: dopo 2
        tick attivo con `gone_streak == 2`; dopo il 3º `is_active is False`, `last_error ==
        "listing_gone: HTTP 404"`, `consecutive_errors == 3`, `suspension_kind == "automatic"`,
        `suspension_reason == "listing_gone"`.
      - `test_listing_gone_streak_resets_on_other_failure`: gone, gone, ParseError, gone → attivo,
        `gone_streak == 1`, `consecutive_errors == 4`.
      - `test_listing_gone_streak_resets_on_success`.
      - `test_http_status_404_from_raising_scraper_counts_as_listing_gone`: stub che solleva
        `httpx.HTTPStatusError` con response 404 → `last_error` inizia con `listing_gone`.
      - `test_auto_suspension_writes_provenance` (soglia classica: `suspension_kind == "automatic"`,
        `suspension_reason == reason` dello stub).
      - `test_listing_gone_confirmations_env` in `tests/unit/test_config.py`: default 3, env
        `LISTING_GONE_CONFIRMATIONS=5` → 5.
- [ ] Implementare §5.1 (`core/exceptions.py`, `core/scraper_base.py`, i cinque scraper,
      `core/scheduler.py::_failure_reason` + rami except) e la regola di sospensione di §5.2
      dentro `_record_failure_and_maybe_disable` con `repo.suspend_product` al posto di
      `deactivate_product` (in T2 il collector NON esiste ancora: la funzione continua a chiamare il
      notifier come oggi, con `format_error_notification`; T4 la sostituisce). `Config` +=
      `listing_gone_confirmations` (env `LISTING_GONE_CONFIRMATIONS`, default 3); `SchedulerDeps`
      += `listing_gone_confirmations: int = 3`; `main._setup_scheduler` lo passa.
- [ ] Verifica: G1-G5; coverage `core/scheduler.py` non scende sotto il valore di T0 (leggere la
      riga in `--cov-report=term-missing`).
- [ ] Commit: `feat(core): recognise removed listings (HTTP 404/410) and suspend them after three confirmations`.

### T3 — `core/textlimits.py`, `core/notices.py` e rendering in `core/alert.py` (D3, D4, D8, D11)

Moduli puri, nessuna dipendenza da scheduler o telegram. Tutti i test sono unit.

- [ ] Test (fallisce): `tests/unit/test_textlimits.py` (proprietà, parametrizzati):
      - `visible_length("• <b>A &amp; B</b> — <code>x &lt; y</code>") == 15`.
      - `truncate_visible`: sotto budget invariato; sopra → `len == budget` con ellissi finale.
      - `split_message`: input di 300 righe da 200 caratteri visibili con `<b>…</b>` ciascuna →
        ogni chunk `visible_length <= 4000`, nessun tag aperto senza chiusura nello stesso chunk
        (conteggio `<b>`/`</b>`, `<code>`/`</code>`, `<a `/`</a>`), l'unione dei chunk contiene
        ogni riga; una riga singola da 6000 visibili con tag → un chunk ≤ 4000 senza `<`
        (degradata); input vuoto → `[]`; input sotto limite → `[text]`; righe vuote preservate;
        entità (`&amp;`) contate come un carattere.
      - `paginate`: 50 blocchi da 300 visibili + header 200 + footer 100 → ogni pagina ≤ 4000, ogni
        chiave in esattamente una pagina, ordine preservato, ogni pagina inizia con l'header e
        finisce col footer; un blocco da 5000 → pagina propria degradata; nessun blocco → `[]`.
- [ ] Test (fallisce): `tests/unit/test_notices.py`:
      - `group_key_for`: `https://a.example.com/p` → `example.com`; `https://shop.example/p` →
        `shop.example`; `""` → `unknown`; `"not a url"` → `unknown`; `http://localhost/x` →
        `localhost`; mai eccezione (8 input).
      - `NoticeCollector.add` dedupe: stesso `(event, product_id)` due volte → `len == 1` e vince
        l'ultimo; stesso prodotto `warning` + `suspended` → 2 eventi.
      - `groups()` ordina per `(user_id, event, group_key)` e i prodotti per `product_id`; prodotti
        aggiunti in ordine inverso escono ordinati; due utenti → due gruppi; stesso utente, due
        domini → due gruppi; `anchor_product_id` è il minimo.
      - `primary_reason`: tutti `listing_gone` → `listing_gone`; 2 `listing_gone` + 1 `parse_error`
        → `listing_gone`; 1 + 1 → `listing_gone` (parità alfabetica); `"zzz"` da solo → `"zzz"`.
      - Collector vuoto → `groups() == []`.
- [ ] Test (fallisce): `tests/unit/test_alert.py` (sostituisce
      `test_format_error_notification_mentions_count`, che va rimosso insieme alla funzione):
      - `test_operational_notice_listing_gone_copy_and_buttons`: 5 eventi `listing_gone` stesso
        dominio → `Listings removed on example.com`, `(5)`, cinque nomi, `HTTP 404`; i bottoni hanno
        «Delete» come PRIMA riga e `callback_data == "ops_del_<anchor>"`.
      - `test_operational_notice_parse_error_reactivate_first`.
      - `test_operational_notice_unknown_reason_uses_default_copy`.
      - `test_operational_notice_last_good_read_and_missing`.
      - `test_operational_notice_last_error_none_and_html_escaped`.
      - `test_operational_notice_budgets_and_cap` (proprietà, F3): 50 eventi con nomi da 200
        caratteri, `last_error` da 300, `detail` da 500, dominio da 150 → al massimo 10 righe `•`,
        `and 40 more`, **`visible_length(text) <= 4000` in un solo messaggio**, nessun nome oltre 60
        visibili, nessun errore oltre 120, ogni riga a tag bilanciati.
      - `test_operational_notice_single_product_reads_fine`.
      - `test_warning_notice_format` (`count/max`, `/errori`, `operational_buttons` → `[]`).
      - `test_operational_notice_it_locale` (fixture `fake_catalog` estesa con due msgid nuovi):
        con `set_locale("it")` il titolo esce tradotto — prova che `_()` è chiamata a run-time.
- [ ] Implementare `core/textlimits.py` (§5.8), `core/notices.py` (§5.4, parte dati) e in
      `core/alert.py`: `format_operational_notice(group) -> str`, `format_warning_notice` (o un
      unico entrypoint che smista su `group.event`), `operational_buttons(group) ->
      list[list[dict[str, str]]]`, `_why(reason, detail) -> str`, la mappa reason→copy (§5.3), i
      budget, la rimozione di `format_error_notification`. `_()` da `price_tracker.bot.messages`.
- [ ] Verifica: G1-G5. G5: i msgid non contengono `àèéìòù` né le parole della blacklist
      (`prezzo`, `errore`, `riprova`, `notifica`, …) — il testo §5.3 è già conforme.
- [ ] Commit: `feat(core): aggregate operational notices per user and domain with reason-aware copy under the Telegram length limit`.

### T4 — Scheduler: collector, flush garantito, pre-avviso, quarantena instradata, unico loop pull-mode (D3, D5, D10)

Rischio: cambia il momento in cui il notifier viene chiamato (fine tick invece che dentro il
fallimento), le firme private e la struttura dei pull-mode. Prima misura: `.venv/bin/pytest -q
tests/integration` e annota.

- [ ] Test (fallisce), `tests/integration/test_scheduler.py`:
      - Adattare `test_product_auto_disabled_after_max_consecutive_errors`: la notifica arriva
        ancora una sola volta al tick 2, testo con il titolo del reason dello stub e `Error:
        <code>…</code>`; `notifier.await_args.kwargs["product_id"] is None`,
        `kwargs["payload"]["kind"] == "operational"`, `["event"] == "suspended"`,
        `["product_ids"] == [pid]`, `["products"][0]["id"] == pid`, `["buttons"]` con due righe,
        nessuna chiave `"product_id"` nel payload.
      - `test_five_products_same_domain_one_message`: 5 prodotti su `example.com` a soglia nello
        stesso tick → `notifier.await_count == 1`, `payload["product_ids"]` 5 id ordinati.
      - `test_two_domains_two_messages` e `test_two_users_two_messages` (`run_check_all`: un flush
        per utente, `await_count == 2`, ordine delle chiamate = ordine degli utenti).
      - `test_same_product_suspended_twice_in_one_sweep_notifies_once`.
      - `test_pull_mode_checkall_flushes_aggregated_notice`: `check_user_products_for_user` con 3
        prodotti stesso dominio a soglia → `await_count == 1`, `CheckResult.disabled is True`,
        `reason == "parse_error"` (o il reason dello stub).
      - `test_check_one_product_for_user_classifies_listing_gone_and_flushes_notice_on_suspension`:
        lo scraper solleva `ListingGone(404)` fino alla soglia; la chiamata non solleva,
        `CheckResult.reason == "listing_gone"`, `gone_streak` cresce, il prodotto viene sospeso e il
        notifier riceve esattamente un gruppo operativo.
      - `test_check_one_product_for_user_unknown_id_returns_empty_result`
        (`CheckResult(product_id=pid, user_id=uid)` con `alert None`, `disabled False`, `reason None`).
      - `test_check_products_for_user_ignores_foreign_and_missing_ids`.
      - `test_mid_tick_exception_flushes_collected_notices_and_reraises`: il primo prodotto aggiunge
        un evento, il secondo solleva dal controllo health; il notifier riceve l'evento del primo e
        il chiamante riceve l'eccezione originale.
      - `test_cancelled_tick_attempts_notice_flush_before_propagating_cancelled_error`: dopo un
        evento raccolto, il task viene cancellato; il flush completa e `CancelledError` continua a
        risalire.
      - `test_warning_at_half_threshold_exactly_once`: `max=10`: al tick 5 `await_count == 1` con
        `payload["event"] == "warning"`; ai tick 6-9 nessuna nuova chiamata; al tick 10 la seconda
        chiamata è `suspended`. Con `max=1` nessun warning; con `max=2` warning al tick 1 e
        sospensione al tick 2.
      - `test_warning_is_routed_as_operational_without_product_id`.
      - `test_notifier_returning_false_does_not_raise` e `test_notifier_raising_does_not_abort_tick`.
      - `test_render_failure_of_one_group_does_not_block_others`.
      - Adattare `test_scheduler_notifies_once_on_quarantine_entry`: `await_args.kwargs["payload"]
        ["kind"] == "operational"`, `["event"] == "quarantine"`, `kwargs["product_id"] is None`; il
        testo resta quello (`pausa automatica`).
      - `test_price_payload_declares_kind_price` (ribasso e restock).
      - `test_flush_sets_and_restores_locale`: `deps.lang="it"` con `fake_catalog`; dopo il flush
        `_("❌ Invalid ID.")` nel task corrente torna al valore precedente; anche quando il rendering
        di un gruppo solleva.
- [ ] Implementare §5.4 in `core/scheduler.py` (`SchedulerDeps.lang`, parametri `collector`,
      `_event`, `_flush_notices` con `try/finally` e `asyncio.shield`, `_operational_payload`,
      `check_products_for_user` come unico loop pull, deleghe di `check_user_products_for_user` e
      `check_one_product_for_user`, `CheckResult.reason`, `kind` nei payload prezzo/restock,
      `_notify_quarantine_entry` via `_notify`), `bot/messages.py` (`set_locale` ritorna il token,
      `reset_locale(token)`), `main._setup_scheduler` (`lang=config.lang`). Rimuovere l'import e
      l'uso di `format_error_notification`. Aggiornare la docstring di modulo di `scheduler.py`
      (paragrafo «Pull») e quella di `_record_failure_and_maybe_disable`.
- [ ] Verifica: G1-G5; `rg "deps\.notifier\(" src/price_tracker/core/scheduler.py` → solo dentro
      `_notify`; `rg "_check_product_core\(" src/price_tracker/core/scheduler.py` → chiamata solo
      da `_scrape_one` (push) e da `check_products_for_user` (pull).
- [ ] Commit: `feat(scheduler): emit one operational notice per user and domain at the end of the sweep, with a guaranteed flush and a half-threshold warning`.

### T5 — Notifier: `kind`, preferenze globali, bottoni, differimento, chunking (D6, D7, D11)

`notifier/telegram.py` è fuori dalla coverage ma ha una suite (`tests/unit/test_telegram_notifier.py`,
`tests/integration/test_notification_flow.py`): i test si scrivono lo stesso.

- [ ] Test (fallisce), `tests/unit/test_preferences.py`: `resolve_global` ignora la riga
      per-prodotto (utente con `digest_mode=True` per-prodotto e `False` globale → `False`); senza
      righe → default.
- [ ] Test (fallisce), `tests/integration/test_notification_flow.py` / `tests/unit/test_telegram_notifier.py`:
      - `test_operational_routes_without_product_id`: `notifier(uid, text, product_id=None,
        payload={"kind": "operational", ...})` con `prefs` presenti → passa da `notify_alert`
        (`prefs.resolve_global` awaited, `prefs.resolve` no).
      - `test_operational_uses_global_prefs_not_product_override` (F5): riga per-prodotto con
        `quiet_hours` attive, riga globale senza → inviato subito; il contrario → accodato.
      - `test_operational_bypasses_mute`; `test_price_alert_still_muted` (regressione).
      - `test_operational_in_quiet_hours_without_digest_is_enqueued_not_dropped`: `digest.enqueue`
        awaited con `product_id=None`, `send_message` no, ritorno `True`, metrica `digest_pending`.
      - `test_operational_throttled_without_digest_is_enqueued`; `test_operational_not_throttled_records_window`.
      - `test_buttons_become_inline_keyboard`: `reply_markup` `InlineKeyboardMarkup` con 2 righe;
        vale per `_send_direct` (`prefs=None`) e per `send_alert`.
      - Proprietà sui bottoni malformati (6 forme) → messaggio inviato, `reply_markup is None`.
      - `test_long_text_is_sent_in_chunks_with_keyboard_on_last` (F3): testo da 250 righe con tag →
        `send_message` chiamato N ≥ 2 volte, ogni `text` con `visible_length <= 4000`,
        `reply_markup` solo nell'ultima chiamata, metrica `notification_sent_total` incrementata di 1.
      - `test_chunk_failure_returns_false_and_logs_index`.
      - `test_event_id_dedupe_still_applies_to_operational`.
- [ ] Implementare §5.5.
- [ ] Verifica: G1-G5.
- [ ] Commit: `feat(notifier): route operational notices by kind past mute, defer instead of drop, and split long messages`.

### T6 — Digest: sezione operativa, paginazione con marcatura parziale, quiet hours in `flush_due` (D6, D11)

Rischio: `flush_due` e `flush_user` sono condivisi con i ribassi. Prima misura:
`.venv/bin/pytest -q tests/unit/test_digest.py tests/unit/test_digest_flush.py
tests/integration/test_scheduled_alert_respects_prefs.py`.

- [ ] Test (fallisce), `tests/unit/test_digest.py`:
      - `test_digest_renders_operational_section`: 1 entry prezzo + 2 operative (`suspended`
        `listing_gone` con `domain`/`count`, `warning` con `count/max`) → header `1 price change`,
        sezione `⚠️ Operational notices`, due righe nel formato di §5.6, footer `/reactivate`.
      - `test_digest_only_operational_entries` (header `0 price changes`).
      - `test_digest_operational_entry_without_fields_does_not_crash` (`{"kind": "operational"}`
        nudo → `unknown — 1 products: tracking suspended (check failed)`).
      - `test_digest_quarantine_entry`; `test_digest_price_entry_with_null_product_id_uses_fallback_name`.
      - `test_digest_blocks_reports_unrenderable_entries` (ND2): coda con 1 entry valida e 1 il
        cui payload non è JSON valido → la valida finisce nei blocchi, l'altra in
        `unrenderable_ids`, nessuna eccezione.
      - `test_flush_user_quarantines_unrenderable_entries` (ND2): coda mista → l'entry illeggibile
        è marcata flusciata insieme alla prima pagina e loggata a WARNING; coda composta SOLO da
        entry illeggibili → nessun `send_message`, le entry sono comunque marcate e il ritorno è
        il loro numero. Il test che una riga malformata NON resti pendente dopo il flush è il
        punto: senza di esso il difetto è invisibile e si ripresenta a ogni giro.
      - `test_flush_user_paginates_and_marks_only_sent_pages` (F3): 50 entry con nomi da 200 →
        `send_message` chiamato ≥ 2 volte, ogni pagina ≤ 4000 visibili, `mark_digest_flushed`
        chiamato una volta per pagina con gli id di QUELLA pagina; con `send_message` che solleva
        alla seconda pagina → marcate solo le entry della prima, eccezione propagata, ritorno non
        raggiunto.
- [ ] Test (fallisce), `tests/unit/test_digest_flush.py`:
      - `test_flush_due_skips_user_inside_quiet_hours` (`22:00-07:00` `Europe/Rome`, `freeze_time`
        02:00 Roma → non flusciato; 08:00 → flusciato).
      - `test_flush_due_user_without_prefs_is_flushed` (regressione).
      - Rinominare `test_flush_at_quiet_hours_end_via_scheduler` in
        `test_flush_due_uses_interval_when_no_prefs`.
- [ ] Implementare §5.6.
- [ ] Verifica: G1-G5.
- [ ] Commit: `feat(digest): render operational notices in their own section, page long digests, and honour quiet hours when flushing`.

### T7 — Callback `ops_*` e registrazione nel dispatcher (D7, D11)

`bot/*` è fuori coverage e con mypy rilassato; i test seguono lo stile di
`tests/unit/test_callback_ownership.py` / `test_callback_id_parsing.py`. Per i test di gruppo usare
il repository in memoria (`apply_migrations` su `:memory:`): la query SQL è parte del contratto.

- [ ] Test (fallisce), `tests/unit/test_ops_callbacks.py`:
      - Prefissi: `ops_react_x` → `❌ ID non valido.`; `ops_react_999` → `Product not found`;
        prodotto di un altro utente (clicker non admin) → `Product not found`.
      - `test_ops_react_acts_only_on_automatic_suspensions_of_same_domain` (F1): utente con 5
        prodotti `suspend_product` su `a.example.com` (3 con `consecutive_errors=10`, 2 con
        `gone_streak=3` e `consecutive_errors=3`), 1 `pause_product` sullo stesso dominio DOPO 7
        errori, 1 riga inattiva con `suspension_kind IS NULL` e `consecutive_errors=10` (pre-migrazione),
        1 auto-sospeso su `b.example.com`, 1 auto-sospeso di un altro utente → riattivati esattamente
        i 5; `scheduler.check_products_for_user` (AsyncMock) chiamato con quei 5 id ordinati e
        `delay_between_products=0.5`; il testo finale ha 5 righe `✅`/`❌` secondo i `CheckResult`
        del mock (2 con `reason="listing_gone"` → `❌ … page not found`).
      - `test_ops_react_result_is_independent_of_thresholds`: stesso scenario con
        `config.max_consecutive_errors=100` → stesso gruppo.
      - `test_ops_react_empty_group_says_nothing_to_do`.
      - `test_ops_react_reply_is_chunked` (F3): 40 prodotti con nomi da 200 → `edit_message_text`
        una volta con `visible_length <= 4000`, `reply_text` per i chunk successivi, tutti ≤ 4000.
      - `test_ops_del_shows_confirmation_with_count_and_cancel`.
      - `test_ops_delok_deletes_group_and_reports_count`; `test_ops_delok_recomputes_group`;
        `test_ops_delok_never_deletes_manual_or_unknown_provenance` (F1).
      - `test_dispatcher_routes_ops_prefix_before_actions`.
- [ ] Test: `tests/integration/test_repository_handler_contract.py` deve continuare a passare —
      eseguirlo esplicitamente dopo l'implementazione.
- [ ] Implementare §5.7 (`_ops.py` con `_reply_chunked`, `callbacks/__init__.py`; i prefissi
      importati da `core/notices.py`, `split_message` da `core/textlimits.py`).
- [ ] Verifica: G1-G5; `tests/integration/test_handler_import_smoke.py` verde.
- [ ] Commit: `feat(bot): reactivate-and-recheck or delete every automatically suspended product of a domain from the notice`.

### T8 — Catalogo i18n, documentazione, CHANGELOG (D8)

- [ ] `bash scripts/i18n.sh extract` poi `bash scripts/i18n.sh update`; aprire il diff dei due
      `.po`: tenere le entry nuove, rimuovere le modifiche di puro rumore (rinumerazioni di
      commenti `#:` accettabili, `fuzzy` su entry esistenti NO: ripristinarle a mano). Compilare le
      msgstr `it_IT` dalla tabella §5.3 e le `en` con `msgstr = msgid`. `bash scripts/i18n.sh
      compile`. `messages.pot` NON si committa (non è tracciato: `git ls-files | grep pot` vuoto).
- [ ] Test (fallisce prima della compilazione): `tests/i18n/test_locale.py` —
      `test_production_catalog_has_operational_notice_strings`: catalogo VERO
      (`get_translation("it_IT")` dopo `cache_clear`), `gettext("Listings removed on {domain}") ==
      "Prodotti rimossi da {domain}"` e altri 3 msgid della tabella.
- [ ] Doc: `docs/notifications.md` — nuova sezione «Operational notices» (cosa sono, mute
      ignorato, SOLO preferenze globali, quiet hours/digest rispettati con differimento, bottoni,
      `LISTING_GONE_CONFIRMATIONS`, provenienza `suspension_kind` e politica per le righe
      pre-migrazione, limite di lunghezza e segmentazione) e correzione del resolution chain sulle
      quiet hours (il flush aspetta la fine della finestra). `docs/operations.md` — riga
      `MAX_CONSECUTIVE_ERRORS` corretta (D-9), nuova riga `LISTING_GONE_CONFIRMATIONS | 3 |
      Consecutive HTTP 404/410 answers before a removed listing is suspended`, nota sulle migrazioni
      014/015 (015 ricostruisce `digest_queue`: backup prima dell'upgrade, come per ogni release).
      `README.md` — stessa riga env. `docs/architecture.md` — solo se nomina
      `format_error_notification` (grep).
- [ ] `CHANGELOG.md` → `[Unreleased]`: `Added` (removed-listing detection, aggregated notices with
      buttons, half-threshold warning, `LISTING_GONE_CONFIRMATIONS`, suspension provenance),
      `Changed` (operational notices honour quiet hours and digest but not mute, using the user's
      global preferences; digest flush waits for quiet hours to end; long messages are split;
      queued digest entries survive the deletion of their product; `Tracking suspended` message
      replaced). Prosa che dice il caso reale, senza nomi di strumenti.
- [ ] Verifica: G1-G5; `git status` mostra i due `.mo` modificati insieme ai `.po`.
- [ ] Commit: `docs: describe operational notices, suspension provenance and the removed-listing threshold`.

### T9 — Chiusura

- [ ] Suite intera G1-G5 dall'inizio, coverage riportata; confronto con T0: `passed` cresciuto di
      almeno il numero di test aggiunti, coverage ≥ 91%.
- [ ] Prova manuale offline della catena (senza Telegram): script in scratchpad che monta
      `Scheduler` con `TelegramNotifier(bot=AsyncMock(), prefs=PreferencesManager(repo),
      digest=DigestService(repo, bot))` su un DB in memoria, 5 prodotti Shopify mockati 404 con
      `respx`, 3 tick → una sola `send_message` con `reply_markup` di due righe e testo `Listings
      removed on …`; poi `delete_product` di uno dei cinque con un'entry operativa in coda → l'entry
      sopravvive con `product_id NULL`. Riportare l'output nella PR.
- [ ] Aprire la PR verso `main` con: incidente, decisioni D1-D11 in tre righe, elenco dei residui
      (§9). Ciclo Review v2 del kernel per-task e babysitting `gh pr checks` fino al merge a verde.

---

## 7. Mappa file → task

| File | Task |
|---|---|
| `src/price_tracker/db/migrations/014_add_suspension_provenance.sql`, `015_digest_queue_nullable_product.sql` (nuovi) | T1 |
| `src/price_tracker/db/models.py`, `db/repository.py`, `notifier/digest.py` (solo firma `enqueue`) | T1 |
| `src/price_tracker/core/exceptions.py`, `core/scraper_base.py` | T2 |
| `src/price_tracker/scrapers/shopify.py`, `generic.py`, `ebay.py`, `nove25.py`, `aliexpress.py` | T2 |
| `src/price_tracker/config.py`, `main.py` | T2 (`listing_gone_confirmations`), T4 (`lang`) |
| `src/price_tracker/core/textlimits.py` (nuovo), `core/notices.py` (nuovo), `core/alert.py` | T3 |
| `src/price_tracker/core/scheduler.py`, `bot/messages.py` (`set_locale` token, `reset_locale`) | T4 |
| `src/price_tracker/notifier/preferences.py` (`resolve_global`), `notifier/telegram.py` | T5 |
| `src/price_tracker/notifier/digest.py` | T6 |
| `src/price_tracker/bot/handlers/callbacks/_ops.py` (nuovo), `callbacks/__init__.py` | T7 |
| `src/price_tracker/locale/*/LC_MESSAGES/messages.po` + `.mo`, `docs/*.md`, `README.md`, `CHANGELOG.md` | T8 |

Un solo writer per file: i task sono sequenziali sullo stesso branch. Se il main loop decide di
parallelizzare, T3 (moduli puri) non condivide file con T1/T2; T5 e T6 dipendono da T1 (`DigestEntry`)
e T3 (`textlimits`); T4 dipende da T1-T3; T7 dipende da T3 e T4.

---

## 8. Test per proprietà — riepilogo degli input ostili coperti

Perché l'elenco degli esempi non condivida i punti ciechi dell'autore, questi input sono
obbligatori (già distribuiti nei task, qui raccolti per controllo di chiusura):

- reason sconosciuto (`"zzz"`), reason `None`/vuoto → copy di default, `_why` → `check failed`;
- `group_key_for` su `""`, non-URL, IP, `localhost`, host senza suffisso → mai eccezione;
- `last_error` `None`, con HTML, con 300 char; `last_checked_at` `None`; `current_price` `None`;
- **nomi da 200 caratteri, errori da 300, domini da 150, 40-50 prodotti**: notifica, digest e
  risposta del callback tutti ≤ 4000 visibili per messaggio, tag bilanciati per chunk;
- prodotto sospeso due volte nello stesso sweep; stesso prodotto `warning` e `suspended`;
- 1 prodotto (nessun plurale rotto);
- **pausa manuale dopo N errori, riga inattiva senza provenienza, soglia cambiata dopo la
  sospensione**: mai nel gruppo delle azioni di massa;
- **cancellazione di un prodotto con entry operativa in coda**: l'entry sopravvive;
- **preferenze per-prodotto divergenti dalla globale**: gli operativi seguono la globale;
- utente senza riga prefs (notifier e `flush_due`); prefs con solo `mute`; quiet hours a cavallo
  di mezzanotte;
- notifier che ritorna `False`; che solleva; che fallisce al secondo chunk; rendering che solleva
  su un gruppo; **eccezione a metà tick; cancellazione del task a metà tick**;
- `buttons` malformati (6 forme) e `callback_data` di 65 byte;
- status 404, 410, 403 (precedenza), 200, 500, 0, -1 su `detect_listing_gone`;
- ordine ostile: eventi aggiunti in ordine inverso di id e di utente;
- `max_consecutive_errors` 1, 2, 10 per il pre-avviso;
- id callback non numerico, inesistente, di altro utente; gruppo vuoto; gruppo cambiato tra
  prompt e conferma;
- digest: invio che fallisce alla seconda pagina → marcate solo le entry della prima.

---

## 9. Cosa questo piano NON fa

- **Transactional outbox** (finding 2, CRITICAL-DESIGN): il flush legge il ritorno del notifier e
  logga i `False`, ma una notifica non consegnata dal trasporto resta persa (il differimento in
  coda copre i gate delle preferenze, non un errore di rete). La outbox è il lavoro successivo e
  dovrà riusare il payload di §5.4 (serializzabile, con `event_id` idempotente) come riga di outbox.
- **Atomicità registrazione-fallimento/sospensione** (finding 3): `record_failure` riduce i commit
  ma la finestra tra rilettura e `suspend_product` resta; il dedupe del collector copre solo due
  sospensioni nello STESSO sweep, non tick e `/checkall` concorrenti.
- **Successo parziale che lascia alto il contatore** (finding 4), **half-open che consuma il budget
  del prodotto** (5), **errori di DB nel contatore** (6): invariati. Il pre-avviso può scattare su
  un contatore gonfiato da un currency mismatch ripetuto.
- **Cache health prima della persistenza** (finding 8).
- Il **redirect a home/collezione** di Shopify per prodotto rimosso: era escluso in revisione 2
  come «non confermato dall'incidente». **Confermato dall'incidente il 2026-09-02** ed entrato in
  T2: eseguendo i cinque prodotti veri contro il negozio, il PRIMO di ogni sessione non usciva come
  `ListingGone` ma come `price_none`, e il log diceva `Shopify rejecting non-product redirect …
  -> https://…/`. Il negozio redirige alla vetrina la prima richiesta di una sessione senza cookie,
  e risponde 404 solo dalla seconda. `_fetch_html` solleva ora `ListingGone(404)` quando l'URL
  RICHIESTO era un product path e la risposta finale non lo è; se l'URL richiesto non era un
  prodotto (una collezione) resta `None`, perché finire altrove non dice nulla su una rimozione, e
  se si atterra su un ALTRO prodotto non è una rimozione ma un URL rimescolato. Due test coprono
  le due distinzioni, e il test preesistente sul redirect a home è stato riscritto per asserire il
  motivo oltre all'assenza di prezzo.
- **Amazon e gli altri dieci scraper** (apple_store, bestbuy, etsy, google_store, mediamarkt,
  newegg, otto, target, walmart, wayfair, zalando) non chiamano `detect_listing_gone`: per loro un
  404 resta `price_none` o `http_error` a seconda dei loro `except`, e il listing sparito resta
  indistinguibile da una lettura fallita. Il ramo scheduler di §5.1 li copre solo se lasciano
  salire l'`HTTPStatusError` (P8, non verificato per esecuzione). Estensione file per file come
  lavoro successivo, con lo stesso pattern dei cinque di T2.
- **Backfill della provenienza** per le righe inattive esistenti: non ricostruibile (P4); restano
  `NULL` e fuori dalle azioni di gruppo. I cinque prodotti dell'incidente rientrano in questo caso:
  per loro vale `/reactivate` (e, riattivati, il nuovo ciclo li risospende con provenienza).
- **Override per-prodotto per i ribassi**: `notify_alert` dei ribassi continua a coalescere
  per-prodotto → globale mentre `flush_due` legge solo la globale (P20, P21): divergenza
  preesistente tra codice e documentazione, non toccata.
- Il testo italiano hardcodato di `format_quarantine_notification` e le stringhe italiane residue
  dei callback legacy non vengono tradotti/normalizzati.
- Il menu «Prodotti in pausa» (`_menu.py:128-145`) non distingue ancora auto-sospesi da pause
  volute: `suspension_kind` e `list_auto_suspended_products` sono pronti per farlo.
- Lo spostamento di `bot/messages.py` in `price_tracker/i18n.py`.
- Soglia percentuale contro `initial_price`, tick per-prodotto, localizzazione completa.

---

## 10. NOTE PER IL MAIN LOOP

1. **La causa persistita in produzione non è `http_error`** (D-1): per i cinque prodotti è
   `price_none`. Non serve correggere il DB; i prodotti restano sospesi con `suspension_kind NULL`
   (fuori dalle azioni di gruppo, per scelta conservativa D7) e si riattivano con `/reactivate`.
2. **`flush_due` consapevole delle quiet hours (T6) cambia anche i ribassi in digest**: oggi un
   utente con quiet hours 22-07 e digest attivo riceve il digest alle ~23:00; dopo T6 lo riceve al
   primo flush dopo le 07:00. È il comportamento che `docs/notifications.md` e la docstring di
   `digest.py` dichiarano già. Se non lo si vuole, D6 va riscritto: gli operativi differiti nelle
   quiet hours arriverebbero comunque di notte.
3. **La migrazione 015 ricostruisce `digest_queue`** (unica via in SQLite per cambiare una FK).
   Verificata live sul migrator con righe preesistenti (P18). Effetto collaterale voluto: anche le
   entry dei RIBASSI sopravvivono alla cancellazione del prodotto (con `product_id NULL` e nome di
   fallback nel digest) invece di sparire. Se il main loop preferisce che i ribassi di un prodotto
   cancellato spariscano, serve un filtro `product_id IS NOT NULL OR kind = 'operational'` in
   `list_pending_digest`: NON l'ho messo, perché un utente che cancella un prodotto dopo aver visto
   il ribasso in coda non perde niente di utile in nessuno dei due casi, e il filtro è un'altra
   regola da mantenere.
4. **Semantica scelta per F5: solo preferenze globali per gli operativi** (D6). Motivazione:
   notifica per dominio, non per prodotto; `flush_due` già globale; documentazione già user-wide.
   L'alternativa (spezzare i gruppi per routing effettivo) distrugge l'aggregazione. Il prezzo è che
   un override per-prodotto di quiet hours non silenzia l'avviso operativo del suo dominio; è
   coerente col fatto che il mute per-prodotto è già ignorato.
5. **`ListingGone` non è retryabile e non è un `BlockEvent`**: uno store che risponde 404 a tutto
   per un bug di deploy sospenderà ogni prodotto dopo 3 check senza attivare la quarantena di
   dominio. Accettato: il messaggio aggregato con «Riattiva e ricontrolla» è la risposta.
6. **Dipendenza core → `bot.messages`** (D8): scelta deliberata; alternativa `price_tracker/i18n.py`
   se il reviewer la boccia.
7. **Baseline**: il 717/91,65% è misurato sul working tree del branch fix, non su `main`. T0
   rimisura; qualunque confronto successivo usa quel numero.
8. **P8 riconciliata** solo per i cinque scraper nominati (ora in T2). Amazon e i dieci restanti
   sono dichiarati in §9 come non coperti: se il main loop vuole la copertura completa, è un task
   T2-bis con un test respx per scraper, non una modifica di questo piano.
9. **P10, P11, P12 sono NON VERIFICATE per esecuzione/documento ufficiale**; nessuna tocca
   security; ognuna ha una mitigazione nel piano (ripristino locale nel `finally`; validazione della
   lunghezza del `callback_data`; contratto di segmentazione con `SAFE_LIMIT = 4000`).
10. Il branch corrente `fix/public-metadata-and-privacy` contiene il fix 403/429 di Shopify
    (`detect_block_event` prima di `raise_for_status`): T2 inserisce `detect_listing_gone` subito
    dopo quella riga. Se il fix non è ancora su `main` quando parte T2, l'esecutore troverà
    `raise_for_status` PRIMA di `detect_block_event` in `_fetch_shopify_response`: fermarsi e
    chiedere l'ordine di merge, non replicare il fix nel branch di questo piano.

---

## 11. Storia delle revisioni

- **rev 1** (2026-09-02): prima stesura.
- **rev 2** (2026-09-02): stress-test avversariale, verdetto «da rivedere», 6 finding integrati:
  F1 provenienza persistita della sospensione (D-4, D7, D9, §3, §5.2, §5.7, T1, T7); F2 coda digest
  senza owner obbligatorio, migrazione 015 (D-10, D6, D9, §5.2, §5.5, §5.6, T1, T5, T6, P17, P18);
  F3 contratto di segmentazione (D-11, D4, D11, §5.3, §5.6, §5.7, §5.8, T3, T5, T6, T7, P12); F4
  flush garantito in `try/finally` con `asyncio.shield` (D3, §5.4, T4, testo applicato verbatim);
  F5 preferenze globali per gli operativi (D6, §5.5, §5.6, T5, P20, P21, §10.4); F6 unico loop
  pull-mode (D-7, §5.4, T4, testo applicato verbatim). Riconciliazione P8: eBay, Nove25, AliExpress
  entrano in T2; Amazon e gli altri dieci dichiarati in §9.

### Revisione 3 (2026-09-02, giro 2 dell'adversarial pass)

I sei finding del giro 1 sono stati confermati ADDRESSED. Il giro 2 ha però trovato due difetti
NUOVI, introdotti dalle correzioni stesse, entrambi con fix dettato per intero:

- **ND1 CRITICO** — `suspend_product` aggiornava la riga incondizionatamente, quindi un `/pausa`
  eseguito fra `record_failure` e la sospensione veniva sovrascritto da `automatic` e il prodotto
  rientrava nel gruppo cancellabile: il difetto F1 rientrava dalla finestra di corsa. Chiuso con
  un compare-and-swap (`AND is_active = 1`) che ritorna `bool`, e l'evento viene aggiunto solo se
  l'UPDATE ha davvero modificato la riga.
- **ND2 MEDIO** — un'entry del digest con payload non valido non entrava in nessuna pagina e non
  veniva mai marcata, restando in coda per sempre. Chiusa con la quarantena esplicita: viene
  marcata flusciata e loggata a WARNING. Delle due vie offerte dal reviewer si è scelta questa e
  non il blocco di ripiego, perché il contenuto di quelle entry è per definizione illeggibile e
  un payload non valido non torna valido al giro dopo.

Entrambe applicate al testo normativo e ai task. Non è stato aperto un terzo giro di review sul
piano: il cap del protocollo è di due, il design ha retto entrambi i giri, e questi due punti
erano dettature, non decisioni. La verifica successiva avviene sui test del codice.

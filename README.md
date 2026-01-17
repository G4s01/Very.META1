# Very.META1 — coupon generator

Script Python per automatizzare in modo **controllato** la raccolta di coupon della promo `promo-meta1` di Very Mobile, replicando il flusso del frontend (POST verso il webhook n8n) con:

- esecuzione **concorrente** (multi‑thread)  
- generazione di **email deterministiche** seed/index  
- salvataggio risultati in **CSV arricchito**  
- **UI da terminale** compatta in stile “scanner” (lista codici + barra di progresso).

> ⚠️ **Uso consentito solo se sei autorizzato** dal proprietario del servizio (Very Mobile / WindTre o tuo committente).  
> L’autore non è responsabile di utilizzi impropri o non conformi a termini e leggi applicabili.

---

## Novità principali (versione attuale)

- UI console in stile `hunter.py`:
  - header con info run (target, modalità email, operatore, workers)
  - una riga per ogni coupon trovato (`🟢 [001] S-XXXXX email@...`)
  - **unica barra di progresso** in fondo aggiornata con `\r`:
    ```text
    🚀 60.00% |██████████░░░░░░░░░░░░░░| 30/50  attempts:180  speed:0.45/s  ETA:00:00:44
    ```
- Formato CSV **esteso**:
  ```text
  USED,COUPON,MVNO,EMAIL
  0,S-ABCDE1,CoopVoce,239d4b49-000314@example.com
  ```
  - `USED`: flag manuale 0/1 per ricordarsi se il coupon è già stato usato.
  - `COUPON`: codice promo.
  - `MVNO`: operatore passato con `--operator`.
  - `EMAIL`: email usata per quel coupon.
- CSV gestito in modalità **merge + dedup**:
  - se `results.csv` esiste, i nuovi risultati vengono fusi evitando doppioni `(coupon,email)`.
- Gestione **seed/index resiliente**:
  - seed primario da `email_seed.txt` (o riuso con `--reuse-seed`);
  - fallback dell’indice da `used_emails.txt` **o** dal CSV esistente;
  - fallback opzionale del **seed** dal CSV se `email_seed.txt` manca.

---

## Obiettivo

- Replicare il comportamento principale del frontend:
  - invio POST al webhook di backend con payload `{email, operator}`;
  - lettura del coupon (da JSON o, in fallback, dalla pagina di ringraziamento);
  - registrazione strutturata in CSV con metadati utili.
- Consentire generazione **massiva, ordinata e tracciabile** dei coupon, evitando:
  - riuso di email deterministiche,
  - duplicati di codici all’interno della stessa run,
  - perdita di storico tra esecuzioni.

---

## Requisiti

- Python **3.8+**
- Dipendenza Python:
  ```bash
  pip install requests
  ```

---

## File principali in repository

- `coupon_gen.py` — script principale (versione concorrente con UI tipo scanner).
- `results.csv` — CSV di output nel formato:
  ```text
  USED,COUPON,MVNO,EMAIL
  0,S-XXXXX,CoopVoce,seedshort-000123@example.com
  ```
- `email_seed.txt` — seed hex usato per generare email deterministiche.
- `email_index.txt` — indice persistente (prossimo numero di email).
- `used_emails.txt` — append‑only con tutte le email usate (per audit e fallback indice).
- `seeds_history.txt` — cronologia seed con timestamp.
- `run.log` — log dettagliato (se abilitato con `--log`).

---

## Modalità di funzionamento

### Flusso HTTP

Per ogni coupon richiesto lo script:

1. Determina l’email da usare (fissa o generata).
2. Invia:
   ```http
   POST STANDARD_WEBHOOK_URL
   Content-Type: application/json

   {"email": "<email>", "operator": "<MVNO>"}
   ```
3. Analizza la risposta:
   - JSON classico:
     ```json
     {"Coupon": "S-XXXXX", "MNP": "CoopVoce", "Page": "grazie"}
     ```
   - oppure altri formati (chiavi `coupon/code/url` o corpo testuale).
4. Se trova un codice nuovo:
   - lo aggiunge allo stato in memoria
   - lo visualizza in console
   - verrà poi riversato in `results.csv`.

---

## UI da terminale

Esempio output:

```text
🚀 VERY META1 COUPON GENERATOR
🎯 Target : 50 coupon(s)
📧 Mode   : deterministic seed/index
📡 Oper.  : CoopVoce
🧵 Workers: 6
------------------------------------------------------------
Coupons:
------------------------------------------------------------
🟢 [001] S-KC91IZ   239d4b49-000314@example.com
🟢 [002] S-RGWWFD   239d4b49-000317@example.com
...
🚀 60.00% |██████████░░░░░░░░░░░░░░| 30/50  attempts:210  speed:0.45/s  ETA:00:00:44
------------------------------------------------------------
✅ Raccolti 50/50 coupon. Seed: 239d4b49
------------------------------------------------------------
```

- Ogni coupon è una riga indipendente.
- La barra finale mostra:
  - % completamento
  - barra grafica
  - `collected/target`
  - tentativi totali (`attempts`)
  - velocità (`coupon/s`)
  - ETA stimata.

---

## Esempi d’uso

### 1. Dry‑run (nessuna richiesta reale)

```bash
python coupon_gen.py --count 3 --no-email
```

Serve solo a verificare:

- che la CLI funzioni
- che le email deterministic siano nel formato atteso.

### 2. Run reale minima (1 coupon)

```bash
python coupon_gen.py \
  --real --yes \
  --count 1 \
  --no-email \
  --operator "CoopVoce" \
  --output results.csv \
  --log run.log
```

Controlla:

- una riga `🟢 [001] S-XXXXX ...` in console;
- una riga nel CSV con `USED,COUPON,MVNO,EMAIL`.

### 3. Run concorrente (batch grande)

```bash
python coupon_gen.py \
  --real --yes \
  --count 500 \
  --no-email \
  --operator "CoopVoce" \
  --concurrency 12 \
  --max-attempts 5000 \
  --output results.csv \
  --log run.log
```

Ripetendo il comando con lo **stesso** `results.csv`:

- i nuovi coupon vengono aggiunti,
- quelli già presenti (stessa coppia `COUPON,EMAIL`) non vengono duplicati.

---

## Opzioni CLI (riassunto)

```bash
python coupon_gen.py [OPZIONI]
```

### Modalità / sicurezza

- `--real`  
  Esegue richieste HTTP reali. Senza, lavora in dry‑run.

- `--yes`  
  Salta la conferma interattiva quando usi `--real`.

### Target & concorrenza

- `--count N`  
  Numero di coupon unici da raccogliere (default `1`).

- `--concurrency N`  
  Numero di worker threads (default `6`).

- `--max-retries N`  
  Valore storico usato per derivare il default di `--max-attempts`.

- `--max-attempts N`  
  Limite di richieste complessive.  
  Se `0`, viene calcolato come:
  `count * max_retries * concurrency * concurrency_max_attempts_multiplier`.

- `--concurrency-max-attempts-multiplier N`  
  Fattore moltiplicativo aggiuntivo per il calcolo automatico di `max_attempts`.

- `--delay SECONDS`  
  Ritardo opzionale tra richieste (per worker).

### Email & seed

- `--no-email`  
  Usa email deterministiche seed/index.

- `--email EMAIL`  
  Email fissa quando **non** usi `--no-email`.

- `--domain DOMAIN`  
  Dominio per le email generate (default `example.com`).

- `--seed-file PATH`  
  File seed principale (default `email_seed.txt`).

- `--index-file PATH`  
  File indice persistente (default `email_index.txt`).

- `--used-file PATH`  
  File append‑only con email usate (default `used_emails.txt`).

- `--reuse-seed`  
  Forza il riuso del seed esistente in `--seed-file`.
  Se il file non c’è, la run viene abortita.

### Operatore & output

- `--operator NAME`  
  Operatore/MVNO da mandare al backend (es. `CoopVoce`).

- `--output PATH`  
  File CSV di output (default `results.csv`).

- `--log PATH`  
  File di log dettagliato (DEBUG).

---

## Gestione seed & indici

Ordine di priorità:

1. **Seed**
   - Se `--reuse-seed` → legge obbligatoriamente da `email_seed.txt`.
   - Altrimenti:
     1. se `email_seed.txt` esiste → lo riusa;
     2. altrimenti, se esiste `results.csv` → prova a ricostruire il seed leggendo il **prefisso** delle email (`seedshort-XXXXXX@...`);
     3. se non riesce → genera un seed nuovo casuale e lo salva.

2. **Indice**
   - Se `email_index.txt` esiste → parte da lì.
   - Se non esiste:
     1. prova a ricostruire l’ultimo indice da `used_emails.txt`;
     2. se ancora 0, ma esiste `results.csv` → legge l’ultimo indice dalle email presenti nel CSV;
     3. scrive il valore risultante in `email_index.txt`.

In questo modo puoi:

- cancellare `used_emails.txt` e `email_index.txt` ma mantenere `results.csv` e `email_seed.txt` → il sistema ricostruisce indice e continua senza conflitti;
- in emergenza, cancellare anche `email_seed.txt` tenendo solo `results.csv` → il seed viene ricavato dal CSV e persistito per il futuro.

---

## CSV di output

Formato definitivo:

```text
USED,COUPON,MVNO,EMAIL
0,S-ONIOLE,CoopVoce,239d4b49-000314@example.com
0,S-5K72GG,CoopVoce,239d4b49-000317@example.com
...
```

- Puoi aprirlo in Excel/LibreOffice e:
  - usare `USED` come “checkbox” manuale (0 = non usato, 1 = usato),
  - filtrare per MVNO o per email.

Ogni nuova run:

- mantiene le righe esistenti,
- aggiunge solo nuove coppie `(COUPON,EMAIL)`,
- non tocca il valore `USED` delle righe già presenti.

---

## Buone pratiche

- Mantieni `--concurrency` e `--max-attempts` ragionevoli per evitare di stressare l’endpoint.
- Usa `--log` quando:
  - cambi codice,
  - noti errori frequenti,
  - vuoi analizzare il comportamento del backend (status code, body, ecc.).
- Usa domini di test (`--domain`) se non vuoi inviare traffico verso mailbox reali.

---

## Note legali / etiche

- Script concepito per **testing interno, QA, monitoraggio autorizzato**.
- Prima di usarlo su infrastruttura reale:
  - assicurati di avere autorizzazioni formali,
  - rispetta termini d’uso e normativa privacy,
  - concorda limiti di volume e finestre temporali con il titolare del servizio.

--- 

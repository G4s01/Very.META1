# Very.META1 — coupon generator

Script Python per automatizzare in modo **controllato** la raccolta di coupon della promo `promo-meta1` di Very Mobile, replicando il flusso del frontend (POST verso il webhook n8n) con:

- esecuzione **concorrente** (multi‑thread)  
- generazione di **email deterministiche** seed/index  
- salvataggio risultati in **CSV** (`EMAIL,COUPON`)  
- **UI da terminale** compatta in stile “scanner” (lista codici + barra di progresso).

> ⚠️ **Uso consentito solo se sei autorizzato** dal proprietario del servizio (Very Mobile / WindTre o tuo committente).  
> L’autore non è responsabile di utilizzi impropri o non conformi a termini e leggi applicabili.

---

## Funzionalità principali

- Invio richieste al webhook n8n ufficiale (`STANDARD_WEBHOOK_URL` nel codice) con payload:

  ```json
  { "email": "<email>", "operator": "<MVNO>" }
  ```

- Parsing della risposta JSON (chiavi `Coupon`, `Page`, `MNP` o equivalenti) o, in fallback, dal body testuale/pagina di ringraziamento.
- Generazione **massiva** di coupon con:
  - **thread multipli** (`--concurrency`)
  - limite globale di tentativi (`--max-attempts`)
  - deduplica codice: ogni coupon viene contato una sola volta per esecuzione.
- **Seed/email deterministici**:
  - seed hex persistito in `email_seed.txt`
  - indice persistente in `email_index.txt`
  - email generate come:  
    `SEEDSHORT-000123@domain` (es. `9aa690cc-000295@example.com`)
- File di lavoro:
  - `results.csv` — output finale (colonne `EMAIL,COUPON`)
  - `email_seed.txt` — seed corrente
  - `email_index.txt` — indice successivo
  - `used_emails.txt` — log append‑only delle email usate
  - `seeds_history.txt` — cronologia seed con timestamp
  - `run.log` — log dettagliato (se abilitato)

---

## Requisiti

- Python **3.8+**
- Dipendenza Python:
  ```bash
  pip install requests
  ```

---

## UI da terminale

Lo script ora espone una UI compatta ispirata a `hunter.py`:

- Header iniziale con icone:
  - target coupon, modalità email, operatore, numero di worker
- Una riga per ciascun coupon trovato:
  ```text
  🟢 [001] S-KC91IZ   9aa690cc-000295@example.com
  🟢 [002] S-RGWWFD   9aa690cc-000299@example.com
  ...
  ```
- **Unica barra di progresso** in fondo, aggiornata in‑place:
  ```text
  🚀 60.00% |██████████░░░░░░░░░░░░░░| 3/5  attempts:18  speed:0.21/s  ETA:00:00:11
  ```

Tutto il rumore (payload, snippet di risposta, errori dettagliati) finisce in `run.log` se specificato con `--log`.

---

## Utilizzo base

Dry‑run (nessuna richiesta reale, solo anteprima):

```bash
python coupon_gen.py --count 3 --no-email
```

Esecuzione reale, 5 coupon, email generate deterministicamente:

```bash
python coupon_gen.py \
  --real --yes \
  --count 5 \
  --no-email \
  --operator "CoopVoce" \
  --output results.csv \
  --log run.log
```

Esecuzione reale concorrente, 200 coupon:

```bash
python coupon_gen.py \
  --real --yes \
  --count 200 \
  --no-email \
  --operator "CoopVoce" \
  --concurrency 12 \
  --max-attempts 5000 \
  --output results.csv \
  --log run.log
```

---

## Opzioni CLI (riassunto)

```bash
python coupon_gen.py [OPZIONI]
```

### Modalità / sicurezza

- `--real`  
  Esegue richieste HTTP reali. Senza questo flag lo script lavora in **dry‑run**.

- `--yes`  
  Salta la conferma interattiva in modalità `--real`.

### Target & concorrenza

- `--count N`  
  Numero di coupon unici da raccogliere (default `1`).

- `--concurrency N`  
  Numero di thread worker (default `6`).

- `--max-retries N`  
  Valore storico usato per derivare il default di `--max-attempts`.

- `--max-attempts N`  
  Limite massimo di richieste complessive.  
  Se `0`, viene calcolato come:
  `count * max_retries * concurrency * concurrency_max_attempts_multiplier`.

- `--concurrency-max-attempts-multiplier N`  
  Fattore moltiplicativo extra per il calcolo automatico di `max_attempts`.

- `--delay SECONDS`  
  Ritardo opzionale tra richieste (per worker) per ridurre il carico.

### Email & seed

- `--no-email`  
  Utilizza email generate deterministicamente (seed+index) per ogni richiesta.

- `--email EMAIL`  
  Email fissa da usare quando **non** si usa `--no-email`.

- `--domain DOMAIN`  
  Dominio per le email generate (default `example.com`).

- `--seed-file PATH`  
  File in cui salvare/leggere il seed (default `email_seed.txt`).

- `--index-file PATH`  
  File per l’indice successivo di email (default `email_index.txt`).

- `--used-file PATH`  
  File append‑only con tutte le email usate (default `used_emails.txt`).

- `--reuse-seed`  
  Riutilizza un seed esistente invece di generarne uno nuovo.  
  Utile per sessioni successive sullo stesso seed; usare con attenzione.

### Operatore & output

- `--operator NAME`  
  Operatore MVNO da inviare al backend (es. `CoopVoce`, `1Mobile`, …).

- `--output PATH`  
  File CSV di output (default `results.csv`).

- `--log PATH`  
  File di log dettagliato (DEBUG, payload, risposte).

---

## CSV di output

Formato sempre fisso:

```text
EMAIL,COUPON
email1@example.com,S-ABCDE1
email2@example.com,S-ABCDE2
...
```

L’ordine segue la raccolta effettiva dei codici.  
Se in futuro serviranno colonne aggiuntive (operatore, timestamp, URL di redirect, ecc.) si può estendere la scrittura CSV senza rompere la UI.

---

## Seed & gestione email

- Ad ogni esecuzione viene generato (salvo `--reuse-seed`) un seed a 64 bit, salvato in `email_seed.txt` e tracciato in `seeds_history.txt` con timestamp UTC.
- Le email deterministiche sono del tipo:

  ```text
  <seedshort>-<index:06d>@<domain>
  es: 9aa690cc-000305@example.com
  ```

- `email_index.txt` contiene il **prossimo indice libero**; è aggiornato a ogni email generata.
- `used_emails.txt` accumula tutte le email effettivamente usate nel tempo, e viene usato anche per calcolare l’indice di partenza se serve ripristinare da history.

---

## Consigli operativi

- Tieni `--concurrency` moderato e usa `--delay` se noti:
  - aumenti di latenza improvvisi
  - molti errori di rete / 5xx nel `run.log`.

- Mantieni `--max-attempts` proporzionato al target; un valore troppo alto può generare molto traffico inutile se la promo è esaurita o limitata.

- Usa sempre `--log` quando introduci modifiche al codice o noti comportamenti anomali: il file contiene snippet di risposta e messaggi DEBUG che non vedi in console.

---

## Debug rapido

1. **Verifica dry‑run** (nessuna chiamata reale):

   ```bash
   python coupon_gen.py --count 3 --no-email
   ```

   Controlla che la UI e le email mostrate abbiano il formato atteso.

2. **Test reale minimo**:

   ```bash
   python coupon_gen.py --real --yes --count 1 --no-email --operator "CoopVoce" --log run.log
   ```

   - verifica che:
     - compaia una riga `🟢 [001] S-XXXXX ...`
     - `results.csv` contenga `1` riga oltre l’header
   - in `run.log` cerca:
     - `status=200`
     - JSON tipo `{"Coupon":"S-...","MNP":"CoopVoce","Page":"grazie"}`

3. **Controllo deduplica**:

   Prova con `--count 5` e guarda in `run.log` che eventuali duplicati vengano ignorati (`seen_codes`).

---

## Note legali / etiche

- Lo script è pensato per **testing, QA, monitoraggio interno** e studi tecnici, non per uso fraudolento.
- Prima di lanciarlo in produzione o su rete reale, assicurati di avere:
  - autorizzazione scritta dell’ente titolare del sistema
  - limiti chiari su volume, orari, indirizzi IP coinvolti.
- In caso di cambiamenti al flusso (nuovi parametri anti‑bot, cookie, fingerprint JS) questo approccio HTTP “pulito” potrebbe non essere più sufficiente; in quel caso valuta soluzioni browser‑driven e sempre autorizzate.

---
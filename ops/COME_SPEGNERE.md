# Come spegnere (e riaccendere) le nuove aperture del bot

Con l'interruttore spento il bot **non apre nuove posizioni**. Quelle già aperte
restano protette come sempre: stop, gestione durante la giornata, chiusura di
fine giornata.

## Metodo 1 — da GitHub (funziona sempre, anche senza Claude)

1. Apri questo link. Dal telefono usa il browser; se ti chiede di entrare, accedi
   con il tuo account GitHub:
   https://github.com/roger-d-ray/Liquid/edit/main/ops/new_openings.txt
2. La prima riga dice `ON`. Cambiala in `OFF`.
3. Se vuoi, nella riga subito sotto scrivi il motivo (per esempio "notizie FOMC").
   Ti arriverà su Telegram.
4. Premi **Commit changes…** e poi di nuovo **Commit changes**. Lascia scelto
   "Commit directly to the main branch".

Fatto. Alla run successiva il bot non apre più nulla e ti manda **un** messaggio
su Telegram: "⏸️ Interruttore di emergenza su OFF…".

### Per riaccendere

Stesso link: al posto di `OFF` rimetti `ON` (la riga del motivo puoi
cancellarla), poi **Commit changes**. Alla run successiva ricevi "▶️
Interruttore di emergenza su ON…" e il bot torna a funzionare come prima.

## Metodo 2 — chiedi a Claude

Scrivi "spegni le aperture" (oppure "riaccendi le aperture") a Claude in una
sessione aperta su questo repository. Funziona **solo se hai una sessione di
Claude attiva** e Claude può completare la modifica: in emergenza usa il Metodo 1.

## Da sapere

- **Quando ha effetto:** dalla prima run che parte dopo il commit. Le run sono
  ogni 2 ore, dalle 07 alle 23 UTC (in Italia: +2 ore d'estate, +1 d'inverno).
  Tra una run e l'altra il bot non apre comunque niente.
- **Una proposta già arrivata su Telegram** non viene cancellata
  dall'interruttore: se non la vuoi, rifiutala.
- **Se sbagli a scrivere** (qualunque cosa diversa da `ON`), il bot resta spento
  per sicurezza e te lo dice su Telegram. Per riaccendere, la prima riga deve
  essere esattamente `ON`.
- **Le righe che iniziano con `#`** sono spiegazioni: lasciale come sono.
- **Se alla run successiva il messaggio non arriva**, apri
  https://github.com/roger-d-ray/Liquid/blob/main/ops/new_openings.txt e
  controlla che la prima riga sia davvero cambiata, cioè che il commit sia
  andato a buon fine.
- Dettagli tecnici: `regime-training/DECISIONS.md`, sezione 15.

# Quality Assurance Report

## Checks completed

- Python compilation: all 31 project Python files compile successfully.
- SQL static validation: 295 literal SQL statements were checked against the current SQLite schema; no syntax or referenced-column errors were found by the check.
- Static name scan: no likely undefined names were found in top-level handler/service functions.
- Callback scan: no obvious unhandled literal callback payloads were found.
- Legacy migration test: the supplied original single-file database schema was created with representative users, tariffs, payment requests, promo codes, settings, raffles, participants and winners. The new initializer migrated it, retained the sample records, created the pre-upgrade backup, passed SQLite `quick_check`, and remained idempotent on a second initialization.
- Internal-balance checkout test: the balance debit, VIP expiry, purchase count, approved order and transaction log were verified together in SQLite.
- Referral cashback test: repeated processing of the same approved order credited cashback only once.

## Checks requiring deployment credentials

A real Telegram API session, private-channel permissions, real CryptoBot credentials and the hosting provider's persistent filesystem cannot be verified without the owner's bot token, channel setup and payment credentials. Before opening the bot to users, complete the deployment checklist in `README.md` and test `/start`, mandatory subscription (if enabled), manual receipt approval, private invite creation, VIP expiry and CryptoBot payment (if enabled).

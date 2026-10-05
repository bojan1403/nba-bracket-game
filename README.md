# NBA Standings Predictions

Players register, rank all 30 NBA teams (1 to 15 in each conference), and compare against the live standings and each other.
Stack: Flask (Python) in a Docker container on Azure App Service, Azure SQL, GitHub Actions for CI/CD.

## User flow
- Not logged in: register / login page (with "Forgot your password?" and "Resend confirmation").
- Registration sends a confirmation email; the account works only after the link is clicked (valid 24 h). The link logs the user in and leads to the form.
- Password reset: emailed link, valid 1 hour, single use (it embeds the tail of the current password hash, so it dies once the password changes). Reset also confirms the email.
- Both forms answer identically whether or not the address exists, and each address gets at most one email per 60 s (`users.last_mail`).
- Login lockout: 5 failed logins in a row lock that account for 15 minutes (`LOGIN_MAX_FAILS`, `LOCKOUT_MINUTES`); the password isn't even checked while locked. A correct password or a password reset clears the counter. Counters live in Azure SQL (`failed_logins`, `locked_until`), so they are shared by all workers and instances.
- First login or registration: prediction form (15 dropdowns per conference, each team once).
- Every later login: standings page (current NBA standings + leaderboard). Picks are final once submitted; "My picks" shows them read-only.
- Users without saved picks are redirected to the form.

## Repo layout
| Path | Purpose |
|---|---|
| `app.py` | The whole web app (routes, templates, Azure SQL access, standings refresh endpoint) |
| `requirements.txt` | flask, gunicorn, pyodbc |
| `TESTING.md` | End-to-end test checklist to run after the first deploy |
| `Dockerfile`, `.dockerignore` | Python 3.12 slim + Microsoft ODBC Driver 18, gunicorn (2 workers, 120 s timeout) on port 8000 |
| `.github/workflows/deploy.yml` | On push to `main`: syntax check, build image, push to ACR (tag = commit SHA), point the web app at it |
| `.github/workflows/refresh-standings.yml` | Option A for the daily refresh: scheduled curl (09:00 UTC) |
| `functions/refresh-standings/` + `.github/workflows/deploy-function.yml` | Option B: timer-triggered Azure Function (recommended if you want it independent of GitHub) |
| `webjobs/` | Option C: triggered WebJob (zip for portal upload). Container persistence unverified |

Use ONE of the three refresh options and delete the others.

## Configuration

### Web app, App Settings
| Setting | Value |
|---|---|
| `SECRET_KEY` | long random string, must stay stable (app refuses to start on Azure without it) |
| `AZURE_SQL_CONN` | `Driver={ODBC Driver 18 for SQL Server};Server=tcp:<server>.database.windows.net,1433;Database=<db>;Uid=<user>;Pwd=<password>;Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60;` |
| `STANDINGS_API_KEY` | key for the standings provider (API-Sports in the code) |
| `REFRESH_TOKEN` | long random string protecting `/internal/refresh-standings` |
| `BASE_URL` | public https URL, e.g. `https://<app>.azurewebsites.net`; used for links in emails (required on Azure) |
| `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASS`, `MAIL_FROM` | outgoing mail, STARTTLS (required on Azure; unset locally = emails are printed to the log) |
| `SEASON_START` | opening tip-off in UTC, e.g. `2026-10-20T23:00:00Z` (check the official schedule). Unset = no lock, no penalty |
| `WEBSITES_PORT` | `8000` |
| `WEBSITES_ENABLE_APP_SERVICE_STORAGE` | `true` only if you use the WebJob option |
Also: Always On, HTTPS Only, B1 plan or higher. Managed identity + `acrUseManagedIdentityCreds=true` + AcrPull role for pulling from ACR.

### Azure SQL
Create server + database (serverless may pause; hence the long connection timeout and gunicorn timeout).
Enable "Allow Azure services and resources to access this server". Tables are created automatically on first request.
Tables that already exist need: `ALTER TABLE dbo.users ADD verified BIT NOT NULL CONSTRAINT DF_users_verified DEFAULT 1, last_mail DATETIME2 NULL, failed_logins INT NOT NULL CONSTRAINT DF_users_failed DEFAULT 0, locked_until DATETIME2 NULL` (existing users count as confirmed).
If tables existed before the `name` column and `standings` table were added: `ALTER TABLE dbo.users ADD name NVARCHAR(40) NOT NULL DEFAULT ''`.

### GitHub
- Secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` (OIDC login), `REFRESH_TOKEN` (option A only).
- Variables: `AZURE_WEBAPP_NAME`, `ACR_NAME` (registry name without `.azurecr.io`), `SITE_URL` (option A), `AZURE_FUNCTION_NAME` (option B).
- The OIDC identity needs: Website Contributor on the web app (and Function App), AcrPush on the registry.

### Daily refresh options
- A: GitHub Actions cron. Simple; scheduled runs may be delayed, and public repos get them disabled after 60 days of inactivity.
- B: Azure Function (Python, timer `0 0 9 * * *`). Needs `SITE_URL` and `REFRESH_TOKEN` in the Function App settings.
- C: WebJob: upload `webjobs/refresh-standings.zip` in the portal, Always On, verify it survives an app restart.

## Not yet verified (nothing here has been run against Azure)
- `fetch_standings()` in `app.py` parses the API-Sports format from memory; check one real response, including that team names match the dropdown names (for example "LA Clippers").
- Standings stay empty until the season's games start.
- WebJob persistence in a custom container (option C).

## Scoring (per team, both conferences)
Spots 1-10 are the playoff/play-in zone. A team counts for the zone it was predicted in; an exact spot replaces the zone points (they don't add up).
| Predicted in | Finishes at the exact spot | Finishes elsewhere in the same zone | Otherwise |
|---|---|---|---|
| 1-10 | 6 | 3 | 0 |
| 11-15 | 3 | 1 | 0 |
Maximum 75 per conference, 150 total. Implemented in `score_conference()` in `app.py`. The refresh endpoint rejects data whose team names differ from the dropdown names, because scoring matches by name.

## Lock date and late penalty
- One submission per player, no edits (INSERT only; the `email` primary key enforces it).
- New picks are accepted until `LOCK_DAYS` (5) days after `SEASON_START`; after that the form is closed. Users who never submitted can still view standings.
- Each started day between the season start and the submission time costs 1 point (0 before the start, 1 within the first 24 h, ... max 5). Scores are not floored at 0.
- The leaderboard shows the penalty in its own column. Logic: `days_late()` and `is_locked()` in `app.py`.

## Known gaps / next steps
- The login lockout is per account only: someone can lock a victim out for 15 minutes on purpose (a password reset unlocks), and one password tried across many accounts isn't caught (that would need a per-IP limit). Email deliverability needs SPF/DKIM on the sending domain; Azure Communication Services Email offers SMTP (check its docs for the credentials, which come from an Entra app), or use any SMTP provider.
- Possible hardening: connect to Azure SQL with the web app's managed identity instead of a password; Key Vault references for secrets; branch protection on `main`.

# End-to-end test checklist

Run this once after the first deploy, in order. Use two or three throwaway email addresses (`test1@…`, `test2@…`, `test3@…`).
Nothing in the app has been run against Azure yet, so expect to fix something in sections 0 to 2.

Tip: changing App Settings restarts the app. Tail **Log stream** in the portal while testing.

## 0. Deployment
- [ ] GitHub Action "Build and deploy container" is green; the web app shows the new image tag (the commit SHA).
- [ ] App starts: Log stream shows gunicorn listening on 8000 (no `RuntimeError` about SECRET_KEY, BASE_URL or SMTP).
- [ ] `https://<app>/` loads; plain `http://` redirects to https (HTTPS Only).
- [ ] First request creates the tables. In Azure SQL: `SELECT name FROM sys.tables;` shows `users`, `predictions`, `standings`.
- [ ] Browser dev tools: the session cookie is marked `Secure` and `HttpOnly`.

## 1. Registration and email confirmation
- [ ] Register `test1`. You land on the home page with "Almost there! We sent a confirmation link…".
- [ ] The email arrives (check spam; if it does, fix SPF/DKIM before launch). The link starts with your `BASE_URL`.
- [ ] Logging in BEFORE confirming is refused with "Please confirm your email first".
- [ ] "Didn't get the confirmation email?" sends a new link; pressing it twice within 60 s sends only one email.
- [ ] Clicking the link logs you in and shows the prediction form (first login goes to the form).
- [ ] A tampered link (change a character) says "invalid or expired".
- [ ] Registering the same email again says it is already registered.

## 2. Standings refresh (before you rely on scoring)
- [ ] `curl -X POST https://<app>/internal/refresh-standings` (no token) returns **403**.
- [ ] Same with `-H "Authorization: Bearer <REFRESH_TOKEN>"` returns `{"ok": true}`, or an error you can read:
  - 502 "Provider team names differ…" lists the mismatching names: fix the names in `EAST`/`WEST` in `app.py` (or map them in `fetch_standings()`).
  - 500: read the Log stream (wrong API key, wrong season, different response format than `fetch_standings()` expects).
- [ ] After a success, `SELECT * FROM standings ORDER BY conf, pos;` has 15 rows per conference and the standings page shows them with an "updated" time.
- [ ] Your chosen scheduler (Function, WebJob or GitHub cron) runs manually once and succeeds; check again after the first scheduled time.
- Before the season starts the provider may return empty or all 0-0 data. That's normal.

## 3. Predictions and one-time submission
- [ ] Each dropdown greys out teams already chosen in that conference.
- [ ] Submit asks for confirmation; after confirming you land on the standings page with "Submitted! Your picks are final."
- [ ] Logging out and in again goes straight to the standings page (later logins).
- [ ] "My picks" shows the picks read-only with the submission time and no submit button.
- [ ] Double submit: open the form in two tabs and submit both; the second one is refused with "already submitted". Check `SELECT COUNT(*) FROM predictions WHERE email='test1@…';` is 1.
- [ ] A user who registers but doesn't submit is sent to the form on every login until they do.

## 4. Scoring (use fake standings)
Create three players with these picks. `test1`: any valid order. `test2`: the same as test1 but East positions 1 and 2 swapped. `test3`: the same as test1 but East positions 10 and 11 swapped.

Load standings that equal test1's picks (this overwrites the real standings; the next refresh restores them):
```sql
DELETE FROM standings;
INSERT INTO standings(conf, pos, team, wins, losses)
SELECT 'E', CAST([key] AS INT) + 1, value, 0, 0 FROM predictions p CROSS APPLY OPENJSON(p.east) WHERE p.email = 'test1@example.com';
INSERT INTO standings(conf, pos, team, wins, losses)
SELECT 'W', CAST([key] AS INT) + 1, value, 0, 0 FROM predictions p CROSS APPLY OPENJSON(p.west) WHERE p.email = 'test1@example.com';
```
Expected leaderboard:
- [ ] `test1` = **150** (10 x 6 + 5 x 3 in each conference).
- [ ] `test2` = **144** (two top-10 teams each drop from 6 to 3).
- [ ] `test3` = **141** (the team predicted 10th drops from 6 to 0, the team predicted 11th from 3 to 0).
- [ ] Order on the page is test1, test2, test3, and the page shows the scoring rules line.

## 5. Late penalty and lock date
Change `SEASON_START` in App Settings (the app restarts). Penalty uses each player's submission time.
- [ ] Set it to 30 hours ago and submit with a new player: the leaderboard shows a penalty of 2 for that player; the form said "Submitting now is 2 day(s) late" before submitting.
- [ ] Set it 6 days ago: the form is closed ("Picks are closed"), POST is refused, a user without picks can still open the standings page and sees the "not on the leaderboard" message.
- [ ] Set it to a future date: no penalty, no lock.
- [ ] Put the real `SEASON_START` back when done (UTC, e.g. `2026-10-20T23:00:00Z`).

## 6. Login lockout
- [ ] 5 wrong passwords in a row for one account: the 5th shows "locked for 15 minutes".
- [ ] The correct password during the lock is also refused, with the minutes remaining.
- [ ] Unlock quickly with SQL: `UPDATE users SET failed_logins=0, locked_until=NULL WHERE email='test1@example.com';` or use password reset (it unlocks too).
- [ ] 4 wrong passwords followed by the correct one works, and the counter is back to 0 (`SELECT failed_logins FROM users`).
- [ ] An unknown email just says "Wrong email or password" and never locks anything.

## 7. Password reset
- [ ] "Forgot your password?" with a real address sends an email; the on-screen message is identical for an unknown address.
- [ ] The link opens the new-password page; setting a password logs nothing in, but the new password works and the old one doesn't.
- [ ] Opening the same link a second time says "invalid or expired" (single use).
- [ ] A second request within 60 s doesn't send a second email.
- [ ] An unconfirmed account that completes a reset can log in afterwards.

## 8. Clean-up before announcing the site
- [ ] Delete test rows: `DELETE FROM predictions WHERE email LIKE 'test%@%'; DELETE FROM users WHERE email LIKE 'test%@%';`
- [ ] Real `SEASON_START` is set; the real standings refresh has run since the fake data (check the "updated" time).
- [ ] Only one daily-refresh option is active (delete the other workflows or the WebJob).
- [ ] `SECRET_KEY`, `REFRESH_TOKEN` and the SQL password exist only in App Settings, not in the repo.
- [ ] Application Insights or the Log stream shows no recurring errors after a day of normal use.

# Emails captured by the Mailpit stub (redacted summary)

The full bodies were removed from the repo (they carried a verification token and ticket link from the local run).
Every token, URL and ID is replaced by a `<placeholder>`. Counts are for the final run of `harness/drive.py`.

| Subject | Recipient role | Count | Carried | Dashboard button (at capture time) |
|---|---|---|---|---|
| New Support Ticket: <ticket-number> (<priority>) | tenant admin (registration) | 1 | - | none |
| New meeting booked | rep (notification_email) | 1 | - | '#' |
| New quote generated | rep (notification_email) | 3 | PDF attachment | '#' |
| New sales-ready lead | lite-tenant rep | 1 | - | '#' |
| New sales-ready lead | rep (notification_email) | 3 | - | '#' |
| Verify your email address | tenant admin (registration) | 3 | - | <url> |
| Your meeting is booked | visitor (lead email) | 1 | - | none |

Rep alert emails (`New sales-ready lead`, `New quote generated`, `New meeting booked`) had a `View in Dashboard` button whose href was `#` in this run (defect 3, fixed in PR #194).
The quote email's PDF attachment is `../quote-attachment.pdf` (name in the email: `quote-<quote-id>.pdf`).

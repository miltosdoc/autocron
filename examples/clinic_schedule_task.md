# Clinic Schedule Check

## Goal
Check the clinic's weekly schedule from the hospital's web portal and send
a summary to Telegram every weekday morning.

## Task
Write a bash script that:
1. Uses `curl` to fetch the schedule page from the clinic's internal portal.
2. Parses the HTML to extract today's appointments, room assignments, and staff.
3. Formats the output as a clean text summary with time, patient count, and notes.
4. Sends the summary to stdout (CoPaw will route it to Telegram).
5. Handles connection failures gracefully with retry logic (max 3 attempts).
6. Logs each execution with timestamp to a log file.

## Schedule
```
30 6 * * 1-5
```
(6:30 AM, Monday through Friday)

## Success Criteria
- Script exits 0 when schedule is fetched successfully.
- Script exits 0 with a "no appointments" message on empty days.
- Script exits 1 only on unrecoverable errors (after retries).
- Output is readable plain text, not raw HTML.

## Environment
- `curl` and `grep`/`sed`/`awk` available at standard paths.
- No browser or JavaScript execution — HTML parsing only.
- Portal URL should be configurable via environment variable `CLINIC_URL`.

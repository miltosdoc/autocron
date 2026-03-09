# Board Meeting & Governance Calendar

## Goal
Automated reminders for recurring corporate governance tasks throughout the year.
Send reminders to Telegram at appropriate lead times before each event.

## Task
Write a bash script that:
1. Defines a calendar of recurring governance events:
   - Quarterly board meetings (last Friday of Mar/Jun/Sep/Dec) — 2 weeks notice
   - Annual financial audit preparation (February) — 1 month notice
   - Annual general meeting / stämma (May) — 3 weeks notice
   - Monthly team review meetings (first Monday of each month) — 3 days notice
   - Tax filing deadlines (quarterly) — 1 week notice
2. Checks today's date against the calendar with lead-time offsets.
3. For each upcoming event within its reminder window, outputs a reminder message.
4. Includes: event name, date, days until event, and any preparation notes.
5. Outputs nothing (exit 0) if no reminders are due today.

## Schedule
```
0 8 * * 1-5
```
(8:00 AM, weekdays only)

## Success Criteria
- Script exits 0 always (no reminders = success too).
- Reminders trigger at the correct lead time before each event.
- Date calculations handle month boundaries and leap years correctly.
- Uses `date` command for date arithmetic, no external dependencies.

## Environment
- GNU `date` or BSD `date` (script should handle both).
- Calendar data embedded in the script (no external files needed).

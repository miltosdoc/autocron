# Disk Usage Monitor

## Goal
Monitor disk usage and alert when any partition exceeds 85% capacity.

## Task
Write a bash script that:
1. Checks disk usage on all mounted partitions using `df`.
2. Identifies partitions above 85% usage.
3. Formats an alert with: mount point, usage %, available space, total size.
4. Outputs the alert to stdout (for CoPaw/Telegram delivery).
5. Outputs nothing if all partitions are healthy (below threshold).
6. Logs each check with timestamp.

## Schedule
```
0 */6 * * *
```
(Every 6 hours)

## Success Criteria
- Script exits 0 always (no alert = success).
- Correctly parses df output regardless of filesystem type.
- Threshold is configurable via DISK_ALERT_THRESHOLD env var (default 85).
- Alert message is clear and actionable.

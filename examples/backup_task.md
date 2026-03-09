# Backup Task

## Goal
Backup the /var/www directory with daily rotation, keeping the last 7 backups.

## Task
Write a bash script that:
1. Creates a timestamped tar.gz backup of /var/www to /backup/www/.
2. Removes backups older than 7 days.
3. Verifies the backup archive is valid (tar -tzf).
4. Logs the result with timestamp, backup size, and file count.
5. Exits with appropriate status code.

## Schedule
```
0 3 * * *
```
(3:00 AM daily)

## Success Criteria
- Backup file exists and is a valid tar.gz.
- Old backups (>7 days) are removed.
- Script exits 0 on success, 1 on any failure.
- Log file shows timestamp, size, and status for each run.

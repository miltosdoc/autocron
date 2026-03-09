# Research Monitor — Weekly MS Research Digest

## Goal
Monitor PubMed and relevant RSS feeds for new multiple sclerosis (MS) research
articles. Maintain a local library of found articles and send a weekly digest.

## Task
Write a bash script that:
1. Queries PubMed's E-utilities API for recent MS research articles
   (last 7 days, keywords: "multiple sclerosis", "MS treatment", "MS pathology").
2. For each new article, extracts: title, authors, journal, DOI, abstract snippet.
3. Checks against a local "seen articles" file to avoid duplicates.
4. Appends new articles to a JSONL library file (`~/.autocron/research/ms_library.jsonl`).
5. Formats a digest with article count, top 5 titles, and links.
6. Outputs the digest to stdout (CoPaw routes to Telegram).

## Schedule
```
0 8 * * 1
```
(8:00 AM every Monday)

## Success Criteria
- Script exits 0 when query succeeds (even if no new articles found).
- Library file grows over time without duplicates.
- Digest is concise and readable.
- Works without any API key (PubMed E-utilities are free).

## Environment
- `curl` and `jq` available.
- Internet access for PubMed API calls.
- Library stored in `~/.autocron/research/`.

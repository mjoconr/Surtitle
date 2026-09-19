---
kind: openq
order: 1
status: open
---
**Batching**: the ingest batch size is 250 while export is 100, and nobody
remembers why. Until that is settled, a batch of 250 is sent to export as one
request and the far end truncates it. Decide whether the two should match, or
whether export really is the smaller unit.

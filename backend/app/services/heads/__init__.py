"""Head launcher — backend half (docs/specs/head-launcher.md).

A head is one short-lived harness process for one job. The backend only
writes a run folder + a spool request and reads the files the host wrapper
(``scripts/head/mc-head``) writes back. No DB table: state lives in files
(spec §6.1); the task card is mirrored with existing status values.
"""

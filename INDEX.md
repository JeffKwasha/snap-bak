# INDEX

Requirement taxonomy. This file is an index, not a second copy: the requirement
text lives in the document named on its row. The document carries the same ID at
the statement.

To read a requirement, take the ID from the test or function, find its row here,
then grep the ID in the named file (or let QMD find the snippet).

Conventions:

- IDs are `R-<AREA>-<NN>` and stable. Never renumber; retire an ID rather than
  reuse it.
- The document marks each requirement with the ID at the statement:
  `[[R-LOCK-02]] Both transitions log...`.
- A test or function cites every ID it covers on its definition line:
  `def f(...):  # [[R-AREA-NN]] [[R-OTHER-NN]]`. A multi-line signature puts them
  on the `def name(` line, so name, IDs and line number share one grep line.
- The mapping is many-to-many: a requirement may have several tests, and one
  test may cite several requirements.
- Tests point at the docs. This file does not track which tests exist, and a
  missing test is not detected here.

| ID | Document | Section |
|---|---|---|
| [[R-DEPLOY-01]] | SPEC.md | Deployment Contract |
| [[R-DEPLOY-02]] | SPEC.md | Deployment Contract |
| [[R-DEPLOY-03]] | SPEC.md | Deployment Contract |
| [[R-DEPLOY-04]] | SPEC.md | Deployment Contract |
| [[R-DEPLOY-05]] | SPEC.md | Deployment Contract |
| [[R-DEPLOY-06]] | SPEC.md | Deployment Contract |
| [[R-DEPLOY-07]] | SPEC.md | Deployment Contract |
| [[R-LOCK-01]] | SPEC.md | Global Lock |
| [[R-LOCK-02]] | SPEC.md | Global Lock |
| [[R-LOCK-03]] | SPEC.md | Global Lock |
| [[R-LOCK-04]] | SPEC.md | Global Lock |
| [[R-LOCK-05]] | SPEC.md | Global Lock |
| [[R-LOCK-06]] | SPEC.md | Global Lock |
| [[R-STATE-01]] | SPEC.md | State & Runtime Directories |
| [[R-STATE-02]] | SPEC.md | State & Runtime Directories |
| [[R-STATE-03]] | SPEC.md | State & Runtime Directories |
| [[R-STATE-04]] | SPEC.md | State & Runtime Directories |
| [[R-STATE-05]] | SPEC.md | State & Runtime Directories |
| [[R-STATE-06]] | SPEC.md | State & Runtime Directories |
| [[R-CONFIG-01]] | SPEC.md | Configuration |
| [[R-CONFIG-02]] | SPEC.md | Configuration |
| [[R-CONFIG-03]] | SPEC.md | Configuration |
| [[R-CONFIG-04]] | SPEC.md | Configuration |
| [[R-CONFIG-05]] | SPEC.md | Configuration |
| [[R-CONFIG-06]] | SPEC.md | Configuration |
| [[R-REPO-01]] | SPEC.md | Configuration, Repo Schema |
| [[R-REPO-02]] | SPEC.md | Configuration, Repo Schema |
| [[R-REPO-03]] | SPEC.md | Configuration, Repo Schema |
| [[R-REPO-04]] | SPEC.md | Configuration, Repo Schema |
| [[R-REPO-05]] | SPEC.md | Configuration, Repo Schema |
| [[R-REPO-06]] | SPEC.md | Configuration, Repo Schema |
| [[R-REPO-07]] | SPEC.md | Configuration, Repo Schema |
| [[R-JOB-01]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-02]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-03]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-04]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-05]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-06]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-07]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-08]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-09]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-10]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-11]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-12]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-13]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-14]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-15]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-16]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-17]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-18]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-19]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-20]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-21]] | SPEC.md | Configuration, Job Schema |
| [[R-STATUS-01]] | SPEC.md | Status File |
| [[R-STATUS-02]] | SPEC.md | Status File |
| [[R-STATUS-03]] | SPEC.md | Status File |
| [[R-STATUS-04]] | SPEC.md | Status File |
| [[R-STATUS-05]] | SPEC.md | Status File |
| [[R-STATUS-06]] | SPEC.md | Status File |
| [[R-STATUS-07]] | SPEC.md | Status File |
| [[R-STATUS-08]] | SPEC.md | Status File |
| [[R-STATUS-09]] | SPEC.md | Status File |
| [[R-ERROR-01]] | SPEC.md | Error Codes |
| [[R-ERROR-02]] | SPEC.md | Error Codes |
| [[R-ERROR-03]] | SPEC.md | Error Codes |
| [[R-ERROR-04]] | SPEC.md | Error Codes |
| [[R-ERROR-05]] | SPEC.md | Error Codes |
| [[R-ERROR-06]] | SPEC.md | Error Codes |
| [[R-ERROR-07]] | SPEC.md | Error Codes |
| [[R-ERROR-08]] | SPEC.md | Error Codes |
| [[R-ERROR-09]] | SPEC.md | Error Codes |
| [[R-ERROR-10]] | SPEC.md | Error Codes |
| [[R-ERROR-11]] | SPEC.md | Error Codes |
| [[R-ERROR-12]] | SPEC.md | Error Codes |
| [[R-ERROR-13]] | SPEC.md | Error Codes |
| [[R-ERROR-14]] | SPEC.md | Error Codes |
| [[R-DELAY-01]] | SPEC.md | Delayed Job Handling |
| [[R-DELAY-02]] | SPEC.md | Delayed Job Handling |
| [[R-DELAY-03]] | SPEC.md | Delayed Job Handling |
| [[R-DELAY-04]] | SPEC.md | Delayed Job Handling |
| [[R-DELAY-05]] | SPEC.md | Delayed Job Handling |
| [[R-DELAY-06]] | SPEC.md | Delayed Job Handling |
| [[R-DELAY-07]] | SPEC.md | Delayed Job Handling |
| [[R-DELAY-08]] | SPEC.md | Delayed Job Handling |
| [[R-DELAY-09]] | SPEC.md | Delayed Job Handling |
| [[R-DELAY-10]] | SPEC.md | Delayed Job Handling |
| [[R-DELAY-11]] | SPEC.md | Delayed Job Handling |
| [[R-DELAY-12]] | SPEC.md | Delayed Job Handling |
| [[R-PRIORITY-01]] | SPEC.md | Priority System |
| [[R-PRIORITY-02]] | SPEC.md | Priority System |
| [[R-PRIORITY-03]] | SPEC.md | Priority System |
| [[R-PRIORITY-04]] | SPEC.md | Priority System |
| [[R-SNAP-01]] | SPEC.md | Snapshot Naming |
| [[R-SNAP-02]] | SPEC.md | Snapshot Naming |
| [[R-SNAP-03]] | SPEC.md | Snapshot Naming |
| [[R-SNAP-04]] | SPEC.md | Snapshot Naming |
| [[R-SNAP-05]] | SPEC.md | Snapshot Naming |
| [[R-SNAP-06]] | SPEC.md | Snapshot Naming |
| [[R-WINDOW-01]] | SPEC.md | Backup Window |
| [[R-WINDOW-02]] | SPEC.md | Backup Window |
| [[R-WINDOW-03]] | SPEC.md | Backup Window |
| [[R-WINDOW-04]] | SPEC.md | Backup Window |
| [[R-WINDOW-05]] | SPEC.md | Backup Window |
| [[R-EXCL-01]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-02]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-03]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-04]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-05]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-06]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-07]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-08]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-09]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-10]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-11]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-12]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-13]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-14]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-15]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-16]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-17]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-18]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-19]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-20]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-21]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-22]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-23]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-24]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-25]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-26]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-27]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-28]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-29]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-30]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-31]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-32]] | SPEC.md | Excludes: Inspection & Application |

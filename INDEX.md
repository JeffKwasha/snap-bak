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
| [[R-DEPLOY-08]] | SPEC.md | Deployment Contract |
| [[R-DEPLOY-09]] | SPEC.md | Deployment Contract |
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
| [[R-STATE-06]] | SPEC.md | State & Runtime Directories |
| [[R-STATE-07]] | SPEC.md | State & Runtime Directories |
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
| [[R-EXCL-33]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-34]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-35]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-36]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-37]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-38]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-39]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-40]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-41]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-42]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-43]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-44]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-45]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-46]] | SPEC.md | Excludes: Inspection & Application |
| [[R-EXCL-47]] | SPEC.md | Excludes: Inspection & Application |
| [[R-APPLY-01]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-02]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-03]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-04]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-05]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-06]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-07]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-08]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-09]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-10]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-11]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-12]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-13]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-14]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-15]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-16]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-17]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-18]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-19]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-20]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-21]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-APPLY-22]] | SPEC.md | `apply excludes`, Arming a scheduled cleanup |
| [[R-GATE-01]] | SPEC.md | Run-time gate |
| [[R-GATE-02]] | SPEC.md | Run-time gate |
| [[R-GATE-03]] | SPEC.md | Run-time gate |
| [[R-GATE-04]] | SPEC.md | Run-time gate |
| [[R-GATE-05]] | SPEC.md | Run-time gate |
| [[R-GATE-06]] | SPEC.md | Run-time gate |
| [[R-GATE-07]] | SPEC.md | Run-time gate |
| [[R-GATE-08]] | SPEC.md | Run-time gate |
| [[R-GATE-09]] | SPEC.md | Run-time gate |
| [[R-GATE-10]] | SPEC.md | Run-time gate |
| [[R-GATE-11]] | SPEC.md | Run-time gate |
| [[R-GATE-12]] | SPEC.md | Run-time gate |
| [[R-GATE-13]] | SPEC.md | Run-time gate |
| [[R-GATE-14]] | SPEC.md | Run-time gate |
| [[R-GATE-15]] | SPEC.md | Run-time gate |
| [[R-EXCL-48]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-49]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-50]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-51]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-52]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-53]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-54]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-55]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-56]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-57]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-58]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-59]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-60]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-61]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-62]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-63]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-64]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-65]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-66]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-67]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-EXCL-68]] | SPEC.md | The report, What counts as a file, -v, Output formats |
| [[R-THROUGHPUT-01]] | SPEC.md | Throughput History |
| [[R-THROUGHPUT-02]] | SPEC.md | Throughput History |
| [[R-CLI-01]] | SPEC.md | CLI |
| [[R-CLI-02]] | SPEC.md | CLI |
| [[R-CLI-03]] | SPEC.md | CLI |
| [[R-CLI-04]] | SPEC.md | CLI |
| [[R-CLI-05]] | SPEC.md | CLI |
| [[R-CLI-06]] | SPEC.md | CLI |
| [[R-CLI-07]] | SPEC.md | CLI |
| [[R-CLI-08]] | SPEC.md | CLI |
| [[R-CLI-09]] | SPEC.md | CLI |
| [[R-CLI-10]] | SPEC.md | CLI |
| [[R-CLI-11]] | SPEC.md | CLI |
| [[R-CLI-12]] | SPEC.md | CLI |
| [[R-CLI-13]] | SPEC.md | CLI |
| [[R-CLI-14]] | SPEC.md | CLI |
| [[R-CLI-15]] | SPEC.md | CLI |
| [[R-CLI-16]] | SPEC.md | CLI |
| [[R-CLI-17]] | SPEC.md | CLI |
| [[R-CLI-18]] | SPEC.md | CLI |
| [[R-CLI-19]] | SPEC.md | CLI |
| [[R-CLI-20]] | SPEC.md | CLI |
| [[R-CLI-21]] | SPEC.md | CLI |
| [[R-CLI-22]] | SPEC.md | CLI |
| [[R-CLI-23]] | SPEC.md | CLI |
| [[R-CLI-24]] | SPEC.md | CLI |
| [[R-CLI-25]] | SPEC.md | CLI |
| [[R-CLI-26]] | SPEC.md | CLI |
| [[R-CLI-27]] | SPEC.md | CLI |
| [[R-CLI-28]] | SPEC.md | CLI |
| [[R-CLI-29]] | SPEC.md | CLI |
| [[R-CLI-30]] | SPEC.md | CLI |
| [[R-ENV-01]] | SPEC.md | CLI, Environment Variables |
| [[R-ENV-02]] | SPEC.md | CLI, Environment Variables |
| [[R-ENV-03]] | SPEC.md | CLI, Environment Variables |
| [[R-ENV-04]] | SPEC.md | CLI, Environment Variables |
| [[R-ENV-05]] | SPEC.md | CLI, Environment Variables |
| [[R-ENV-06]] | SPEC.md | CLI, Environment Variables |
| [[R-PRIV-01]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-02]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-03]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-04]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-05]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-06]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-07]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-08]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-09]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-10]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-11]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-12]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-13]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-14]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-15]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-16]] | SPEC.md | Privileged Operations & Security |
| [[R-PRIV-17]] | SPEC.md | Privileged Operations & Security |
| [[R-FLOW-01]] | SPEC.md | Execution Flow |
| [[R-FLOW-02]] | SPEC.md | Execution Flow |
| [[R-FLOW-03]] | SPEC.md | Execution Flow |
| [[R-FLOW-04]] | SPEC.md | Execution Flow |
| [[R-FLOW-05]] | SPEC.md | Execution Flow |
| [[R-FLOW-06]] | SPEC.md | Execution Flow |
| [[R-FLOW-07]] | SPEC.md | Execution Flow |
| [[R-FLOW-08]] | SPEC.md | Execution Flow |
| [[R-FLOW-09]] | SPEC.md | Execution Flow |
| [[R-FLOW-10]] | SPEC.md | Execution Flow |
| [[R-FLOW-11]] | SPEC.md | Execution Flow |
| [[R-GLOBAL-01]] | SPEC.md | Overview, Constraints |
| [[R-TIME-01]] | SPEC.md | Overview, Constraints |

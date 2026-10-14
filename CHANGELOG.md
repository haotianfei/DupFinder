# 更新日志

本文件记录本项目的所有重要变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

版本策略：1.0 之前的发布允许调整命令行界面；新增兼容行为升小版本，缺陷修复升
补丁版本；改变默认值或匹配语义的条目会在正文中写明。

---

# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

Version policy: pre-1.0 releases may adjust the command-line surface; a new
minor version adds backwards-compatible behavior, a patch version fixes
defects, and anything that changes documented defaults or matching semantics
is announced in the entry body.

---

## [0.2.0] - 2026-10-14

### 新增

- 异名重复门禁（renamed-dupes gate，默认开启）：处理队列最终确定之后，候选与
  组内保留项 basename 不同时移出执行队列、写入待审清单，绝不自动删除/移动；
  同名重复（跨目录同步副本）照常处理。动机：内容相同而文件名不同的两个文件
  可能各有引用语义（配置路径、应用绑定），"内容重复"≠"引用冗余"。
- `renamed_pending_<YYYYmmdd_HHMMSS_ffffff>.txt` 待审清单：与结果 JSON 同目录，
  纯文本 TSV 标记格式（**opt-out**：行默认预标记 `x`=拟执行；保护=改成空格或
  `-`、或直接删行；`!`=病态路径行永不执行，未知标记/缺列=坏行告警剔除），列为
  标记<TAB>候选绝对路径<TAB>保留绝对路径<TAB>sha256<TAB>size，按组聚集并带
  `# kept:` 注释头；排序稳定（重跑逐字节一致，diff 友好），上千条场景可用
  sed/vim 批量保护。`--try-run` 下照常写出（审查产物豁免）；无异名项时不产生
  文件。
- body-sha256 未经审阅拒绝门：清单头部记录 `# body-sha256: <hex>`，正文=文件
  中所有不以 `#` 开头的行（含空行）按原始字节以 `\n` 连接计算 sha256。
  `--approve` 启动先重算比对：一致=从生成起未被人工编辑（只改注释不解锁）→
  拒绝 rc1 零动作并提示先审阅或使用 `--approve-all`；头部缺该记录的文件
  （手工伪造/损坏）同样拒绝。此门承担"误跑未编辑清单"的安全底线，使默认方向
  可以安全地取 `x`（opt-out 批量更方便）。
- `--approve PATH`：审批执行通道（代码形状对齐 `--undo`，与
  `-d/--undo/--duplicates-result-file` 互斥，rc2）。只执行行首恰为 `x` 的行；
  每行执行前四项校验：候选存在且 size 与记录一致（skipped_gone）、mtime 较
  扫描时点变化仅告警（warn_modified，不拦截）、内容摘要与记录一致——经哈希
  缓存命中、miss 现算（skipped_mismatch）、保留项仍在位（skipped_kept_gone）；
  通过后走主流程同款 delete/move 通道（回收目录结构、同盘/异盘空间检查一致），
  move 逐条写标准 `moves_*.jsonl` 清单 → `--undo` 天然回滚；收尾打印
  executed/skipped/mismatch/total 计数（恒自洽）。
- `--approve-all`：`--approve` 的零编辑逃逸开关——跳过 body-sha256 审阅门，
  执行清单中全部 `x` 行（显式给出该参数本身即人工审阅声明）；被保护、被删与
  `!` 行依然永不执行。须与 `--approve` 连用，单独使用报 rc2。
- `--renamed-pending-prefix PATH`：待审清单输出目录/文件名前缀。
- `--no-gate-renamed`：关闭门禁，恢复"全部入选候选照常处理"的旧行为。
- `tests/test_renamed_gate.py`：门禁与审批闭环 23 个端到端用例（判定、清单
  格式与 opt-out 默认标记、body-sha256 重算与拒绝门三态、保护/删行/
  --approve-all、审批校验分桶、病态路径、互斥、manifest→undo 全链路）。

### 变更

- **破坏性（默认值）：** 异名重复候选默认不再被自动删除/移动——此前一切入选
  候选直接执行，现在 basename 与保留项不同者一律进入待审清单，须经人工审阅
  （改标记保护要留的文件/删行，或直接 `--approve-all` 全批）并 `--approve`
  方可执行（文件安全优先，安全底线由 body-sha256 未经审阅拒绝门承担）。
  保持旧行为请显式传 `--no-gate-renamed`。
- `--export-candidates` 导出的仍是完整选择队列（不受门禁截留影响）；当门禁
  开启且确有异名项被截留时，清单末尾追加一行注释说明实际处理集更小。
- 端到端测试套件扩展至 118 用例；既有依赖"异名重复被处理"断言的用例逐条
  显式传 `--no-gate-renamed`，原断言语义未弱化。

---

### Added

- Renamed-dupes gate (ON by default): once the processing queue is final, a
  candidate whose basename differs from its group's kept file leaves the
  execution queue and lands in a review file instead of being deleted or
  moved; same-name duplicates (synchronized copies) keep being processed.
  Rationale: identical content under different names can carry different
  reference semantics (config paths, application bindings) — duplicate
  content is not redundant reference.
- `renamed_pending_<YYYYmmdd_HHMMSS_ffffff>.txt` review file: written next to
  the result JSON, plain-text TSV with a leading single-character mark column
  (**opt-out**: rows come pre-marked `x` = intended for processing; protect by
  flipping the mark to ' ' or `-`, or delete the row; `!` = pathological path,
  never executed; unknown marks / wrong field count = malformed, rejected at
  parse with warning) and columns mark<TAB>candidate<TAB>kept<TAB>sha256<TAB>
  size; rows group under `# kept:` comment headers, sorting is stable (reruns
  produce byte-identical files, diff-friendly), thousands of rows are
  batch-protectable with sed/vim. It is also written under `--try-run` (review
  artifact exemption) and never created when nothing is held back.
- body-sha256 review gate: the header records `# body-sha256: <hex>` over the
  body — all lines not starting with `#` (blank lines included), raw bytes
  joined with `\n`. `--approve` recomputes first: equal means nobody edited
  the list since generation (comment-only changes do not unlock) → rc1, zero
  actions, with a hint to review or use `--approve-all`; a missing record
  (hand-forged or damaged header) is refused the same way. This file-level
  floor is what makes the default direction safely `x`.
- `--approve PATH`: the approval channel (same code shape as `--undo`,
  mutually exclusive with `-d/--undo/--duplicates-result-file`, rc2). Check 0
  is the review gate above; then only rows marked exactly `x` execute and each
  passes four checks first —
  candidate exists with the recorded size (skipped_gone), mtime drift since
  the scan only warns (warn_modified, non-blocking), content digest matches
  via the hash cache or a fresh hash (skipped_mismatch), and the kept file is
  still in place (skipped_kept_gone). Approved rows then go through the main
  delete/move machinery (identical recycle-dir structure and free-space
  guard), moves append standard `moves_*.jsonl` entries so `--undo` rolls
  them back, and an executed/skipped/mismatch/total summary is printed.
- `--approve-all`: the zero-edit escape hatch for `--approve` — skip the
  body-sha256 gate and execute every `x` row (passing the flag is itself the
  human review declaration); protected (' '/'-'), deleted and `!` rows still
  never execute. Must be combined with `--approve`; alone it errors (rc2).
- `--renamed-pending-prefix PATH`: output directory or file-name prefix for
  the review file.
- `--no-gate-renamed`: disables the gate and restores the old behavior of
  processing every selected candidate.
- `tests/test_renamed_gate.py`: 23 end-to-end cases for the gate and the
  approval loop (decision, pending format and opt-out default marks,
  body-sha256 recompute and review-gate states, protect/delete/all-approve,
  verification buckets, pathological paths, mutual exclusion,
  manifest-to-undo round trip).

### Changed

- **Breaking (defaults):** renamed candidates are no longer auto-deleted or
  auto-moved — everything that used to act on the full queue now holds back
  any candidate whose basename differs from its kept file until a human
  reviews the list (protect rows with ' '/'-' or delete them, or accept
  everything with `--approve-all`) and runs `--approve` (file safety first;
  the body-sha256 review gate is the floor). Pass `--no-gate-renamed`
  explicitly to keep the old behavior.
- `--export-candidates` still exports the full selection queue (the gate does
  not shrink the export); when the gate holds items back, a trailing comment
  line notes that the actually-processed set is smaller.
- The end-to-end suite grew to 118 cases; existing cases that assert renamed
  duplicates being processed now pass `--no-gate-renamed` explicitly, keeping
  their original assertions intact.

## [0.1.0] - 2026-10-14

首个以"安全基线 + 审阅与回滚"命令行形态为基线的版本，变更概览按类型分组如下。

### 新增

- 带**尺寸预过滤**的两阶段扫描：先按每条路径一次 `stat` 得到的字节大小分组，
  只有与其他候选文件大小相同的文件才会计算哈希。大小唯一的文件以专用哨兵值记入
  缓存且内容永不被读取，因此首次扫描大目录树依然很快，增量运行也持续短路。
- 扫描安全基线：0 字节文件与符号链接永不成为重复候选；`--min-size N`
  可一般化该下限；单个不可读或消失的文件只产生一条告警而不中断运行；
  跳过计数在扫描汇总行中报告。
- `--min-size` —— 参与重复检测的文件最小字节数（默认 1）。
- `--cache-batch N` —— 累计多少条新哈希结果后一次性原子重写哈希缓存（默认 2000）。
- `--hash-algo` —— 可选摘要算法（`sha256`、`sha512`、`sha1`、`md5`、`blake2b`、
  `blake2s`，默认 `sha256`）。缓存条目按算法隔离命名空间：非默认算法的 id 带
  `algo:` 前缀存储且绝不跨算法混用；默认 `sha256` 延续历史裸十六进制缓存格式，
  逐字节完全复用。
- `--min-free-percent` —— 目标盘剩余空间低于给定百分比时拒绝移动（默认 5）。
- `--cross-dirs-only` —— 仅处理跨越两个及以上 `-d` 扫描根的重复组；
  单根内部的重复组整组跳过。
- `--export-candidates PATH` —— 在任何动作执行前导出待处理队列（每行一个绝对
  源路径）；与最终实际处理对象逐行一致，配合 `--try-run` 即为纯审阅模式。
- `moves_*.jsonl` 清单：每次成功移动立即向回收目录根部的清单追加
  `{"src", "dst"}` 并即时落盘，进程突然中断也不丢失已完成记录、随时可审计。
- `--undo PATH` —— 回放移动清单（单个文件或包含清单的回收目录）的回滚模式：
  原位置已被占用则跳过、绝不覆盖，缺失的目标位置计入统计，回收目录内清空后的
  目录骨架自动清理，最后打印 已恢复/已跳过/缺失/总数 汇总。
- 同步文件夹感知：扫描根直接包含常见文件同步标记（`.stfolder`、`.sync` 前缀）
  时输出告警，提示移动/删除动作会传播到对端设备；仅提示，不阻塞。
- 中断安全缓存：扫描中收到 `SIGINT`/`SIGTERM` 时在锁内一次性刷写已完成条目，
  输出一条中断汇总日志并以非零码退出；缓存写入始终经由同目录临时文件加原子替换。
- `pathlogic.py` —— 无第三方依赖的模块，负责路径分量匹配与结果文件结构校验，
  供主程序与辅助脚本共用。
- 结果文件载入守卫：`--duplicates-result-file` 的输入在任何消费发生前先行结构
  校验，问题按组/条目位置列出并非零退出，不触碰任何文件。
- `pyproject.toml` 打包（运行时依赖 `filelock`，`dev` 附加包含 `pytest` 与
  `mypy`）、测试入口，以及在 Python 3.11 / 3.12 / 3.13 上运行全量测试与 mypy 的
  GitHub Actions CI 工作流。
- 端到端测试套件（95 用例）完全经由 CLI 进程边界驱动工具、使用私有临时目录树；
  长集成用例带有 `slow` 标记。
- 四个顶层模块全部补齐类型标注，最简 mypy 配置下零报错。

### 变更

- **破坏性（匹配语义）：** `--retain` / `--retain-file` 关键字及
  `change_priority.py` 的路径匹配从裸子串改为整路径分量（或连续分量段）匹配：
  `dirA` 匹配 `dirA/...` 但绝不匹配兄弟目录 `dirA-nm/...`，关键字末尾的分隔符
  不再改变行为。
- **破坏性（默认值）：** 缓存重写阈值从每 10 个文件降为默认每 2000 个
  （`--cache-batch`），扫描结束与中断时的刷写点不变；大规模扫描的写入量下降
  约两个数量级。
- 进度报告：扫描循环内的逐文件调试记录改为每处理 1,000 个文件一条 INFO 里程碑；
  文件日志处理器只记录 INFO 及以上并按 3 天轮转。大目录树全量扫描的日志体积
  缩小约三个数量级。
- `--try-run` 现在完全不写缓存、结果 JSON 与移动清单（仍会读取并在内存中复用
  既有缓存）；此模式下 `--export-candidates` 的候选清单依然可导出。
- 内容摘要改用 `hashlib.file_digest`（单次流式调用）替代手工分块循环；
  十六进制输出不变。

### 修复

- `configure_logger` 现在对 logger 对象标识也遵循 `log_file_name` 参数，
  而不仅是日志文件名。
- 单个不可读或消失的文件不再中断整场扫描；扫描遭遇信号时无 traceback 退出、
  不丢失已完成的缓存成果。
- 移动清单文件名加入微秒精度，同一秒内的多次运行不再共用（并静默追加到）
  同一份清单。
- `change_priority.py` 会校验输入结构，缺失 `path`/`priority` 字段时给出带位置
  信息的可读错误，而非运行中途以裸 `KeyError` 崩溃；`--adjust` 帮助文本现按
  代码实际实现说明符号方向（正值通过减法提升保留优先级，下限截断为 0）。

---

本版英文条目如下。

English entries for this version follow.

First release with the hardened, review-and-rollback command line as the
baseline. Highlights, grouped by kind.

### Added

- Two-phase scan with a **size pre-filter**: entries are grouped by byte size
  from a single `stat` per path; only files that share a size with at least one
  other candidate are ever hashed. Size-unique files are recorded in the cache
  with a dedicated sentinel value and their content is never read, so first
  passes over large trees stay fast and incremental runs keep short-circuiting.
- Safety baseline for scanning: 0-byte files and symlinks never become
  duplicate candidates; `--min-size N` generalizes the floor; an unreadable or
  vanished file yields exactly one warning instead of aborting the run; skip
  counts are reported in a scan summary line.
- `--min-size` — minimum file size in bytes participating in duplicate
  detection (default 1).
- `--cache-batch N` — number of newly hashed entries accumulated before the
  hash cache is rewritten in one atomic batch (default 2000).
- `--hash-algo` — selectable digest (`sha256`, `sha512`, `sha1`, `md5`,
  `blake2b`, `blake2s`, default `sha256`). Cache entries are namespaced per
  algorithm: non-default ids are stored with an `algo:` prefix and are never
  exchanged between algorithms' runs, while default `sha256` keeps the
  historical bare-hex cache format fully reusable byte-for-byte.
- `--min-free-percent` — refuse a move when the destination volume drops below
  the given free-space percentage (default 5).
- `--cross-dirs-only` — act only on duplicate groups spanning two or more of
  the `-d` scan roots; single-root groups are skipped as a whole.
- `--export-candidates PATH` — write the pending processing queue (one
  absolute source path per line) before any action; line-for-line identical to
  what would be acted on, including with `--try-run` (review-only mode).
- `moves_*.jsonl` manifests: every successful move appends `{"src", "dst"}` to
  a manifest at the root of the recycle dir, flushed immediately, so completed
  moves survive an abrupt stop and remain auditable.
- `--undo PATH` — rollback mode replaying move manifests (a single file or a
  recycle dir holding them): occupied original locations are skipped and never
  overwritten, missing destinations are counted, emptied directory skeletons
  inside the recycle dir are pruned, and a restored/skipped/missing/total
  summary is printed.
- Synchronized-folder awareness: when a scan root directly contains common
  file-synchronization markers (`.stfolder`, `.sync` prefixes), a warning
  notes that move/delete actions propagate to peer devices; advisory only.
- Interrupt-safe cache: `SIGINT`/`SIGTERM` during a scan flushes completed
  entries once inside the lock, logs a single interrupted-summary line, and
  exits non-zero; cache writes always go through a same-directory temporary
  file plus an atomic replace.
- `pathlogic.py` — dependency-free module owning path-component matching and
  result-file schema validation, shared by the main tool and utilities.
- Result-file load guard: `--duplicates-result-file` input is schema-checked
  before anything is consumed; problems are listed with group/entry locations
  and the run exits non-zero without touching files.
- `pyproject.toml` packaging (runtime dependency `filelock`, `dev` extras with
  `pytest` and `mypy`), test entry point, and a GitHub Actions CI workflow
  running the full suite and mypy on Python 3.11 / 3.12 / 3.13.
- End-to-end test suite (95 cases) driving the tools purely through the CLI
  process boundary with private temporary trees; long integration cases are
  marked `slow`.
- Type annotations across the four top-level modules with a minimal mypy
  configuration that reports no errors.

### Changed

- **Breaking (matching semantics):** `--retain` / `--retain-file` keywords and
  the `change_priority.py` path matcher now match whole path components (or a
  contiguous run of components) instead of bare substrings: `dirA` matches
  `dirA/...` but never a sibling `dirA-nm/...`, and trailing separators on the
  keyword no longer change behavior.
- **Breaking (defaults):** the cache rewrite threshold dropped from every 10
  files to every 2000 by default (`--cache-batch`), with the same flush points
  at scan end and on interrupt; write volume during large scans falls by two
  orders of magnitude.
- Progress reporting: per-file debug records inside the scan loops were
  replaced by one INFO milestone per 1,000 processed files; the file handler
  now records INFO and above with 3-day rotation. A full scan's log footprint
  shrinks by roughly three orders of magnitude on large trees.
- `--try-run` now writes no cache, no result JSON, and no move manifests at
  all (it still reads and reuses the existing cache in memory); the candidate
  list from `--export-candidates` remains available in this mode.
- Content digests are produced with `hashlib.file_digest` (single streaming
  call) instead of a manual chunked loop; hex output is unchanged.

### Fixed

- `configure_logger` now honors its `log_file_name` argument for the logger
  object identity, not just the log-file name.
- A single unreadable or vanished file no longer aborts an entire scan; scans
  also survive signals without traceback and without losing completed cache
  work.
- Move-manifest file names gained microsecond precision so repeated runs in
  the same second no longer share (and silently append to) one manifest.
- `change_priority.py` validates its input shape and reports missing
  `path`/`priority` fields with located, human-readable errors instead of
  crashing mid-run with a bare `KeyError`; its `--adjust` help now states the
  sign direction the code actually implements (positive raises retention
  priority by subtracting, clamped at 0).

[0.2.0]: https://github.com/haotianfei/DupFinder/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/haotianfei/DupFinder/releases/tag/v0.1.0

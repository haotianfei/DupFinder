# DupFinder

[![License: GPL v3](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
[![CI](https://github.com/haotianfei/DupFinder/actions/workflows/ci.yml/badge.svg)](https://github.com/haotianfei/DupFinder/actions/workflows/ci.yml)

按文件大小与哈希查找并处理重复文件的命令行工具：先审阅、再动手，一步可回滚。
A command-line tool that finds and handles duplicate files by size and hash — review before acting, and roll back in one command.

[中文文档](#中文文档) · [English Documentation](#english-documentation)

---

<a name="中文文档"></a>

## 中文文档

**目录**

- [概述](#概述)
- [特性](#特性)
- [快速开始](#快速开始)
- [安装、测试与类型检查](#安装测试与类型检查)
- [参数一览](#参数一览)
- [完整帮助（逐字）](#完整帮助逐字)
- [运行产物](#运行产物)
- [使用示例](#使用示例)
- [事后微调优先级](#事后微调优先级)
- [注意事项](#注意事项)
- [目录结构](#目录结构)
- [贡献、更新日志与许可证](#贡献更新日志与许可证)

### 概述

`DupFinder` 是一个按文件大小与哈希查找并处理重复文件的命令行工具。每个重复组
仅保留一个文件（由确定性的优先级规则决定：修改时间、路径深度、保留关键字），
其余文件被删除或移动。大规模目录采用两阶段大小预过滤，只有存在同大小候选的文件
才会被计算哈希；增量哈希缓存让重复运行非常便宜。

### 特性

- **两阶段大小预过滤**：扫描先对每个路径仅 stat 一次并按字节大小分组，只有与
  其他候选同大小的文件才进入哈希阶段；大小全局唯一的文件不读内容，缓存中以专用
  哨兵值记录。
- **增量哈希缓存**：结果缓存于运行目录的 `file_cache.json`。写入为原子替换、
  批量落盘且抗中断：`SIGINT`/`SIGTERM` 时会把已完成部分先落盘再退出；
  `--try-run` 演练模式完全不写缓存与结果文件。
- **跨目录专属去重**：`--cross-dirs-only` 只处理成员分布在两个及以上扫描根的
  重复组——以 A 为参照清理 B 时，两侧各自内部的重复都不会被触碰。
- **执行前审查**：`--export-candidates FILE` 在任何动作前把实际处理队列逐行
  （绝对路径）导出为文本清单；与 `--try-run` 组合即可"只看不动"。
- **异名重复默认保护**：内容相同但文件名（basename）不同的重复可能各有引用
  语义（配置路径、应用绑定），"内容重复"≠"引用冗余"。门禁（gate）默认开启：
  同名重复（跨目录同步副本）照常处理，异名候选移出执行队列并写入
  `renamed_pending_*.txt` 待审清单（纯文本 TSV 标记格式）。清单为 **opt-out**
  语义：行默认预标记 `x`（拟执行），保护 = 把标记改成空格/`-` 或直接删行；
  文件级安全底线是 **body-sha256 未经审阅拒绝门**——清单头部记录正文摘要，
  未经人工编辑（只改注释不算）的清单被 `--approve` 直接拒绝（rc1 零动作），
  审阅后全部照批可用 `--approve-all` 零编辑执行；`--approve` 的 move 模式建议
  与 `-m` 连用——不带 `-m` 时按原地改名处理，不产生清单、无法 `--undo` 回滚。
  `--approve` 逐条四项校验
  （存在+size、mtime 告警、内容摘要经缓存、保留项在位）后再执行，
  `--no-gate-renamed` 可退回旧行为。
- **可追溯、可回滚的移动**：每条成功的移动都会即时写入回收目录根部的
  `moves_*.jsonl` 清单；`--undo` 按清单反向恢复，原位已被占用则跳过不覆盖，
  结束后清理变空的目录骨架并输出 restored/skipped/missing 计数核对。
- **扫描安全基线**：0 字节文件与软链接默认不参与去重（`--min-size` 可
  一般化）；单个不可读或中途消失的文件只产生一条警告，不会中断整轮扫描。
- **同步目录警示**：扫描根直接子项含常见第三方文件同步工具的目录标记
  （`.stfolder`、`.sync` 前缀）时，在处理动作前提示"移动/删除会向对端设备
  传播"——只警示不阻塞。
- **确定性优先级**：更早修改、路径更深的文件优先保留；`--retain` 关键字按
  完整路径分量（连续分量序列）匹配，`dirA` 永远不会误中兄弟目录 `dirA-nm`。
- **质量保障**：118 个端到端测试全部经 CLI 进程边界运行，mypy 静态类型检查，
  CI 覆盖 push 与 pull request。

### 快速开始

```bash
git clone https://github.com/haotianfei/DupFinder.git
cd DupFinder
pip install -e .            # 运行时依赖：filelock

# 1) 先审查：导出待处理清单，零改动
python dup_finder.py -d dir_a/ dir_b/ --cross-dirs-only \
    --export-candidates pending.txt --try-run
less pending.txt

# 2) 再执行：把 dir_b 中与 dir_a 重复的文件移入回收目录，
#    dir_a 自身与 dir_b 内部重复都不动
python dup_finder.py -d dir_a/ dir_b/ --cross-dirs-only \
    --action move -m trash/

# 3) 万一有误操作，一条命令回滚
python dup_finder.py --undo trash/
```

完整参数参考请运行 `python dup_finder.py --help`，或查看下方
[参数一览](#参数一览)。

### 安装、测试与类型检查

```bash
pip install -e .[dev]                      # 额外安装 pytest 与 mypy
python -m pytest tests/ -q                 # 全量 118 例（CLI seam 端到端）
python -m pytest tests/ -q -m "not slow"   # 跳过 slow 标记用例
python -m mypy dup_finder.py logger.py pathlogic.py change_priority.py
```

要求 Python 3.11 及以上（依赖 `hashlib.file_digest`）。

### 参数一览

权威清单以 `python dup_finder.py --help` 为准（共 21 个参数，下一节逐字粘贴），
本表与之一一对应。

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `-d, --directories` | 除 `--undo`/`--approve` 外必选 | 查找重复文件的目录，可以输入多个目录，以空格分隔 |
| `--action` | 可选，默认为 `move` | 对重复文件执行的操作，可选值为 `delete` 或 `move` |
| `--priority-order, -p` | 可选 | 自定义优先级顺序，默认为 `modified_time path`：更早修改的文件优先保留（可能是更有保留价值的原件），路径层级更深的文件优先保留 |
| `--move-to-dir, -m` | 可选 | 移动文件的目标目录；未指定时就地改名追加 `.dup_finder` 后缀 |
| `--try-run, -n` | 可选 | 尝试运行模式：仅打印操作，不实际执行，也不写哈希缓存与结果文件 |
| `--min-size` | 可选，默认 1（字节） | 参与重复判定的最小文件字节数；更小的文件（默认即 0 字节文件）在扫描阶段跳过并计入汇总 |
| `--cache-batch` | 可选，默认 2000 | 内存中累积多少条新哈希后批量重写一次缓存文件；扫描结束与中断时各再落盘一次 |
| `--hash-algo` | 可选，默认 `sha256` | 识别重复内容的哈希算法（可选 `sha256/sha512/sha1/md5/blake2b/blake2s`）。缓存条目按算法命名空间隔离：非默认算法以 `algo:` 前缀存储，不与其它算法的运行互相命中；默认 sha256 保持历史裸十六进制格式逐字节可复用 |
| `--min-free-percent` | 可选，默认 5 | 目标卷剩余空间低于该百分比时拒绝移动；剩余字节不足以容纳单个文件时同样拒绝 |
| `--cross-dirs-only` | 可选，默认关闭 | 只处理成员分布在两个及以上扫描根的重复组；单一目录内部的重复整组跳过 |
| `--exclude` | 可选 | 排除关键字，路径**包含**该关键字（子串）的文件在扫描阶段即排除，适合临时文件等 |
| `--exclude-file` | 可选 | 同上，但将排除关键字写入一个文件中，每行一个关键字 |
| `--retain` | 可选 | 强制保留关键字，按**路径分量**匹配（等于某个完整分量、或连续分量序列），命中的文件提升到最高优先级 0；例如 `dirA` 只命中 `dirA/...`，不会命中 `dirA-nm/...` |
| `--retain-file` | 可选 | 同上，但将强制保留关键字写入一个文件中，每行一个关键字 |
| `--duplicates-result-file` | 可选 | 从既有结果 JSON 载入分组继续处理（跳过扫描）；载入前会校验结构，缺 `path`/`priority` 等字段时逐条报错并零动作退出 |
| `--export-candidates` | 可选（PATH） | 在任何动作执行前，把将被处理的文件清单（绝对源路径，一行一个）写入 PATH，与实际操作队列逐行一致；父目录自动创建；与 `--try-run` 组合即可"只导清单不执行" |
| `--undo` | 可选（PATH） | 回滚模式：读取 `moves_*.jsonl` 清单（单个文件或含清单的目录），把每条记录从目标位置搬回原位；原位已被占用则跳过不覆盖（保护回滚期间新数据），结束后清理回收目录内变空的目录骨架并打印 restored/skipped_conflict/missing/total 计数。与 `-d` 互斥 |
| `--no-gate-renamed` | 可选（默认不传 = 门禁保持开启） | 异名重复门禁开关的反向参数：默认门禁开启——候选与组内保留项 basename 不同时移出执行队列、写入待审清单（同名重复照常处理）；传入本参数即恢复旧行为，所有入选候选不经审查直接处理 |
| `--approve` | 可选（PATH） | 审批模式：执行 `renamed_pending_*.txt` 待审清单中行首恰为 `x` 的行（清单默认预标记 `x`，保护=改空格/`-` 或删行）。第 0 项为文件级审阅门：头部 `# body-sha256:` 记录与正文（所有非 `#` 行按原始字节 `\n` 连接）重算一致 = 未编辑 → rc1 拒绝零动作（只改注释不解锁；缺记录同样拒绝），除非加 `--approve-all`。之后每行四项校验：候选存在且 size 与记录一致（否则 skipped_gone）、mtime 较扫描时点变化仅告警（warn_modified）、内容摘要与记录一致（经哈希缓存，miss 现算；不一致 skipped_mismatch）、保留项仍在位（否则 skipped_kept_gone）；通过后走与主流程一致的 delete/move 通道，move 写标准 `moves_*.jsonl` 清单、可被 `--undo` 回滚，收尾打印 executed/skipped/mismatch/total。与 `-d`/`--undo`/`--duplicates-result-file` 互斥 |
| `--approve-all` | 可选，默认关闭 | `--approve` 的零编辑逃逸开关：跳过 body-sha256 审阅门，执行清单中全部 `x` 行——显式给出本参数本身即人工审阅声明；被保护（空格/`-`）、被删行与 `!` 病态行依然不执行。必须与 `--approve` 连用，单独使用报 rc2 |
| `--renamed-pending-prefix` | 可选（PATH） | 待审清单的输出位置（默认与结果 JSON 同目录，即当前工作目录，文件名 `renamed_pending_<YYYYmmdd_HHMMSS_ffffff>.txt`）：取值为既有目录或以路径分隔符结尾时在该目录内用默认文件名；否则作为文件名前缀 `<PATH>_<时间戳>.txt` |

### 完整帮助（逐字）

```
$ python dup_finder.py --help
usage: dup_finder.py [-h] [--directories DIRECTORIES [DIRECTORIES ...]]
                     [--action {delete,move}]
                     [--priority-order PRIORITY_ORDER [PRIORITY_ORDER ...]]
                     [--move-to-dir MOVE_TO_DIR] [--try-run]
                     [--min-size MIN_SIZE] [--cache-batch CACHE_BATCH]
                     [--hash-algo {sha256,sha512,sha1,md5,blake2b,blake2s}]
                     [--min-free-percent PERCENT] [--cross-dirs-only]
                     [--exclude EXCLUDE [EXCLUDE ...]]
                     [--exclude-file EXCLUDE_FILE]
                     [--retain RETAIN [RETAIN ...]]
                     [--retain-file RETAIN_FILE]
                     [--duplicates-result-file DUPLICATES_RESULT_FILE]
                     [--undo PATH] [--export-candidates PATH]
                     [--no-gate-renamed] [--approve PATH] [--approve-all]
                     [--renamed-pending-prefix PATH]

Find and process duplicate files.

options:
  -h, --help            show this help message and exit
  --directories, -d DIRECTORIES [DIRECTORIES ...]
                        Directories to search for duplicate files (required
                        unless --undo or --approve is used)
  --action {delete,move}
                        Action to process files (default: move)
  --priority-order, -p PRIORITY_ORDER [PRIORITY_ORDER ...]
                        Custom priority order: default is modified_time, path
  --move-to-dir, -m MOVE_TO_DIR
                        Directory to move files to (if not specified, rename
                        files with .dup_finder suffix)
  --try-run, -n         Try run mode: only print actions without executing
                        them
  --min-size MIN_SIZE   Minimum file size in bytes to participate in duplicate
                        detection; files smaller than this (including 0-byte
                        files, by default) are skipped during scanning and
                        counted in the scan summary (default: 1)
  --cache-batch CACHE_BATCH
                        Number of newly hashed files to accumulate in memory
                        before the hash cache file is rewritten in one batch;
                        the cache is also flushed once when the scan ends and
                        once more if the scan is interrupted (default: 2000)
  --hash-algo {sha256,sha512,sha1,md5,blake2b,blake2s}
                        Hash algorithm used to identify duplicate content
                        (default: sha256). Cache entries are namespaced per
                        algorithm: ids stored by a non-default algorithm are
                        prefixed 'algo:' and are never exchanged with other
                        algorithms' runs, while the default sha256 keeps the
                        historical bare-hex cache format fully reusable byte-
                        for-byte
  --min-free-percent PERCENT
                        Refuse a move when the destination volume's free space
                        drops below this percentage of its total capacity
                        (default: 5); a move is also refused when the free
                        bytes cannot hold the single file
  --cross-dirs-only     Only act on duplicate groups whose files span two or
                        more of the scan roots given by -d; groups lying
                        entirely inside one root are skipped as a whole.
                        Example: dup_finder.py -d A B --cross-dirs-only
                        --action move -m trash moves B's files that also exist
                        in A while leaving A itself, and B's own internal
                        duplicates, untouched. (default: off)
  --exclude EXCLUDE [EXCLUDE ...]
                        Exclude files use keywords,
  --exclude-file EXCLUDE_FILE
                        File containing exclude keywords, one per line
  --retain RETAIN [RETAIN ...]
                        Retain keywords,
  --retain-file RETAIN_FILE
                        File containing retain keywords, one per line
  --duplicates-result-file DUPLICATES_RESULT_FILE
                        File containing the duplicates result JSON data
  --undo PATH           Rollback mode: replay a moves_*.jsonl manifest (a file
                        path, or a directory containing them) and move each
                        recorded file from its destination back to its
                        original source. Never overwrites a source location
                        that is already occupied, prunes emptied directory
                        skeletons under the recycle dir, and prints a
                        restored/skipped/missing/total count summary at the
                        end. Mutually exclusive with -d/--directories.
                        Example: dup_finder.py --undo trash
  --export-candidates PATH
                        Before any action runs, write the list of files that
                        will be processed to PATH, one absolute source path
                        per line, matching exactly the selected processing
                        queue (after cross-root filtering and priority/retain
                        selection). Works with delete, move or --try-run;
                        combine with --try-run to only review the list, since
                        try-run still writes no cache, result or manifest
                        files. The output file is a user-requested artifact;
                        its parent directories are created as needed, and the
                        run stops before acting if the list cannot be written.
  --no-gate-renamed     Renamed-dupes gate (ON by default): a candidate whose
                        basename differs from its group's kept file is held
                        back from delete/move and written to a
                        renamed_pending_*.txt review file next to the result
                        JSON; same-name duplicates keep being processed as
                        before. Rows come pre-marked 'x': protect what to keep
                        ('x' -> ' ' or '-', or delete the row) and run
                        --approve; an unedited list is refused (rc1) unless
                        --approve-all. This flag restores the old behavior of
                        processing every selected candidate without any
                        review. (default: gate on)
  --approve PATH        Approval mode: execute the rows marked 'x' in a
                        renamed_pending_*.txt review file (PATH). Rows are
                        generated pre-marked 'x' (opt-out): to protect a file
                        change its mark to ' ' or '-', or delete the row.
                        Check 0 is the review gate — a whose-body file nobody
                        edited ('# body-sha256:' recomputes equal, comment-
                        only edits do not unlock; a missing record is also
                        refused) aborts with rc1 and zero actions unless
                        --approve-all is passed. Every row then passes four
                        checks before acting: candidate exists with the
                        recorded size, content digest matches the recorded id
                        (via the hash cache, re-hashed on miss), and the kept
                        file is still in place; an mtime change since the
                        review file was generated only warns. Approved rows go
                        through the same delete/move machinery (--action move
                        -m DIR writes a standard moves_*.jsonl manifest that
                        --undo rolls back). Prints an
                        executed/skipped/mismatch/total summary. Mutually
                        exclusive with -d/--directories, --undo and
                        --duplicates-result-file. Example: sed -i
                        '/wechat/s/^x/-/' renamed_pending_*.txt &&
                        dup_finder.py --approve renamed_pending_*.txt --action
                        move -m trash
  --approve-all         Skip the body-sha256 review gate of --approve and
                        execute every 'x' row as generated: passing this flag
                        is itself the human review declaration for an all-
                        approve decision. Rows protected (' ' or '-'), deleted
                        rows and '!' pathological rows still never execute.
                        Must be combined with --approve PATH; using it alone
                        is an error (exit code 2)
  --renamed-pending-prefix PATH
                        Where the renamed-dupes review file is written
                        (default: renamed_pending_<YYYYmmdd_HHMMSS_ffffff>.txt
                        next to the result JSON, i.e. the working directory).
                        An existing directory or a value ending in a path
                        separator keeps the default file name inside it; any
                        other value is used as a file-name prefix:
                        <PATH>_<YYYYmmdd_HHMMSS_ffffff>.txt
```

### 运行产物

| 文件 | 何时产生 |
| --- | --- |
| `file_cache.json`（及 `.lock`） | 哈希缓存，任何非演练运行都可能更新 |
| `duplicates_YYYYMMDD_HHMMSS.json` | 一次查找结果（`--try-run` 不写） |
| `renamed_pending_YYYYMMDD_HHMMSS_ffffff.txt` | 异名重复待审清单（门禁拦到异名候选才写，行默认预标记 `x`，头部含 `# body-sha256:` 审阅门记录；`--try-run` 也写，属审查产物） |
| `<回收目录>/moves_YYYYMMDD_HHMMSS_ffffff.jsonl` | 每条成功移动一行 `{"src", "dst"}`，供 `--undo` 回放 |
| `logs/DupFinder.log`（按天轮转，保留 3 天） | INFO 里程碑与告警日志 |

### 使用示例

1. 移动到回收目录（随数据生成清单，可回滚）：
   ```bash
   python dup_finder.py -d dir_a/ dir_b/ --action move -m trash/
   python dup_finder.py --undo trash/
   ```

2. 保留路径分量含 `important` 的文件（分量精确匹配）：
   ```bash
   python dup_finder.py -d dir_a/ --retain important --action delete
   ```

3. 自定义优先级顺序：
   ```bash
   python dup_finder.py -d dir_a/ --priority-order modified_time path --action move
   ```

4. 演练模式，仅打印操作、不实际执行：
   ```bash
   python dup_finder.py -d dir_a/ --try-run
   python dup_finder.py -d dir_a/ -n        # 简写
   ```

5. 排除关键字（子串匹配）：
   ```bash
   python dup_finder.py -d dir_a/ --exclude temp --exclude pdf
   python dup_finder.py -d dir_a/ --exclude-file exclude_keywords.txt
   ```

6. 只导清单不执行：
   ```bash
   python dup_finder.py -d dir_a/ dir_b/ --action move -m trash/ \
       --export-candidates review_list.txt --try-run
   ```

7. 跨目录专属清理：
   ```bash
   python dup_finder.py -d dir_a/ dir_b/ --cross-dirs-only \
       --action move -m trash/
   ```

8. 换用更严格的摘要算法（缓存按算法隔离，sha256 历史不受影响）：
   ```bash
   python dup_finder.py -d dir_a/ --hash-algo sha512 --try-run
   ```

9. 异名重复审批闭环（默认门禁，opt-out 标记，上千条也不崩）：
   ```bash
   # 1) 照常跑：同名重复已处理，异名候选全部进待审清单、原地不动
   python dup_finder.py -d dir_a/ dir_b/ --action move -m trash/
   # 2) 审阅清单（TSV：标记<TAB>候选路径<TAB>保留路径<TAB>sha256<TAB>size，
   #    行默认已带 'x'=拟执行；直接对未编辑清单 --approve 会被拒绝 rc1）
   less renamed_pending_*.txt
   # 3a) 要保护的就改标记或删行：批量保护某目录/模式，或全保护后个别放行
   sed -i '/keepassxc/s/^x/-/' renamed_pending_20261014_101530_123456.txt
   vim -c ':g/WeiXin/s/^x/ /' renamed_pending_20261014_101530_123456.txt
   # 3b) 自审后确认全部照批：零编辑，显式 --approve-all 即为审阅声明
   # 4) 执行剩余 'x' 行（逐条四项校验），随后仍可一条 --undo 回滚
   python dup_finder.py --approve renamed_pending_20261014_101530_123456.txt \
       --action move -m trash/
   python dup_finder.py --approve renamed_pending_20261014_101530_123456.txt \
       --approve-all --action delete
   python dup_finder.py --undo trash/
   ```

### 事后微调优先级

在把结果文件经 `--duplicates-result-file` 回喂处理前，可用
`change_priority.py` 微调每组 `priority`：关键字匹配与 `--retain` 同为分量
语义，载入时先做结构校验，缺字段会给出带位置的 readable 错误。

```
$ python change_priority.py --help
usage: change_priority.py [-h] -i INPUT -o OUTPUT -p PATH -a ADJUST

调整重复文件优先级

options:
  -h, --help           show this help message and exit
  -i, --input INPUT    输入JSON文件路径
  -o, --output OUTPUT  输出JSON文件路径
  -p, --path PATH      目标路径关键字，按路径分量匹配（与 dup_finder --retain
                       同语义）：关键字须等于某个完整路径分量、或作为连续分量序列出现；例如 dirA 命中 dirA/...
                       但不会命中兄弟目录 dirA-nm/...
  -a, --adjust ADJUST  调整值。优先级数值越小越优先保留；本工具计算 priority - adjustment（下限
                       0），所以正值提升保留优先级、负值降低
```

```bash
# 提升路径分量含 archive_2024 的文件的保留优先级（数值减少 1）：
python change_priority.py -i duplicates.json -o adjusted.json -p archive_2024 -a 1
```

### 注意事项

- 对疑似活跃同步目录执行移动/删除会向对端设备传播，工具会在动作前警示
  （扫描根下存在 `.stfolder` / `.sync` 标记时），请留意日志。
- 异名重复门禁默认开启：内容相同但 basename 不同的候选只进待审清单、不被
  自动处理。清单行默认预标记 `x`（opt-out）：审阅时把要保留的改成空格/`-` 或
  删行即可；未经人工编辑的清单会被 `--approve` 直接拒绝（rc1，只改注释不算
  审阅），确认全批用 `--approve-all`。`--approve` 对每条已批准行重新校验
  存在性/大小/摘要与保留项在位后才动文件。
- 首轮正式运行前建议先演练并导出清单审查；确认结果前保留回收目录。

### 目录结构

| 路径 | 职责 |
| --- | --- |
| `dup_finder.py` | 主程序：两阶段扫描、哈希缓存、异名门禁与待审清单、动作执行、移动清单与回滚 |
| `pathlogic.py` | 纯函数：路径分量匹配、结果文件结构校验，导入零副作用 |
| `logger.py` | 统一日志配置：控制台 INFO 里程碑 + 按天轮转的文件日志 |
| `change_priority.py` | 独立脚本：回喂处理前微调结果 JSON 中的优先级 |
| `tests/` | 端到端测试（仅经 CLI 进程边界驱动，118 用例） |

### 贡献、更新日志与许可证

- 贡献指南（中文见 [CONTRIBUTING.md](CONTRIBUTING.md) 首段，English 见同文件后半）：
  提交遵循 Conventional Commits，测试与 mypy 必须保持全绿。
- 各版本可见变更记于 [CHANGELOG.md](CHANGELOG.md)。
- 本项目采用 [GPL-3.0](LICENSE) 许可证。

---

<a name="english-documentation"></a>

## English Documentation

**Contents**

- [Overview](#overview)
- [Features](#features)
- [Quickstart](#quickstart)
- [Install, test and type-check](#install-test-and-type-check)
- [CLI reference](#cli-reference)
- [Full help output](#full-help-output)
- [Generated files](#generated-files)
- [Examples](#examples)
- [Adjusting priorities afterwards](#adjusting-priorities-afterwards)
- [Notes](#notes)
- [Repository layout](#repository-layout)
- [Contributing, changelog and license](#contributing-changelog-and-license)

### Overview

`DupFinder` is a command-line tool that finds and processes duplicate files by
file size and cryptographic hash. It keeps one file per duplicate group — chosen
by deterministic priority rules (modification time, path depth, retain
keywords) — and deletes or moves the rest. Large trees are handled with a
two-phase size pre-filter so that only plausible candidates are ever hashed,
and an incremental hash cache makes repeat runs cheap.

### Features

- **Two-phase size pre-filter.** A scan first groups entries by byte size with
  a single `stat` per path; only files sharing a size with at least one other
  candidate are ever hashed. Size-unique files are recorded with a cache
  sentinel and their content is never read.
- **Incremental hash cache.** Results are cached in `file_cache.json` (same
  directory as the run). Writes are atomic, batched, and interrupt-safe: a
  `SIGINT`/`SIGTERM` still flushes completed work before exiting, and a
  `--try-run` writes no cache or result files at all.
- **Cross-directory-only mode.** `--cross-dirs-only` acts only on duplicate
  groups whose members span two or more scan roots, so cleaning tree B against
  reference tree A never touches either tree's own internal duplicates.
- **Review before acting.** `--export-candidates FILE` dumps the exact
  processing queue (one absolute path per line) before any action runs; combine
  with `--try-run` to review without touching anything.
- **Renamed-duplicate protection by default.** Identical content under a
  different basename can carry different reference semantics (config paths,
  application bindings) — "duplicate content" is not "redundant reference".
  The gate is ON by default: same-name duplicates (synchronized copies) keep
  being processed, while renamed candidates leave the execution queue and land
  in a `renamed_pending_*.txt` review file (plain-text TSV mark format). The
  file is **opt-out**: rows come pre-marked `x` (intended for processing), and
  protecting means flipping the mark to ' ' or `-` or deleting the row. The
  file-level body-sha256 review gate is the safety floor — the header records
  a digest of the body, and `--approve` refuses (rc1, zero actions) against a
  list nobody edited (comment-only changes do not unlock); once reviewed,
   accepting every row is a zero-edit `--approve-all`; pair `--approve` moves
   with `-m` — without it the move degrades to an in-place rename with no
   manifest and no `--undo` rollback. `--approve` re-verifies
  every row with four checks (existence + size, mtime warning, digest via the
  cache, kept file still in place) before touching anything;
  `--no-gate-renamed` restores the old behavior.
- **Auditable, reversible moves.** Every successful move is appended to a
  `moves_*.jsonl` manifest at the root of the recycle dir; `--undo` replays a
  manifest backwards, never overwriting an occupied original location, pruning
  emptied directory skeletons, and reporting restored/skipped/missing totals.
- **Scan-safety baseline.** 0-byte files and symlinks never become duplicate
  candidates (generalized by `--min-size`); an unreadable or vanished file is
  one warning line, never an aborted run.
- **Synchronized-folder awareness.** When a scan root directly contains common
  third-party file-synchronization markers (`.stfolder`, `.sync` prefixes), a
  warning states that move/delete actions propagate to peer devices — advisory
  only, the run continues.
- **Deterministic priorities.** Earlier-modified files and deeper paths are
  kept first; `--retain` keywords match whole path components (contiguous
  component runs), so `dirA` can never accidentally match a sibling `dirA-nm`.
- **Quality rails.** 118 end-to-end tests at the CLI process boundary, static
  type checking with mypy, CI on push and pull requests.

### Quickstart

```bash
git clone https://github.com/haotianfei/DupFinder.git
cd DupFinder
pip install -e .            # runtime dependency: filelock

# 1) Review first — export the pending list, touch nothing:
python dup_finder.py -d dir_a/ dir_b/ --cross-dirs-only \
    --export-candidates pending.txt --try-run
less pending.txt

# 2) Act — move files of dir_b that duplicate dir_a into a recycle dir;
#    dir_a and dir_b's own internal duplicates stay untouched:
python dup_finder.py -d dir_a/ dir_b/ --cross-dirs-only \
    --action move -m trash/

# 3) If anything went wrong — one command rolls it back:
python dup_finder.py --undo trash/
```

Run `python dup_finder.py --help` for the full parameter reference, or jump to
the [CLI table](#cli-reference) below.

### Install, test and type-check

```bash
pip install -e .[dev]                      # adds pytest and mypy
python -m pytest tests/ -q                 # full suite (118 tests, CLI seam)
python -m pytest tests/ -q -m "not slow"   # skip the marked slow cases
python -m mypy dup_finder.py logger.py pathlogic.py change_priority.py
```

Requires Python 3.11 or newer (`hashlib.file_digest`).

### CLI reference

The authoritative list is `python dup_finder.py --help` (21 options, pasted
verbatim in the next section); this table mirrors it 1:1.

| Parameter | Kind | Description |
| --- | --- | --- |
| `-d, --directories` | required unless `--undo`/`--approve` | Directories to search for duplicate files; several may be given, space-separated |
| `--action` | optional, default `move` | What to do with duplicate files: `delete` or `move` |
| `--priority-order, -p` | optional | Custom priority order; default `modified_time path` — earlier-modified files are kept first (likely the originals worth keeping), and deeper paths win within a group |
| `--move-to-dir, -m` | optional | Destination directory for moves; without it, files are renamed in place with a `.dup_finder` suffix |
| `--try-run, -n` | optional | Try-run mode: prints actions only, executes nothing, and writes no cache or result files |
| `--min-size` | optional, default 1 (bytes) | Minimum file size in bytes that participates in duplicate detection; smaller files (0-byte by default) are skipped during scanning and counted in the summary |
| `--cache-batch` | optional, default 2000 | How many newly hashed entries accumulate in memory before the cache file is rewritten in one batch; extra flushes happen at scan end and on interrupt |
| `--hash-algo` | optional, default `sha256` | Hash algorithm for content identity (`sha256/sha512/sha1/md5/blake2b/blake2s`). Cache entries are namespaced per algorithm: non-default ids are stored with an `algo:` prefix and never cross-match other algorithms' runs; default `sha256` keeps the historical bare-hex format fully reusable byte-for-byte |
| `--min-free-percent` | optional, default 5 | Refuse a move when the destination volume's free space drops below this percentage; also refuses when free bytes cannot hold the single file |
| `--cross-dirs-only` | optional, off | Act only on duplicate groups whose members span two or more scan roots; groups internal to one root are skipped as a whole |
| `--exclude` | optional | Exclusion keywords: paths **containing** a keyword (substring) are dropped during scanning — good for temporary files |
| `--exclude-file` | optional | Same, but keywords read from a file, one per line |
| `--retain` | optional | Keep-keywords matched by **path component** (a whole component, or a contiguous run of components); matched files get top priority 0; e.g. `dirA` matches `dirA/...` but never a sibling `dirA-nm/...` |
| `--retain-file` | optional | Same, but keywords read from a file, one per line |
| `--duplicates-result-file` | optional | Load groups from an existing result JSON and continue (skipping the scan); the file is schema-checked first — missing `path`/`priority` fields produce located errors and a zero-action exit |
| `--export-candidates` | optional (PATH) | Before any action, write the pending-processing list (one absolute source path per line) to PATH, line-for-line identical to the real queue; parent directories are created; pair with `--try-run` for review-only use |
| `--undo` | optional (PATH) | Rollback mode: replay `moves_*.jsonl` manifests (one file, or a directory holding them), moving each entry back from destination to source; occupied original locations are skipped and never overwritten, emptied recycle-dir skeletons are pruned, and a restored/skipped_conflict/missing/total summary is printed. Mutually exclusive with `-d` |
| `--no-gate-renamed` | optional, omit to keep gate on | Inverse switch of the renamed-dupes gate: by default a candidate whose basename differs from its group's kept file leaves the execution queue and is written to the review file (same-name duplicates keep being processed); passing this flag restores the old behavior of processing every selected candidate without review |
| `--approve` | optional (PATH) | Approval mode: execute rows whose leading mark is exactly `x` in a `renamed_pending_*.txt` review file. Rows are generated pre-marked `x` (opt-out): protect by flipping the mark to ' ' or `-`, or delete the row. Check 0 is the file-level review gate — recomputing the body digest (all non-`#` lines, raw bytes joined with `\\n`) equals the recorded `# body-sha256:` means nobody edited the list (comment-only changes do not unlock) → rc1, zero actions, unless `--approve-all`; a missing record is refused the same way. Each row then passes four checks before acting: candidate exists with the recorded size (else skipped_gone), mtime drift since the scan only warns (warn_modified), content digest matches via the hash cache or a fresh hash (else skipped_mismatch), kept file still in place (else skipped_kept_gone); approved rows use the same delete/move machinery, moves write a standard `moves_*.jsonl` manifest that `--undo` rolls back, and an executed/skipped/mismatch/total summary is printed. Mutually exclusive with `-d`/`--undo`/`--duplicates-result-file` |
| `--approve-all` | optional, off | Zero-edit escape hatch for `--approve`: skip the body-sha256 review gate and execute every `x` row — passing the flag is itself the human review declaration; protected (' '/'-'), deleted and `!` pathological rows still never execute. Must be combined with `--approve`; alone it errors (rc2) |
| `--renamed-pending-prefix` | optional (PATH) | Where the review file is written (default: `renamed_pending_<YYYYmmdd_HHMMSS_ffffff>.txt` next to the result JSON, i.e. the working directory); an existing directory or trailing-separator value keeps the default name inside it, any other value becomes a file-name prefix `<PATH>_<timestamp>.txt` |

### Full help output

```
$ python dup_finder.py --help
usage: dup_finder.py [-h] [--directories DIRECTORIES [DIRECTORIES ...]]
                     [--action {delete,move}]
                     [--priority-order PRIORITY_ORDER [PRIORITY_ORDER ...]]
                     [--move-to-dir MOVE_TO_DIR] [--try-run]
                     [--min-size MIN_SIZE] [--cache-batch CACHE_BATCH]
                     [--hash-algo {sha256,sha512,sha1,md5,blake2b,blake2s}]
                     [--min-free-percent PERCENT] [--cross-dirs-only]
                     [--exclude EXCLUDE [EXCLUDE ...]]
                     [--exclude-file EXCLUDE_FILE]
                     [--retain RETAIN [RETAIN ...]]
                     [--retain-file RETAIN_FILE]
                     [--duplicates-result-file DUPLICATES_RESULT_FILE]
                     [--undo PATH] [--export-candidates PATH]
                     [--no-gate-renamed] [--approve PATH] [--approve-all]
                     [--renamed-pending-prefix PATH]

Find and process duplicate files.

options:
  -h, --help            show this help message and exit
  --directories, -d DIRECTORIES [DIRECTORIES ...]
                        Directories to search for duplicate files (required
                        unless --undo or --approve is used)
  --action {delete,move}
                        Action to process files (default: move)
  --priority-order, -p PRIORITY_ORDER [PRIORITY_ORDER ...]
                        Custom priority order: default is modified_time, path
  --move-to-dir, -m MOVE_TO_DIR
                        Directory to move files to (if not specified, rename
                        files with .dup_finder suffix)
  --try-run, -n         Try run mode: only print actions without executing
                        them
  --min-size MIN_SIZE   Minimum file size in bytes to participate in duplicate
                        detection; files smaller than this (including 0-byte
                        files, by default) are skipped during scanning and
                        counted in the scan summary (default: 1)
  --cache-batch CACHE_BATCH
                        Number of newly hashed files to accumulate in memory
                        before the hash cache file is rewritten in one batch;
                        the cache is also flushed once when the scan ends and
                        once more if the scan is interrupted (default: 2000)
  --hash-algo {sha256,sha512,sha1,md5,blake2b,blake2s}
                        Hash algorithm used to identify duplicate content
                        (default: sha256). Cache entries are namespaced per
                        algorithm: ids stored by a non-default algorithm are
                        prefixed 'algo:' and are never exchanged with other
                        algorithms' runs, while the default sha256 keeps the
                        historical bare-hex cache format fully reusable byte-
                        for-byte
  --min-free-percent PERCENT
                        Refuse a move when the destination volume's free space
                        drops below this percentage of its total capacity
                        (default: 5); a move is also refused when the free
                        bytes cannot hold the single file
  --cross-dirs-only     Only act on duplicate groups whose files span two or
                        more of the scan roots given by -d; groups lying
                        entirely inside one root are skipped as a whole.
                        Example: dup_finder.py -d A B --cross-dirs-only
                        --action move -m trash moves B's files that also exist
                        in A while leaving A itself, and B's own internal
                        duplicates, untouched. (default: off)
  --exclude EXCLUDE [EXCLUDE ...]
                        Exclude files use keywords,
  --exclude-file EXCLUDE_FILE
                        File containing exclude keywords, one per line
  --retain RETAIN [RETAIN ...]
                        Retain keywords,
  --retain-file RETAIN_FILE
                        File containing retain keywords, one per line
  --duplicates-result-file DUPLICATES_RESULT_FILE
                        File containing the duplicates result JSON data
  --undo PATH           Rollback mode: replay a moves_*.jsonl manifest (a file
                        path, or a directory containing them) and move each
                        recorded file from its destination back to its
                        original source. Never overwrites a source location
                        that is already occupied, prunes emptied directory
                        skeletons under the recycle dir, and prints a
                        restored/skipped/missing/total count summary at the
                        end. Mutually exclusive with -d/--directories.
                        Example: dup_finder.py --undo trash
  --export-candidates PATH
                        Before any action runs, write the list of files that
                        will be processed to PATH, one absolute source path
                        per line, matching exactly the selected processing
                        queue (after cross-root filtering and priority/retain
                        selection). Works with delete, move or --try-run;
                        combine with --try-run to only review the list, since
                        try-run still writes no cache, result or manifest
                        files. The output file is a user-requested artifact;
                        its parent directories are created as needed, and the
                        run stops before acting if the list cannot be written.
  --no-gate-renamed     Renamed-dupes gate (ON by default): a candidate whose
                        basename differs from its group's kept file is held
                        back from delete/move and written to a
                        renamed_pending_*.txt review file next to the result
                        JSON; same-name duplicates keep being processed as
                        before. Rows come pre-marked 'x': protect what to keep
                        ('x' -> ' ' or '-', or delete the row) and run
                        --approve; an unedited list is refused (rc1) unless
                        --approve-all. This flag restores the old behavior of
                        processing every selected candidate without any
                        review. (default: gate on)
  --approve PATH        Approval mode: execute the rows marked 'x' in a
                        renamed_pending_*.txt review file (PATH). Rows are
                        generated pre-marked 'x' (opt-out): to protect a file
                        change its mark to ' ' or '-', or delete the row.
                        Check 0 is the review gate — a whose-body file nobody
                        edited ('# body-sha256:' recomputes equal, comment-
                        only edits do not unlock; a missing record is also
                        refused) aborts with rc1 and zero actions unless
                        --approve-all is passed. Every row then passes four
                        checks before acting: candidate exists with the
                        recorded size, content digest matches the recorded id
                        (via the hash cache, re-hashed on miss), and the kept
                        file is still in place; an mtime change since the
                        review file was generated only warns. Approved rows go
                        through the same delete/move machinery (--action move
                        -m DIR writes a standard moves_*.jsonl manifest that
                        --undo rolls back). Prints an
                        executed/skipped/mismatch/total summary. Mutually
                        exclusive with -d/--directories, --undo and
                        --duplicates-result-file. Example: sed -i
                        '/wechat/s/^x/-/' renamed_pending_*.txt &&
                        dup_finder.py --approve renamed_pending_*.txt --action
                        move -m trash
  --approve-all         Skip the body-sha256 review gate of --approve and
                        execute every 'x' row as generated: passing this flag
                        is itself the human review declaration for an all-
                        approve decision. Rows protected (' ' or '-'), deleted
                        rows and '!' pathological rows still never execute.
                        Must be combined with --approve PATH; using it alone
                        is an error (exit code 2)
  --renamed-pending-prefix PATH
                        Where the renamed-dupes review file is written
                        (default: renamed_pending_<YYYYmmdd_HHMMSS_ffffff>.txt
                        next to the result JSON, i.e. the working directory).
                        An existing directory or a value ending in a path
                        separator keeps the default file name inside it; any
                        other value is used as a file-name prefix:
                        <PATH>_<YYYYmmdd_HHMMSS_ffffff>.txt
```

### Generated files

| File | Written when |
| --- | --- |
| `file_cache.json` (+ `.lock`) | Hash cache, updated by any non-dry run |
| `duplicates_YYYYMMDD_HHMMSS.json` | Result of each real scan (`--try-run` writes none) |
| `renamed_pending_YYYYMMDD_HHMMSS_ffffff.txt` | Renamed-duplicate review file — rows pre-marked `x` (opt-out), header carries the `# body-sha256:` review-gate record; only written when the gate holds something back, also under `--try-run` (review artifact) |
| `<recycle dir>/moves_YYYYMMDD_HHMMSS_ffffff.jsonl` | One `{"src", "dst"}` line per completed move, for `--undo` |
| `logs/DupFinder.log` (daily rotation, 3 days kept) | INFO milestones and warnings |

### Examples

1. Move duplicates into a recycle dir (manifest written alongside the data;
   rollback available):
   ```bash
   python dup_finder.py -d dir_a/ dir_b/ --action move -m trash/
   python dup_finder.py --undo trash/
   ```

2. Keep files whose path has a component named `important`
   (component-accurate matching):
   ```bash
   python dup_finder.py -d dir_a/ --retain important --action delete
   ```

3. Custom priority order:
   ```bash
   python dup_finder.py -d dir_a/ --priority-order modified_time path --action move
   ```

4. Dry run — print actions, execute nothing:
   ```bash
   python dup_finder.py -d dir_a/ --try-run
   python dup_finder.py -d dir_a/ -n        # short form
   ```

5. Exclude keywords (substring matching):
   ```bash
   python dup_finder.py -d dir_a/ --exclude temp --exclude pdf
   python dup_finder.py -d dir_a/ --exclude-file exclude_keywords.txt
   ```

6. Review-only list export:
   ```bash
   python dup_finder.py -d dir_a/ dir_b/ --action move -m trash/ \
       --export-candidates review_list.txt --try-run
   ```

7. Cross-directory-only cleanup:
   ```bash
   python dup_finder.py -d dir_a/ dir_b/ --cross-dirs-only \
       --action move -m trash/
   ```

8. Stricter digest algorithm (the cache stays namespaced; sha256 history is
   unaffected):
   ```bash
   python dup_finder.py -d dir_a/ --hash-algo sha512 --try-run
   ```

9. Renamed-duplicate approval loop (the default gate; opt-out marks, scales to
   thousands):
   ```bash
   # 1) Run as usual: same-name duplicates are processed, renamed candidates
   #    all land in the review file (pre-marked 'x') and stay in place.
   python dup_finder.py -d dir_a/ dir_b/ --action move -m trash/
   # 2) Review the TSV: mark <TAB> candidate <TAB> kept <TAB> sha256 <TAB> size
   #    --approve on an unedited list is refused (rc1); comment-only edits do
   #    not unlock it.
   less renamed_pending_*.txt
   # 3a) Protect what must stay: flip marks (pattern batch or every row), or delete rows
   sed -i '/keepassxc/s/^x/-/' renamed_pending_20261014_101530_123456.txt
   vim -c ':g/WeiXin/s/^x/ /' renamed_pending_20261014_101530_123456.txt
   # 3b) Reviewed and everything may go: zero-edit all-approve escape hatch
   # 4) Execute the remaining 'x' rows (four checks each); --undo still rolls back
   python dup_finder.py --approve renamed_pending_20261014_101530_123456.txt \
       --action move -m trash/
   python dup_finder.py --approve renamed_pending_20261014_101530_123456.txt \
       --approve-all --action delete
   python dup_finder.py --undo trash/
   ```

### Adjusting priorities afterwards

`change_priority.py` edits the `priority` numbers inside a generated
`duplicates_*.json` before you feed it back via `--duplicates-result-file`.
Key matching uses the same whole-component rule as `--retain`; the input is
schema-checked first (missing `path`/`priority` fields yield located,
human-readable errors, not a crash). The tool's own help is printed in Chinese
because its CLI texts are authored in Chinese:

```
$ python change_priority.py --help
usage: change_priority.py [-h] -i INPUT -o OUTPUT -p PATH -a ADJUST

调整重复文件优先级

options:
  -h, --help           show this help message and exit
  -i, --input INPUT    输入JSON文件路径
  -o, --output OUTPUT  输出JSON文件路径
  -p, --path PATH      目标路径关键字，按路径分量匹配（与 dup_finder --retain
                       同语义）：关键字须等于某个完整路径分量、或作为连续分量序列出现；例如 dirA 命中 dirA/...
                       但不会命中兄弟目录 dirA-nm/...
  -a, --adjust ADJUST  调整值。优先级数值越小越优先保留；本工具计算 priority - adjustment（下限
                       0），所以正值提升保留优先级、负值降低
```

```bash
# Raise the keep-priority of every entry under a component named archive_2024:
python change_priority.py -i duplicates.json -o adjusted.json -p archive_2024 -a 1
```

### Notes

- Moving or deleting inside an actively synchronized folder propagates the
  change to peer devices. DupFinder warns before actions when a scan root
  contains synchronization markers such as `.stfolder` / `.sync`; heed the
  warning.
- The renamed-dupes gate is ON by default: identical content under a different
  basename only ever lands in the review file and is never auto-processed.
  Rows are pre-marked `x` (opt-out) — protect by flipping the mark to ' ' or
  `-` or deleting the row — and a list nobody edited is refused by
  `--approve` (rc1) via the body-sha256 review gate (comment-only edits do not
  unlock); use `--approve-all` for a zero-edit all-approve. `--approve`
  re-verifies existence/size, digest and the kept file in place before
  touching each approved row.
- Prefer `--try-run` plus `--export-candidates` review before the first real
  pass, and keep the recycle dir until `--undo` confirmed the outcome.

### Repository layout

| Path | Role |
| --- | --- |
| `dup_finder.py` | Main CLI: two-phase scan, hash cache, renamed gate and review file, actions, manifests, rollback |
| `pathlogic.py` | Pure functions: path-component matching, result-file schema validation, zero import side effects |
| `logger.py` | Shared logging setup: console INFO milestones plus a rotating file log |
| `change_priority.py` | Standalone utility to tune priorities in a result JSON before reprocessing |
| `tests/` | 118 end-to-end tests driven purely through the CLI process boundary |

### Contributing, changelog and license

- See [CONTRIBUTING.md](CONTRIBUTING.md) (the file carries a full Chinese
  guide followed by the complete English one). Commits follow Conventional
  Commits; the test suite and mypy must stay green.
- Notable changes per release are recorded in [CHANGELOG.md](CHANGELOG.md).
- This project is licensed under the [GPL-3.0](LICENSE) license.

---

Questions or ideas? Open an issue. 有问题或建议，欢迎提 issue。

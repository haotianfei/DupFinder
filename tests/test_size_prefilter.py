"""两阶段 size 预过滤 + 唯一 size 哨兵 + 流式摘要的行为测试。

覆盖：预过滤结果与全量哈希参照完全等价；size 唯一文件全程零内容读取（以"连 open
都不发生"的行为侧证 + 缓存哨兵条目证明），预热后二次运行仍跳过；旧格式真实 sha256
缓存载入后全部命中、逐字节不变（slow）；大目录基准下真实哈希条目数远小于文件总数；
哨兵条目在 size 组变大后必须重算（哨兵不算命中）。

全部经 CLI 进程边界断言外部行为；不导入被测模块，期望值由测试内独立复算的
参照实现（oracle）给出。
"""

import hashlib
import json
import stat as stat_mod

import pytest

from conftest import console_output, scan_summary

UNIQUE_SIZE_SENTINEL = "Z:unique-size"  # CLI 行为契约字符串：含非 hex 字符，任何摘要都不等于它


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _make_pair(t, rel_a, rel_b, content):
    t.add_file(rel_a, content)
    t.add_file(rel_b, content)


def _real_run_retained(t, retain=()):
    """真实扫描（写缓存与结果文件）但用 retain 关键字保留全部成员，杜绝文件动作。

    关键字取完整文件名：既匹配当前的子串语义，也匹配后续计划中的分量语义。
    """
    t.mkdir("trash")
    args = ["-d", "A", "B", "--action", "move", "-m", "trash"]
    if retain:
        args += ["--retain", *retain]
    return t.run(*args)


def _full_hash_reference(t, min_size=1):
    """参照实现（改造前的"全量哈希"语义）：所有可读常规非软链接候选按内容 sha256
    分组，仅保留 >=2 的组。返回 {digest: sorted[(path, size)]}。"""
    groups = {}
    for base in ("A", "B"):
        for p in sorted((t.workdir / base).rglob("*")):
            if p.is_symlink():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if not stat_mod.S_ISREG(st.st_mode) or st.st_size < min_size:
                continue
            try:
                with open(p, "rb") as f:
                    digest = _sha(f.read())
            except OSError:
                continue
            groups.setdefault(digest, []).append(
                (str(p.relative_to(t.workdir)), st.st_size)
            )
    return {k: sorted(v) for k, v in groups.items() if len(v) > 1}


def _project(data):
    return {k: sorted((e["path"], e["size"]) for e in v) for k, v in data.items()}


# ---------------------------------------------------------------------------
# AC1 等价性：预过滤输出 == 全量哈希参照
# ---------------------------------------------------------------------------

def test_size_prefilter_output_equals_full_hash_reference(dup_tree):
    """跨目录对/目录内三重奏/同 size 不同内容的诱饵/size 唯一者混排：
    预过滤后的 duplicates 与全量哈希参照逐组一致（同 size 不同内容的两个文件
    都进了哈希、因内容不同未成组——预过滤不产生任何漏判或误判）。"""
    t = dup_tree
    t.add_file("A/empty.bin")  # 0 字节：两种实现都不该出现在结果里
    _make_pair(t, "A/x_cross.txt", "B/x_cross.dup", b"cross" * 240)          # 1200B 跨目录对
    c3 = b"trio" * 300                                                        # 1800B 目录内三重奏
    t.add_file("A/y1.bin", c3)
    t.add_file("A/sub/y2.bin", c3)
    t.add_file("B/y3.bin", c3)
    t.add_file("A/decoy1.bin", b"a" * 700)                                   # 同 size 不同内容
    t.add_file("B/decoy2.bin", b"b" * 700)                                   # 的诱饵对
    t.add_file("A/u_mine.bin", b"u" * 2100)                                  # size 唯一者
    t.add_file("B/u_also.bin", (b"v" * 100)[:99])                            # 99B 唯一者

    expected = _full_hash_reference(t)
    assert expected, "参照实现下应至少存在重复组"

    proc = _real_run_retained(t)
    assert proc.returncode == 0, console_output(proc)

    data, path = t.load_result_json()
    assert path.parent == t.workdir
    assert _project(data) == expected
    assert UNIQUE_SIZE_SENTINEL not in data  # 哨兵绝不成为分组 key


# ---------------------------------------------------------------------------
# AC2 size 唯一文件全程零内容读取（含预热二次运行）
# ---------------------------------------------------------------------------

def test_unique_size_file_never_reads_content(dup_tree):
    """对 chmod 000 且 size 唯一的文件：正常实现会打开它读内容并告警（旧行为），
    预过滤实现连 open 都不发生——无警告、errors=0、缓存条目为哨兵。
    同树二次运行（缓存已热）仍不读内容、结果与首跑一致、哨兵原样保留。"""
    t = dup_tree
    t.add_unreadable("A/guard.bin", b"g" * 777)        # 不可读 + size 唯一（777）
    pair = b"pairme" * 60                              # 360B，两处同内容 → 哈希组
    _make_pair(t, "B/pair1.bin", "B/pair2.bin", pair)  # 可读的正常重复对

    retain = ("pair1.bin", "pair2.bin")
    proc1 = _real_run_retained(t, retain)
    out1 = console_output(proc1)
    assert proc1.returncode == 0, out1
    # 行为侧证：不可读的 size 唯一文件没有产生任何读取尝试（错误/警告为零）
    assert "Error accessing" not in out1, out1
    assert scan_summary(out1)[3] == 0, f"errors 计数应为 0:\n{out1}"

    cache = json.loads(t.cache_path().read_text())
    assert cache["A/guard.bin"]["file_id"] == UNIQUE_SIZE_SENTINEL
    guard_stat = (t.workdir / "A/guard.bin").stat()
    assert cache["A/guard.bin"]["size"] == guard_stat.st_size
    assert cache["A/guard.bin"]["modified_time"] == guard_stat.st_mtime
    assert cache["B/pair1.bin"]["file_id"] == _sha(pair)
    assert cache["B/pair2.bin"]["file_id"] == _sha(pair)
    result1, _ = t.load_result_json()
    assert _project(result1) == {
        _sha(pair): [("B/pair1.bin", len(pair)), ("B/pair2.bin", len(pair))]
    }

    # 二次运行：缓存已热（含哨兵条目）——仍不读内容、结果一致、哨兵原样保留
    proc2 = _real_run_retained(t, retain)
    out2 = console_output(proc2)
    assert proc2.returncode == 0, out2
    assert "Error accessing" not in out2, out2
    assert scan_summary(out2)[3] == 0
    cache2 = json.loads(t.cache_path().read_text())
    assert cache2["A/guard.bin"]["file_id"] == UNIQUE_SIZE_SENTINEL
    files = sorted(t.workdir.glob("duplicates_*.json"))
    assert 1 <= len(files) <= 2, [f.name for f in files]
    parsed = [json.loads(f.read_text()) for f in files]
    assert parsed[0] == parsed[-1], "二次运行结果应与首跑一致"


def test_sentinel_entry_rehashed_once_size_group_grows(dup_tree):
    """增量语义硬约束：缓存里是哨兵（mtime/size 与磁盘一致）的文件，本次 size 组
    >=2（另一同 size 文件进入扫描）→ 哨兵不算命中，必须真算哈希：运行后两条缓存
    条目变为真实 sha256，且内容不同的二者不成组（结果为空）。"""
    t = dup_tree
    c1, c2 = b"x" * 500, b"y" * 500  # 同 size 不同内容
    t.add_file("A/i1.bin", c1)
    t.add_file("B/i2.bin", c2)
    t.mkdir("trash")
    # 手工种入"上次运行认为二者 size 唯一"的哨兵缓存（mtime/size 与磁盘一致）
    st1, st2 = (t.workdir / "A/i1.bin").stat(), (t.workdir / "B/i2.bin").stat()
    seeded = {
        "A/i1.bin": {"file_id": UNIQUE_SIZE_SENTINEL, "modified_time": st1.st_mtime, "size": st1.st_size},
        "B/i2.bin": {"file_id": UNIQUE_SIZE_SENTINEL, "modified_time": st2.st_mtime, "size": st2.st_size},
    }
    t.cache_path().write_text(json.dumps(seeded))

    proc = t.run("-d", "A", "B", "--action", "move", "-m", "trash",
                 "--retain", "i1.bin", "i2.bin")
    assert proc.returncode == 0, console_output(proc)

    cache = json.loads(t.cache_path().read_text())
    assert cache["A/i1.bin"]["file_id"] == _sha(c1)
    assert cache["B/i2.bin"]["file_id"] == _sha(c2)
    data, _ = t.load_result_json()
    assert data == {}  # 内容不同不成组；哨兵值也从不出现在结果里


# ---------------------------------------------------------------------------
# AC3 slow：旧格式真实 sha256 缓存全部命中、逐字节不变、零重算
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_legacy_sha256_cache_all_hits_byte_identical(dup_tree_factory):
    """用改造前版本会产出的缓存格式（真实 sha256、mtime/size 与磁盘一致）预热一棵
    全配对树：载入后每一条都命中，运行前后缓存文件内容逐字节不变——零重算的回归底线。"""
    t = dup_tree_factory("legacy-cache")
    contents = {}
    for i in range(8):
        c = (f"legacy-{i}-".encode() + b".") * 150
        c = c[:600 + i]  # 每对不同 size，避免跨对撞 size
        contents[i] = c
        _make_pair(t, f"A/l{i}.bin", f"B/l{i}.bin", c)

    legacy = {}
    for rel, data in [("A/l%d.bin" % i, contents[i]) for i in range(8)] + \
                     [("B/l%d.bin" % i, contents[i]) for i in range(8)]:
        st = (t.workdir / rel).stat()
        legacy[rel] = {"file_id": _sha(data), "modified_time": st.st_mtime, "size": st.st_size}
    before_bytes = json.dumps(legacy)
    t.cache_path().write_text(before_bytes)

    retain = tuple(f"l{i}.bin" for i in range(8))
    proc = _real_run_retained(t, retain)
    out = console_output(proc)
    assert proc.returncode == 0, out

    # 缓存文件逐字节不变：命中条目原样复用、无重算写回、无哨兵覆盖（树内无唯一 size）
    assert t.cache_path().read_bytes().decode() == before_bytes

    # 结果分组仍以真实 sha256 为 key 且成员完整——命中值直接参与分组
    data, _ = t.load_result_json()
    assert _project(data) == {
        _sha(contents[i]): sorted([(f"A/l{i}.bin", len(contents[i])),
                                   (f"B/l{i}.bin", len(contents[i]))])
        for i in range(8)
    }


@pytest.mark.slow
def test_legacy_cache_hit_path_never_recomputes(dup_tree_factory):
    """命中路径的行为学证明：预热缓存的 file_id 全部替换为等长的错误 hex（与磁盘
    内容哈希不符、但 mtime/size 一致且非哨兵）。运行后：结果分组 key 用的是缓存里的
    错误 id（若发生重算，key 必是真实 sha256），且缓存文件逐字节不变——证明命中
    条目被原样复用、内容从未被读取重算。"""
    t = dup_tree_factory("poisoned-legacy")
    contents = {}
    for i in range(5):
        c = (f"poison-{i}-".encode() + b".") * 140
        c = c[:520 + i]
        contents[i] = c
        _make_pair(t, f"A/p{i}.bin", f"B/p{i}.bin", c)

    fake = {i: _sha(f"definitely-not-the-file-content-{i}".encode()) for i in range(5)}
    entries = {}
    for i in range(5):
        for rel in (f"A/p{i}.bin", f"B/p{i}.bin"):
            st = (t.workdir / rel).stat()
            entries[rel] = {"file_id": fake[i], "modified_time": st.st_mtime, "size": st.st_size}
    before = json.dumps(entries)
    t.cache_path().write_text(before)

    retain = tuple(f"p{i}.bin" for i in range(5))
    proc = _real_run_retained(t, retain)
    assert proc.returncode == 0, console_output(proc)

    data, _ = t.load_result_json()
    # key 是伪造 id 而非真实 sha256 → 命中路径零重算
    assert set(data) == {fake[i] for i in range(5)}
    for i in range(5):
        members = sorted((e["path"], e["size"]) for e in data[fake[i]])
        assert members == sorted([(f"A/p{i}.bin", len(contents[i])),
                                  (f"B/p{i}.bin", len(contents[i]))])
    assert t.cache_path().read_bytes().decode() == before


# ---------------------------------------------------------------------------
# AC4 大目录基准：哈希条目数远小于文件总数
# ---------------------------------------------------------------------------

def test_hash_entries_much_fewer_than_total_files(dup_tree):
    """260 文件树：250 个 size 全局唯一 + 5 对同 size 重复。缓存中真实哈希条目恰为
    同 size 组成员数（10 条），其余 250 条为哨兵——"写过哈希"的文件数远小于文件
    总数（预过滤让唯一 size 者根本不进哈希队列）。"""
    t = dup_tree
    n_unique = 250
    for i in range(n_unique):
        t.add_file(f"A/s{i:03d}.bin", b"s" * (i + 1))          # size 1..250 互不相同
    pair_sizes = [512 + 10 * k for k in range(5)]              # 均 > 250，互不相同
    pair_digests = {}
    for k, size in enumerate(pair_sizes):
        c = (f"pair-{k}-".encode() + b"#") * (size // 8 + 1)
        c = c[:size]
        pair_digests[k] = _sha(c)
        _make_pair(t, f"A/pp{k}.bin", f"B/pp{k}.bin", c)

    proc = _real_run_retained(t, tuple(f"pp{k}.bin" for k in range(5)))
    assert proc.returncode == 0, console_output(proc)

    cache = json.loads(t.cache_path().read_text())
    total = n_unique + 10
    assert len(cache) == total, "全部候选（唯一者+哈希者）都应有缓存条目"
    sentinel_ids = [rel for rel, v in cache.items() if v["file_id"] == UNIQUE_SIZE_SENTINEL]
    hashed_ids = [rel for rel, v in cache.items() if v["file_id"] != UNIQUE_SIZE_SENTINEL]
    assert len(sentinel_ids) == n_unique, "size 唯一者应恰为哨兵条目"
    assert len(hashed_ids) == 10, "真实哈希条目应恰为同 size 组成员"
    assert len(hashed_ids) < total / 2, "哈希条目数应远小于文件总数"

    # 哈希条目值确为真实内容摘要；结果 JSON 恰为 5 个配对组
    for rel in hashed_ids:
        p = t.workdir / rel
        assert cache[rel]["file_id"] == _sha(p.read_bytes())
    data, _ = t.load_result_json()
    assert set(data) == set(pair_digests.values())
    for k in range(5):
        assert sorted(e["path"] for e in data[pair_digests[k]]) == [f"A/pp{k}.bin", f"B/pp{k}.bin"]

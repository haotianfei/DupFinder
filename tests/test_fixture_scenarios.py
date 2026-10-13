"""fixture 工厂与终态断言辅助自身的用例：七类场景可构造、缓存两态可表达、辅助函数可靠。

只断言当前已有行为；空文件跳过、cross-dirs-only 语义、--try-run 不写正式缓存等
属后续计划中的行为升级，本文件对其不预设立场（对 CLI 的调用仅限无不可读/0 字节/
软链接参与断言语义的最小树）。
"""

import hashlib
import json
import os

import pytest


def test_factory_builds_all_seven_scenarios(full_tree):
    """一棵临时树内构造全部六类目录场景：跨目录重复对/目录内部重复/0 字节/软链接/不可读/size 唯一；
    第七类"缓存未热"由本树的冷缓存态体现（"预热"见下一用例）。"""
    t = full_tree

    # 1. 跨目录重复对：A 与 B 各一个，内容逐字节相同
    assert t.byte_identical("A/x_cross.txt", t.cross_content)
    assert t.byte_identical("B/x_cross.dup", t.cross_content)

    # 2. 目录内部重复：A 与其子目录内两文件内容相同
    assert t.byte_identical("A/y_indir1.txt", t.indir_content)
    assert t.byte_identical("A/sub/y_indir2.txt", t.indir_content)

    # 3. 0 字节文件
    assert t.workdir.joinpath("A/empty.bin").stat().st_size == 0
    assert t.workdir.joinpath("B/empty.bin").stat().st_size == 0

    # 4. 软链接文件：相对链接指向同目录目标，跟随读内容一致
    link = t.workdir / "B/sym_link.txt"
    assert link.is_symlink()
    assert link.readlink().name == "sym_target.txt"
    assert t.byte_identical("B/sym_link.txt", t.sym_content)

    # 5. 不可读文件：权限位 000；非 root 下确实读不了（root 无权限概念，只查权限位）。
    #    它刻意与一个同尺寸可读伴生文件共享 size，以便在阶段二被真实尝试读取并触发警告
    #    （若其 size 全局唯一，会被哨兵短路、根本不读内容，警告路径无从验证）。
    unreadable = t.workdir / "A/secret.bin"
    twin = t.workdir / "B/readable_twin.bin"
    assert unreadable.stat().st_mode & 0o777 == 0
    assert unreadable.stat().st_size == twin.stat().st_size
    if os.geteuid() != 0:
        assert not os.access(unreadable, os.R_OK)

    # 6. size 全局唯一文件：大小不与树内任何其他文件相同（会被哨兵短路、不读内容）
    size_count = {}
    for state in t.snapshot().values():
        size_count[state[0]] = size_count.get(state[0], 0) + 1
    for rel in ("A/unique_a.bin", "B/unique_b.bin"):
        size = (t.workdir / rel).stat().st_size
        assert size_count[size] == 1, f"{rel} 的 size {size} 不全局唯一"

    # 7. 缓存未热：full_tree 为冷启动，无缓存文件
    assert not t.cache_path().exists()


def test_cache_cold_and_warm_states(dup_tree_factory):
    """缓存"未热/预热"两种起跑态各建一棵树：冷树无缓存文件；预热缓存与正式 JSON
    格式逐字段兼容（key=相对路径，value=file_id(sha256)/modified_time/size），条目与磁盘实态一致。"""
    cold = dup_tree_factory("cold")
    cold.build_full_scenarios()
    assert not cold.cache_path().exists()

    warm = dup_tree_factory("warm")
    warm.build_full_scenarios()
    n = warm.prewarm_cache()
    assert n > 0
    entries = json.loads(warm.cache_path().read_text())
    assert len(entries) == n
    for rel, entry in entries.items():
        assert set(entry) == {"file_id", "modified_time", "size"}
        p = warm.workdir / rel
        assert entry["file_id"] == hashlib.sha256(p.read_bytes()).hexdigest()
        assert entry["size"] == p.stat().st_size
        assert entry["modified_time"] == p.stat().st_mtime
    # 不可读文件与软链接不入预热缓存（理由见 prewarm_cache 文档字符串）
    assert "A/secret.bin" not in entries
    assert "B/sym_link.txt" not in entries


def test_factory_builds_independent_trees(dup_tree_factory):
    """factory 可在一个用例内建多棵互不干扰的隔离树：一棵跑真实扫描，另一棵终态零变化。"""
    t1 = dup_tree_factory("first")
    t2 = dup_tree_factory("second")
    assert t1.workdir != t2.workdir and t1.workdir.is_dir() and t2.workdir.is_dir()

    t1.build_minimal()
    before2 = t2.snapshot()
    proc = t1.run("-d", "A", "B")  # 真实扫描模式才会产出结果文件
    assert proc.returncode == 0, proc.stderr

    data, path = t1.load_result_json()
    assert path.parent == t1.workdir and data
    t2.assert_no_file_changed(before2)
    assert t2.artifact_digests() == {}


def test_terminal_state_helpers(dup_tree):
    """终态断言辅助可靠：无变化时 changed_paths 为空集；新增/篡改能被精确点名；
    assert_file_unchanged 对未动文件通过、assert_no_file_changed 对已动树抛错；
    一次 --try-run 扫描不新增任何源文件变化，且正式产物零写入、日志仍落在 workdir。"""
    t = dup_tree
    t.build_minimal()
    before = t.snapshot()
    assert t.changed_paths(before) == set()
    assert t.artifact_digests() == {}  # 起跑前无任何产物

    # 新增一个唯一 size 文件 → 被点名
    t.add_file("A/extra_note.txt", b"n")
    assert t.changed_paths(before) == {"A/extra_note.txt"}

    # 篡改跨目录对之一 → 连同新增一起被点名；未动文件单独断言仍不变
    (t.workdir / "A/x_cross.txt").write_bytes(b"tampered-9")
    assert t.changed_paths(before) == {"A/extra_note.txt", "A/x_cross.txt"}
    t.assert_file_unchanged("B/x_cross.dup", before)
    with pytest.raises(AssertionError):
        t.assert_no_file_changed(before)

    # 一次 try-run 扫描：退出码 0，扫描后源树终态与扫描前完全一致（无新变化）
    stable = t.snapshot()
    proc = t.run_scan()
    assert proc.returncode == 0, proc.stderr
    t.assert_no_file_changed(stable)
    assert t.changed_paths(before) == {"A/extra_note.txt", "A/x_cross.txt"}

    # 演练模式零正式产物：不写结果 JSON、不写缓存；日志照常落在 workdir（隔离在位）
    assert list(t.workdir.glob("duplicates_*.json")) == []
    assert not t.cache_path().exists()
    digests = t.artifact_digests()
    assert any(name.startswith("logs/") for name in digests)
    assert not any(name.startswith("duplicates_") for name in digests)
    assert "file_cache.json" not in digests

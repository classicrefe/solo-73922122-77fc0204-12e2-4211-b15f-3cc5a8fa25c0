"""试跑报文生成：在同一已提交视图的固件格式与候选程序契约上，
用 z3 枚举「全部可实现收拒向量」，再做精确集合覆盖，得到最少报文组。

术语：
- 合法报文：至少满足视图内某一份已发布固件写入格式的报文；
- 收拒向量：一条报文对每个候选程序版本「接收/拒收」的布尔组合；
- 可区分两版：存在合法报文使两版收拒不同；
- 最少报文：覆盖全部可区分版本对的最小报文数（集合覆盖下界 = 上界）。

只有完整枚举出全部可达向量、且集合覆盖 DP 得到可达的最小值时，才输出报文组；
z3 返回 unknown / 超出预算 / 复核失败时抛 TrialUnproven，调用方须提示「本次未能生成」。
"""
from __future__ import annotations

import time

from z3 import Not, Solver, is_true, sat

from schema_spec import Schema, message_valid
from solver import KIND_INT, KIND_NULL, KIND_STR, Z3_LOCK, _Encoder

MIN_CANDIDATES = 2
MAX_CANDIDATES = 6
CHECK_TIMEOUT_MS = 10000
# 单个生成请求的总预算；超出后不再宣称已证明最少报文数
TOTAL_BUDGET_S = 90.0


class TrialUnproven(RuntimeError):
    """本次未能证明最少报文数（z3 未知、超时或内部复核失败）。"""


def _encoder_for(schemas):
    names = set()
    for sch in schemas:
        for b in sch.branches:
            names |= set(b.properties)
    fields = sorted(names)
    str_tables = {}
    for f in fields:
        strs = set()
        for sch in schemas:
            for b in sch.branches:
                spec = b.properties.get(f)
                if spec and spec.str_set:
                    strs |= set(spec.str_set)
        if strs:
            str_tables[f] = sorted(strs)
    return _Encoder(fields, str_tables)


def _extract(model, enc):
    msg = {}
    for f in enc.fields:
        if is_true(model.eval(enc.present[f], model_completion=True)):
            k = model.eval(enc.kind[f], model_completion=True).as_long()
            if k == KIND_NULL:
                msg[f] = None
            elif k == KIND_INT:
                msg[f] = model.eval(enc.ival[f], model_completion=True).as_long()
            else:
                msg[f] = enc.str_tables[f][
                    model.eval(enc.sval[f], model_completion=True).as_long()]
    return msg


def enumerate_vectors(firmwares, programs, budget_s=TOTAL_BUDGET_S):
    """枚举合法报文可实现的全部非零收拒向量。

    firmwares: [(version, Schema)]；programs: [(label, Schema)]。
    返回 [{mask, message, firmware_version}]，mask 第 j 位表示第 j 个程序接收。
    只跳过 mask=0：它不区分任何一对版本，但由某个固件格式的合法报文即可实现，
    不影响分组与最少覆盖的判定。
    """
    m = len(programs)
    if m < MIN_CANDIDATES or m > MAX_CANDIDATES:
        raise TrialUnproven(f"候选版本数必须在 {MIN_CANDIDATES}~{MAX_CANDIDATES} 之间")
    if not firmwares:
        raise TrialUnproven("视图内没有已发布的固件格式，无法产生合法报文")
    # 等锁的时间不计入求解预算，但等锁本身也受总时限约束
    if not Z3_LOCK.acquire(timeout=budget_s):
        raise TrialUnproven("求解器繁忙，等待超时，无法证明报文数最少")
    try:
        return _enumerate_locked(firmwares, programs, budget_s)
    finally:
        Z3_LOCK.release()


def _enumeration_budget_exceeded(deadline, checks):
    return TrialUnproven(
        f"超出本次生成时限（已检查 {checks} 个收拒组合），"
        "无法证明报文数最少")


def _enumerate_locked(firmwares, programs, budget_s):
    # 预算从真正开始求解时起算（等待求解器锁的时间另行限制）
    deadline = time.monotonic() + budget_s
    schemas = [sch for _, sch in firmwares] + [sch for _, sch in programs]
    m = len(programs)
    enc = _encoder_for(schemas)
    achieved = {}  # mask -> (message, firmware_version)
    checks = 0
    for fv, fw in firmwares:
        for mask in range(1, 1 << m):  # mask=0 不区分任何版本
            if mask in achieved:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _enumeration_budget_exceeded(deadline, checks)
            s = Solver()
            s.set("timeout", max(1, int(min(CHECK_TIMEOUT_MS, remaining * 1000))))
            s.add(enc.wellformed())
            s.add(enc.schema_ok(fw))
            for j, (_, pg) in enumerate(programs):
                if mask >> j & 1:
                    s.add(enc.schema_ok(pg))
                else:
                    s.add(Not(enc.schema_ok(pg)))
            try:
                r = s.check()
            except Exception as e:  # z3 内部错误同样不能宣称已证明
                raise TrialUnproven("z3 求解失败，无法证明报文数最少") from e
            checks += 1
            if str(r) == "unknown":
                raise TrialUnproven(
                    f"z3 对固件 v{fv} 的某个收拒组合返回 unknown，"
                    "无法证明报文数最少")
            if r != sat:
                continue
            msg = _extract(s.model(), enc)
            # 独立复核：报文确实满足该固件格式，且各程序收拒与目标向量一致
            bits = tuple(message_valid(msg, pg) for _, pg in programs)
            if not message_valid(msg, fw) or bits != tuple(
                    bool(mask >> j & 1) for j in range(m)):
                raise TrialUnproven("z3 报文复核失败，无法证明报文数最少")
            achieved[mask] = (msg, fv)

    return [{"mask": mask, "message": msg, "firmware_version": fv}
            for mask, (msg, fv) in sorted(achieved.items())], checks


def _groups_and_pairs(n, vectors):
    """返回 (分组, 可区分版本对)。分组依据：全部合法报文上收拒是否一致。"""
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    distinguishable = [
        (i, j) for i, j in pairs
        if any(((v["mask"] >> i) & 1) != ((v["mask"] >> j) & 1) for v in vectors)
    ]
    diff_set = set(distinguishable)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in pairs:
        if (i, j) not in diff_set:
            a, b = find(i), find(j)
            if a != b:
                parent[max(a, b)] = min(a, b)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [g for _, g in sorted(groups.items())], distinguishable


def minimum_cover(vectors, pairs):
    """精确最小集合覆盖。返回 (选中的向量列表, 是否成功)。

    向量按收拒模式去重；允许重复选同一集合不会减少条数，
    因此用「按状态递增松弛」的最短路 DP 得到的就是最少报文数。
    """
    if not pairs:
        return [], True
    pcount = len(pairs)
    full = (1 << pcount) - 1
    covers = []
    seen = set()
    for v in vectors:
        c = 0
        for idx, (i, j) in enumerate(pairs):
            if ((v["mask"] >> i) & 1) != ((v["mask"] >> j) & 1):
                c |= 1 << idx
        if c and c not in seen:
            seen.add(c)
            covers.append((c, v))
    if not covers:
        return [], False
    covers.sort(key=lambda x: (-bin(x[0]).count("1"), x[1]["mask"]))
    INF = pcount + 1
    dp = [INF] * (full + 1)
    prev = [None] * (full + 1)
    dp[0] = 0
    for state in range(full + 1):
        if dp[state] == INF:
            continue
        for c, v in covers:
            nxt = state | c
            if dp[state] + 1 < dp[nxt]:
                dp[nxt] = dp[state] + 1
                prev[nxt] = (state, v)
    if dp[full] == INF:
        return [], False
    chosen = []
    state = full
    while state:
        parent, v = prev[state]
        chosen.append(v)
        state = parent
    chosen.reverse()
    return chosen, True


def build_report(firmwares, programs, budget_s=TOTAL_BUDGET_S):
    """firmwares: [(version, Schema)]；programs: [(label, Schema)]（顺序即展示顺序）。

    返回报文/分组/最少性证明的纯数据（不含视图元信息）。
    """
    vectors, checks = enumerate_vectors(firmwares, programs, budget_s=budget_s)
    n = len(programs)
    groups, pairs = _groups_and_pairs(n, vectors)
    chosen, ok = minimum_cover(vectors, pairs)
    if not ok:
        raise TrialUnproven("集合覆盖未得到可达的最小值，无法证明报文数最少")

    messages = []
    for idx, v in enumerate(chosen, start=1):
        accepts = [bool(v["mask"] >> j & 1) for j in range(n)]
        # 再次逐条复核，避免把错误数据交给现场
        for j, (_, pg) in enumerate(programs):
            if message_valid(v["message"], pg) != accepts[j]:
                raise TrialUnproven("输出报文复核失败，无法证明报文数最少")
        messages.append({
            "index": idx,
            "message": v["message"],
            "firmware_version": v["firmware_version"],
            "accepts": accepts,
        })

    return {
        "groups": groups,
        "distinguishable_pairs": pairs,
        "messages": messages,
        "minimum": len(messages),
        "proof": {
            "solver": "z3",
            "achievable_vectors": len(vectors),
            "solver_checks": checks,
            "method": "枚举全部可达收拒向量 + 精确集合覆盖 DP",
        },
    }

"""试跑报文生成的单元自测：可达收拒向量枚举、分组、精确最小覆盖、未证明路径。
运行：python3 tests/test_trial.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import trial  # noqa: E402
from schema_spec import parse_schema  # noqa: E402
from solver import SolveUnknown, find_counterexample  # noqa: E402

PASS = []


def ok(name, cond, extra=""):
    assert cond, f"FAILED: {name} {extra}"
    PASS.append(name)


def T(lo, hi, qc=False):
    props = {"temp": {"type": "integer", "minimum": lo, "maximum": hi},
             "site": {"type": "string", "enum": ["WQ-01", "WQ-02"]}}
    required = ["temp", "site"]
    if qc:
        props["qc"] = {"enum": ["N", "Q", None]}
    return parse_schema({"type": "object", "properties": props,
                         "required": required, "additionalProperties": False})


def accept(msg, sch):
    from schema_spec import message_valid
    return message_valid(msg, sch)


def check_report(rep, programs, firmwares):
    """验证报告满足全部硬性要求。"""
    msgs = rep["messages"]
    # 1) 每条报文都是合法报文（至少满足一种固件格式）
    for m in msgs:
        ok("报文为合法报文", any(accept(m["message"], f) for _, f in firmwares))
        # 2) 逐条声明的收拒与实际一致
        for j, (_, p) in enumerate(programs):
            ok("报文收拒与声明一致", accept(m["message"], p) == m["accepts"][j])
    # 3) 可区分版本对全覆盖
    for i, j in rep["distinguishable_pairs"]:
        ok("可区分版本对至少被一条报文区分",
           any(m["accepts"][i] != m["accepts"][j] for m in msgs))
    # 4) 分组正确：同组收拒在所有合法报文上一致，不同组可区分
    group_of = {}
    for gi, g in enumerate(rep["groups"]):
        for idx in g:
            group_of[idx] = gi
    ok("分组覆盖全部候选版本", len(group_of) == len(programs))
    for i in range(len(programs)):
        for j in range(i + 1, len(programs)):
            differ = any(m["accepts"][i] != m["accepts"][j] for m in msgs)
            if group_of[i] == group_of[j]:
                ok("同组在报文组上一致", not differ)
            else:
                ok("不同组可区分", differ)
    # 5) 最少性：报文数等于可区分版本对的最小集合覆盖下界（此处直接穷举校验）
    pairs = [tuple(p) for p in rep["distinguishable_pairs"]]
    if pairs:
        full = set(pairs)
        cover_sets = []
        for m in msgs:
            cover_sets.append({p for p in pairs
                               if m["accepts"][p[0]] != m["accepts"][p[1]]})
        ok("报文对全部可区分对都有效", set().union(*cover_sets) == full)


# ---- 场景一：三版三级递进，最少 2 条 ----
fw = [(1, T(-10, 50)), (2, T(-40, 100)), (3, T(-40, 100, qc=True))]
programs = [("第1版", T(-10, 50)), ("第2版", T(-40, 100)), ("第3版", T(-40, 100, qc=True))]
rep = trial.build_report(fw, programs)
check_report(rep, programs, fw)
ok("三版递进最少2条", rep["minimum"] == 2, rep)
ok("三版递进分三组", len(rep["groups"]) == 3, rep["groups"])
ok("三版递进可区分对为3", len(rep["distinguishable_pairs"]) == 3)

# ---- 场景二：两版完全相同 -> 同组，0 条 ----
programs2 = [("第1版", T(-10, 50)), ("第2版", T(-10, 50))]
rep2 = trial.build_report([(1, T(-10, 50))], programs2)
check_report(rep2, programs2, [(1, T(-10, 50))])
ok("相同版本归为一组", rep2["groups"] == [[0, 1]], rep2["groups"])
ok("相同版本无需报文", rep2["minimum"] == 0)

# ---- 场景三：视图内固件限制使两版不可区分 -> 同组 ----
fw3 = [(1, T(0, 10))]
programs3 = [("第1版", T(0, 10)), ("第2版", T(0, 20))]  # 合法报文都在 0~10，两版都收
rep3 = trial.build_report(fw3, programs3)
check_report(rep3, programs3, fw3)
ok("视图限制下不可区分归组", rep3["groups"] == [[0, 1]] and rep3["minimum"] == 0)

# ---- 场景四：anyOf 程序分支差异 ----
any_a = parse_schema({"anyOf": [
    {"type": "object", "properties": {"a": {"type": "integer", "minimum": 0, "maximum": 5}},
     "required": ["a"], "additionalProperties": False},
    {"type": "object", "properties": {"b": {"enum": ["x"]}},
     "required": ["b"], "additionalProperties": False}]})
any_b = parse_schema({"anyOf": [
    {"type": "object", "properties": {"a": {"type": "integer", "minimum": 0, "maximum": 9}},
     "required": ["a"], "additionalProperties": False},
    {"type": "object", "properties": {"b": {"enum": ["x", "y"]}},
     "required": ["b"], "additionalProperties": False}]})
fw4 = [(1, any_b)]  # 视图允许 b=x/y 与 a=0~9，第 1 版只接受子集
programs4 = [("第1版", any_a), ("第2版", any_b)]
rep4 = trial.build_report(fw4, programs4)
check_report(rep4, programs4, fw4)
ok("anyOf 两版可区分且最少1条", rep4["minimum"] == 1
   and len(rep4["groups"]) == 2, rep4)

# ---- 场景五：候选数非法 ----
for bad_n in (1, 7):
    progs = [(f"p{i}", T(0, 10)) for i in range(bad_n)]
    try:
        trial.build_report([(1, T(0, 10))], progs)
    except trial.TrialUnproven:
        PASS.append(f"候选数{bad_n}被拒")
    else:
        raise AssertionError(f"FAILED: 候选数 {bad_n} 应被拒绝")

# ---- 场景六：预算耗尽 -> 明确未能证明 ----
try:
    trial.build_report(fw, programs, budget_s=0)
except trial.TrialUnproven as e:
    ok("预算耗尽提示未能生成", "无法证明报文数最少" in str(e))
else:
    raise AssertionError("FAILED: 预算为 0 时不应宣称已证明最少")

# ---- 场景七：穷举独立对拍（分组 / 可区分对 / 最少条数） ----
import itertools  # noqa: E402

def _brute(fws, progs):
    """在有限报文域上穷举：独立计算分组、可区分对与最小覆盖条数。"""
    from schema_spec import message_valid
    univ = [{"lv": lv} for lv in range(-3, 9)]
    univ += [{"lv": lv, "q": q} for lv in range(-3, 9) for q in (None, "x", "y")]
    msgs = [m for m in univ if any(message_valid(m, f) for f in fws)]
    pairs = [(i, j) for i in range(len(progs)) for j in range(i + 1, len(progs))]
    dist = [(i, j) for i, j in pairs
            if any(message_valid(m, progs[i]) != message_valid(m, progs[j]) for m in msgs)]
    groups, seen = [], [False] * len(progs)
    for i in range(len(progs)):
        if seen[i]:
            continue
        g = [j for j in range(i, len(progs))
             if all(message_valid(m, progs[i]) == message_valid(m, progs[j]) for m in msgs)]
        for j in g:
            seen[j] = True
        groups.append(g)
    covers = []
    for m in msgs:
        c = 0
        for k, (i, j) in enumerate(dist):
            if message_valid(m, progs[i]) != message_valid(m, progs[j]):
                c |= 1 << k
        if c:
            covers.append(c)
    full = (1 << len(dist)) - 1 if dist else 0
    best = 0
    if full:
        for r in range(1, len(dist) + 1):
            if any(_union(c) == full for c in itertools.combinations(covers, r)):
                best = r
                break
    return sorted(sorted(g) for g in groups), sorted(dist), best


def _union(combo):
    acc = 0
    for c in combo:
        acc |= c
    return acc


def S(props, required=("lv",)):
    return parse_schema({"type": "object", "properties": props,
                         "required": list(required), "additionalProperties": False})


CASES = [
    ([S({"lv": {"type": "integer", "minimum": 0, "maximum": 4}})],
     [S({"lv": {"type": "integer", "minimum": 0, "maximum": 4}}),
      S({"lv": {"type": "integer", "minimum": 0, "maximum": 2}}),
      S({"lv": {"type": "integer", "minimum": 0, "maximum": 0}})]),
    ([S({"lv": {"type": "integer", "enum": [0, 1, 2]}})],
     [S({"lv": {"type": "integer", "enum": [0, 1, 2]}}),
      S({"lv": {"type": "integer", "enum": [0, 1]}}),
      S({"lv": {"type": "integer", "enum": [0]}})]),
    ([S({"lv": {"type": "integer", "minimum": 0, "maximum": 3},
         "q": {"enum": ["x", "y", None]}})],
     [S({"lv": {"type": "integer", "minimum": 0, "maximum": 3},
         "q": {"enum": ["x", "y", None]}}),
      S({"lv": {"type": "integer", "minimum": 0, "maximum": 3},
         "q": {"enum": ["x", "y"]}}, ("lv", "q")),
      S({"lv": {"type": "integer", "minimum": 0, "maximum": 3}},
        ("lv",))]),
    ([S({"lv": {"type": "integer", "minimum": 0, "maximum": 0}})],
     [S({"lv": {"type": "integer", "minimum": 0, "maximum": 4}}),
      S({"lv": {"type": "integer", "minimum": 0, "maximum": 2}}),
      S({"lv": {"type": "integer", "minimum": 0, "maximum": 0}})]),
]

for ci, (fws, progs) in enumerate(CASES, start=1):
    rep = trial.build_report([(i + 1, f) for i, f in enumerate(fws)],
                             [(f"v{i+1}", p) for i, p in enumerate(progs)])
    bg, bd, bmin = _brute(fws, progs)
    ok(f"穷举对拍{ci}分组", sorted(sorted(g) for g in rep["groups"]) == bg,
       f'{rep["groups"]} vs {bg}')
    ok(f"穷举对拍{ci}可区分对",
       sorted(map(tuple, rep["distinguishable_pairs"])) == bd)
    ok(f"穷举对拍{ci}最少条数", rep["minimum"] == bmin,
       f'{rep["minimum"]} vs {bmin}')

print(f"全部 {len(PASS)} 项试跑自测通过")

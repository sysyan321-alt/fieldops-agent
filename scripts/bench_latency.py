#!/usr/bin/env python3
# scripts/bench_latency.py
# 延迟压测：把要对内/对外说明的延迟数字，用可复现的方式量出来。
#
# 覆盖各段，彼此独立，任一段不可用不影响其它段：
#   ① 冷启动      —— BGE-M3 / BGE-Reranker 模型加载耗时
#   ② 检索段      —— BGE 编码 + Milvus 混合召回 + 精排（不含 LLM 生成）
#   ③ 缓存        —— 同问检索的 Redis miss → hit
#   ④ 快通道      —— 判定段（进程内 BM25）与接口端到端（HTTP + JWT + 检查点落库）
#   ⑤ RAG 链路    —— SOP 问答慢链路端到端（检索 + LLM 生成）
#   ⑥ 统一入口    —— 意图路由端到端
#   ⑦ 告警研判    —— 到 interrupt 暂停点的端到端
#   ⑧ 照护流程    —— /start 端到端
#   ⑨ 评估链路    —— 需要一份评估表 PDF，见文件末尾说明
#
# ⚠️ ⑤⑥⑦⑧ 都需要可用的 LLM Key。Key 无效时它们会返回兜底文案，脚本会把这类
#    数字标成"无效"而不是当成真实延迟——否则量到的是"LLM 快速失败"的时间。
#
# 用法：
#   python scripts/bench_latency.py                    # 全部（需要基础设施已启动）
#   python scripts/bench_latency.py --no-e2e           # 只跑不需要后端的部分
#   python scripts/bench_latency.py --repeat 10        # 调整采样次数
#
# 依赖：基础设施（PostgreSQL/Redis/Milvus）与本地模型权重；④⑤ 还需要后端在 8000 端口。
# 所有数字都带口径：本机 CPU、预热后、样本数。引用时必须连口径一起写。

import argparse
import asyncio
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

BACKEND = "http://127.0.0.1:8000"
DEFAULT_USER = ("supervisor", "Supervisor@123")
# 各 Agent 返回兜底文案时会出现这些词；出现即说明该次请求没走到真实 LLM
FALLBACK_MARKERS = ("失败", "暂时不可用", "稍后重试", "转交站长人工核实")

# 汇总表：每段把自己的结论追加进来，最后统一打印
SUMMARY: list[tuple[str, str]] = []

# LLM 预检结果：Key 不可用时，所有含 LLM 的段都不可信
# （意图识别会静默走关键词降级、告警研判会静默用默认风险等级，都不吐"失败"字样）
LLM_OK = False

# 这批问句必须「不命中快通道」，才能量到完整的检索段
RETRIEVAL_QUERIES = [
    "老人最近夜里总起夜，白天精神不好，护理上要注意什么？",
    "为什么老人会突然吃不下饭？",
    "长期卧床的老人皮肤应该怎么观察？",
    "老人情绪低落不愿意配合照护怎么办？",
    "如何判断老人有没有脱水？",
]
# 这批必须「命中快通道」（取自 data/quick_index 的相似问法，已实测命中）
QUICK_QUERIES = [
    "Morse量表怎么评？",
    "压疮分几期？",
    "防滑垫放哪里？",
    "床栏啥时拉起来？",
]


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def line(label: str, values: list[float], unit: str = "ms") -> str:
    if not values:
        return f"  {label}: 无样本"
    # P50 是中位数，不等于平均值；两个都给，避免"平均值"口径含糊
    return (f"  {label}: mean {statistics.fmean(values):.2f}{unit} | "
            f"P50 {pct(values, 0.5):.2f}{unit} | "
            f"P95 {pct(values, 0.95):.2f}{unit} | "
            f"min {min(values):.2f} | max {max(values):.2f} | n={len(values)}")


def preflight_llm() -> tuple[bool, str]:
    """发一次最小请求确认 LLM Key 可用。Key 坏了的话，所有"看起来很快"的
    LLM 链路数字都是假的——降级路径不会报错，只会悄悄返回兜底答案。"""
    from backend.config import get_settings

    s = get_settings()
    provider = (s.default_llm_provider or "qwen").strip().lower()
    if provider == "deepseek":
        key, base, model = s.deepseek_api_key, s.deepseek_base_url, s.deepseek_model_chat
    else:
        key, base, model = s.qwen_api_key, s.qwen_base_url, s.qwen_model_chat
    if not key:
        return False, f"{provider} 未配置 API Key"
    try:
        import httpx

        resp = httpx.post(
            base.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model,
                  "messages": [{"role": "user", "content": "回复 ok"}],
                  "max_tokens": 4},
            timeout=20,
        )
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"
    if resp.status_code == 200:
        return True, f"{provider} / {model} 可用"
    return False, f"HTTP {resp.status_code}: {resp.text[:160]}"


# ── ① 冷启动 ──────────────────────────────────────────────────

def bench_cold_start() -> None:
    print("\n=== ① 冷启动：模型加载 ===")
    from backend.core.knowledge_base import BGEMEmbedder
    from backend.core.reranker import BGEReranker

    t = time.perf_counter(); BGEMEmbedder.get_instance(); emb = time.perf_counter() - t
    t = time.perf_counter(); BGEReranker.get_instance(); rrk = time.perf_counter() - t
    print(f"  BGE-M3 嵌入模型   : {emb:.2f} s")
    print(f"  BGE-Reranker 精排 : {rrk:.2f} s")
    print(f"  冷启动合计        : {emb + rrk:.2f} s")
    print("  口径：本机 CPU、本地权重、进程首次调用")
    SUMMARY.append(("① 模型冷启动", f"{emb + rrk:.2f}s（M3 {emb:.2f}s + Reranker {rrk:.2f}s）"))


# ── ② 检索段 ──────────────────────────────────────────────────

def bench_retrieval(repeat: int) -> None:
    print("\n=== ② 检索段（BGE 编码 + Milvus 混合召回 + 精排，不含 LLM）===")
    from backend.core.reranker import retrieve

    latencies: list[float] = []
    for i in range(repeat):
        query = RETRIEVAL_QUERIES[i % len(RETRIEVAL_QUERIES)]
        t = time.perf_counter()
        docs, conf = retrieve(query, "tenant_default", None, 10, 3)
        dt = (time.perf_counter() - t) * 1000
        latencies.append(dt)
        print(f"    {dt:8.1f} ms | {len(docs)} 段 | conf={conf:.3f}")
    if len(latencies) > 1:
        print("  首次调用含懒加载，单独看：", f"{latencies[0]:.1f} ms")
        print(line("预热后", latencies[1:]))
    else:
        print(line("全部", latencies))
    print("  口径：本机 CPU、模型已加载、Milvus 110 个 chunk")
    warm = latencies[1:] if len(latencies) > 1 else latencies
    if warm:
        SUMMARY.append(("② 检索段（不含 LLM）",
                        f"mean {statistics.fmean(warm):.0f}ms / P95 {pct(warm, 0.95):.0f}ms（n={len(warm)}）"))


# ── ③ 缓存 ────────────────────────────────────────────────────

async def bench_cache() -> None:
    print("\n=== ③ 检索结果缓存：Redis miss → hit ===")
    from backend.core.cache import get_cache
    from backend.core.retrieval_cache import cached_retrieve

    if not await get_cache().ping():
        print("  Redis 不可用（缓存是 fail-open，主链路不受影响）")
        return

    # 用带时间戳的问句保证首次一定 miss：否则上一轮压测留下的缓存会让"miss"变成命中
    query = f"鼻饲管多久更换一次？（压测 {time.time_ns()}）"
    t = time.perf_counter()
    docs1, _ = await cached_retrieve(query, "tenant_default", None, 10, 3)
    miss = (time.perf_counter() - t) * 1000
    t = time.perf_counter()
    docs2, _ = await cached_retrieve(query, "tenant_default", None, 10, 3)
    hit = (time.perf_counter() - t) * 1000
    print(f"  miss（走完整检索）: {miss:.1f} ms | docs={len(docs1)}")
    print(f"  hit （读 Redis）  : {hit:.1f} ms | docs={len(docs2)}")
    if hit > 0:
        print(f"  → 同问检索段加速 {miss / hit:.0f}x")
    print("  口径：同一问句连续两次、TTL 300s、本机 Redis")
    SUMMARY.append(("③ 检索缓存 miss→hit", f"{miss:.0f}ms → {hit:.1f}ms"))


# ── ④ 快通道 ──────────────────────────────────────────────────

def bench_quick_index(repeat: int) -> None:
    print("\n=== ④-1 快通道判定段（进程内 BM25 + 三重门控）===")
    from backend.core.quick_index import get_quick_index

    index = get_quick_index()
    latencies: list[float] = []
    hits = 0
    for i in range(repeat):
        query = QUICK_QUERIES[i % len(QUICK_QUERIES)]
        t = time.perf_counter()
        res = index.search(query)
        latencies.append((time.perf_counter() - t) * 1000)
        hits += 1 if res.get("hit") else 0
    print(line("判定段", latencies))
    print(f"  命中 {hits}/{repeat}")
    print("  口径：本机 CPU、73 条索引、仅判定不含 HTTP 与落库")
    SUMMARY.append(("④-1 快通道判定段", f"mean {statistics.fmean(latencies):.2f}ms（n={len(latencies)}）"))


def _login() -> str:
    req = urllib.request.Request(
        BACKEND + "/api/v1/auth/login",
        data=json.dumps({"username": DEFAULT_USER[0], "password": DEFAULT_USER[1]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())["access_token"]


def _ask(token: str, query: str, session_tag: str, timeout: int = 180) -> tuple[float, str, str]:
    """返回 (耗时ms, answer_mode, 回答正文)。"""
    payload = {"elder_id": "e001", "session_id": f"{session_tag}-{time.time_ns()}", "query": query}
    req = urllib.request.Request(
        BACKEND + "/api/v1/sop-qa/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "ignore")
    elapsed = (time.perf_counter() - t) * 1000

    mode, content = "", ""
    for raw in body.splitlines():
        if not raw.startswith("data:"):
            continue
        try:
            data = json.loads(raw[5:].strip())
        except json.JSONDecodeError:
            continue
        mode = data.get("answer_mode", mode)
        content = data.get("content", content)
    return elapsed, mode, content


def bench_e2e(repeat: int) -> None:
    print("\n=== ④-2 快通道：接口端到端（HTTP + JWT + 图执行 + 检查点落库）===")
    try:
        token = _login()
    except Exception as e:
        print(f"  后端不可用（{type(e).__name__}），跳过端到端")
        return

    latencies: list[float] = []
    others: list[str] = []
    for i in range(repeat + 1):        # 多打一发，第一发通常含懒加载
        elapsed, mode, _ = _ask(token, QUICK_QUERIES[i % len(QUICK_QUERIES)], "bench-quick")
        if i == 0:
            print(f"  首次（含懒加载）: {elapsed:.1f} ms | mode={mode}")
            continue
        # 只有真的命中快通道（mode=quick）才算快通道的延迟；其余是慢链路，不能混进来
        if mode == "quick":
            latencies.append(elapsed)
            print(f"    {elapsed:8.1f} ms | mode={mode}")
        else:
            others.append(f"{elapsed:.0f}ms/{mode}")
            print(f"    {elapsed:8.1f} ms | mode={mode}  ← 未命中快通道，不计入")
    print(line("快通道端到端", latencies))
    if others:
        print(f"  未命中（走慢链路，未计入）：{', '.join(others)}")
    print("  口径：本机 localhost、连接预热、每轮写 5 个 LangGraph 检查点")
    if latencies:
        SUMMARY.append(("④-2 快通道端到端",
                        f"mean {statistics.fmean(latencies):.1f}ms / P95 {pct(latencies, 0.95):.1f}ms（n={len(latencies)}）"))


def bench_llm(repeat: int) -> None:
    print("\n=== ⑤ RAG 链路：接口端到端（含检索 + LLM 生成）===")
    if not LLM_OK:
        print("  ⏭ LLM 不可用（见开头预检），本段跳过——此时测到的只是兜底返回的耗时")
        SUMMARY.append(("⑤ RAG 链路端到端", "未测到（LLM 不可用）"))
        return
    try:
        token = _login()
    except Exception as e:
        print(f"  后端不可用（{type(e).__name__}），跳过")
        return

    latencies: list[float] = []
    for i in range(repeat):
        elapsed, mode, content = _ask(token, RETRIEVAL_QUERIES[i % len(RETRIEVAL_QUERIES)], "bench-rag")
        head = (content or "")[:40].replace("\n", " ")
        broken = any(mark in (content or "") for mark in FALLBACK_MARKERS)
        print(f"    {elapsed:8.1f} ms | mode={mode:10s} | {head}")
        if broken:
            print("    ⚠️ 这是兜底文案——LLM 调用失败，请先修 Key，再重跑本段")
            SUMMARY.append(("⑤ RAG 链路端到端", "未测到（LLM 调用失败，返回兜底文案）"))
            return
        latencies.append(elapsed)
    print(line("RAG 链路端到端", latencies))
    print("  口径：本机 localhost、含检索段与生成段、不含前端渲染")
    SUMMARY.append(("⑤ RAG 链路端到端",
                    f"mean {statistics.fmean(latencies):.0f}ms / P95 {pct(latencies, 0.95):.0f}ms（n={len(latencies)}）"))


# ── ⑥⑦⑧ 其余链路 ─────────────────────────────────────────────

def _post_json(path: str, payload: dict, token: str, timeout: int = 300) -> tuple[float, str]:
    req = urllib.request.Request(
        BACKEND + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "ignore")
    return (time.perf_counter() - t) * 1000, body


def _get(path: str, token: str, timeout: int = 60) -> tuple[float, int, str]:
    """GET 并返回 (耗时ms, 状态码, body)。404/403 也要计时——它同样走完了查库路径。"""
    req = urllib.request.Request(BACKEND + path, headers={"Authorization": "Bearer " + token})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body, code = resp.read().decode("utf-8", "ignore"), resp.status
    except urllib.error.HTTPError as e:
        body, code = e.read().decode("utf-8", "ignore"), e.code
    return (time.perf_counter() - t) * 1000, code, body


def bench_read_endpoints(repeat: int) -> None:
    """⑩ 只读数据接口：入口 → 数据。这是"访问接口拿数据"的真实延迟，不调 LLM。"""
    print("\n=== ⑩ 只读数据接口：入口 → 数据（读 PostgreSQL，不调 LLM）===")
    try:
        token = _login()
    except Exception as e:
        print(f"  后端不可用（{type(e).__name__}），跳过")
        return

    cases = [
        ("GET /devices/elder/e001/readings", "/api/v1/devices/elder/e001/readings"),
        ("GET /care-flow/events", "/api/v1/care-flow/events"),
        ("GET /care-flow/webhooks", "/api/v1/care-flow/webhooks"),
        ("GET /care-flow/active/e001", "/api/v1/care-flow/active/e001"),
        ("GET /assessment/{不存在}", "/api/v1/assessment/asmt-not-exist"),
        ("GET /alert/{不存在}/review", "/api/v1/alert/alert-not-exist/review"),
        ("GET /care-flow/{不存在}/report", "/api/v1/care-flow/sess-not-exist/report"),
    ]

    all_lat: list[float] = []
    for label, path in cases:
        lat: list[float] = []
        code = 0
        for _ in range(repeat):
            ms, code, _ = _get(path, token)
            lat.append(ms)
        all_lat.extend(lat)
        print(f"  {label:34s} mean {statistics.fmean(lat):6.2f} ms | "
              f"P50 {pct(lat, 0.5):6.2f} | P95 {pct(lat, 0.95):6.2f} | "
              f"max {max(lat):6.2f} | HTTP {code}")
    print(line("只读接口整体", all_lat))
    print("  口径：本机 localhost、连接预热、含 JWT 鉴权与 PostgreSQL 查询，不含 LLM")

    # 这个接口名字叫"检索"，实际要跑 BGE + Milvus + 精排，不是纯读库，单独列
    ms, _body = _post_json("/api/v1/knowledge/search", {"query": "压疮护理", "top_k": 3}, token)
    print(f"\n  POST /knowledge/search（含向量检索，非纯读库）: {ms:.0f} ms")

    if all_lat:
        SUMMARY.append(("⑩ 只读数据接口端到端",
                        f"mean {statistics.fmean(all_lat):.2f}ms / P95 {pct(all_lat, 0.95):.2f}ms（n={len(all_lat)}）"))


def bench_other_paths(repeat: int) -> None:
    """⑥ 统一入口 / ⑦ 告警研判 / ⑧ 照护流程：三条都必须调 LLM，量级必然是秒级。"""
    print("\n=== ⑥⑦⑧ 其余链路：接口端到端（都需要可用的 LLM Key）===")
    if not LLM_OK:
        print("  ⏭ LLM 不可用（见开头预检），本段跳过。")
        print("     提示：这时的数字会『看起来很快』——意图识别静默走关键词降级、")
        print("     告警研判静默用默认风险等级，都不是真实链路耗时。")
        for key in ("⑥ 统一入口端到端", "⑦ 告警研判端到端", "⑧ 照护流程端到端"):
            SUMMARY.append((key, "未测到（LLM 不可用）"))
        return
    try:
        token = _login()
    except Exception as e:
        print(f"  后端不可用（{type(e).__name__}），跳过")
        return

    plans = [
        ("⑥ 统一对话入口（意图路由）", "/api/v1/chat",
         {"elder_id": "e001", "query": "压疮怎么护理"}, "⑥ 统一入口端到端"),
        ("⑦ 告警研判（到 interrupt 暂停点）", "/api/v1/alert/trigger",
         {"elder_id": "e001", "device_id": "dev-001", "alert_type": "heart_rate_abnormal"},
         "⑦ 告警研判端到端"),
        ("⑧ 照护流程 /start", "/api/v1/care-flow/start",
         {"elder_id": "e001"}, "⑧ 照护流程端到端"),
    ]

    for label, path, payload, summary_key in plans:
        latencies: list[float] = []
        broken = False
        for _ in range(repeat):
            body_payload = dict(payload)
            body_payload["session_id"] = f"bench-{time.time_ns()}"
            try:
                elapsed, body = _post_json(path, body_payload, token)
            except Exception as e:
                print(f"  {label}: 请求异常 {type(e).__name__}")
                broken = True
                break
            hit_fallback = any(mark in body for mark in FALLBACK_MARKERS)
            note = "  ← 兜底文案，数字无效" if hit_fallback else ""
            print(f"  {label}: {elapsed:8.1f} ms{note}")
            if hit_fallback:
                broken = True
                break
            latencies.append(elapsed)
        if broken or not latencies:
            SUMMARY.append((summary_key, "未测到（LLM 调用失败或请求异常）"))
        else:
            print(line(f"{label} 汇总", latencies))
            SUMMARY.append((summary_key,
                            f"mean {statistics.fmean(latencies):.0f}ms / P95 {pct(latencies, 0.95):.0f}ms（n={len(latencies)}）"))
    print("  口径：本机 localhost、连接预热、含 LLM 调用")


# ── 汇总 ──────────────────────────────────────────────────────

def print_summary() -> None:
    print("\n" + "=" * 68)
    print("汇总：可以对外引用的延迟数字（每条都必须连口径一起写）")
    print("=" * 68)
    for name, value in SUMMARY:
        print(f"  {name:24s} {value}")
    print("\n  两条硬规矩：")
    print("   ① 不同层级的耗时分开写（判定段 ≠ 检索段 ≠ 端到端），不要合并求平均；")
    print("   ② 任何含 LLM 调用的链路都是秒级——写毫秒级前先看这条路径调不调 LLM。")
    print("  ⑨ 评估链路需一份评估表 PDF（POST /api/v1/assessment/upload），"
          "本脚本未覆盖。")


def main() -> int:
    global LLM_OK
    parser = argparse.ArgumentParser(description="CareAgent 延迟压测（数字均带口径）")
    parser.add_argument("--repeat", type=int, default=5, help="每段采样次数（默认 5）")
    parser.add_argument("--no-e2e", action="store_true", help="跳过需要后端的端到端段")
    args = parser.parse_args()

    print("CareAgent 延迟压测 —— 所有数字均为本机实测，请连口径一起引用")
    LLM_OK, detail = preflight_llm()
    print(f"\n[预检] LLM: {'✅ 可用' if LLM_OK else '❌ 不可用'} —— {detail}")
    if not LLM_OK:
        print("       Key 不可用时，⑤⑥⑦⑧ 段会静默走降级路径并『看起来很快』，")
        print("       这些数字一律不能对外引用；只引用 ①②③④ 段（不依赖 LLM）。")
    try:
        bench_cold_start()
        bench_retrieval(args.repeat)
        asyncio.run(bench_cache())
        bench_quick_index(args.repeat)
        if args.no_e2e:
            print("\n（--no-e2e：已跳过接口端到端）")
        else:
            bench_e2e(args.repeat)
            bench_llm(2)
            bench_other_paths(2)
            bench_read_endpoints(args.repeat)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"\n压测中断：{type(e).__name__}: {e}")
        print("提示：基础设施（PostgreSQL 5433 / Redis 6380 / Milvus 19531）需先启动")
        return 1
    print_summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

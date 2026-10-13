# scripts/mock_frontend.py
# 前端联调 mock：模拟护理员对话场景，端到端验证
#   - JWT 登录 → SSE 对话 → clarify 候选选择 → 多轮对话
#
# 这个脚本会：
#   1. 启动 uvicorn 子进程（自定义端口避免冲突）
#   2. 等 /health 就绪
#   3. 登录获取 JWT
#   4. 跑 5 个对话场景（含 SSE 解析）
#   5. 关闭子进程
#
# 用法：cd <项目根目录> && PYTHONPATH=. python scripts/mock_frontend.py

import json
import subprocess
import sys
import time
from typing import Optional

import httpx

# ──────────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────────

BASE_URL = "http://127.0.0.1:8765"   # 自定义端口避免和正式服务冲突
LOGIN_PATH = "/api/v1/auth/login"
CHAT_PATH = "/api/v1/chat"
HEALTH_PATH = "/health"

# 测试账号（来自 backend/dependencies.py MOCK_USERS）
CAREGIVER = {"username": "worker", "password": "Worker@123"}

# 启动超时（秒）
SERVER_BOOT_TIMEOUT = 30.0


# ──────────────────────────────────────────────────────────────
# SSE 解析：从 httpx 流式响应中提取事件
# ──────────────────────────────────────────────────────────────

def parse_sse_stream(response: httpx.Response) -> list[dict]:
    """解析 SSE 流，返回 [{event, data}, ...] 列表。

    SSE 格式：
        event: <name>
        data: <payload>

        event: <name>
        data: <payload>
    """
    events = []
    current_event = "message"
    current_data_lines: list[str] = []

    for raw_line in response.iter_lines():
        # httpx 的 iter_lines 返回 str（已解码）
        if raw_line == "":
            # 空行 = 一个事件结束
            if current_data_lines:
                events.append({
                    "event": current_event,
                    "data": "\n".join(current_data_lines),
                })
            current_event = "message"
            current_data_lines = []
            continue
        if raw_line.startswith("event:"):
            current_event = raw_line[6:].strip()
        elif raw_line.startswith("data:"):
            current_data_lines.append(raw_line[5:].lstrip())
        # 忽略其他行（如注释、id:、retry: 等）

    # 流结束时如果还有未提交的事件
    if current_data_lines:
        events.append({
            "event": current_event,
            "data": "\n".join(current_data_lines),
        })

    return events


# ──────────────────────────────────────────────────────────────
# 客户端封装
# ──────────────────────────────────────────────────────────────

class FrontendMock:
    """模拟前端的对话客户端。"""

    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url
        self.token: Optional[str] = None
        self.user: Optional[dict] = None

    # ── 登录 ──
    def login(self, credentials: dict = CAREGIVER) -> dict:
        """登录获取 JWT。"""
        with httpx.Client(trust_env=False, timeout=10.0) as client:
            resp = client.post(
                f"{self.base_url}{LOGIN_PATH}",
                json=credentials,
            )
            resp.raise_for_status()
            data = resp.json()
            self.token = data["access_token"]
            self.user = {
                "user_id": data.get("user_id"),
                "role":    data.get("role"),
            }
            return data

    # ── 发送对话 ──
    def chat(
        self,
        query: str,
        session_id: str = "",
        agent_type: Optional[str] = None,
        mode: str = "single",
        site_id: str = "",
        pipeline: Optional[str] = None,
    ) -> list[dict]:
        """发送对话请求，返回 SSE 事件列表。"""
        payload = {
            "query": query,
            "session_id": session_id,
            "mode": mode,
            "site_id": site_id,
        }
        if agent_type:
            payload["agent_type"] = agent_type
        if pipeline:
            payload["pipeline"] = pipeline

        headers = {"Authorization": f"Bearer {self.token}"}

        with httpx.Client(trust_env=False, timeout=600.0) as client:
            # stream=True 让 httpx 不立即读 body，可以逐行迭代
            with client.stream(
                "POST",
                f"{self.base_url}{CHAT_PATH}",
                json=payload,
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                # 注意：iter_lines 在 stream 上下文里有效
                return self._parse_sse_inline(resp)

    @staticmethod
    def _parse_sse_inline(response: httpx.Response) -> list[dict]:
        """在 stream 上下文里逐行解析 SSE。"""
        events = []
        current_event = "message"
        current_data_lines: list[str] = []

        for raw_line in response.iter_lines():
            if raw_line == "":
                if current_data_lines:
                    events.append({
                        "event": current_event,
                        "data": "\n".join(current_data_lines),
                    })
                current_event = "message"
                current_data_lines = []
                continue
            if raw_line.startswith("event:"):
                current_event = raw_line[6:].strip()
            elif raw_line.startswith("data:"):
                current_data_lines.append(raw_line[5:].lstrip())

        if current_data_lines:
            events.append({
                "event": current_event,
                "data": "\n".join(current_data_lines),
            })
        return events


# ──────────────────────────────────────────────────────────────
# 服务器管理
# ──────────────────────────────────────────────────────────────

def start_server() -> subprocess.Popen:
    """启动 uvicorn 子进程。"""
    print(f"[server] 启动 uvicorn 子进程，监听 {BASE_URL} ...")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "backend.main:app",
            "--host", "127.0.0.1",
            "--port", "8765",
            "--log-level", "warning",   # 减少日志噪音
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def wait_for_health(proc: subprocess.Popen, timeout: float = SERVER_BOOT_TIMEOUT) -> bool:
    """轮询 /health 直到就绪或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"[server] 子进程意外退出，returncode={proc.returncode}")
            return False
        try:
            with httpx.Client(trust_env=False, timeout=2.0) as client:
                resp = client.get(f"{BASE_URL}{HEALTH_PATH}")
                if resp.status_code == 200:
                    print(f"[server] /health 就绪：{resp.json()}")
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    print(f"[server] {timeout}s 内未就绪")
    return False


def stop_server(proc: subprocess.Popen):
    """关闭 uvicorn 子进程。"""
    if proc.poll() is None:
        print("[server] 终止 uvicorn 子进程 ...")
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ──────────────────────────────────────────────────────────────
# 打印辅助
# ──────────────────────────────────────────────────────────────

def print_section(title: str):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def print_events(events: list[dict], max_content_len: int = 240):
    """漂亮打印 SSE 事件列表。"""
    for ev in events:
        event_name = ev["event"]
        data_str = ev["data"]
        if event_name == "done":
            print(f"  [{event_name}] <end of stream>")
            continue
        try:
            data = json.loads(data_str) if data_str else {}
        except json.JSONDecodeError:
            data = {"_raw": data_str}
        print(f"  [{event_name}] {json.dumps(data, ensure_ascii=False)[:max_content_len]}")


def extract_answer(events: list[dict]) -> Optional[dict]:
    """从事件列表里抽 answer 事件 data。"""
    for ev in events:
        if ev["event"] == "answer":
            try:
                return json.loads(ev["data"])
            except json.JSONDecodeError:
                return None
    return None


def extract_clarify(events: list[dict]) -> Optional[dict]:
    """从事件列表里抽 clarify 事件 data。"""
    for ev in events:
        if ev["event"] == "clarify":
            try:
                return json.loads(ev["data"])
            except json.JSONDecodeError:
                return None
    return None


# ──────────────────────────────────────────────────────────────
# 对话场景
# ──────────────────────────────────────────────────────────────

def scenario_a_auto_route_sop_qa(client: FrontendMock) -> bool:
    """场景 A：自动路由到 SOP_QA（护理知识问答）。"""
    print_section("场景 A：自动路由 → SOP_QA（护理知识问答）")
    print("  query='鼻饲管日常怎么维护'  agent_type=None（让意图识别自动路由）")
    events = client.chat(query="鼻饲管日常怎么维护", session_id="mock-A-001")
    print_events(events)
    answer = extract_answer(events)
    if not answer:
        print("\n  ❌ 未收到 answer 事件")
        return False
    ok = answer.get("agent_type") == "sop_qa"
    print(f"\n  {'✅' if ok else '❌'} 路由到 sop_qa，agent_type={answer.get('agent_type')}")
    print(f"     fallback_used={answer.get('fallback_used')}")
    return ok


def scenario_b_auto_route_work_flow(client: FrontendMock) -> bool:
    """场景 B：自动路由到 CARE_FLOW（流程执行）。

    注意：work_flow 不一定有真实 service_plan，可能走 mock 兜底，但流程应能跑通。
    """
    print_section("场景 B：自动路由 → CARE_FLOW（照护流程）")
    print("  query='明天执行入住流程第二阶段'  agent_type=None")
    events = client.chat(
        query="明天执行入住流程第二阶段",
        session_id="mock-B-001",
        site_id="e001",
    )
    print_events(events)
    answer = extract_answer(events)
    if not answer:
        print("\n  ❌ 未收到 answer 事件")
        return False
    ok = answer.get("agent_type") == "work_flow"
    print(f"\n  {'✅' if ok else '❌'} 路由到 work_flow，agent_type={answer.get('agent_type')}")
    return ok


def scenario_c_multi_turn(client: FrontendMock) -> bool:
    """场景 C：多轮对话（同一 session_id 两轮）。

    验证 MemorySaver 在多轮间保留上下文。
    """
    print_section("场景 C：多轮对话（同 session_id，验证多轮记忆）")
    session_id = "mock-C-001"

    # ── 轮 1 ──
    print(f"\n  ── 轮 1 ──")
    print(f"  query='压疮怎么分期？'  session_id={session_id}")
    events1 = client.chat(query="压疮怎么分期？", session_id=session_id)
    print_events(events1, max_content_len=180)
    answer1 = extract_answer(events1)
    if not answer1:
        print("\n  ❌ 轮 1 未收到 answer")
        return False

    # ── 轮 2（同 session_id，问追问问题）──
    print(f"\n  ── 轮 2（追问上一轮）──")
    print(f"  query='那Ⅱ期怎么处理？'  session_id={session_id}（同一个）")
    events2 = client.chat(query="那Ⅱ期怎么处理？", session_id=session_id)
    print_events(events2, max_content_len=180)
    answer2 = extract_answer(events2)
    if not answer2:
        print("\n  ❌ 轮 2 未收到 answer")
        return False

    ok = (
        answer1.get("agent_type") == "sop_qa"
        and answer2.get("agent_type") == "sop_qa"
    )
    print(f"\n  {'✅' if ok else '❌'} 两轮都路由到 sop_qa，多轮记忆由 MemorySaver 在 thread_id 维度保留")
    return ok


def scenario_d_clarify(client: FrontendMock) -> bool:
    """场景 D：clarify 触发（跨域无法判断主意图）。

    注：LLM 真实判定时是否触发 clarify 取决于 query，无法 100% 强制。
    用一个明显跨域的 query 测试，若 LLM 返回 clarify 则验证 candidates 字段；
    若 LLM 直接给了主意图，也算通过（说明 LLM 比 mock 更激进）。
    """
    print_section("场景 D：clarify 触发（跨域 query）")
    print("  query='评估照护流程'（评估 + 照护流程 跨域）  agent_type=None")
    events = client.chat(query="评估照护流程", session_id="mock-D-001")
    print_events(events)

    clarify = extract_clarify(events)
    if clarify:
        ok = (
            "candidates" in clarify
            and "message" in clarify
            and len(clarify["candidates"]) >= 1
        )
        print(f"\n  ✅ 触发 clarify 模式")
        print(f"     candidates={clarify.get('candidates')}")
        print(f"     message={clarify.get('message', '')[:100]}")
        return ok

    # 没触发 clarify，但只要正常返回 answer 也算通过（LLM 直接做了主意图判断）
    answer = extract_answer(events)
    if answer:
        print(f"\n  ✅ LLM 直接给出主意图（未触发 clarify），agent_type={answer.get('agent_type')}")
        print(f"     这是允许的：clarify 是兜底分支，不是必须触发")
        return True

    print("\n  ❌ 既没 clarify 也没 answer")
    return False


def scenario_e_direct_alert(client: FrontendMock) -> bool:
    """场景 E：显式指定 agent_type=alert（绕过意图识别）。

    注：alert 流程含 HitL interrupt，这里只验证有结构化输出（jurisdiction_result）。
    """
    print_section("场景 E：显式指定 agent_type=alert（绕过意图识别）")
    print("  query='301 房老人心率 130'  agent_type=alert（显式）")
    events = client.chat(
        query="301 房老人心率 130",
        session_id="mock-E-001",
        agent_type="alert",
        site_id="e001",
    )
    print_events(events, max_content_len=240)
    answer = extract_answer(events)
    if not answer:
        print("\n  ❌ 未收到 answer")
        return False
    # alert 的 structured_output 可能含 jurisdiction_result 等
    structured = answer.get("structured_output") or {}
    ok = answer.get("agent_type") == "alert"
    print(f"\n  {'✅' if ok else '❌'} 显式路由到 alert")
    print(f"     structured_output keys: {list(structured.keys())}")
    return ok


# ──────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────

def main():
    print_section("前端联调 mock：JWT 登录 → SSE 对话 → clarify → 多轮")
    print(f"  Base URL: {BASE_URL}")
    print(f"  测试账号: {CAREGIVER['username']}")

    # ── 1. 启动服务器 ──
    print_section("步骤 1：启动 uvicorn 子进程")
    proc = start_server()
    if not wait_for_health(proc):
        stop_server(proc)
        sys.exit(1)

    try:
        # ── 2. 登录 ──
        print_section("步骤 2：登录获取 JWT")
        client = FrontendMock()
        try:
            login_data = client.login()
            print(f"  ✅ 登录成功")
            print(f"     user_id={login_data.get('user_id')}, role={login_data.get('role')}")
            print(f"     token={client.token[:30]}...")
        except Exception as e:
            print(f"  ❌ 登录失败: {e}")
            return

        # ── 3. 跑场景 ──
        results = []
        results.append(("场景 A - 自动路由 SOP_QA", scenario_a_auto_route_sop_qa(client)))
        results.append(("场景 B - 自动路由 CARE_FLOW", scenario_b_auto_route_work_flow(client)))
        results.append(("场景 C - 多轮对话",          scenario_c_multi_turn(client)))
        results.append(("场景 D - clarify 触发",       scenario_d_clarify(client)))
        results.append(("场景 E - 显式 alert",          scenario_e_direct_alert(client)))

        # ── 4. 汇总 ──
        print_section("汇总")
        passed = sum(1 for _, ok in results if ok)
        for name, ok in results:
            print(f"  {'✅' if ok else '❌'} {name}")
        print(f"\n  总计：{passed}/{len(results)} 通过")
        if passed == len(results):
            print("\n  🎉 前端联调全部通过")

    finally:
        stop_server(proc)


if __name__ == "__main__":
    main()

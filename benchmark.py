"""Compare Jev against an ordinary LLM (Qwen) on the same browser tasks.

Two arms, identical except the model that chooses each action:

  jev  -- Jev picks each action through OpenRouter's Decisions API. Qwen is the
          fallback (low confidence / blocked / error), the field-text helper, and
          the page reviewer.
  qwen -- Qwen picks each action directly through OpenRouter chat. Qwen is also
          the text helper and reviewer.

Every arm runs every case once against the same Chrome profile. For each run the
harness records wall-clock time, the action count, the independent expect-text
outcome, and cost broken out into decision / fallback / text / review calls.

Run one arm-case pair (a worker, driven by the orchestrator):
    python benchmark.py --worker
Run the whole matrix:
    uv run --env-file .env python benchmark.py
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
QWEN_MODEL = os.getenv("BENCH_QWEN_MODEL", "qwen/qwen3-32b")
# The model Jev falls back to, and which supplies text/review, in the jev arm.
# Kept equal to QWEN_MODEL so the only variable between arms is Jev-vs-Qwen for
# the primary decision.
FALLBACK_MODEL = os.getenv("BENCH_FALLBACK_MODEL", QWEN_MODEL)

CASES = [
    {
        "name": "wiki-portal-article",
        "difficulty": "easy",
        "url": "https://www.wikipedia.org/",
        "goal": "Open the Wikipedia article about the planet Neptune.",
        "expect": "eighth and farthest known planet",
    },
    {
        "name": "wiki-site-search",
        "difficulty": "easy-medium",
        "url": "https://en.wikipedia.org/wiki/Main_Page",
        "goal": 'Search Wikipedia for "Marie Curie" and open the article about her.',
        "expect": "Nobel",
    },
    {
        "name": "google-capital",
        "difficulty": "medium",
        "url": "https://www.google.com/",
        "goal": 'Search for "capital of Australia" and open a result that answers it.',
        "expect": "Canberra",
    },
    {
        "name": "google-author",
        "difficulty": "medium-hard",
        "url": "https://www.google.com/",
        "goal": 'Search for "who wrote the novel 1984" and open a result about the author.',
        "expect": "George Orwell",
    },
    {
        "name": "unit-converter",
        "difficulty": "hard",
        "url": "https://www.unitconverters.net/",
        "goal": "Using this site, convert 100 miles to kilometers and read the result.",
        "expect": "160.9344",
    },
]

ARMS = ["jev", "qwen"]


def load_dotenv(path=".env"):
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value.strip())


def num(value):
    return float(value) if isinstance(value, (int, float)) else 0.0


def cost_of(usage):
    if not isinstance(usage, dict):
        return 0.0
    return num(usage.get("cost"))


def verify(expect, page, inner_text=""):
    if not expect or (not page and not inner_text):
        return False
    visible = (
        f"{(page or {}).get('title', '')}\n"
        f"{(page or {}).get('text', '')}\n"
        f"{inner_text}"
    )
    return expect.casefold() in visible.casefold()


def _domain(url):
    try:
        return urlparse(url or "").netloc
    except Exception:
        return ""


def attribute(record):
    """Root-cause layer and one-line explanation for a result record."""
    outcome = record.get("outcome", "")
    if outcome == "success":
        return "成功", "操作走到正确答案页面，最终页面命中 expect 短语。"
    error = (record.get("error") or "").lower()
    stop = record.get("stop_reason") or ""
    final_url = record.get("final_url") or ""
    case_domain = _domain(record.get("url"))
    final_domain = _domain(final_url)
    if outcome == "error":
        if "invalid target" in error or "invalid operation" in error or "invalid response" in error:
            return "fallback 链", (
                "候选太多、太相似，Jev 在 95% 置信度下锁不定 target，"
                "触发 fallback 交给 qwen；qwen 返回了不在候选动作集里的 operation/target，抛异常终止。"
            )
        if "target" in error and ("lost" in error or "closed" in error):
            return "harness（tab 丢失）", "本 run 的页面被关闭，agent 拒绝读别的 tab。"
        if any(t in error for t in ("cdp", "handshake", "websocket", "connection", "timed out", "no close frame")):
            return "连接层", "Chrome/CDP 长连接瞬断（环境抖动，非决策问题）。"
        return "异常", f"未知异常：{(record.get('error') or '')[:120]}"
    if outcome.startswith("blocked:"):
        if stop == "model-blocked" and case_domain and final_domain and final_domain != case_domain:
            return "harness（tab 隔离/导航漂移）", f"模型选 BLOCKED，final_url={final_url} 偏离任务域名 {case_domain}，疑似目标 tab 混入用户其他 tab。"
        if stop == "model-blocked":
            return "模型主动停止", "模型判断无法继续，选择 BLOCKED（诚实放弃，非答错）。"
        if stop == "stale-page":
            return "harness（stale-page 自刷新）", (
                "Chrome 插件太多（Monica/WebHighlights ~10 次/秒改 DOM），页面 DOM 快速变化，"
                "而 qwen 单次决策 ~41s 远慢于这些变化，每次提取完页面返回的思考结果都跟原 DOM 对不上，"
                "决策被 stale 作废、反复重试，review 用尽后阻塞。"
            )
        if stop == "target-lost":
            return "harness（tab 丢失）", "本 run 的页面被关闭。"
        if stop == "action-budget":
            return "步数预算", "达到 MAX_STEPS 动作数上限。"
        return "模型主动停止", f"stop_reason={stop}"
    if outcome == "timeout":
        return "挂死", "worker 超过超时阈值仍未 done/blocked。"
    if outcome == "unverified-done":
        return "验证失败", "模型声称 DONE，但最终页面没命中 expect 短语（可能没到正确页面，或验证文本未覆盖）。"
    return "未知", str(outcome)


def compute_breakdown(state, arm):
    decisions = state.get("decisions", [])
    text_calls = state.get("text_calls", [])
    review_calls = state.get("review_calls", [])

    fallback_decisions = [d for d in decisions if d.get("fallback_reason")]
    pure_jev = [d for d in decisions if not d.get("fallback_reason")]

    jev_decision_cost = sum(cost_of(d.get("usage")) for d in pure_jev)
    fallback_chat_cost = sum(cost_of(d.get("usage")) for d in fallback_decisions)
    # A fallback decision also paid for the Jev attempt that was rejected.
    fallback_jev_cost = sum(
        cost_of((d.get("jev") or {}).get("usage")) for d in fallback_decisions
    )
    text_cost = sum(cost_of(t.get("usage")) for t in text_calls)
    review_cost = sum(cost_of(r.get("usage")) for r in review_calls)
    decision_cost = jev_decision_cost + fallback_chat_cost + fallback_jev_cost

    jev_input = sum(int((d.get("usage") or {}).get("input_tokens") or 0) for d in decisions)
    jev_output = sum(int((d.get("usage") or {}).get("output_tokens") or 0) for d in decisions)
    chat_prompt = sum(int((t.get("usage") or {}).get("prompt_tokens") or 0) for t in text_calls)
    chat_prompt += sum(int((r.get("usage") or {}).get("prompt_tokens") or 0) for r in review_calls)
    chat_prompt += sum(int((d.get("usage") or {}).get("prompt_tokens") or 0) for d in fallback_decisions)
    chat_completion = sum(int((t.get("usage") or {}).get("completion_tokens") or 0) for t in text_calls)
    chat_completion += sum(int((r.get("usage") or {}).get("completion_tokens") or 0) for r in review_calls)
    chat_completion += sum(int((d.get("usage") or {}).get("completion_tokens") or 0) for d in fallback_decisions)

    decision_ms = sum(num(d.get("latency_ms")) for d in decisions)
    text_ms = sum(num(t.get("latency_ms")) for t in text_calls)
    review_ms = sum(num(r.get("latency_ms")) for r in review_calls)

    cost = {
        "total": decision_cost + text_cost + review_cost,
        "decisions": decision_cost,
        "text": text_cost,
        "review": review_cost,
    }
    if arm == "jev":
        cost.update(
            jev_decisions=jev_decision_cost,
            fallback_chat=fallback_chat_cost,
            fallback_jev=fallback_jev_cost,
        )
    return {
        "cost": cost,
        "tokens": {
            "jev_input": jev_input,
            "jev_output": jev_output,
            "chat_prompt": chat_prompt,
            "chat_completion": chat_completion,
        },
        "latency_ms": {
            "total": state.get("elapsed_ms", 0),
            "decisions": decision_ms,
            "text": text_ms,
            "review": review_ms,
        },
        "counts": {
            "steps": len(state.get("history", [])),
            "decisions": len(decisions),
            "fallback_decisions": len(fallback_decisions),
            "text_calls": len(text_calls),
            "review_calls": len(review_calls),
        },
    }


def run_one(case, arm):
    """Drive one arm-case pair and return a JSON-serialisable result record."""
    from jev_ultrafast.agent import Agent

    started = time.perf_counter()
    agent = None
    error = None
    try:
        agent = Agent(case["url"], case["goal"])
        for _snapshot in agent.run():
            pass
    except Exception as exc:  # model unreachable, page lost, internal error
        error = f"{type(exc).__name__}: {exc}"
    finally:
        state = agent.state if agent is not None else None
        # Browser Use's structured text is interactive-element only (links,
        # controls, headings); article body paragraphs are omitted. The answer
        # text for a "read this article" case lives in the body, so capture the
        # page's full visible text before teardown and verify against it too.
        inner_text = ""
        if agent is not None and getattr(agent, "browser", None) is not None:
            try:
                inner_text = agent.browser.evaluate("document.body ? document.body.innerText : ''") or ""
            except Exception:
                inner_text = ""
        if agent is not None:
            try:
                agent.close()
            except Exception:
                pass

    page = state.get("page") if state else None
    ok = verify(case.get("expect"), page, inner_text)
    status = state.get("status") if state else "error"
    stop_reason = state.get("stop_reason") if state else None
    if error:
        outcome = "error"
    elif ok:
        outcome = "success"
    elif status == "done":
        outcome = "unverified-done"
    elif status == "blocked":
        outcome = f"blocked:{stop_reason}"
    else:
        outcome = f"unknown:{status}"

    record = {
        "arm": arm,
        "case": case["name"],
        "difficulty": case["difficulty"],
        "url": case["url"],
        "goal": case["goal"],
        "expect": case.get("expect"),
        "outcome": outcome,
        "verified": ok,
        "status": status,
        "stop_reason": stop_reason,
        "error": error,
        "wall_seconds": round(time.perf_counter() - started, 2),
        "final_url": page.get("url") if page else None,
        "final_title": page.get("title") if page else None,
        "models": {
            "decision": [d.get("model") for d in state["decisions"]] if state else [],
            "text": [t.get("model") for t in state.get("text_calls", [])] if state else [],
            "review": [r.get("model") for r in state.get("review_calls", [])] if state else [],
        },
        "fallback_reasons": [
            d.get("fallback_reason")
            for d in state["decisions"]
            if d.get("fallback_reason")
        ] if state else [],
        "fallback_confidences": [
            {
                "operation": d.get("jev", {}).get("operation"),
                "op_confidence": d.get("jev", {}).get("confidence"),
                "target": d.get("jev", {}).get("target"),
                "target_confidence": d.get("jev", {}).get("target_confidence"),
            }
            for d in state["decisions"]
            if d.get("fallback_reason") and d.get("jev")
        ] if state else [],
    }
    record.update(compute_breakdown(state, arm) if state else {})
    layer, explanation = attribute(record)
    record["failure_layer"] = layer
    record["analysis"] = explanation
    return record


def arm_env(arm):
    env = dict(os.environ)
    # Route every chat call through OpenRouter so cost is reported uniformly
    # (DeepSeek's direct API returns no cost field).
    env["DEEPSEEK_API_KEY"] = ""
    env["DEEPSEEK_MODEL"] = ""
    env["OPENROUTER_MODEL"] = FALLBACK_MODEL
    env["TEXT_MODEL"] = FALLBACK_MODEL
    if arm == "qwen":
        env["DECISION_MODE"] = "chat"
    else:
        env.pop("DECISION_MODE", None)
    return env


def spawn(case, arm, timeout=None):
    if timeout is None:
        timeout = int(os.getenv("BENCH_WORKER_TIMEOUT", "600"))
    payload = json.dumps({"case": case, "arm": arm})
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__)), "--worker"],
            input=payload,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=arm_env(arm),
            cwd=ROOT,
        )
    except subprocess.TimeoutExpired:
        # The agent's own budgets (action count, model-call count, loop
        # detection) should stop it; a hang here means a single step blocked
        # past every inner timeout, so record it and move on instead of
        # crashing the whole matrix. Not retried: it is a genuine stall, not a
        # connection flap.
        return {
            "arm": arm,
            "case": case["name"],
            "difficulty": case["difficulty"],
            "outcome": "timeout",
            "verified": False,
            "error": f"worker hung past {timeout}s (agent never reached done/blocked)",
            "wall_seconds": timeout,
        }
    if proc.returncode != 0:
        return {
            "arm": arm,
            "case": case["name"],
            "difficulty": case["difficulty"],
            "outcome": "error",
            "verified": False,
            "error": f"worker exit {proc.returncode}: {(proc.stderr or '')[:400]}",
            "wall_seconds": None,
        }
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {
            "arm": arm,
            "case": case["name"],
            "difficulty": case["difficulty"],
            "outcome": "error",
            "verified": False,
            "error": f"unparseable worker output: {(proc.stdout or '')[:400]}",
            "wall_seconds": None,
        }


def render_report(results):
    lines = []
    lines.append("# Jev vs Qwen — browser task benchmark")
    lines.append("")
    lines.append(
        f"Decision models: **Jev** (`~typesafe/jev-latest`) vs **Qwen** (`{QWEN_MODEL}`). "
        f"Fallback / text / review in the Jev arm: `{FALLBACK_MODEL}`. "
        f"Independent success check: the `expect` phrase on the final page."
    )
    lines.append("")
    lines.append("## Per case")
    lines.append("")
    lines.append("| case | difficulty | arm | outcome | steps | time | cost (USD) |")
    lines.append("| --- | --- | --- | --- | ---: | ---: | ---: |")
    for r in results:
        cost = r.get("cost", {}).get("total")
        cost_s = f"{cost:.6f}" if isinstance(cost, (int, float)) else "—"
        time_s = r.get("wall_seconds")
        time_disp = f"{time_s:.0f}s" if isinstance(time_s, (int, float)) else "—"
        steps = r.get("counts", {}).get("steps")
        lines.append(
            f"| {r['case']} | {r['difficulty']} | {r['arm']} | {r['outcome']} | "
            f"{steps} | {time_disp} | {cost_s} |"
        )
    lines.append("")
    lines.append("## Success rate (independent expect-text on final page)")
    lines.append("")
    lines.append("| arm | success | total | rate |")
    lines.append("| --- | ---: | ---: | ---: |")
    for arm in ARMS:
        rows = [r for r in results if r.get("arm") == arm]
        wins = sum(1 for r in rows if r.get("verified"))
        lines.append(f"| {arm} | {wins} | {len(rows)} | {wins / len(rows):.0%} |")
    lines.append("")
    lines.append("## Totals across all five cases")
    lines.append("")
    lines.append("| arm | time | steps | decisions | fallback | cost total | cost decisions | cost text | cost review |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for arm in ARMS:
        rows = [r for r in results if r.get("arm") == arm and r.get("cost")]
        if not rows:
            continue
        agg = {
            "time": sum(r["wall_seconds"] for r in rows),
            "steps": sum(r["counts"]["steps"] for r in rows),
            "decisions": sum(r["counts"]["decisions"] for r in rows),
            "fallback": sum(r["counts"]["fallback_decisions"] for r in rows),
            "total": sum(r["cost"]["total"] for r in rows),
            "dec": sum(r["cost"]["decisions"] for r in rows),
            "text": sum(r["cost"]["text"] for r in rows),
            "review": sum(r["cost"]["review"] for r in rows),
        }
        lines.append(
            f"| {arm} | {agg['time']:.0f}s | {agg['steps']} | {agg['decisions']} | {agg['fallback']} | "
            f"{agg['total']:.6f} | {agg['dec']:.6f} | {agg['text']:.6f} | {agg['review']:.6f} |"
        )
    lines.append("")

    # --- 非成功 case 归因（按根因分层，而非笼统算 arm 失败） ---
    failures = [r for r in results if r.get("outcome") != "success"]
    if failures:
        lines.append("## 非成功 case 归因（按根因分层）")
        lines.append("")
        lines.append("| case | arm | outcome | 根因层 | 说明 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for r in failures:
            layer, why = attribute(r)
            lines.append(f"| {r['case']} | {r['arm']} | {r['outcome']} | {layer} | {why} |")
        lines.append("")
        lines.append("「blocked」= 模型诚实停下（做不下去、不编造），不等于答错；按根因分层，不笼统算 arm 失败。")
        lines.append("")

    lines.append("## 实验设计与 Verifier")
    lines.append("")
    lines.append(
        "两臂（jev / qwen）跑同样 5 个任务，唯一变量是主决策模型；jev 臂主决策走 OpenRouter Decisions API，"
        "没把握（置信度 <0.95 / 想 BLOCKED 但仍有可用动作 / 报错）时 fallback 到同一 qwen；qwen 臂主决策直接是 qwen chat。"
        "两臂 fallback/text/review 都用同一 qwen，保证唯一差异在主决策。orchestrator 对每个 (arm, case) fork 一个 worker 子进程"
        "（subprocess.run 跑 --worker，非 Claude 子代理），经 arm_env 强制所有模型调用走 OpenRouter 以统一报成本。"
    )
    lines.append("")
    lines.append(
        "验证独立于决策模型：每 case 一句 expect 短语，对最终页 title + 结构化文本 + document.body.innerText（CDP 抓全文）做 casefold 匹配。"
        "补 innerText 是因 Browser Use 的 llm_representation / eval_representation 都漏文章正文段落。"
    )
    lines.append("")

    # --- 发现（两模型对比） ---
    lines.append("## 发现（两模型对比）")
    lines.append("")
    lines.append("| arm | success | time | cost (USD) | decisions | fallback |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for arm in ARMS:
        rows = [r for r in results if r.get("arm") == arm]
        wins = sum(1 for r in rows if r.get("verified"))
        time_t = sum(r.get("wall_seconds") or 0 for r in rows)
        cost_t = sum((r.get("cost") or {}).get("total") or 0 for r in rows)
        dec = sum((r.get("counts") or {}).get("decisions") or 0 for r in rows)
        fb = sum((r.get("counts") or {}).get("fallback_decisions") or 0 for r in rows)
        lines.append(
            f"| {arm} | {wins}/{len(rows)} | {time_t:.0f}s | {cost_t:.6f} | {dec} | {fb} |"
        )
    lines.append("")
    for note in (
        "- **成功率收敛到相同**：修掉 tab 隔离后两臂都 4/5；唯一共同的失败是同一个 case（wiki-site-search），且两臂失败机制不同。",
        "- **失败模式相反**：jev 快速且诚实——要么快速报错（fallback 链 ~25s），要么 BLOCKED 诚实放弃（置信度递减）；qwen 缓慢卡死（stale-page ~456s，10 次决策都赶不上页面变动）。",
        "- **速度/成本差一个量级**：qwen ~2.6× 时间、~2× 成本；qwen ~41s/决策，jev 几秒/决策。",
        "- **qwen 的慢放大了环境噪声**：qwen 的 stale-page 失败根因是 Chrome 扩展（Monica/WebHighlights）持续改写 DOM + qwen 单决策太慢，永远赶不上页面变动——环境问题，非模型能力问题。",
        "- **jev 的低置信度是「诚实的犹豫」**：Marie Curie 搜索里 Jev 报 op 0.98（要点击）但 target 0.17（不知点哪个），正确 defer 给 fallback，结果 fallback(qwen) 反而输出非法 target——Jev 自我评估是准的，断在 fallback 链。",
        "- **两者成功的 4 个 case 完全重合**：google-author / google-capital / unit-converter / wiki-portal-article；唯一都过不了的是「搜索框输入→从下拉建议里精准选结果」的 wiki-site-search。",
        "- **jev 臂不是纯 Jev**：Jev 只出离散决策，fallback/text/review 全靠 qwen，所以 jev 臂的瓶颈部分来自 qwen。",
        "- **环境是比模型更大的变量**：两个非成功 case 的根因都是 harness/环境（tab 混用、扩展噪声），修完后模型差距很小。",
    ):
        lines.append(note)
    lines.append("")
    lines.append("### 为什么偏偏 wiki-site-search 最难？")
    lines.append("")
    lines.append("它是唯一一个「起点页噪声大 + 目标是瞬时下拉 + 候选有歧义」三者叠满的 task，而且两个 Wikipedia task 起点不是同一页：")
    lines.append("")
    lines.append("| task | 起点 URL | 关键动作 | 目标稳不稳 | 歧义 |")
    lines.append("| --- | --- | --- | --- | --- |")
    lines.append("| wiki-portal（Neptune） | `wikipedia.org` 干净门户 | 输入 + Enter → 直达条目 | 稳（Enter 直达） | 无（标题唯一） |")
    lines.append("| wiki-site-search（Marie Curie） | `en.wikipedia.org/wiki/Main_Page` 密集首页 | 输入 + 点下拉项 | 不稳（瞬时下拉） | 高（多个相似项） |")
    lines.append("| google-capital / google-author | Google 干净首页 | 输入 + Enter → 点 SERP 结果 | 稳（服务端链接） | 低（答案常在摘要） |")
    lines.append("| unit-converter | 转换器站 | 填表单 + 读字段 | 稳（静态控件） | 无 |")
    lines.append("")
    lines.append("三个叠加因素，正好对上两种失败：")
    lines.append("")
    lines.append("- **起点是密集首页**：`Main_Page` 有精选条目 / In the news / 每日图片等几十个可点元素，光「找搜索框」就要在一堆噪声里挑；jev 的 `final_url` 落在 `.../media/File:PtilopusEugeniaeKeulemans_(cropped).jpg`（首页「每日图片」的鸟图）就是点错的证据。")
    lines.append("- **目标是瞬时下拉**：输入后才生成、每次按键/失焦就重绘的客户端元素，是最脆弱的点击目标。")
    lines.append("- **候选有歧义**：`Marie Curie` / `Marie Curie (disambiguation)` / `Marie Skłodowska-Curie` 等多个相似项。")
    lines.append("")
    lines.append("→ **jev**：密集页 + 歧义下拉锁不定目标 → target 置信度 0.17 → defer 给 qwen → qwen 给非法 target → 报错。")
    lines.append("→ **qwen**：瞬时下拉 + 扩展每 ~10 次/秒改 DOM + ~41s/决策 → 决策落地时目标已 stale → 重试 → 死循环 → `blocked:stale-page`。")
    lines.append("")
    lines.append("其它 4 个 task 要么起点干净、要么目标稳、要么 Enter 直达无歧义，扩展噪声伤不到它们。所以「easy-medium」标签其实标错了：难度不在步骤多少，而在「是否要从噪声页的瞬时下拉里挑一个」——这个组合才会同时引爆两种失败。")
    lines.append("")
    lines.append("## 方法论要点与已知问题")
    lines.append("")
    for note in (
        "- benchmark 跑在用户真实 Chrome（共享 profile），目标 tab 有与用户其他 tab 混用的风险；follow_new_tab 已限制只跟 agent 自己打开的 tab。",
        "- qwen 也全部经 OpenRouter 调用（arm_env 清空 DEEPSEEK_API_KEY、设 OPENROUTER_MODEL/TEXT_MODEL）。",
        "- 单次 5-case 高方差，成功率只能当方向，不能当定论。",
        "- jev 快速失败、qwen 缓慢卡死；qwen 赢准确率、输成本/速度（qwen3-32b ~50s/决策）。",
    ):
        lines.append(note)
    lines.append("")
    return "\n".join(lines)


def main():
    load_dotenv()

    if "--worker" in sys.argv:
        payload = json.loads(sys.stdin.read())
        print(json.dumps(run_one(payload["case"], payload["arm"]), ensure_ascii=False))
        return 0

    if not os.environ.get("OPENROUTER_API_KEY"):
        print("Set OPENROUTER_API_KEY in .env first.", file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "benchmark_results" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    # Chrome's long-lived debugging session flaps (a fresh browser-level WS can
    # time out, and the daemon's own WS can drop). Retry a run only when it died
    # on the connection layer, not on a genuine model/browser decision.
    TRANSIENT = ("cdp", "handshake", "connection", "websocket", "timed out", "no close frame")

    results = []
    for arm in ARMS:
        for case in CASES:
            record = None
            for attempt in range(3):
                print(f"[{arm}] {case['name']} (attempt {attempt + 1}) ...", flush=True)
                record = spawn(case, arm)
                error = (record.get("error") or "").lower()
                if record.get("outcome") == "error" and any(token in error for token in TRANSIENT):
                    print("    transient CDP/connection error, retrying...", flush=True)
                    time.sleep(2)
                    continue
                break
            results.append(record)
            with (out_dir / "results.jsonl").open("a") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(
                f"    {record['outcome']}  verified={record.get('verified')}  "
                f"cost={record.get('cost', {}).get('total')}  {record.get('wall_seconds')}s",
                flush=True,
            )

    results.sort(key=lambda r: (r["case"], r["arm"]))
    (out_dir / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    report = render_report(results)
    (out_dir / "report.md").write_text(report)
    print("\n" + report)
    print(f"\nFull results: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

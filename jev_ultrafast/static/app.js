const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
let state = null,
  busy = false,
  automatic = false,
  serviceControlAvailable = false,
  serviceOnline = true;
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const percent = (value) => `${(value * 100).toFixed(value < 0.01 ? 1 : 0)}%`;
async function call(name, body = {}) {
  const response = await fetch(`/api/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Demo-Token": token },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "Request failed");
  state = data;
  render();
  return data;
}
function controls() {
  const live = state?.page && !["done", "blocked"].includes(state.status);
  $("start").disabled = busy;
  $("url").disabled = busy;
  $("expect-text").disabled = busy;
  $("goal").disabled = busy;
  $("choose").disabled = busy || !live;
  $("execute").disabled = busy || !state?.decision || !live;
  $("auto").disabled = busy || !live;
  $("auto").hidden = automatic;
  $("stop").hidden = !automatic;
  $("download").disabled = !state?.history?.length;
  $("service-restart").disabled = busy || !serviceOnline || !serviceControlAvailable;
  $("service-stop").disabled = busy || !serviceOnline || !serviceControlAvailable;
}
async function perform(fn, label) {
  if (busy) return;
  busy = true;
  $("error").hidden = true;
  controls();
  $("status").textContent = label;
  try {
    await fn();
  } catch (error) {
    automatic = false;
    try {
      state = await fetch("/api/state").then((r) => r.json());
      render();
    } catch {
      /* Preserve the original failure if the server disconnected. */
    }
    $("error").textContent = error.message;
    $("error").hidden = false;
    $("status").textContent = "已暂停 · 需要处理";
  } finally {
    busy = false;
    controls();
  }
}
function render() {
  if (!state) return;
  $("helper").textContent = `文本模型 · ${state.text_model}`;
  $("plan").innerHTML = (state.plan || [])
    .map(
      (goal, i) =>
        `<div class="plan-step ${i === state.plan_index ? "current" : ""}"><span>${i < state.plan_index ? "✓" : i + 1}</span>${escape(goal)}</div>`,
    )
    .join("");
  const page = state.page,
    d =
      state.decision ||
      (["done", "blocked"].includes(state.status) ? state.decisions?.at(-1) : null);
  const labels = {
    idle: "等待任务",
    ready: "页面已读取 · 等待下一步",
    predicted: "已选择操作 · 可检查或执行",
    done: state.verified === true
      ? "完成 · 页面文本验证通过"
      : state.expect_text
        ? "模型报告完成 · 验证文本未匹配"
        : state.stop_reason === "review-goal-met"
          ? "完成 · 页面复核认为目标已达成（未设置文本验证）"
          : "模型报告完成 · 未设置文本验证",
    blocked: blockedLabel(state),
  };
  $("status").textContent = labels[state.status] || state.status;
  if (!page) {
    controls();
    return;
  }
  $("empty").hidden = Boolean(page.screenshot);
  $("empty").innerHTML =
    '<p class="empty-symbol">[ ↗ ]</p><h2>观察浏览器操作过程</h2><p>提交任务后，这里会显示受控浏览器、可用操作和模型选择。</p>';
  $("screenshot").hidden = !page.screenshot;
  if (page.screenshot) $("screenshot").src = `data:image/jpeg;base64,${page.screenshot}`;
  $("url").textContent = page.url;
  $("page-title").textContent = page.title;
  $("action-count").textContent = `${state.elements.length} 个页面元素`;
  const chosen = page.actions.find((a) => a.id === d?.choice);
  $("choice-title").textContent = d
    ? chosen?.label || d.choice
    : "选择下一步操作";
  $("latency").textContent = d ? `${d.latency_ms} ms` : "—";
  $("confidence").textContent = d?.target_confidence != null ? percent(d.target_confidence) : "—";
  $("completion").textContent = d ? d.operation : "—";
  $("ranking-note").textContent = d
    ? d.fallback_reason ? "DeepSeek fallback" : "Jev 评分"
    : "尚未评分";
  const op = Object.entries(d?.operation_probabilities || {}).sort((a,b)=>b[1]-a[1]);
  $("operation-choices").innerHTML = op.map(([name,p]) =>
    `<span class="operation-choice ${name === d.operation ? 'best' : ''}">${escape(name)} <b>${percent(p)}</b></span>`).join('');
  const probability = e => d?.target_probabilities[e.index] ??
    Math.max(-1, ...(e.options || []).map(o=>d?.target_probabilities[o.index] ?? -1));
  const selectedIndex = d?.target?.split(':')[0];
  const elements = [...state.elements];
  if (d) elements.sort((a,b)=>probability(b)-probability(a));
  $("choices").innerHTML = elements.map(e => {
    const p = probability(e);
    return `<div class="choice ${selectedIndex === e.index ? 'best' : ''}" data-action="${escape(e.index)}"><span class="choice-id">[${escape(e.index)}]</span><div class="choice-label">${escape(e.label)}<small>${escape(e.role)} · ${escape(e.operations.join(' / '))}${e.value ? ' · '+escape(e.value) : ''}${e.checked !== undefined ? ' · checked '+escape(e.checked) : ''}</small>${p >= 0 ? `<div class="bar" style="--probability:${p*100}%"></div>` : ''}</div><span class="probability">${p >= 0 ? percent(p) : '—'}</span></div>`;
  }).join('');
  const targets = new Map();
  for (const a of page.actions) if (a.rect && !targets.has(a.node)) targets.set(a.node, a);
  $("targets").innerHTML = [...targets.values()].map((a,i) => {
    const index=String(i+1);
    return `<div class="target ${index === selectedIndex ? 'selected' : ''}" data-action="${index}" style="left:${100*a.rect.x/page.w}%;top:${100*a.rect.y/page.h}%;width:${100*a.rect.w/page.w}%;height:${100*a.rect.h/page.h}%"><span>${index}</span></div>`;
  }).join('');
  $("targets").hidden = !$("overlays").checked;
  $("history").innerHTML = state.history.length
    ? state.history
        .map(
          (h) =>
            `<div class="trace-row"><span class="number">${String(h.step).padStart(2, "0")}</span><div>${escape(h.action)}${h.text ? ` <b>“${escape(h.text)}”</b><small>${escape(h.text_helper)}</small>` : ""}</div><span class="time">${h.latency_ms} ms · ${percent(h.probability)}</span><span class="effect">${h.no_progress ? "No progress · this choice is suppressed" : h.page_changed ? "Page changed" : "No change observed"}</span></div>`,
        )
        .join("")
    : '<p class="muted">Each executed action leaves an observed result.</p>';
  $("step-count").textContent = `${state.history.length} 次操作 · ${(state.elapsed_ms / 1000).toFixed(2)} 秒`;
  $("model-state").textContent = JSON.stringify(
    d?.request || {
      goal: state.goal,
      url: page.url,
      title: page.title,
      headings: page.headings || [],
      visible_text: page.text,
      actions: page.actions.map(({ rect, node, ...rest }) => rest),
      omitted_actions: page.omitted_actions || 0,
    },
    null,
    2,
  );
  const counts = page.actions.reduce((out, action) => {
    out[action.kind] = (out[action.kind] || 0) + 1;
    return out;
  }, {});
  $("debug-state").textContent = JSON.stringify({
    status: state.status,
    stop_reason: state.stop_reason || null,
    page_source: page.source || "unknown",
    page: {
      url: page.url,
      title: page.title,
      headings_extracted: page.headings || [],
      visible_text_characters: page.text?.length || 0,
      actions_by_kind: counts,
      candidate_diagnostics: page.candidate_diagnostics || null,
      omitted_actions: page.omitted_actions || 0,
    },
    recent_decisions: (state.decisions || []).slice(-8).map((item) => ({
      model: item.model,
      operation: item.operation,
      target: item.target,
      choice: item.choice,
      confidence: item.confidence,
      target_confidence: item.target_confidence,
      fallback_reason: item.fallback_reason || null,
      jev: item.jev ? {
        model: item.jev.model,
        operation: item.jev.operation,
        target: item.jev.target,
        confidence: item.jev.confidence,
        target_confidence: item.jev.target_confidence,
      } : null,
    })),
    recent_text_helper_calls: (state.text_calls || []).slice(-8).map((item) => ({
      field: item.field,
      model: item.model,
      status: item.status || "ok",
      error: item.error || null,
    })),
  }, null, 2);
  controls();
}
function blockedLabel(current) {
  const last = current.decisions?.at(-1);
  switch (current.stop_reason) {
    case "model-blocked":
      return last?.fallback_reason
        ? "已停止 · Jev 与 fallback 都选择 BLOCKED"
        : "已停止 · Jev 选择 BLOCKED（展开调试信息查看候选）";
    case "same-target-no-change":
      return "已停止 · 同一目标连续操作未改变页面";
    case "repeated-fill":
      return "已停止 · 同一输入框重复填写未推进";
    case "three-actions-no-change":
      return "已停止 · 连续三步没有可见变化";
    case "stale-page": {
      const review = current.review_calls?.at(-1);
      if (review?.status === "error") {
        return "已停止 · 页面多次变化，页面复核未能完成";
      }
      if (review?.status === "ok") {
        return `已停止 · 页面多次变化，复核认为目标未达成${review.reason ? `（${review.reason}）` : ""}`;
      }
      return "已停止 · 页面多次变化，决策已过期";
    }
    case "target-lost":
      return "已停止 · 运行的页面已被关闭，未读取或操作任何其他标签页";
    case "action-budget":
      return "已停止 · 达到操作步数上限";
    default:
      return "已停止 · 展开调试信息查看原因";
  }
}
async function serviceAction(action) {
  if (busy || !serviceOnline || !serviceControlAvailable) return;
  const restarting = action === "restart";
  const message = restarting
    ? "重启服务会关闭当前浏览器任务、清空本次操作记录并重新加载服务。继续吗？"
    : "停止服务会关闭当前浏览器任务和本地界面。之后需要在终端重新运行 browser-agent-ui。继续吗？";
  if (!window.confirm(message)) return;
  busy = true;
  controls();
  $("service-status").textContent = restarting ? "正在重启服务…" : "正在停止服务…";
  try {
    const response = await fetch(`/api/service/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Demo-Token": token },
      body: "{}",
    });
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "服务操作失败");
    if (!restarting) {
      serviceOnline = false;
      $("service-status").textContent = "服务已停止 · 请在终端重新启动";
      $("status").textContent = "服务已停止";
      return;
    }
    $("status").textContent = "服务重启中…";
    await new Promise((resolve) => setTimeout(resolve, 500));
    for (let attempt = 0; attempt < 20; attempt++) {
      try {
        const check = await fetch("/api/state", { cache: "no-store" });
        if (check.ok) {
          window.location.reload();
          return;
        }
      } catch {
        // The loopback server is briefly unavailable while it binds again.
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    throw Error("服务尚未恢复；请检查启动终端或手动运行 browser-agent-ui。");
  } catch (error) {
    if (restarting) serviceOnline = false;
    $("service-status").textContent = restarting ? "服务未恢复" : "服务状态未知";
    $("error").textContent = error.message;
    $("error").hidden = false;
    $("status").textContent = "服务操作失败";
  } finally {
    busy = false;
    controls();
  }
}
$("service-restart").addEventListener("click", () => serviceAction("restart"));
$("service-stop").addEventListener("click", () => serviceAction("stop"));
$("task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  automatic = false;
  perform(async () => {
    await call("reset", {
      url: $("url").value.trim(),
      goal: $("goal").value,
      expect_text: $("expect-text").value.trim(),
    });
    await runAutomatically();
  }, "正在打开浏览器并执行任务…");
});
async function runAutomatically() {
  automatic = true;
  controls();
  for (let i = 0; i < state.max_steps * 2 && automatic; i++) {
    $("status").textContent = "自动执行中…";
    if ($("pace").checked) {
      await call("predict");
      await new Promise(resolve => setTimeout(resolve, 450));
      if (!automatic) break;
      await call("act", {fingerprint: state.page.fingerprint});
    } else {
      await call("tick");
    }
    if (["done", "blocked"].includes(state.status)) break;
  }
  automatic = false;
}
$("choose").addEventListener("click", () =>
  perform(() => call("predict"), "Jev 正在比较可用操作…"),
);
$("execute").addEventListener("click", () =>
  perform(
    () => call("act", { fingerprint: state.page.fingerprint }),
    "Executing the choice…",
  ),
);
$("auto").addEventListener("click", () =>
  perform(runAutomatically, "正在自动运行…"),
);
$("stop").addEventListener("click", () => {
  automatic = false;
  $("status").textContent = "Pausing after the current request…";
  controls();
});
$("overlays").addEventListener("change", () => {
  $("targets").hidden = !$("overlays").checked;
});
$("choices").addEventListener("pointerover", (event) => {
  const id = event.target.closest("[data-action]")?.dataset.action;
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === id || t.dataset.action === state?.decision?.target?.split(':')[0],
      ),
    );
});
$("choices").addEventListener("pointerleave", () =>
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === state?.decision?.target?.split(':')[0],
      ),
    ),
);
$("download").addEventListener("click", () => {
  const { page, ...rest } = state;
  const blob = new Blob(
    [
      JSON.stringify(
        { ...rest, page: { ...page, screenshot: undefined } },
        null,
        2,
      ),
    ],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "typesafe-browser-trace.json";
  a.click();
  URL.revokeObjectURL(url);
});
fetch("/api/state")
  .then((r) => r.json())
  .then((s) => {
    serviceOnline = true;
    serviceControlAvailable = Boolean(s.service_control);
    $("service-status").textContent = serviceControlAvailable
      ? "本机服务运行中"
      : "重启服务进程以启用控制按钮";
    state = s;
    render();
  })
  .catch(() => {
    serviceOnline = false;
    $("service-status").textContent = "无法连接本机服务";
    $("status").textContent = "Cannot reach local demo server";
    controls();
  });

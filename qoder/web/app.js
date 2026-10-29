/*! bicycles.txt — browser client for 知新 Recall Coach. Vanilla JS, no build step. */
(function () {
  "use strict";

  var state = {
    status: null,
    materials: [],
    courses: [],
    filter: "",
    tab: "store",
    session: null,
    cardIndex: 0,
    answered: {},
    memory: null,
  };

  function $(id) {
    return document.getElementById(id);
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function pct(value) {
    return Math.round((Number(value) || 0) * 100) + "%";
  }

  function short(text, limit) {
    text = String(text == null ? "" : text);
    return text.length > limit ? text.slice(0, limit) + "…" : text;
  }

  function toast(message, bad) {
    var box = $("toast");
    box.textContent = message;
    box.className = "toast show" + (bad ? " error" : "");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(function () {
      box.className = "toast" + (bad ? " error" : "");
    }, 3600);
  }

  async function api(path, options) {
    var response;
    try {
      response = await fetch(path, options);
    } catch (error) {
      throw new Error("网络连接失败，请确认后端服务已启动");
    }
    var body = null;
    try {
      body = await response.json();
    } catch (error) {
      body = null;
    }
    if (!response.ok) {
      var detail = body && (body.detail || body.message);
      if (Array.isArray(detail)) detail = detail.map(function (item) { return item.msg || JSON.stringify(item); }).join("；");
      throw new Error(detail || "请求失败（HTTP " + response.status + "）");
    }
    return body;
  }

  function post(path, payload) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  function chip(kind, text, strongText, title) {
    return (
      '<span class="chip ' + kind + '" title="' + esc(title || "") + '"><span class="dot"></span>' +
      esc(text) + (strongText ? " <strong>" + esc(strongText) + "</strong>" : "") + "</span>"
    );
  }

  // ------------------------------------------------------------- top status
  function renderStatus() {
    var status = state.status;
    var memory = (status && status.memory) || {};
    var backend = (status && status.backend) || {};
    var embedding = (status && status.embedding) || {};
    var stats = (status && status.stats) || {};
    var chips = [];

    if (!status) {
      chips.push(chip("off", "服务未连接"));
    } else {
      var isSeekdb = backend.backend === "seekdb";
      chips.push(
        chip(
          isSeekdb ? "seekdb" : "mirror",
          isSeekdb ? "seekdb" : "SQLite 镜像",
          isSeekdb ? backend.mode : "无 seekdb",
          JSON.stringify(backend)
        )
      );
      chips.push(chip("off", "向量", (embedding.dim || "?") + " 维 · " + (embedding.name || "?"), "embedding profile"));
      if (stats.fts_parser) chips.push(chip("off", "全文", stats.fts_parser, "FTS parser"));
      chips.push(
        memory.available
          ? chip("pc", "PowerContext", memory.mode, "base_url: " + (memory.base_url || ""))
          : chip("off", "记忆不可用", "", memory.error || "PowerContext 未启动")
      );
    }
    $("chips").innerHTML = chips.join("");

    $("stat-materials").textContent = stats.materials != null ? stats.materials : 0;
    $("stat-mastery").textContent = pct(stats.avg_mastery || 0);
    $("stat-reviews").textContent = stats.reviews != null ? stats.reviews : 0;
    $("stat-weak").textContent = stats.weak != null ? stats.weak : 0;

    var isSeekdb = backend.backend === "seekdb";
    $("material-sub").textContent = "检索底座 · " + (isSeekdb ? "seekdb" : "SQLite 镜像（seekdb 降级）");
    $("review-sub").textContent = memory.available
      ? "检索决定看什么 · PowerContext 决定怎么讲"
      : "检索决定看什么 · 记忆不可用时按掌握度排序";
  }

  async function refreshStatus() {
    try {
      state.status = await api("/api/status");
    } catch (error) {
      state.status = null;
      toast("状态获取失败：" + error.message, true);
    }
    renderStatus();
  }

  // --------------------------------------------------------------- materials
  function renderCourses() {
    var buttons = ['<button class="tab' + (state.filter ? "" : " active") + '" data-course="">全部课程</button>'];
    state.courses.forEach(function (course) {
      buttons.push(
        '<button class="tab' + (state.filter === course.course ? " active" : "") +
          '" data-course="' + esc(course.course) + '" title="' +
          esc(course.total + " 段 · 平均掌握 " + pct(course.avg_mastery)) + '">' +
          esc(course.course) + " · " + course.total + "</button>"
      );
    });
    $("course-filter").innerHTML = buttons.join("");
    $("course-filter").style.flexWrap = "wrap";

    var options = ['<option value="">全部课程</option>'];
    state.courses.forEach(function (course) {
      options.push('<option value="' + esc(course.course) + '">' + esc(course.course) + "</option>");
    });
    var select = $("session-course");
    var keep = select.value;
    select.innerHTML = options.join("");
    select.value = state.courses.some(function (item) { return item.course === keep; }) ? keep : "";

    $("course-options").innerHTML = state.courses
      .map(function (course) { return '<option value="' + esc(course.course) + '"></option>'; })
      .join("");
  }

  function renderMaterials() {
    var box = $("material-list");
    var items = state.materials.filter(function (item) {
      return !state.filter || item.course === state.filter;
    });
    if (!items.length) {
      box.innerHTML = '<div class="empty">还没有资料。点「载入示例」或导入一段笔记。</div>';
      return;
    }
    box.innerHTML = items
      .map(function (item) {
        var mastery = Number(item.mastery) || 0;
        return (
          '<div class="material">' +
          '<div class="head"><b>' + esc(item.title) + "</b>" +
          '<span class="course">' + esc(item.course) + "</span></div>" +
          '<div class="body">' + esc(short(item.body, 150)) + "</div>" +
          '<div class="mastery-line"><span>掌握 ' + mastery.toFixed(2) + "</span>" +
          '<div class="bar"><i style="width:' + Math.round(mastery * 100) + '%"></i></div>' +
          "<span>复习 " + (item.reviews || 0) + " 次</span></div>" +
          "</div>"
        );
      })
      .join("");
  }

  async function refreshMaterials() {
    try {
      var body = await api("/api/materials");
      state.materials = body.items || [];
      state.courses = body.courses || [];
    } catch (error) {
      toast("资料加载失败：" + error.message, true);
    }
    renderCourses();
    renderMaterials();
  }

  // ------------------------------------------------------------------ search
  function renderSearchResult(body) {
    var box = $("search-results");
    if (!body.hits || !body.hits.length) {
      box.innerHTML = '<div class="empty">没有命中。换个关键词试试。</div>';
      return;
    }
    box.innerHTML = body.hits
      .map(function (hit) {
        var material = hit.material || {};
        var pieces = ["融合 " + Number(hit.fused || 0).toFixed(4)];
        if (hit.vector) {
          pieces.push("向量 #" + hit.vector.rank + (hit.vector.distance != null ? "（距离 " + Number(hit.vector.distance).toFixed(3) + "）" : ""));
        }
        if (hit.lexical) {
          pieces.push("全文 #" + hit.lexical.rank + (hit.lexical.score != null ? "（得分 " + Number(hit.lexical.score).toFixed(3) + "）" : ""));
        }
        return (
          '<div class="item"><div><b>' + esc(material.title || "") + "</b> " +
          '<span class="meta">' + esc(material.course || "") + " · " + pieces.join(" · ") + "</span></div>" +
          '<div class="meta">' + esc(short(material.body, 180)) + "</div></div>"
        );
      })
      .join("");
  }

  async function runSearch() {
    var query = $("search-input").value.trim();
    if (!query) {
      toast("先输入检索词", true);
      return;
    }
    try {
      var body = await post("/api/search", { query: query, course: state.filter || null, top_k: 6 });
      renderSearchResult(body);
      await refreshEvidence(state.tab);
    } catch (error) {
      toast("检索失败：" + error.message, true);
    }
  }

  // ---------------------------------------------------------------- session
  var VERDICT_TEXT = { correct: "答对", hard: "标记为有点难", again: "没答对" };
  var VERDICT_CLASS = { correct: "ok", hard: "hard", again: "bad" };

  function memoryBanner() {
    var memory = (state.session && state.session.memory) || {};
    var context = memory.context || {};
    if (!memory.available) {
      return '<div class="note">PowerContext 未连接：本轮只按掌握度与复习次数排序，答题不会写入记忆。</div>';
    }
    var hits = memory.hits || [];
    var lines = [];
    var seenHints = {};
    if (memory.preference) {
      lines.push("讲解偏好：" + String(memory.preference).replace(/^讲解偏好[：:]\s*/, ""));
    }
    hits.forEach(function (hit) {
      var text = String(hit.text || "");
      // Repeated practice writes one row per attempt; show the topic once.
      var key = text.replace(/（复习第.*$/, "");
      if (seenHints[key]) return;
      seenHints[key] = true;
      lines.push("· " + short(text, 90) + (hit.score != null ? "（相关 " + Number(hit.score).toFixed(2) + "）" : ""));
    });
    if (!lines.length) {
      lines.push("（还没有与本节相关的记忆；答错与偏好会陆续写入）");
    }
    var statusLabel = { ready: "已注入", empty: "本轮为空", missing: "无记忆", offline: "未连接" }[context.status] ||
      (memory.available ? "已就绪" : "未连接");
    return (
      '<div class="memory-banner"><h3>PowerContext 本轮记忆' +
      '<span class="pill">' + esc(statusLabel) + "</span>" +
      '<span class="pill">' + (context.content_bytes || 0) + " 字节</span></h3>" +
      "<pre>" + esc(lines.join("\n")) + "</pre></div>"
    );
  }

  function renderCard() {
    var session = state.session;
    var card = session.cards[state.cardIndex];
    var total = session.cards.length;
    var answered = state.answered[state.cardIndex];

    var head =
      '<div class="card-head"><span>' + esc(card.course) + " · 第 " + (state.cardIndex + 1) + "/" + total + " 张</span>" +
      '<span class="grow"></span>' +
      '<span>记忆相关度 ' + (card.affinity != null ? card.affinity : 0) + "</span></div>";

    var progress = '<div class="progress">';
    for (var i = 0; i < total; i += 1) {
      progress += '<i class="' + (state.answered[i] ? "done" : i === state.cardIndex ? "now" : "") + '"></i>';
    }
    progress += "</div>";

    var options = '<div class="options">';
    card.options.forEach(function (option) {
      var classes = "option";
      if (answered) {
        classes += " disabled";
        if (option === answered.answer) classes += " correct";
        if (answered.picked === option && answered.verdict !== "correct") classes += " wrong";
      }
      options +=
        '<button class="' + classes + '" data-option="' + esc(option) + '"' + (answered ? " disabled" : "") + ">" +
        "<span class=\"idx\">" + (card.options.indexOf(option) + 1) + "</span>" + esc(option) + "</button>";
    });
    options += "</div>";

    var body =
      '<div class="card">' + head + '<div class="question">' + esc(card.question) + "</div>" +
      '<div class="prompt">' + esc(card.prompt) + "</div>" + options;

    if (answered) {
      var writeNote;
      var writtenEntry = answered.memory_write ? (answered.memory_write.entry || ((answered.memory_write.records || [])[0] || {})) : {};
      if (answered.memory_write && writtenEntry.text) {
        writeNote = "<span>已写入 PowerContext：" + esc(writtenEntry.text) + "</span>";
      } else if (answered.memory_write) {
        writeNote = "<span>已写入 PowerContext（服务端已确认记录）。</span>";
      } else if (answered.verdict === "hard") {
        writeNote = "<span>标记为「有点难」不写入记忆；下次仍然答错时会记录薄弱点。</span>";
      } else if (answered.verdict === "again") {
        writeNote = "<span>薄弱点未写入 PowerContext（记忆服务暂不可用），复习记录已保存在 seekdb。</span>";
      } else {
        writeNote = "<span>本次未产生新记忆（每 3 次答对记录一次进度）。</span>";
      }
      body +=
        '<div class="verdict ' + (VERDICT_CLASS[answered.verdict] || "ok") + '">' +
        "<b>" + esc(VERDICT_TEXT[answered.verdict] || answered.verdict) + "</b>" +
        "<span>正确答案：" + esc(answered.answer) + " · 掌握度 " + pct(answered.mastery) + "</span>" +
        writeNote +
        "</div>";

      var related = answered.related || [];
      if (related.length) {
        body += '<div class="related">';
        related.forEach(function (item) {
          body +=
            '<div class="item"><div><b>' + esc(item.title) + "</b> " +
            '<span class="meta">' + esc(item.course) + " · 融合 " + Number(item.fused || 0).toFixed(4) +
            (item.vector_rank ? " · 向量 #" + item.vector_rank : "") +
            (item.lexical_rank ? " · 全文 #" + item.lexical_rank : "") + "</span></div>" +
            '<div class="meta">' + esc(short(item.snippet, 200)) + "</div></div>";
        });
        body += "</div>";
      }

      var retrieval = answered.retrieval || [];
      if (retrieval.length) {
        body +=
          '<details><summary class="tiny">本轮召回执行记录（' + retrieval.length + ' 条 SQL）</summary>' +
          retrieval.map(traceStoreRow).join("") + "</details>";
      }

      body += '<div class="row">';
      body += answered.remaining > 0
        ? '<button class="primary" id="btn-next">下一张（剩 ' + answered.remaining + "）</button>"
        : '<button class="primary" id="btn-fresh">再来一轮</button>';
      body += "</div>";
    } else {
      body +=
        '<div class="row"><button id="btn-hard">有点难，看答案</button><span class="grow"></span>' +
        '<span class="tiny">选项由 seekdb 从本课程相似段落里挑出</span></div>';
    }
    body += "</div>";
    return memoryBanner() + progress + body;
  }

  function renderSession() {
    $("review-stage").innerHTML = renderCard();
  }

  async function startSession() {
    var course = $("session-course").value || null;
    var budget = Number($("session-budget").value) || 8;
    try {
      state.session = await post("/api/session", { course: course, budget: budget });
    } catch (error) {
      toast("无法开始本轮：" + error.message, true);
      return;
    }
    state.cardIndex = 0;
    state.answered = {};
    renderSession();
    await refreshEvidence(state.tab);
  }

  async function grade(option, forced) {
    if (!state.session || state.answered[state.cardIndex]) return;
    var card = state.session.cards[state.cardIndex];
    var payload = forced
      ? { material_id: card.material_id, verdict: forced }
      : { material_id: card.material_id, picked: option };
    var result;
    try {
      result = await post("/api/session/" + encodeURIComponent(state.session.session_id) + "/grade", payload);
    } catch (error) {
      toast("提交失败：" + error.message, true);
      return;
    }
    state.answered[state.cardIndex] = result;
    renderSession();
    await refreshStatus();
    await refreshMaterials();
    await refreshEvidence(state.tab);
    if (result.memory_write) {
      await refreshMemory();
      if (state.tab === "memory") await refreshEvidence("memory");
    }
  }

  // ----------------------------------------------------------------- memory
  var KIND_LABEL = { weakness: "薄弱点", progress: "进度", preference: "偏好", misconception: "误区", goal: "目标" };

  function renderMemory(snapshot) {
    state.memory = snapshot;
    var box = $("memory-entries");
    if (!snapshot || !snapshot.available) {
      box.innerHTML =
        '<div class="empty">PowerContext 未启动或不可用' +
        (snapshot && snapshot.error ? "：" + esc(short(snapshot.error, 120)) : "") + "</div>";
      return;
    }
    var entries = snapshot.entries || [];
    if (!entries.length) {
      box.innerHTML = '<div class="empty">还没有记忆。答错一张卡，或保存一条讲解偏好试试。</div>';
      return;
    }
    box.innerHTML = entries
      .map(function (entry, index) {
        var inactive = entry.state === "inactive";
        return (
          '<div class="entry' + (inactive ? " inactive" : "") + '" data-index="' + index + '">' +
          '<div class="head"><span class="kind ' + esc(entry.kind) + '">' + esc(KIND_LABEL[entry.kind] || entry.kind) + "</span>" +
          '<span class="meta">v' + esc(entry.version) + " · " + esc(entry.state) + "</span>" +
          '<span class="spacer"></span>' +
          (inactive ? "" : '<button data-edit="1">修正</button><button class="ghost" data-retire="1">失效</button>') +
          "</div>" +
          '<div class="text">' + esc(entry.text) + "</div>" +
          "</div>"
        );
      })
      .join("");
    box.querySelectorAll(".entry").forEach(function (node) {
      var entry = entries[Number(node.dataset.index)];
      if (!entry) return;
      var edit = node.querySelector("[data-edit]");
      var retire = node.querySelector("[data-retire]");
      if (edit) edit.addEventListener("click", function () { reviseEntry(entry); });
      if (retire) retire.addEventListener("click", function () { retireEntry(entry); });
    });
    if (snapshot.preference) {
      $("preference-input").placeholder = "当前偏好：" + short(snapshot.preference.replace(/^讲解偏好：/, ""), 36);
    }
  }

  async function refreshMemory() {
    try {
      var snapshot = await api("/api/memory");
      renderMemory(snapshot);
    } catch (error) {
      renderMemory({ available: false, error: error.message, entries: [] });
    }
  }

  async function reviseEntry(entry) {
    var next = window.prompt("修正这条记忆的文字：", entry.text);
    if (next == null) return;
    next = next.trim();
    if (!next) return;
    try {
      var body = await post("/api/memory/revise", {
        artifact: entry.artifact,
        state_version: entry.state_version || 0,
        kind: entry.kind,
        text: next,
      });
      renderMemory(body.memory);
      toast("记忆已修正（旧版本仍可审计）");
      await refreshEvidence("memory");
    } catch (error) {
      toast("修正失败：" + error.message, true);
    }
  }

  async function retireEntry(entry) {
    try {
      var body = await post("/api/memory/retire", {
        artifact: entry.artifact,
        state_version: entry.state_version || 0,
        reason: "学习者在记忆面板中标记失效",
      });
      renderMemory(body.memory);
      toast("记忆已失效（历史仍可审计）");
      await refreshEvidence("memory");
    } catch (error) {
      toast("失效失败：" + error.message, true);
    }
  }

  async function savePreference() {
    var value = $("preference-input").value.trim();
    if (!value) {
      toast("先写一条偏好，例如「先讲例子再讲定义」", true);
      return;
    }
    try {
      var body = await post("/api/memory/preference", { value: value });
      $("preference-input").value = "";
      renderMemory(body.memory);
      toast("已写入 PowerContext 偏好记忆");
      await refreshEvidence("memory");
    } catch (error) {
      toast("保存失败：" + error.message, true);
    }
  }

  // --------------------------------------------------------------- evidence
  function traceStoreRow(row) {
    return (
      '<div class="row"><div class="label"><span class="channel ' + esc(row.channel || "") + '">' +
      esc(row.channel || "") + "</span><span>" + (row.rows || 0) + " 行 · " + (row.duration_ms || 0) + " ms</span></div>" +
      "<code>" + esc(short(row.sql || "", 420)) + "</code>" +
      (row.params && Object.keys(row.params).length
        ? "<code>params: " + esc(short(JSON.stringify(row.params), 260)) + "</code>"
        : "") +
      (row.note ? '<div class="label">' + esc(row.note) + "</div>" : "") +
      "</div>"
    );
  }

  function traceMemoryRow(row) {
    return (
      '<div class="row"><div class="label"><span class="channel memory">memory</span>' +
      "<span>" + esc(row.status || "") + " · " + esc(row.method || "") + " " + esc(row.path || "") +
      " · " + (row.duration_ms || 0) + " ms</span></div>" +
      "<code>request: " + esc(short(JSON.stringify(row.request || {}), 300)) + "</code>" +
      "<code>response: " + esc(short(JSON.stringify(row.response || {}), 300)) + "</code></div>"
    );
  }

  async function refreshEvidence(which) {
    try {
      var body = await api("/api/evidence?limit=40");
      var rows = which === "memory" ? body.memory || [] : body.store || [];
      var box = $("trace-list");
      if (!rows.length) {
        box.innerHTML = '<div class="empty">' + (which === "memory" ? "还没有 PowerContext 读写记录。" : "还没有 seekdb 执行记录。") + "</div>";
        return;
      }
      box.innerHTML = rows.map(which === "memory" ? traceMemoryRow : traceStoreRow).join("");
    } catch (error) {
      $("trace-list").innerHTML = '<div class="empty">执行记录暂不可用：' + esc(short(error.message, 120)) + "</div>";
    }
  }

  function switchTab(tab) {
    state.tab = tab;
    document.querySelectorAll("#evidence-tabs .tab").forEach(function (button) {
      button.classList.toggle("active", button.dataset.tab === tab);
    });
    refreshEvidence(tab);
  }

  // ------------------------------------------------------------------- boot
  async function boot() {
    $("search-box").classList.remove("hidden");

    $("btn-seed").addEventListener("click", async function () {
      try {
        var body = await post("/api/seed", {});
        toast("示例资料：" + body.imported + " 段新导入，跳过 " + body.skipped + " 条");
        await refreshStatus();
        await refreshMaterials();
        await refreshEvidence(state.tab);
      } catch (error) {
        toast("载入失败：" + error.message, true);
      }
    });

    $("course-filter").addEventListener("click", function (event) {
      var button = event.target.closest("[data-course]");
      if (!button) return;
      state.filter = button.dataset.course;
      $("session-course").value = state.filter;
      renderCourses();
      renderMaterials();
    });

    $("btn-import").addEventListener("click", async function () {
      var course = $("import-course").value.trim() || "未分类";
      var title = $("import-title").value.trim() || "未命名笔记";
      var text = $("import-text").value.trim();
      if (!text) {
        toast("先粘贴一点笔记内容", true);
        return;
      }
      try {
        var body = await post("/api/materials", { course: course, title: title, text: text, source: "manual" });
        $("import-hint").textContent = "已写入 " + body.passages + " 段（" + body.course + "）";
        $("import-text").value = "";
        toast("导入成功：" + body.passages + " 段已进入 seekdb");
        await refreshStatus();
        await refreshMaterials();
        await refreshEvidence(state.tab);
      } catch (error) {
        toast("导入失败：" + error.message, true);
      }
    });

    $("upload-file").addEventListener("change", async function () {
      var file = this.files && this.files[0];
      if (!file) return;
      var form = new FormData();
      form.append("file", file);
      form.append("course", $("import-course").value.trim() || "未分类");
      form.append("title", $("import-title").value.trim());
      try {
        var body = await api("/api/materials/upload", { method: "POST", body: form });
        $("import-hint").textContent = "上传成功：" + body.passages + " 段（" + body.bytes + " 字节）";
        toast("上传成功：" + body.passages + " 段（" + body.bytes + " 字节）");
        await refreshStatus();
        await refreshMaterials();
        await refreshEvidence(state.tab);
      } catch (error) {
        toast("上传失败：" + error.message, true);
      }
    });

    $("btn-search").addEventListener("click", runSearch);
    $("search-input").addEventListener("keydown", function (event) {
      if (event.key === "Enter") runSearch();
    });

    $("btn-session").addEventListener("click", startSession);

    $("review-stage").addEventListener("click", function (event) {
      var option = event.target.closest("[data-option]");
      if (option && !option.disabled) {
        grade(option.dataset.option, null);
        return;
      }
      if (event.target.id === "btn-hard") {
        grade(null, "hard");
      } else if (event.target.id === "btn-next") {
        if (state.session && state.cardIndex + 1 < state.session.cards.length) {
          state.cardIndex += 1;
          renderSession();
        }
      } else if (event.target.id === "btn-fresh") {
        startSession();
      }
    });

    $("btn-memory-refresh").addEventListener("click", refreshMemory);
    $("btn-preference").addEventListener("click", savePreference);
    $("preference-input").addEventListener("keydown", function (event) {
      if (event.key === "Enter") savePreference();
    });

    $("evidence-tabs").addEventListener("click", function (event) {
      var tab = event.target.closest("[data-tab]");
      if (tab) switchTab(tab.dataset.tab);
    });

    await refreshStatus();
    await refreshMaterials();
    await refreshMemory();
    await refreshEvidence(state.tab);

    if (!state.materials.length) {
      toast("资料库是空的：点「载入示例」用内置课程笔记开始。");
    }
    setInterval(refreshStatus, 15000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();

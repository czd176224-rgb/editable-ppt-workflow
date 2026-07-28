(function () {
  "use strict";

  var app = document.getElementById("app");
  var progress = document.getElementById("progress");
  var catalogs = {};
  var recommendation = null;
  var state = {};

  var labels = {
    audience: "目标受众",
    core_message: "核心信息",
    delivery_context: "使用场景",
    content_divergence: "内容处理边界",
    canvas: "画布比例",
    additional_requirements: "自然语言补充要求",
    page_count: "检测到的 Word 页数",
    pagination_mode: "分页识别方式",
    one_page_to_one_slide: "一页对应一张幻灯片"
  };

  function node(tag, className, text) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (text != null) element.textContent = text;
    return element;
  }

  function requestJson(url, options) {
    return fetch(url, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) throw new Error(data.error || ("请求失败：" + response.status));
        return data;
      });
    });
  }

  function recommendationStage(value) {
    var stage = String(value || "").toLowerCase();
    if (stage === "stage1") return 1;
    if (stage === "stage2") return 2;
    if (stage === "stage3") return 3;
    return 0;
  }

  function updateProgress(stage) {
    progress.replaceChildren();
    ["沟通与边界", "视觉系统", "生产机制"].forEach(function (name, index) {
      var step = node("div", "progress-step", name);
      if (index + 1 === stage) step.classList.add("active");
      if (index + 1 < stage) step.classList.add("done");
      step.prepend(node("span", "progress-number", String(index + 1)));
      progress.appendChild(step);
    });
  }

  function section(kicker, title, copy) {
    var wrapper = node("section", "panel");
    wrapper.appendChild(node("p", "section-kicker", kicker));
    wrapper.appendChild(node("h2", "section-title", title));
    if (copy) wrapper.appendChild(node("p", "section-copy", copy));
    return wrapper;
  }

  function textAreaField(name, value, rows) {
    var field = node("label", "field");
    field.appendChild(node("span", "field-label", labels[name] || name));
    var input = node("textarea", "text-input");
    input.name = name;
    input.rows = rows || 3;
    input.value = value == null ? "" : String(value);
    input.addEventListener("input", function () { state[name] = input.value; });
    field.appendChild(input);
    return field;
  }

  function selectField(name, title, value, options) {
    var field = node("label", "field compact-field");
    field.appendChild(node("span", "field-label", title));
    var select = node("select", "select-input");
    select.name = name;
    (options || []).forEach(function (option) {
      var item = node("option", "", option.name || option.id);
      item.value = option.id;
      item.selected = option.id === value;
      select.appendChild(item);
    });
    select.addEventListener("change", function () {
      state[name] = select.value;
      updateCombinedPreview();
    });
    field.appendChild(select);
    return field;
  }

  function actionBar(label, handler) {
    var bar = node("div", "action-bar");
    var status = node("p", "action-status", "确认后将进入下一阶段");
    status.id = "action-status";
    var button = node("button", "primary-button", label);
    button.type = "button";
    button.addEventListener("click", function () { handler(button, status); });
    bar.append(status, button);
    return bar;
  }

  function valueOf(field) {
    return field && typeof field === "object" && Object.prototype.hasOwnProperty.call(field, "value")
      ? field.value : field;
  }

  function renderStage1() {
    updateProgress(1);
    state = {};
    app.replaceChildren();
    var intro = section(
      "STAGE 01 · COMMUNICATION",
      "确认受众、核心表达与材料边界",
      "下方文字与画布可编辑；分页事实来自已锁定的 Word 项目，只供核对。"
    );
    var facts = node("div", "fact-grid");
    recommendation.read_only_fields.forEach(function (name) {
      var card = node("article", "fact-card");
      card.dataset.readonly = "true";
      card.appendChild(node("span", "fact-label", labels[name]));
      var raw = valueOf(recommendation[name]);
      var display = typeof raw === "boolean" ? (raw ? "是" : "否") : String(raw);
      var output = node("output", "fact-value", display);
      output.name = name;
      card.appendChild(output);
      facts.appendChild(card);
    });
    intro.appendChild(facts);
    var fields = node("div", "field-grid");
    recommendation.editable_fields.forEach(function (name) {
      state[name] = valueOf(recommendation[name]);
      if (name === "canvas") {
        fields.appendChild(selectField(name, labels[name], state[name], catalogs.canvas));
      } else {
        fields.appendChild(textAreaField(name, state[name], name === "audience" ? 2 : 3));
      }
    });
    intro.appendChild(fields);
    intro.appendChild(actionBar("确认沟通与边界", function (button, status) {
      submit(button, status, Object.assign({stage: "stage1"}, state), 2);
    }));
    app.appendChild(intro);
  }

  function directionName(direction, index) {
    return direction.name_zh || direction.name_en || direction.name || ("方向 " + (index + 1));
  }

  function safeDirectionIndex(index, candidates) {
    if (Number.isInteger(index) && index >= 0 && index < candidates.length) return index;
    return 0;
  }

  function chooseDirection(index, render) {
    var directions = recommendation.design_directions || {};
    var candidates = Array.isArray(directions.candidates) ? directions.candidates : [];
    var safeIndex = safeDirectionIndex(index, candidates);
    var direction = candidates[safeIndex];
    if (!direction) return false;
    state.direction = safeIndex;
    state.visual_style = direction.visual_style;
    state.icons = direction.icons;
    state.color = JSON.parse(JSON.stringify(direction.color));
    state.typography = JSON.parse(JSON.stringify(direction.typography));
    state.image_rendering = JSON.parse(JSON.stringify(direction.image_rendering));
    state.style_axes = JSON.parse(JSON.stringify(direction.style_axes));
    state.information_density = direction.information_density;
    if (render) renderStage2();
    return true;
  }

  function addDirectionCards(panel) {
    var grid = node("div", "direction-grid");
    recommendation.design_directions.candidates.forEach(function (direction, index) {
      var card = node("button", "direction-card");
      card.type = "button";
      card.dataset.direction = String(index);
      if (index === state.direction) card.classList.add("selected");
      var image = node("img", "direction-image");
      var style = (catalogs.visual_style || []).find(function (item) {
        return item.id === direction.visual_style;
      });
      image.src = "/static/style_previews/" + ((style && style.preview) || "editorial.svg");
      image.alt = directionName(direction, index) + "风格预览";
      card.appendChild(image);
      card.appendChild(node("strong", "direction-name", directionName(direction, index)));
      card.appendChild(node("span", "direction-note", direction.note_zh || direction.note || "协调的版式、色彩、字体与图像表达"));
      card.addEventListener("click", function () { chooseDirection(index, true); });
      grid.appendChild(card);
    });
    panel.appendChild(grid);
  }

  function updateCombinedPreview() {
    var preview = document.getElementById("combined-preview");
    if (!preview || !state.color || !state.typography) return;
    var palette = state.color.palette || {};
    preview.style.background = palette.background || "#FFFFFF";
    preview.style.color = palette.body_text || "#1F2937";
    preview.style.borderColor = palette.secondary_accent || "#CBD5E1";
    preview.querySelector("h3").style.color = palette.primary || "#17365D";
    preview.querySelector("h3").style.fontFamily = state.typography.heading.css || "sans-serif";
    preview.querySelector("p").style.fontFamily = state.typography.body.css || "sans-serif";
    preview.querySelector("i").style.background = palette.accent || "#D97706";
  }

  function paletteControls(panel) {
    var palette = state.color.palette;
    var roles = {
      background: "背景",
      secondary_bg: "次级背景",
      primary: "主色",
      accent: "强调色",
      secondary_accent: "辅助强调色",
      body_text: "正文色"
    };
    var grid = node("div", "palette-grid");
    Object.keys(roles).forEach(function (role) {
      var field = node("label", "color-field");
      field.appendChild(node("span", "field-label", roles[role]));
      var row = node("div", "color-row");
      var picker = node("input", "color-picker");
      picker.type = "color";
      picker.value = palette[role];
      var text = node("input", "hex-input");
      text.type = "text";
      text.value = palette[role];
      function setColor(value) {
        if (/^#[0-9a-f]{6}$/i.test(value)) {
          palette[role] = value.toUpperCase();
          picker.value = value;
          text.value = value.toUpperCase();
          updateCombinedPreview();
        }
      }
      picker.addEventListener("input", function () { setColor(picker.value); });
      text.addEventListener("change", function () { setColor(text.value.trim()); });
      row.append(picker, text);
      field.appendChild(row);
      grid.appendChild(field);
    });
    panel.appendChild(grid);
  }

  function simpleInput(parent, title, value, onInput, type) {
    var field = node("label", "field compact-field");
    field.appendChild(node("span", "field-label", title));
    var input = node("input", "text-input");
    input.type = type || "text";
    input.value = value;
    input.addEventListener("input", function () { onInput(input.value); updateCombinedPreview(); });
    field.appendChild(input);
    parent.appendChild(field);
  }

  function renderStage2() {
    updateProgress(2);
    var initialize = !state.color;
    if (initialize) {
      var candidates = recommendation.design_directions.candidates || [];
      var recommended = recommendation.design_directions.selected;
      if (safeDirectionIndex(recommended, candidates) !== recommended) {
        recommended = (recommendation.recommend || {}).direction;
      }
      recommended = safeDirectionIndex(recommended, candidates);
      chooseDirection(recommended, false);
      state.delivery_purpose = (recommendation.recommend || {}).delivery_purpose || "balanced";
      state.mode = (recommendation.recommend || {}).mode || "pyramid";
      state.additional_requirements = (recommendation.recommend || {}).additional_requirements || "";
    }
    app.replaceChildren();
    var panel = section(
      "STAGE 02 · VISUAL SYSTEM",
      "选择一个协调方向，再精调视觉系统",
      "每个方向同时协调版式、色彩、字体、图标与图像渲染。选择方向后仍可逐项调整。"
    );
    addDirectionCards(panel);

    var preview = node("article", "combined-preview");
    preview.id = "combined-preview";
    preview.appendChild(node("i", "preview-accent"));
    preview.appendChild(node("h3", "", "整体印象 · 清晰、可信、有节奏"));
    preview.appendChild(node("p", "", "标题、正文、色彩与图像表达在同一视觉系统内协同。"));
    panel.appendChild(preview);

    var selectors = node("div", "field-grid two-columns");
    selectors.appendChild(selectField("delivery_purpose", "阅读方式", state.delivery_purpose, catalogs.delivery_purpose));
    selectors.appendChild(selectField("mode", "信息结构", state.mode, catalogs.mode));
    selectors.appendChild(selectField("visual_style", "视觉风格", state.visual_style, catalogs.visual_style));
    selectors.appendChild(selectField("icons", "图标体系", state.icons, catalogs.icons));
    selectors.appendChild(selectField("information_density", "信息密度", state.information_density, catalogs.information_density));
    panel.appendChild(selectors);

    panel.appendChild(node("h3", "subheading", "色彩系统"));
    paletteControls(panel);

    panel.appendChild(node("h3", "subheading", "字体系统"));
    var typography = node("div", "field-grid three-columns");
    simpleInput(typography, "标题中文字体", state.typography.heading.cjk, function (v) { state.typography.heading.cjk = v; });
    simpleInput(typography, "标题西文字体", state.typography.heading.latin, function (v) { state.typography.heading.latin = v; });
    simpleInput(typography, "标题预览栈", state.typography.heading.css, function (v) { state.typography.heading.css = v; });
    simpleInput(typography, "正文中文字体", state.typography.body.cjk, function (v) { state.typography.body.cjk = v; });
    simpleInput(typography, "正文西文字体", state.typography.body.latin, function (v) { state.typography.body.latin = v; });
    simpleInput(typography, "正文字号 px", state.typography.body_size, function (v) { state.typography.body_size = Number(v); }, "number");
    simpleInput(typography, "页面标题 pt", state.typography.type_scale_pt.page_title, function (v) { state.typography.type_scale_pt.page_title = Number(v); }, "number");
    simpleInput(typography, "分组标题 pt", state.typography.type_scale_pt.section_title, function (v) { state.typography.type_scale_pt.section_title = Number(v); }, "number");
    simpleInput(typography, "正文 pt", state.typography.type_scale_pt.body, function (v) { state.typography.type_scale_pt.body = Number(v); }, "number");
    simpleInput(typography, "注释 pt", state.typography.type_scale_pt.caption, function (v) { state.typography.type_scale_pt.caption = Number(v); }, "number");
    panel.appendChild(typography);

    panel.appendChild(node("h3", "subheading", "风格程度与补充要求"));
    var axes = node("div", "field-grid three-columns");
    simpleInput(axes, "正式程度 0–100", state.style_axes.formal, function (v) { state.style_axes.formal = Number(v); }, "number");
    simpleInput(axes, "现代程度 0–100", state.style_axes.modern, function (v) { state.style_axes.modern = Number(v); }, "number");
    simpleInput(axes, "简约程度 0–100", state.style_axes.minimal, function (v) { state.style_axes.minimal = Number(v); }, "number");
    panel.appendChild(axes);
    panel.appendChild(textAreaField("additional_requirements", state.additional_requirements, 3));

    panel.appendChild(node("h3", "subheading", "图像渲染表达"));
    panel.appendChild(node("p", "section-copy", "只定义图像在视觉系统中的表现方式，不改变内容来源。"));
    var rendering = node("div", "field-grid three-columns");
    simpleInput(rendering, "渲染类型", state.image_rendering.rendering, function (v) { state.image_rendering.rendering = v; });
    simpleInput(rendering, "视觉语言", state.image_rendering.visual_zh || state.image_rendering.visual || "", function (v) { state.image_rendering.visual_zh = v; });
    simpleInput(rendering, "情绪气质", state.image_rendering.mood_zh || state.image_rendering.mood || "", function (v) { state.image_rendering.mood_zh = v; });
    panel.appendChild(rendering);
    updateCombinedPreview();

    panel.appendChild(actionBar("确认视觉系统", function (button, status) {
      submit(button, status, {
        stage: "stage2",
        direction: state.direction,
        delivery_purpose: state.delivery_purpose,
        mode: state.mode,
        visual_style: state.visual_style,
        color: state.color,
        icons: state.icons,
        typography: state.typography,
        image_rendering: state.image_rendering,
        style_axes: state.style_axes,
        information_density: state.information_density,
        additional_requirements: state.additional_requirements
      }, 3);
    }));
    app.appendChild(panel);
    updateCombinedPreview();
  }

  function renderStage3() {
    updateProgress(3);
    state = {
      formula_policy: (recommendation.recommend || {}).formula_policy || "mixed",
      generation_mode: (recommendation.recommend || {}).generation_mode || "continuous",
      refine_spec: Boolean(valueOf(recommendation.refine_spec)),
      image_quality: (recommendation.recommend || {}).image_quality || "high",
      max_concurrency: Number((recommendation.recommend || {}).max_concurrency || 4),
      automatic_repair_budget: Number((recommendation.recommend || {}).automatic_repair_budget == null ? 2 : (recommendation.recommend || {}).automatic_repair_budget),
      editable_output: (recommendation.recommend || {}).editable_output !== false,
      start_generation: (recommendation.recommend || {}).start_generation !== false
    };
    app.replaceChildren();
    var panel = section(
      "STAGE 03 · PRODUCTION",
      "确认生产与交付机制",
      "视觉方向已经锁定；此阶段只确认公式处理、生成节奏与是否继续细化规格。"
    );
    var fields = node("div", "field-grid two-columns");
    fields.appendChild(selectField("formula_policy", "公式处理", state.formula_policy, catalogs.formula_policy));
    fields.appendChild(selectField("generation_mode", "生成节奏", state.generation_mode, catalogs.generation_mode));
    fields.appendChild(selectField("image_quality", "图像质量", state.image_quality, catalogs.image_quality));
    simpleInput(fields, "最大并发页数", state.max_concurrency, function (v) { state.max_concurrency = Number(v); }, "number");
    simpleInput(fields, "单页自动修复预算", state.automatic_repair_budget, function (v) { state.automatic_repair_budget = Number(v); }, "number");
    var checkbox = node("label", "check-field");
    var input = node("input", "");
    input.type = "checkbox";
    input.checked = state.refine_spec;
    input.addEventListener("change", function () { state.refine_spec = input.checked; });
    checkbox.append(input, node("span", "", "在生成前进一步细化设计规格"));
    fields.appendChild(checkbox);
    var editable = node("label", "check-field");
    var editableInput = node("input", "");
    editableInput.type = "checkbox";
    editableInput.checked = state.editable_output;
    editableInput.addEventListener("change", function () { state.editable_output = editableInput.checked; });
    editable.append(editableInput, node("span", "", "输出可编辑 PowerPoint"));
    fields.appendChild(editable);
    var start = node("label", "check-field");
    var startInput = node("input", "");
    startInput.type = "checkbox";
    startInput.checked = state.start_generation;
    startInput.addEventListener("change", function () { state.start_generation = startInput.checked; });
    start.append(startInput, node("span", "", "最终确认后开始生成"));
    fields.appendChild(start);
    panel.appendChild(fields);
    panel.appendChild(actionBar("最终确认并开始制作", function (button, status) {
      submit(button, status, Object.assign({stage: "stage3"}, state), 0);
    }));
    app.appendChild(panel);
  }

  function renderComplete() {
    updateProgress(4);
    app.replaceChildren();
    var panel = section("CONFIRMED", "三阶段确认已完成", "确认结果已写入项目，可以关闭此页面。演示文稿制作将按锁定的分页和视觉系统继续。");
    panel.classList.add("complete-panel");
    app.appendChild(panel);
  }

  function submit(button, status, payload, nextStage) {
    button.disabled = true;
    status.textContent = "正在保存确认结果…";
    requestJson("/api/confirm", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    }).then(function () {
      if (!nextStage) {
        renderComplete();
        return;
      }
      status.textContent = "已保存，等待下一阶段建议…";
      waitForStage(nextStage, status);
    }).catch(function (error) {
      status.textContent = error.message;
      status.classList.add("error");
      button.disabled = false;
    });
  }

  function waitForStage(target, status) {
    window.setTimeout(function poll() {
      requestJson("/api/recommendations").then(function (data) {
        if (recommendationStage(data.stage) !== target) throw new Error("下一阶段尚未就绪");
        recommendation = data;
        state = {};
        render();
      }).catch(function () {
        status.textContent = "已保存，等待下一阶段建议…";
        window.setTimeout(poll, 1000);
      });
    }, 500);
  }

  function render() {
    var stage = recommendationStage(recommendation.stage);
    if (stage === 1) renderStage1();
    else if (stage === 2) renderStage2();
    else if (stage === 3) renderStage3();
    else throw new Error("无法识别确认阶段");
  }

  Promise.all([
    requestJson("/api/catalogs"),
    requestJson("/api/recommendations")
  ]).then(function (values) {
    catalogs = values[0];
    recommendation = values[1];
    render();
  }).catch(function (error) {
    app.replaceChildren();
    var panel = section("SESSION ERROR", "无法打开确认会话", error.message);
    panel.classList.add("error-panel");
    app.appendChild(panel);
  });
}());

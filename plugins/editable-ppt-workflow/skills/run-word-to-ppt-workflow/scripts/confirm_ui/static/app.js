(function () {
  "use strict";

  var app = document.getElementById("app");
  var facts = document.getElementById("project-facts");
  var dialog = document.getElementById("color-dialog");
  var catalogs = {};
  var recommendation = null;
  var state = {};
  var step = 1;
  var customFields = new Set();
  var colorSession = null;
  var activeSettingGroup = "foundation";
  var activePreviewField = "color";
  var resetDisclosureOpen = false;

  var productionProfiles = [
    {id: "quality", name: "质量优先", note: "适合重要汇报和复杂页面", quality: "高", concurrency: 3, repair: 2, detail: "高质量生图 · 最多并发3页 · 每页最多自动修复2次"},
    {id: "balanced", name: "均衡", note: "质量、速度和稳定性平衡", quality: "高", concurrency: 5, repair: 1, detail: "高质量生图 · 最多并发5页 · 每页最多自动修复1次"},
    {id: "speed", name: "速度优先", note: "适合页数较多、时间紧的材料", quality: "中", concurrency: 8, repair: 1, detail: "中等质量生图 · 最多并发8页 · 每页最多自动修复1次"}
  ];
  var fieldLabels = {
    background_system: "页面背景体系", composition_tendency: "页面构图倾向", evidence_strength: "证据表达强度",
    brand_device: "品牌装置", image_role: "图像角色与占比", image_usage_policy: "图片使用策略", color: "语义配色", typography: "字体与字号",
    information_density: "信息密度", layout_preferences: "页面组织优先级", image_rendering: "图像表现方式",
    style_axes: "风格程度", regional_style: "地区特色", additional_requirements: "补充要求"
  };
  function fieldLabel(field) { return fieldLabels[field] || field; }

  var colorRoles = [
    {id: "background", label: "页面背景"},
    {id: "primary", label: "固定层页面标题", locked: true},
    {id: "section_title", label: "分组标题"},
    {id: "body_text", label: "正文文字"},
    {id: "accent", label: "主强调色"},
    {id: "key_number", label: "关键数字"},
    {id: "secondary_bg", label: "浅色分区", advanced: true},
    {id: "secondary_accent", label: "辅助强调", advanced: true},
    {id: "table_header", label: "表格表头", advanced: true},
    {id: "border", label: "边框与分隔线", advanced: true}
  ];
  var layoutOptions = [
    {id: "auto", name: "内容自动", note: "让 Image2 根据每页内容选择"},
    {id: "editorial", name: "编辑式", note: "标题、正文与证据形成清晰层级"},
    {id: "conclusion-first", name: "结论先行", note: "先给判断，再展示依据"},
    {id: "split", name: "左右分栏", note: "适合图文或观点与证据"},
    {id: "table", name: "表格对比", note: "适合多维度比较"},
    {id: "matrix", name: "矩阵结构", note: "适合明确的二维关系"},
    {id: "data-led", name: "数据主导", note: "关键数字和图表优先"},
    {id: "timeline", name: "时间轴", note: "只用于明确阶段和顺序"},
    {id: "modular", name: "轻量模块", note: "少量并列内容分区"}
  ];
  var fontChoices = [
    {id: "Microsoft YaHei", name: "微软雅黑"},
    {id: "Source Han Sans SC", name: "思源黑体"},
    {id: "Noto Sans CJK SC", name: "Noto Sans 中文"},
    {id: "SimSun", name: "宋体"},
    {id: "FangSong", name: "仿宋"}
  ];
  var latinFontChoices = [
    {id: "Arial", name: "Arial"},
    {id: "Aptos Display", name: "Aptos Display"},
    {id: "Aptos", name: "Aptos"},
    {id: "Helvetica Neue", name: "Helvetica Neue"},
    {id: "Georgia", name: "Georgia"}
  ];
  var imageOptions = [
    {id: "auto", name: "根据内容自动", note: "不预设具体图像构图"},
    {id: "none", name: "不使用额外图像", note: "排版、表格和基础图形为主"},
    {id: "data-visual", name: "数据图形优先", note: "图表、矩阵和信息图形"},
    {id: "photographic", name: "真实摄影", note: "真实场景、人物和产品证据"},
    {id: "technology", name: "科技视觉", note: "产品实拍、结构光与技术图形"},
    {id: "abstract-business", name: "抽象商务", note: "克制的几何与商务隐喻"}
  ];

  function node(tag, className, text) {
    var item = document.createElement(tag);
    if (className) item.className = className;
    if (text != null) item.textContent = text;
    return item;
  }
  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function requestJson(url, options) {
    return fetch(url, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) throw new Error(data.error || ("请求失败：" + response.status));
        return data;
      });
    });
  }
  function presets() { return catalogs.template_presets || []; }
  function selectedPreset() { return presets().find(function (item) { return item.id === state.template_selection.id; }); }
  function selectedDefaults() {
    var preset = selectedPreset();
    if (!preset) return {};
    if (preset.substyles) {
      var sub = preset.substyles.find(function (item) { return item.id === state.template_selection.substyle_id; });
      return sub ? sub.defaults : preset.substyles[0].defaults;
    }
    return preset.defaults;
  }
  function optionName(key, id) {
    var item = (catalogs[key] || []).find(function (entry) { return entry.id === id; });
    return item ? item.name : id;
  }
  function markCustom(field) {
    customFields.add(field);
    state.template_selection.override_fields = Array.from(customFields).sort();
  }
  function snapshotCustom() {
    var snapshot = {};
    customFields.forEach(function (field) { snapshot[field] = clone(state[field]); });
    return snapshot;
  }
  function applyPreset(templateId, substyleId, preserveCustom) {
    var previous = preserveCustom ? snapshotCustom() : {};
    var preset = presets().find(function (item) { return item.id === templateId; }) || presets()[0];
    var substyle = null;
    var defaults = preset.defaults;
    if (preset.substyles) {
      substyle = preset.substyles.find(function (item) { return item.id === substyleId; }) || preset.substyles[0];
      defaults = substyle.defaults;
    }
    Object.keys(defaults).forEach(function (key) { state[key] = clone(defaults[key]); });
    Object.keys(previous).forEach(function (key) { state[key] = previous[key]; });
    state.direction = Math.max(0, presets().indexOf(preset));
    state.template_selection = {
      id: preset.id,
      label: preset.name,
      version: "1.0",
      substyle_id: substyle ? substyle.id : null,
      override_fields: Array.from(customFields).sort()
    };
  }
  function initializeState() {
    var recommended = recommendation.recommend || {};
    var index = Number.isInteger(recommended.direction) ? recommended.direction : 0;
    var preset = presets()[Math.max(0, Math.min(index, presets().length - 1))] || presets()[0];
    if (!recommendation.fixed_region || recommendation.fixed_region.read_only !== true || recommendation.fixed_region.canvas !== "ppt169") {
      throw new Error("项目缺少 fixed-canvas-cm-v2 固定厘米区域合同");
    }
    state.canvas = "ppt169";
    state.regional_style = clone(recommended.regional_style || {enabled: false});
    state.additional_requirements = recommended.additional_requirements || "";
    state.production_profile = recommended.production_profile || "balanced";
    applyPreset(preset.id, preset.substyles ? preset.substyles[0].id : null, false);
    state.image_usage_policy = recommended.image_usage_policy || "content-driven";
  }

  function fixedRegionNotice() {
    var fixed = recommendation.fixed_region;
    var body = fixed.body_cm;
    var wrapper = section("固定画布与正文生成区域 · 只读", "每页采用同一16:9坐标系统。下列位置由程序精确执行，不随模板、配色或构图选择改变。");
    var board = node("div", "fixed-region-board");
    var diagram = node("div", "fixed-region-diagram");
    diagram.append(
      node("span", "fixed-region-title", "页面主标题"),
      node("span", "fixed-region-logo", "SVG LOGO"),
      node("span", "fixed-region-body", "Image2 正文自由设计区域\n像素尺寸动态"),
      node("span", "fixed-region-footer", "页脚 · 页码")
    );
    var factsList = node("dl", "fixed-region-facts");
    [
      ["整页画布", fixed.slide_cm.w + " × " + fixed.slide_cm.h + " cm"],
      ["正文位置", "左 " + body.x + " cm · 上 " + body.y + " cm"],
      ["正文尺寸", body.w + " × " + body.h + " cm"],
      ["剩余边距", "右 " + fixed.remaining_cm.right + " cm · 下 " + fixed.remaining_cm.bottom + " cm"],
      ["允许误差", fixed.tolerance_percent + "%"],
      ["固定图层", "页面主标题 · 右上角SVG Logo · 页脚 · 页码"]
    ].forEach(function (pair) { var row = node("div"); row.append(node("dt", "", pair[0]), node("dd", "", pair[1])); factsList.appendChild(row); });
    board.append(diagram, factsList);
    wrapper.append(board, node("p", "fixed-region-note", "标题字体、字号和颜色仍可在下方选择；UI中的示意预览只帮助理解选择效果，不会作为图片或视觉参考发送给 Image2。"));
    return wrapper;
  }
  function renderFacts() {
    facts.replaceChildren();
    [
      "共 " + recommendation.page_count + " 页",
      recommendation.pagination_mode === "explicit_text_markers" ? "文字标记分页" : "Word物理分页",
      "一次确认"
    ].forEach(function (label) { facts.appendChild(node("span", "fact-pill", label)); });
  }
  function stepper() {
    var bar = node("nav", "stepper");
    [
      {number: 1, title: "选择模板", note: "先确定完整视觉基线"},
      {number: 2, title: "调整细节", note: "所有模板默认均可修改"},
      {number: 3, title: "确认合同", note: "检查后一次性锁定"}
    ].forEach(function (item) {
      var block = node("div", "step-item" + (step === item.number ? " active" : "") + (step > item.number ? " complete" : ""));
      block.append(node("span", "step-number", String(item.number)), node("strong", "", item.title), node("small", "", item.note));
      bar.appendChild(block);
    });
    return bar;
  }
  function templateVisual(preset, substyle) {
    var defaults = substyle ? substyle.defaults : (preset.defaults || (preset.substyles && preset.substyles[0].defaults));
    var palette = defaults.color.palette;
    var visual = node("div", "template-visual template-specimen");
    visual.style.setProperty("--sample-bg", palette.background);
    visual.style.setProperty("--sample-title", palette.primary);
    visual.style.setProperty("--sample-accent", palette.accent);
    visual.style.setProperty("--sample-secondary", palette.secondary_bg);
    visual.append(node("i", "sample-title-line"), node("i", "sample-rule"), node("i", "sample-block one"), node("i", "sample-block two"), node("i", "sample-block three"));
    return visual;
  }
  function selectTemplate(templateId, substyleId) {
    var changed = !state.template_selection || state.template_selection.id !== templateId || state.template_selection.substyle_id !== (substyleId || null);
    if (!changed) return;
    applyPreset(templateId, substyleId, customFields.size > 0);
    renderStep();
  }
  function templateTokens(preset, substyle) {
    var defaults = substyle ? substyle.defaults : (preset.defaults || preset.substyles[0].defaults);
    var rail = node("div", "template-token-rail");
    [
      defaults.typography.heading.cjk,
      optionName("information_density", defaults.information_density),
      optionName("composition_tendency", defaults.composition_tendency)
    ].forEach(function (label) { rail.appendChild(node("span", "", label)); });
    var colors = node("span", "template-mini-palette");
    [defaults.color.palette.primary, defaults.color.palette.accent, defaults.color.palette.secondary_bg].forEach(function (color) { var swatch = node("i"); swatch.style.background = color; colors.appendChild(swatch); });
    rail.appendChild(colors);
    return rail;
  }
  function renderTemplateStep(panel) {
    var intro = node("header", "page-intro");
    intro.append(node("p", "eyebrow", "STEP 01 · TEMPLATE BASELINE"), node("h2", "", "先选择最接近使用场景的模板"), node("p", "", "模板会一次性匹配配色、字体、信息密度、页面节奏、图像角色和证据表达方式，后续仍可逐项修改。"));
    panel.appendChild(intro);
    var grid = node("div", "template-grid");
    presets().forEach(function (preset) {
      var selected = state.template_selection.id === preset.id;
      var card = node("button", "template-card" + (selected ? " selected" : ""));
      card.type = "button";
      card.append(templateVisual(preset), templateTokens(preset), node("span", "template-tag", preset.tagline), node("strong", "", preset.name), node("p", "", preset.impact), node("small", "", "适合：" + preset.best_for));
      card.addEventListener("click", function () { selectTemplate(preset.id, preset.substyles ? preset.substyles[0].id : null); });
      grid.appendChild(card);
      if (selected && preset.substyles) {
        var subgrid = node("div", "substyle-grid");
        subgrid.appendChild(node("h3", "", "选择投资 BP 的视觉基底"));
        preset.substyles.forEach(function (sub) {
          var active = state.template_selection.substyle_id === sub.id;
          var subcard = node("button", "substyle-card" + (active ? " selected" : ""));
          subcard.type = "button";
          subcard.append(templateVisual(preset, sub), templateTokens(preset, sub), node("strong", "", sub.name), node("span", "", sub.tagline), node("small", "", sub.impact));
          subcard.addEventListener("click", function () { selectTemplate(preset.id, sub.id); });
          subgrid.appendChild(subcard);
        });
        grid.appendChild(subgrid);
      }
    });
    panel.appendChild(grid);
    if (customFields.size) {
      var notice = node("div", "inline-notice", "切换模板时已保留 " + customFields.size + " 项自定义。若希望完整采用当前模板，请恢复模板默认值。 ");
      var reset = node("button", "text-button", "恢复当前模板默认值");
      reset.type = "button";
      reset.addEventListener("click", function () { customFields.clear(); applyPreset(state.template_selection.id, state.template_selection.substyle_id, false); renderStep(); });
      notice.appendChild(reset);
      panel.appendChild(notice);
    }
    panel.appendChild(navigation(false, true));
  }
  function section(title, note) {
    var wrapper = node("section", "form-section");
    var group = /配色|字体|背景|品牌/.test(title) ? "foundation" : /构图|密度|组织/.test(title) ? "organization" : /图像|证据/.test(title) ? "evidence" : "advanced";
    wrapper.dataset.group = group;
    var head = node("header", "form-section-head");
    head.append(node("h3", "", title), node("p", "", note));
    wrapper.appendChild(head);
    return wrapper;
  }
  function choiceCards(key, title, note, selected, onSelect, multi) {
    var wrapper = section(title, note);
    var grid = node("div", "option-grid");
    var options = key === "layout_preferences" ? layoutOptions : (key === "image_rendering" ? imageOptions : catalogs[key] || []);
    options.forEach(function (option) {
      var id = option.id;
      var active = multi ? selected.indexOf(id) >= 0 : selected === id;
      var button = node("button", "option-card" + (active ? " selected" : ""));
      button.type = "button";
      button.dataset.option = id;
      button.dataset.effect = key;
      button.append(node("strong", "", option.name || option.label), node("small", "", option.note || "选择后写入最终视觉合同"));
      button.addEventListener("click", function () { activePreviewField = key; onSelect(id); });
      grid.appendChild(button);
    });
    wrapper.appendChild(grid);
    return wrapper;
  }
  function colorButton(role) {
    var value = state.color.palette[role.id];
    var button = node("button", "color-role");
    button.type = "button";
    button.dataset.effect = "color";
    var label = node("span", "color-role-label");
    var swatch = node("span", "color-swatch");
    swatch.style.background = value;
    label.append(swatch, node("span", "", role.label + (role.locked ? " · 精确锁定" : "")));
    button.append(label, node("span", "color-value", value));
    button.addEventListener("click", function () { activePreviewField = "color"; openColorDialog(role); });
    return button;
  }
  function colorControls() {
    var wrapper = section("语义配色", "模板已经区分标题、正文、关键数字和分区颜色；点击任意色块可精确输入 RGB 或 HEX。 ");
    var list = node("div", "palette-list");
    colorRoles.filter(function (role) { return !role.advanced; }).forEach(function (role) { list.appendChild(colorButton(role)); });
    wrapper.appendChild(list);
    var advanced = node("details", "advanced-panel");
    advanced.appendChild(node("summary", "", "高级颜色角色"));
    var extra = node("div", "palette-list");
    colorRoles.filter(function (role) { return role.advanced; }).forEach(function (role) { extra.appendChild(colorButton(role)); });
    advanced.appendChild(extra);
    wrapper.appendChild(advanced);
    return wrapper;
  }
  function selectField(label, value, options, onChange) {
    var row = node("label", "field-row");
    row.appendChild(node("span", "", label));
    var select = node("select", "select-input");
    options.forEach(function (option) {
      var item = node("option", "", option.name || option.label);
      item.value = option.id;
      item.selected = item.value === value;
      select.appendChild(item);
    });
    select.addEventListener("change", function () { onChange(select.value); });
    row.appendChild(select);
    return row;
  }
  function numberField(label, role, min, max) {
    var row = node("label", "field-row compact");
    row.appendChild(node("span", "", label));
    var input = node("input", "number-input");
    input.type = "number"; input.min = String(min); input.max = String(max); input.value = String(state.typography.type_scale_pt[role]);
    input.addEventListener("input", function () {
      state.typography.type_scale_pt[role] = Math.max(min, Math.min(max, Number(input.value) || min));
      if (role === "body") state.typography.body_size = state.typography.type_scale_pt[role] * 2;
      markCustom("typography");
    });
    row.append(input, node("small", "unit", "pt"));
    return row;
  }
  function typographyControls() {
    var wrapper = section("字体与字号", "模板已匹配中英文字体和三级正文层级，可按项目实际情况调整。 ");
    var fields = node("div", "field-stack two-column");
    fields.append(
      selectField("中文标题", state.typography.heading.cjk, fontChoices, function (v) { state.typography.heading.cjk = v; markCustom("typography"); }),
      selectField("中文正文", state.typography.body.cjk, fontChoices, function (v) { state.typography.body.cjk = v; markCustom("typography"); }),
      selectField("英文标题", state.typography.heading.latin, latinFontChoices, function (v) { state.typography.heading.latin = v; markCustom("typography"); }),
      selectField("英文正文", state.typography.body.latin, latinFontChoices, function (v) { state.typography.body.latin = v; markCustom("typography"); }),
      numberField("页面标题", "page_title", 12, 72), numberField("分组标题", "section_title", 10, 48), numberField("正文", "body", 8, 32), numberField("注释", "caption", 8, 24)
    );
    wrapper.appendChild(fields);
    return wrapper;
  }
  function axisField(label, key, left, right) {
    var row = node("label", "range-control");
    var head = node("span", "range-head");
    head.append(node("span", "", left), node("strong", "", label + " " + state.style_axes[key] + "%"), node("span", "", right));
    var input = node("input"); input.type = "range"; input.min = "0"; input.max = "100"; input.value = String(state.style_axes[key]);
    input.addEventListener("input", function () { state.style_axes[key] = Number(input.value); head.querySelector("strong").textContent = label + " " + input.value + "%"; markCustom("style_axes"); });
    row.append(head, input);
    return row;
  }
  function applyVars(element, vars) {
    Object.keys(vars || {}).forEach(function (name) { element.style.setProperty(name, vars[name]); });
  }
  function renderSettingNav() {
    var nav = node("nav", "setting-nav");
    nav.appendChild(node("p", "setting-nav-title", "设计系统"));
    [
      {id: "foundation", index: "01", name: "基础视觉", note: "色彩、字体、背景"},
      {id: "organization", index: "02", name: "页面组织", note: "构图、密度、版式"},
      {id: "evidence", index: "03", name: "图片与证据", note: "角色、占比、证据"},
      {id: "advanced", index: "04", name: "高级设置", note: "风格程度与补充"}
    ].forEach(function (item) {
      var button = node("button", "setting-nav-item" + (activeSettingGroup === item.id ? " active" : ""));
      button.type = "button";
      button.append(node("span", "setting-index", item.index), node("strong", "", item.name), node("small", "", item.note));
      button.addEventListener("click", function () {
        activeSettingGroup = item.id;
        var target = document.querySelector('[data-group="' + item.id + '"]');
        if (target) target.scrollIntoView({behavior: "smooth", block: "start"});
        document.querySelectorAll(".setting-nav-item").forEach(function (entry) { entry.classList.toggle("active", entry === button); });
      });
      nav.appendChild(button);
    });
    return nav;
  }
  function renderContextPreview() {
    var specimen = window.VisualSystem.deriveSpecimen(state, activePreviewField);
    var card = node("section", "context-preview specimen-" + specimen.kind);
    applyVars(card, specimen.vars);
    var copy = node("div", "context-copy");
    copy.append(node("span", "context-kicker", "局部规则示意 · 不代表最终页面"), node("h3", "", specimen.caption), node("p", "", specimen.effect));
    var visual = node("div", "context-visual");
    visual.append(node("i", "context-title-line"), node("i", "context-section-line"), node("i", "context-copy-line one"), node("i", "context-copy-line two"), node("i", "context-data"));
    card.append(copy, visual);
    return card;
  }
  function renderStyleSummary() {
    var aside = node("aside", "style-summary");
    aside.append(node("p", "eyebrow", "CURRENT SYSTEM"), node("h3", "", "当前风格摘要"));
    var palette = node("div", "summary-palette");
    ["background", "primary", "accent", "body_text"].forEach(function (key) { var swatch = node("span"); swatch.style.background = state.color.palette[key]; swatch.title = state.color.palette[key]; palette.appendChild(swatch); });
    aside.appendChild(palette);
    var list = node("dl", "summary-token-list");
    [
      ["模板", state.template_selection.label],
      ["标题字体", state.typography.heading.cjk + " · " + state.typography.type_scale_pt.page_title + "pt"],
      ["正文", state.typography.body.cjk + " · " + state.typography.type_scale_pt.body + "pt"],
      ["密度", optionName("information_density", state.information_density)],
      ["构图", optionName("composition_tendency", state.composition_tendency)],
      ["图片", optionName("image_usage_policy", state.image_usage_policy)],
      ["已修改", customFields.size ? customFields.size + " 项" : "使用模板默认"]
    ].forEach(function (pair) { var row = node("div"); row.append(node("dt", "", pair[0]), node("dd", "", pair[1])); list.appendChild(row); });
    aside.appendChild(list);
    var note = node("p", "summary-note", "标题、Logo、页脚、页码和四周留白使用每页相同的固定区域；正文风格由本次选择控制，具体构图仍由 Image2 决定。 ");
    aside.appendChild(note);
    if (customFields.size) {
      var reset = node("button", "text-button reset-trigger", "查看并恢复模板默认"); reset.type = "button";
      reset.addEventListener("click", function () { resetDisclosureOpen = !resetDisclosureOpen; renderStep(true); });
      aside.appendChild(reset);
      if (resetDisclosureOpen) {
        var disclosure = node("div", "reset-disclosure");
        disclosure.append(node("strong", "", "将恢复 " + customFields.size + " 项设置"), node("p", "", Array.from(customFields).map(fieldLabel).join("、")));
        var cancel = node("button", "secondary-button", "取消"); cancel.type = "button"; cancel.addEventListener("click", function () { resetDisclosureOpen = false; renderStep(true); });
        var confirm = node("button", "primary-button", "恢复默认"); confirm.type = "button"; confirm.addEventListener("click", function () { customFields.clear(); resetDisclosureOpen = false; applyPreset(state.template_selection.id, state.template_selection.substyle_id, false); renderStep(true); });
        disclosure.append(cancel, confirm); aside.appendChild(disclosure);
      }
    }
    return aside;
  }
  function renderDetailStep(panel) {
    var intro = node("header", "page-intro compact-intro");
    intro.append(node("p", "eyebrow", "STEP 02 · DETAIL TUNING"), node("h2", "", "在模板基础上调整细节"), node("p", "", "每组选项都说明它会怎样影响最终页面。未修改的字段继续使用模板默认值。"));
    var badge = node("div", "selected-template-bar");
    badge.append(node("span", "", "当前模板"), node("strong", "", state.template_selection.label + (state.template_selection.substyle_id ? " · " + optionSubstyleName() : "")), node("small", "", customFields.size ? (customFields.size + " 项已自定义") : "全部为模板默认"));
    panel.append(intro, badge);
    var form = node("div", "detail-grid setting-workspace");
    form.append(
      choiceCards("background_system", "页面背景体系", "决定整套页面的明暗节奏，不只是背景色。", state.background_system, function (id) { state.background_system = id; markCustom("background_system"); renderStep(true); }, false),
      choiceCards("composition_tendency", "页面构图倾向", "约束页面的组织语言，但不固定每页布局。", state.composition_tendency, function (id) { state.composition_tendency = id; markCustom("composition_tendency"); renderStep(true); }, false),
      choiceCards("evidence_strength", "证据表达强度", "决定数据、案例、实物和来源在页面中的优先级。", state.evidence_strength, function (id) { state.evidence_strength = id; markCustom("evidence_strength"); renderStep(true); }, false),
      choiceCards("brand_device", "品牌装置强度", "控制页眉、品牌线条、页脚和背景纹理的存在感。", state.brand_device, function (id) { state.brand_device = id; markCustom("brand_device"); renderStep(true); }, false)
    );
    form.append(
      choiceCards("image_usage_policy", "图片使用策略", "这是跨页的使用边界，不会转换为每页图片数量要求。", state.image_usage_policy, function (id) { state.image_usage_policy = id; markCustom("image_usage_policy"); renderStep(true); }, false),
      fixedRegionNotice(), colorControls(), typographyControls()
    );
    form.append(choiceCards("information_density", "信息密度", "影响留白、字号和每页可承载的信息量。", state.information_density, function (id) { state.information_density = id; markCustom("information_density"); renderStep(true); }, false));
    form.append(choiceCards("layout_preferences", "页面组织优先级", "可多选；已选择的顺序就是优先级。", state.layout_preferences, function (id) {
      var index = state.layout_preferences.indexOf(id);
      if (index >= 0 && state.layout_preferences.length > 1) state.layout_preferences.splice(index, 1); else if (index < 0) state.layout_preferences.push(id);
      markCustom("layout_preferences"); renderStep(true);
    }, true));
    form.append(choiceCards("image_rendering", "图像表现方式", "控制图像语言，不指定每页具体构图。", state.image_rendering.rendering, function (id) {
      var item = imageOptions.find(function (option) { return option.id === id; });
      state.image_rendering = {name_zh: item.name, rendering: item.id, visual_zh: item.note, mood_zh: "与模板整体气质协调"};
      markCustom("image_rendering"); renderStep(true);
    }, false));
    var axes = section("风格程度", "这三项是柔性偏好，为 Image2 保留页面级设计空间。 ");
    axes.append(axisField("正式度", "formal", "活跃", "正式"), axisField("现代度", "modern", "经典", "现代"), axisField("简约度", "minimal", "丰富", "极简"));
    form.appendChild(axes);
    var extra = section("可选补充", "地区特色默认关闭；自然语言只用于模板选项无法表达的要求。 ");
    var toggle = node("label", "toggle-row");
    toggle.appendChild(node("span", "", "克制表达地区特色"));
    var checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = state.regional_style.enabled;
    checkbox.addEventListener("change", function () { state.regional_style.enabled = checkbox.checked; if (!checkbox.checked) delete state.regional_style.region; markCustom("regional_style"); renderStep(true); });
    toggle.appendChild(checkbox); extra.appendChild(toggle);
    if (state.regional_style.enabled) {
      var region = node("input", "text-input"); region.placeholder = "例如：浙江、杭州"; region.value = state.regional_style.region || "";
      region.addEventListener("input", function () { state.regional_style.region = region.value.trim(); markCustom("regional_style"); });
      extra.appendChild(region);
    }
    var requirements = node("textarea", "text-input"); requirements.placeholder = "例如：减少彩色卡片，重要数据只突出一次"; requirements.value = state.additional_requirements;
    requirements.addEventListener("input", function () { state.additional_requirements = requirements.value; markCustom("additional_requirements"); });
    extra.appendChild(requirements); form.appendChild(extra);
    var consoleShell = node("div", "visual-console");
    var workspace = node("div", "setting-workspace-shell");
    workspace.append(renderContextPreview(), form);
    consoleShell.append(renderSettingNav(), workspace, renderStyleSummary());
    panel.appendChild(consoleShell);
    panel.appendChild(navigation(true, true));
  }
  function optionSubstyleName() {
    var preset = selectedPreset();
    if (!preset || !preset.substyles) return "";
    var sub = preset.substyles.find(function (item) { return item.id === state.template_selection.substyle_id; });
    return sub ? sub.name : "";
  }
  function summaryRow(label, value, custom) {
    var row = node("div", "contract-row" + (custom ? " custom" : ""));
    row.append(node("dt", "", label), node("dd", "", value), node("small", "", custom ? "已自定义" : "模板默认"));
    return row;
  }
  function specificationGroup(title, note, rows, tone) {
    var group = node("section", "specification-group " + (tone || ""));
    group.append(node("p", "specification-label", title), node("p", "specification-note", note));
    var list = node("dl", "specification-list");
    rows.forEach(function (pair) { var row = node("div"); row.append(node("dt", "", pair[0]), node("dd", "", pair[1])); list.appendChild(row); });
    group.appendChild(list); return group;
  }
  function renderProductionProfiles() {
    var sectionNode = node("section", "production-section");
    sectionNode.append(node("p", "eyebrow", "PRODUCTION & DELIVERY"), node("h3", "", "确认生产与交付机制"), node("p", "production-intro", "三档模式会直接映射为以下技术参数；所有模式均采用正文逐页独立生成、异常页单独重试和严格项目内缓存。固定框架错误只在本地重装，不消耗 Image2 额度。"));
    var grid = node("div", "production-grid");
    productionProfiles.forEach(function (profile) {
      var active = state.production_profile === profile.id;
      var card = node("button", "production-card" + (active ? " selected" : "")); card.type = "button"; card.dataset.effect = "production_profile";
      card.append(node("span", "production-choice", active ? "已选择" : "选择"), node("strong", "", profile.name), node("small", "", profile.note), node("p", "production-detail", profile.detail));
      var metrics = node("div", "production-metrics");
      [["图像质量", profile.quality], ["并发页数", String(profile.concurrency)], ["自动修复", profile.repair + "次/页"]].forEach(function (pair) { var item = node("span"); item.append(node("small", "", pair[0]), node("b", "", pair[1])); metrics.appendChild(item); });
      card.appendChild(metrics); card.addEventListener("click", function () { state.production_profile = profile.id; renderStep(true); }); grid.appendChild(card);
    });
    sectionNode.appendChild(grid); return sectionNode;
  }
  function requirementList(label, values, emptyText) {
    var block = node("div", "requirement-detail");
    block.appendChild(node("strong", "", label));
    if (!values.length) {
      block.appendChild(node("span", "requirement-empty", emptyText));
      return block;
    }
    var list = node("ul", "requirement-list");
    values.forEach(function (value) { list.appendChild(node("li", "", value)); });
    block.appendChild(list); return block;
  }
  function renderPageRequirementSummary() {
    var board = node("section", "detected-page-requirements");
    board.setAttribute("aria-label", "分页要求只读摘要");
    var header = node("header", "requirement-summary-header");
    header.append(
      node("p", "eyebrow", "READ-ONLY PAGE REQUIREMENTS"),
      node("h3", "", "已识别的分页要求与素材动作"),
      node("span", "read-only-badge", "只读")
    );
    board.appendChild(header);
    board.appendChild(node("p", "precedence-notice", recommendation.precedenceNotice));
    var pages = node("div", "page-requirement-grid");
    (recommendation.pageRequirementSummary || []).forEach(function (item) {
      var hasContent = item.directives.length || item.plannedSearches.length || item.materialActions.length || item.rejectedHardRuleOverrides.length;
      if (!hasContent) return;
      var card = node("article", "page-requirement-card");
      card.appendChild(node("h4", "", "第 " + item.page + " 页"));
      card.append(
        requirementList("已识别批注/指令", item.directives, "无"),
        requirementList("计划搜索", item.plannedSearches, "无需搜索"),
        requirementList("素材动作", item.materialActions, "无额外动作"),
        requirementList("被拒绝的硬规则覆盖", item.rejectedHardRuleOverrides, "无")
      );
      pages.appendChild(card);
    });
    if (!pages.children.length) pages.appendChild(node("p", "requirement-empty-state", "未检测到分页批注或额外素材动作。"));
    board.appendChild(pages);
    board.appendChild(node("p", "requirement-boundary", "此处仅用于核对系统已理解的分页要求，不会产生分页审批，也不能修改Word事实、标题、Logo、页脚或页码。"));
    return board;
  }
  function renderContractStep(panel) {
    var intro = node("header", "page-intro");
    intro.append(node("p", "eyebrow", "STEP 03 · CONTRACT REVIEW"), node("h2", "", "确认视觉、生产与交付合同"), node("p", "", "这是唯一一次人工确认。确认后每页独立生成，但共同使用这份视觉合同和生产机制。"));
    panel.appendChild(intro);
    var layoutNames = state.layout_preferences.map(function (id) { var item = layoutOptions.find(function (entry) { return entry.id === id; }); return item ? item.name : id; }).join("、");
    var specification = node("div", "specification-board");
    specification.append(
      specificationGroup("已锁定", "固定框架每页面积和位置完全相同；正文区域共享同一视觉合同。", [["模板", state.template_selection.label + (state.template_selection.substyle_id ? " · " + optionSubstyleName() : "")], ["页面区域", "距左0.81 cm · 距上2.3 cm · 23.78 × 11.18 cm"], ["固定区域", "统一标题、右上角Logo、页脚、页码与留白"], ["标题颜色", state.color.palette.primary], ["标题字体", state.typography.heading.cjk + " · " + state.typography.type_scale_pt.page_title + "pt"], ["正文字体", state.typography.body.cjk + " · " + state.typography.type_scale_pt.body + "pt"]], "locked"),
      specificationGroup("设计偏好", "Image2遵循这些方向，但仍可根据每页原文决定具体构图。", [["页面组织", layoutNames], ["构图倾向", optionName("composition_tendency", state.composition_tendency)], ["信息密度", optionName("information_density", state.information_density)], ["图片策略", optionName("image_usage_policy", state.image_usage_policy)]], "preference"),
      specificationGroup("您修改的内容", customFields.size ? "以下项目覆盖了模板默认值。" : "当前完整使用模板默认值。", customFields.size ? Array.from(customFields).map(function (field) { return [fieldLabel(field), "已自定义"]; }) : [["自定义项目", "无"]], "changed")
    );
    panel.append(specification, renderPageRequirementSummary(), renderProductionProfiles());
    var audit = node("details", "contract-sheet contract-audit"); audit.appendChild(node("summary", "", "展开完整合同字段"));
    var list = node("dl", "contract-list");
    list.append(summaryRow("背景体系", optionName("background_system", state.background_system), customFields.has("background_system")), summaryRow("构图倾向", optionName("composition_tendency", state.composition_tendency), customFields.has("composition_tendency")), summaryRow("图片策略", optionName("image_usage_policy", state.image_usage_policy), customFields.has("image_usage_policy")), summaryRow("品牌装置", optionName("brand_device", state.brand_device), customFields.has("brand_device")), summaryRow("页面组织", layoutNames, customFields.has("layout_preferences")), summaryRow("信息密度", optionName("information_density", state.information_density), customFields.has("information_density")), summaryRow("补充要求", state.additional_requirements.trim() || "无", customFields.has("additional_requirements")));
    audit.appendChild(list); panel.appendChild(audit);
    var boundary = node("section", "contract-boundary");
    boundary.append(node("h3", "", "合同怎样参与生成"), node("p", "", "Image2接收完整UI视觉合同、本页完整原文和一条“不生成页面主标题”的硬约束，自由完成正文构图。程序精确生成固定标题、Logo、页脚、页码和留白；确认后系统连续执行，不再逐页询问。"));
    panel.appendChild(boundary); panel.appendChild(navigation(true, false, true));
  }
  function navigation(showBack, showNext, showConfirm) {
    var nav = node("footer", "step-actions");
    if (showBack) {
      var back = node("button", "secondary-button", "上一步"); back.type = "button"; back.addEventListener("click", function () { step -= 1; renderStep(); }); nav.appendChild(back);
    }
    nav.appendChild(node("span", "action-spacer"));
    if (showNext) {
      var next = node("button", "primary-button", step === 1 ? "使用此模板并调整细节" : "查看最终合同"); next.type = "button"; next.addEventListener("click", function () { step += 1; renderStep(); }); nav.appendChild(next);
    }
    if (showConfirm) {
      var status = node("p", "action-status", "确认后将按锁定页序自动继续，不再逐页询问。 "); status.id = "action-status";
      var confirm = node("button", "primary-button", "确认风格与生产方式，开始生成"); confirm.type = "button"; confirm.addEventListener("click", function () { submitConfirmation(confirm, status); });
      nav.append(status, confirm);
    }
    return nav;
  }
  function renderStep(preserveScroll) {
    var scrollTop = preserveScroll ? window.scrollY : 0;
    app.replaceChildren();
    var shell = node("div", "wizard-shell"); shell.appendChild(stepper());
    var panel = node("main", "wizard-panel");
    if (step === 1) renderTemplateStep(panel); else if (step === 2) renderDetailStep(panel); else renderContractStep(panel);
    shell.appendChild(panel); app.appendChild(shell); window.scrollTo({top: scrollTop, behavior: preserveScroll ? "auto" : "smooth"});
  }
  function applyDialogColor(value) {
    state.color.palette[colorSession.role.id] = value;
    document.getElementById("color-new").style.background = value;
    document.getElementById("color-hex").value = value;
    document.getElementById("color-spectrum").value = value;
    var rgb = window.ColorTools.hexToRgb(value);
    document.getElementById("color-r").value = rgb.r; document.getElementById("color-g").value = rgb.g; document.getElementById("color-b").value = rgb.b;
    document.getElementById("color-error").textContent = "";
  }
  function openColorDialog(role) {
    var value = state.color.palette[role.id];
    var defaults = selectedDefaults();
    colorSession = {role: role, draft: window.ColorTools.createDraft(value), defaultValue: defaults.color.palette[role.id]};
    document.getElementById("color-dialog-title").textContent = "选择" + role.label + "颜色";
    document.getElementById("color-current").style.background = value;
    applyDialogColor(value); dialog.showModal();
  }
  function rgbInputs() { return window.ColorTools.rgbToHex({r: document.getElementById("color-r").value, g: document.getElementById("color-g").value, b: document.getElementById("color-b").value}); }
  function tryColor(action) { try { action(); } catch (error) { document.getElementById("color-error").textContent = error.message; } }
  function initializeColorDialog() {
    document.getElementById("color-spectrum").addEventListener("input", function (event) { tryColor(function () { applyDialogColor(window.ColorTools.setDraftHex(colorSession.draft, event.target.value)); }); });
    ["color-r", "color-g", "color-b"].forEach(function (id) { document.getElementById(id).addEventListener("input", function () { tryColor(function () { applyDialogColor(window.ColorTools.setDraftRgb(colorSession.draft, rgbInputs())); }); }); });
    document.getElementById("color-hex").addEventListener("change", function (event) { tryColor(function () { applyDialogColor(window.ColorTools.setDraftHex(colorSession.draft, event.target.value)); }); });
    document.getElementById("color-reset").addEventListener("click", function () { tryColor(function () { applyDialogColor(window.ColorTools.setDraftHex(colorSession.draft, colorSession.defaultValue)); }); });
    document.getElementById("color-cancel").addEventListener("click", function (event) { event.preventDefault(); applyDialogColor(window.ColorTools.cancelDraft(colorSession.draft)); dialog.close("cancel"); renderStep(true); });
    document.getElementById("color-confirm").addEventListener("click", function (event) { event.preventDefault(); window.ColorTools.commitDraft(colorSession.draft); markCustom("color"); dialog.close("default"); renderStep(true); });
    dialog.addEventListener("cancel", function (event) { event.preventDefault(); applyDialogColor(window.ColorTools.cancelDraft(colorSession.draft)); dialog.close("cancel"); renderStep(true); });
  }
  function submitConfirmation(button, status) {
    button.disabled = true; status.classList.remove("error"); status.textContent = "正在锁定视觉合同…";
    state.template_selection.override_fields = Array.from(customFields).sort();
    var payload = {
      stage: "final", direction: state.direction, template_selection: state.template_selection, canvas: state.canvas,
      visual_style: state.visual_style, color: state.color, icons: state.icons, typography: state.typography,
      image_rendering: state.image_rendering, style_axes: state.style_axes, layout_preferences: state.layout_preferences,
      information_density: state.information_density, regional_style: state.regional_style,
      background_system: state.background_system, image_role: state.image_role, evidence_strength: state.evidence_strength,
      image_usage_policy: state.image_usage_policy,
      composition_tendency: state.composition_tendency, brand_device: state.brand_device,
      production_profile: state.production_profile,
      additional_requirements: state.additional_requirements
    };
    requestJson("/api/confirm", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)}).then(function () {
      return requestJson("/api/v5/lifecycle").catch(function () { return {enabled: false}; });
    }).then(function (lifecycle) {
      if (lifecycle.enabled) renderProgress("视觉合同已确认，项目已进入自动生产流程。");
      else {
        app.replaceChildren(); var complete = node("section", "complete-panel");
        complete.append(node("p", "eyebrow", "VISUAL CONTRACT LOCKED"), node("h2", "", "视觉合同已确认"), node("p", "", "可以关闭此页面。插件将按锁定页序自动生成、检查并重建。")); app.appendChild(complete);
      }
    }).catch(function (error) { status.textContent = error.message; status.classList.add("error"); button.disabled = false; });
  }
  var progressSource = null;
  var progressOrder = ["preparing", "finding_materials", "designing", "making_editable", "checking", "complete"];
  var progressLabels = {
    preparing: "准备内容", finding_materials: "查找真实素材", designing: "设计页面",
    making_editable: "重建为可编辑对象", checking: "检查最终演示文稿", complete: "完成"
  };
  function renderProgress(note) {
    if (progressSource) progressSource.close();
    app.replaceChildren();
    var shell = node("section", "progress-console");
    var header = node("header", "progress-header");
    header.append(node("p", "eyebrow", "PROJECT IN MOTION"), node("h2", "", "演示文稿正在生成"), node("p", "progress-note", note || "继续处理，不需要再次确认。"));
    var rail = node("ol", "progress-rail");
    progressOrder.forEach(function (stage, index) {
      var item = node("li", "progress-step"); item.dataset.stage = stage;
      item.append(node("span", "progress-index", String(index + 1).padStart(2, "0")), node("strong", "", progressLabels[stage]), node("small", "", "等待中"));
      rail.appendChild(item);
    });
    var live = node("p", "progress-live", "正在等待项目事件…"); live.setAttribute("aria-live", "polite");
    var cancel = node("button", "progress-cancel", "停止未完成任务"); cancel.type = "button";
    cancel.addEventListener("click", function () {
      cancel.disabled = true; cancel.textContent = "正在停止…";
      requestJson("/api/v5/cancel", {method: "POST"}).then(function () {
        if (progressSource) progressSource.close();
        live.textContent = "已停止未完成任务；已完成的页面和素材仍然保留。";
        cancel.textContent = "已停止";
      }).catch(function (error) { live.textContent = error.message; cancel.disabled = false; cancel.textContent = "停止未完成任务"; });
    });
    var details = node("details", "diagnostics-disclosure");
    details.append(node("summary", "", "诊断信息"), node("p", "", "此区域仅用于排查问题；正常使用无需查看。"));
    var diagnosticLog = node("pre", "diagnostics-log", "尚无技术事件"); details.appendChild(diagnosticLog);
    shell.append(header, rail, live, cancel, details); app.appendChild(shell);
    function applyEvent(event) {
      var activeIndex = progressOrder.indexOf(event.stage);
      progressOrder.forEach(function (stage, index) {
        var item = rail.querySelector('[data-stage="' + stage + '"]');
        item.classList.toggle("active", index === activeIndex && event.stage !== "complete");
        item.classList.toggle("done", index < activeIndex || event.stage === "complete");
        item.querySelector("small").textContent = index < activeIndex || event.stage === "complete" ? "已完成" : (index === activeIndex ? "进行中" : "等待中");
      });
      live.textContent = event.page_number ? ("第 " + event.page_number + " 页 · " + event.label) : event.label;
      if (event.technical) diagnosticLog.textContent = JSON.stringify(event.technical, null, 2);
      if (event.stage === "complete") {
        header.querySelector("h2").textContent = "演示文稿已完成";
        header.querySelector(".progress-note").textContent = "最终文件已通过交付检查。";
      }
    }
    progressSource = new EventSource("/api/v5/events?diagnostics=1");
    progressSource.onmessage = function (message) {
      var batch = JSON.parse(message.data);
      (batch.events || []).forEach(applyEvent);
    };
    progressSource.onerror = function () { live.textContent = "进度连接正在自动恢复，项目会继续运行。"; };
  }
  function showError(error) {
    app.replaceChildren(); var panel = node("section", "error-panel"); panel.append(node("h2", "", "无法打开视觉确认"), node("p", "", error.message || String(error))); app.appendChild(panel);
  }

  initializeColorDialog();
  requestJson("/api/v5/lifecycle").catch(function () { return {enabled: false}; }).then(function (lifecycle) {
    if (lifecycle.enabled && lifecycle.status === "confirmed") {
      renderProgress("视觉合同已经锁定，本次打开不会再次要求确认。");
      return null;
    }
    return Promise.all([requestJson("/api/catalogs"), requestJson("/api/recommendations"), requestJson("/api/pages")]).then(function (values) {
      catalogs = values[0]; recommendation = values[1];
      if (String(recommendation.stage).toLowerCase() !== "final") throw new Error("项目尚未进入视觉合同确认阶段");
      initializeState(); renderFacts(); renderStep();
    });
  }).catch(showError);
})();

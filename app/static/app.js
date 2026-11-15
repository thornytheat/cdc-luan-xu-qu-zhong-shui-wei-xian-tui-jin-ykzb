// 原生 JS：统一读取页面 data-endpoint，提交表单并渲染结果；无框架、无打包器。
(function () {
  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  // 1) 分段控件（结果页 5 段 / 旧版 .tabs 同样适用）
  qsa('[data-tab-target]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var group = btn.closest('.segmented') || btn.closest('.tabs');
      if (group) qsa('button', group).forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      var target = document.getElementById(btn.getAttribute('data-tab-target'));
      var host = btn.closest('.panel') || document;
      if (!target) return;
      qsa('.tab-panel', host).forEach(function (panel) { panel.hidden = (panel !== target); });
    });
  });

  // 2) 统一 POST JSON
  window.postJSON = async function (url, payload) {
    var res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {})
    });
    var data = null;
    try { data = await res.json(); } catch (e) { data = { detail: '响应不是 JSON' }; }
    if (!res.ok) {
      var msg = (data && (data.detail || data.message)) || ('请求失败：' + res.status);
      if (typeof msg !== 'string') msg = JSON.stringify(msg);
      throw new Error(msg);
    }
    return data;
  };

  window.showError = function (host, err) {
    if (!host) return;
    host.hidden = false;
    host.textContent = '执行失败：' + (err && err.message ? err.message : String(err));
  };

  // 3) 表格导出 CSV（只导出当前可见行，带 BOM 便于 Excel 识别 UTF-8）
  function exportTable(table, filename) {
    if (!table) return;
    var rows = qsa('tr', table).filter(function (tr) {
      return !tr.hidden && tr.offsetParent !== null;
    });
    var lines = rows.map(function (tr) {
      return qsa('th,td', tr).map(function (cell) {
        var text = (cell.innerText || cell.textContent || '').replace(/\s+/g, ' ').trim();
        return '"' + text.replace(/"/g, '""') + '"';
      }).join(',');
    });
    var blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var link = document.createElement('a');
    link.href = url;
    link.download = filename || ((table.id || 'table') + '.csv');
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  qsa('[data-export-csv]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var table = document.getElementById(btn.getAttribute('data-export-csv'));
      exportTable(table, btn.getAttribute('data-export-name'));
    });
  });

  // 4) 复核页：变更日志关键字过滤 + 命中计数（纯前端，不新增接口）
  var search = qs('#review-search');
  var logTable = qs('#changelog-table');
  if (search && logTable) {
    var stat = qs('#review-stat');
    var total = qsa('tbody tr', logTable).length;
    var applyFilter = function () {
      var needle = search.value.trim().toLowerCase();
      var shown = 0;
      qsa('tbody tr', logTable).forEach(function (tr) {
        var haystack = ((tr.getAttribute('data-search') || '') + ' ' + tr.textContent).toLowerCase();
        var hit = !needle || haystack.indexOf(needle) !== -1;
        tr.hidden = !hit;
        if (hit) shown += 1;
      });
      if (stat) {
        stat.textContent = needle
          ? '命中 ' + shown + ' / ' + total + ' 条'
          : '本页 ' + total + ' 条';
      }
    };
    search.addEventListener('input', applyFilter);
    search.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { search.value = ''; applyFilter(); }
    });
    applyFilter();
  }

  // --- 工作台：执行合并 + 进度 + 即时摘要 --------------------------------
  var form = qs('#merge-form');
  if (!form) return;

  var btn = qs('#run-btn');
  var errBox = qs('#form-error');
  var steps = qsa('#progress-steps li');
  var note = qs('#progress-note');
  var summaryCard = qs('#summary-card');
  var metricsBox = qs('#summary-metrics');
  var gateBox = qs('#summary-gate');
  var resultLink = qs('#result-link');
  var snapshotLink = qs('#snapshot-link');
  var timer = null;

  function resetSteps() {
    steps.forEach(function (li) { li.classList.remove('done', 'active', 'failed'); });
  }

  function startProgress() {
    resetSteps();
    var index = 0;
    steps[0].classList.add('active');
    note.textContent = '正在执行：' + steps[0].textContent.trim();
    timer = setInterval(function () {
      if (index < steps.length - 1) {
        steps[index].classList.remove('active');
        steps[index].classList.add('done');
        index += 1;
        steps[index].classList.add('active');
        note.textContent = '正在执行：' + steps[index].textContent.trim();
      }
    }, 180);
  }

  function finishProgress(ok, message) {
    if (timer) { clearInterval(timer); timer = null; }
    steps.forEach(function (li) {
      li.classList.remove('active');
      li.classList.add(ok ? 'done' : 'failed');
    });
    note.textContent = message;
  }

  function kpi(value, label, note) {
    var box = document.createElement('div');
    box.className = 'kpi';
    var l = document.createElement('div');
    l.className = 'k-label';
    l.textContent = label;
    var v = document.createElement('div');
    v.className = 'k-value';
    if (String(value).length > 8) {
      v.style.fontSize = '13px';
      v.style.fontFamily = 'var(--font-mono)';
      v.style.letterSpacing = '0';
    }
    v.textContent = value;
    var n = document.createElement('div');
    n.className = 'k-note';
    n.textContent = note || '';
    box.appendChild(l);
    box.appendChild(v);
    if (note) box.appendChild(n);
    return box;
  }

  var VERDICT_ZH = {
    compatible: '可放行',
    compatible_with_backfill: '放行+回填',
    blocked: '阻断'
  };

  function renderSummary(data) {
    var s = data.summary;
    metricsBox.innerHTML = '';
    metricsBox.appendChild(kpi(s.alive_rows, '合并后最终行数', '存活键'));
    metricsBox.appendChild(kpi(s.conflicts, '同版本冲突', '需人工裁决'));
    metricsBox.appendChild(kpi(s.late, '迟到事件', '含墓碑后忽略 ' + s.ignored_after_tombstone));
    metricsBox.appendChild(kpi(s.watermark, '全局水位线（UTC）', 'min(每键最大 ts)'));
    metricsBox.appendChild(kpi(s.open_keys, '未闭合键', '水位线之上'));
    metricsBox.appendChild(kpi(s.tombstones, '删除墓碑', '保留不清理'));
    metricsBox.appendChild(kpi(s.ignored_after_tombstone, '墓碑后忽略', '低版本不再改状态'));
    metricsBox.appendChild(kpi(s.deduped_silent, '同内容静默去重', '不计冲突'));
    metricsBox.appendChild(kpi(VERDICT_ZH[s.gate_verdict] || s.gate_verdict, '门禁总判定', '逐条理由见结果页'));

    var counts = s.gate_counts || {};
    gateBox.textContent =
      '门禁：可放行 ' + (counts.compatible || 0) + ' 条 · 放行+回填 ' +
      (counts.compatible_with_backfill || 0) + ' 条 · 阻断 ' + (counts.blocked || 0) +
      ' 条。已合并表：' + data.tables.join(' / ') + '（目标 schema v' + data.schema_version + '）。';
    resultLink.href = data.result_url;
    snapshotLink.href = data.snapshot_url;
    summaryCard.hidden = false;
    summaryCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  form.addEventListener('submit', async function (event) {
    event.preventDefault();
    errBox.hidden = true;
    var tables = qsa('input[name="tables"]:checked', form).map(function (input) { return input.value; });
    var versionInput = qs('input[name="schema_version"]:checked', form);
    if (!tables.length) {
      window.showError(errBox, new Error('请至少选择一张表'));
      return;
    }
    if (!versionInput) {
      window.showError(errBox, new Error('请选择目标 schema 版本'));
      return;
    }
    btn.disabled = true;
    var label = btn.textContent;
    btn.textContent = '执行中…';
    summaryCard.hidden = true;
    startProgress();
    var startedAt = Date.now();
    try {
      var data = await window.postJSON('/api/merge', {
        tables: tables,
        schema_version: Number(versionInput.value)
      });
      var cost = ((Date.now() - startedAt) / 1000).toFixed(2);
      finishProgress(true, '完成：run_id ' + data.run_id + '，用时 ' + cost + ' 秒。');
      renderSummary(data);
    } catch (err) {
      finishProgress(false, '执行失败，请看左面板错误提示后重试。');
      window.showError(errBox, err);
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  });
})();

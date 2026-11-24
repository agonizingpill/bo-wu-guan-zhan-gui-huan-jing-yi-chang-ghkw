/* 湿度短时越限累计与馆长复核面板，挂载在异常事件页面。 */
const humidityReview = (() => {
  const token = () => localStorage.getItem('museum_token');
  const endpoint = '/api/humidity-reviews';
  const STATUS_LABEL = { pending: '待送审', submitted: '待馆长复核', settled: '已结清' };
  const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
  const fmt = (value) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';

  async function request(path, options = {}) {
    const response = await fetch(endpoint + path, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...(token() ? { Authorization: `Bearer ${token()}` } : {}) },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || '湿度复核数据读取失败');
    return data;
  }

  const minutes = (seconds) => `${Math.round(Number(seconds) / 60)} 分钟`;

  function caseCard(row) {
    const canSettle = window.__museumUser?.role === 'supervisor';
    return `<article class="humidity-card ${row.due ? 'due' : ''}">
      <div class="humidity-card-head">
        <div><span class="case-code">${esc(row.case_code)}</span><b>${esc(row.artifact_title)}</b>
          <small>RH 窗口 ${row.target_rh_min}% — ${row.target_rh_max}%</small></div>
        ${row.due ? '<span class="humidity-badge">累计已满 1 小时 · 等待送审入账</span>' : ''}
      </div>
      <div class="humidity-metrics">
        <div><small>待送审累计</small><b>${minutes(row.pending_seconds)}</b><span>${row.pending_count} 段</span></div>
        <div><small>送审中</small><b>${minutes(row.submitted_seconds)}</b><span>${row.submitted_count} 单时段</span></div>
        <div><small>已结清</small><b>${minutes(row.settled_seconds)}</b><span>${row.settled_count} 段</span></div>
      </div>
      <div class="humidity-card-actions">
        <button type="button" class="text-button" data-humidity-detail="${row.case_id}">查看时段明细 →</button>
        ${canSettle ? '<button type="button" class="text-button" data-humidity-submissions="' + row.case_id + '">馆长复核送审单 →</button>' : ''}
      </div>
    </article>`;
  }

  function segmentRows(segments) {
    if (!segments.length) return '<p class="humidity-empty">该展柜暂未识别到符合口径的湿度短时越限时段。</p>';
    return `<div class="humidity-table"><table>
      <thead><tr><th>时段起点</th><th>时段终点</th><th>时长</th><th>湿度 %</th><th>状态</th><th>送审单</th></tr></thead>
      <tbody>${segments.map((g) => `<tr>
        <td>${fmt(g.start_sample_at)}</td><td>${fmt(g.end_sample_at)}</td>
        <td>${minutes(g.duration_seconds)}</td>
        <td>${g.start_humidity} → ${g.end_humidity}</td>
        <td><span class="humidity-status ${g.status}">${STATUS_LABEL[g.status] || g.status}</span></td>
        <td>${g.review_submission_id ? `#${g.review_submission_id}（${STATUS_LABEL[g.submission_status] || g.submission_status}）` : '—'}</td>
      </tr>`).join('')}</tbody></table></div>`;
  }

  function submissionRows(rows, canSettle) {
    if (!rows.length) return '<p class="humidity-empty">当前没有馆长复核送审单。</p>';
    return `<div class="humidity-table"><table>
      <thead><tr><th>送审单</th><th>展柜</th><th>送审时间</th><th>列明时段</th><th>累计时长</th><th>状态</th><th>结清</th></tr></thead>
      <tbody>${rows.map((r) => `<tr>
        <td>#${r.id}</td><td>${esc(r.case_code)}</td><td>${fmt(r.submitted_at)}</td>
        <td>${r.segment_count} 段</td><td>${minutes(r.total_duration_seconds)}</td>
        <td><span class="humidity-status ${r.status}">${STATUS_LABEL[r.status] || r.status}</span>
          ${r.review_note ? `<small class="humidity-note">${esc(r.review_note)}</small>` : ''}</td>
        <td>${r.status === 'submitted'
          ? (canSettle ? `<form data-humidity-settle="${r.id}"><input name="note" required minlength="3" maxlength="500" placeholder="馆长复核说明（≥3 字）"><button type="submit">整单结清</button></form>` : '待主管结清')
          : `${esc(r.settled_by_name || '—')} · ${fmt(r.settled_at)}`}</td>
      </tr>`).join('')}</tbody></table></div>`;
  }

  async function render(container) {
    const role = window.__museumUser?.role;
    container.innerHTML = '<div class="panel humidity-panel"><div class="panel-head"><div><p class="eyebrow">HUMIDITY BREACH / 馆长复核</p><h2>湿度短时越限累计</h2></div><span class="filter-pill" id="humidity-summary">加载中…</span></div><div id="humidity-body" class="humidity-body"></div></div>';
    const body = container.querySelector('#humidity-body');
    const summary = container.querySelector('#humidity-summary');
    try {
      const [overview, submissions] = await Promise.all([
        request('/overview'),
        request('/submissions'),
      ]);
      const totalPending = overview.reduce((sum, row) => sum + Number(row.pending_seconds), 0);
      const totalSettled = overview.reduce((sum, row) => sum + Number(row.settled_seconds), 0);
      summary.textContent = `待送审 ${minutes(totalPending)} · 已结清 ${minutes(totalSettled)}`;
      const withData = overview.filter((row) => row.pending_count + row.submitted_count + row.settled_count > 0);
      const cards = (withData.length ? withData : overview).map(caseCard).join('');
      body.innerHTML = `
        <p class="humidity-rule">口径：同柜相邻有效采样间隔 ≤30 分钟，前一条判离群且湿度超出该柜 RH 窗口，即把两条采样之间的时长计入；未送审时段累计满 1 小时自动送馆长复核，重跑分析不重复计时。</p>
        <div class="humidity-cards">${cards}</div>
        <section class="humidity-section"><h3>馆长复核送审单</h3>${submissionRows(submissions, role === 'supervisor')}</section>
        <section class="humidity-section" id="humidity-detail-section" hidden><h3 id="humidity-detail-title">时段明细</h3><div id="humidity-detail-body"></div></section>`;
      bind(body);
    } catch (error) {
      body.innerHTML = `<p class="humidity-empty">${esc(error.message)}</p>`;
    }
  }

  function bind(scope) {
    scope.querySelectorAll('[data-humidity-detail]').forEach((button) => {
      button.addEventListener('click', async () => {
        const caseId = button.dataset.humidityDetail;
        const section = scope.querySelector('#humidity-detail-section');
        const title = scope.querySelector('#humidity-detail-title');
        const detailBody = scope.querySelector('#humidity-detail-body');
        section.hidden = false;
        title.textContent = `时段明细 · 展柜 #${caseId}`;
        detailBody.innerHTML = '<p class="humidity-empty">正在读取时段…</p>';
        detailBody.innerHTML = segmentRows(await request(`/cases/${caseId}/segments`));
        if (typeof section.scrollIntoView === 'function') {
          section.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
      });
    });
    scope.querySelectorAll('[data-humidity-submissions]').forEach((button) => {
      button.addEventListener('click', () => {
        const section = scope.querySelector('.humidity-section');
        if (section && typeof section.scrollIntoView === 'function') {
          section.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
      });
    });
    scope.querySelectorAll('form[data-humidity-settle]').forEach((form) => {
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const id = form.dataset.humiditySettle;
        const note = new FormData(form).get('note').toString().trim();
        if (note.length < 3) return;
        const button = form.querySelector('button');
        button.disabled = true;
        try {
          await request(`/submissions/${id}/settle`, { method: 'POST', body: JSON.stringify({ note }) });
          window.dispatchEvent(new CustomEvent('humidity-review:changed'));
        } catch (error) {
          alert(error.message);
          button.disabled = false;
        }
      });
    });
  }

  function mount(container) {
    if (!token() || !container) return;
    render(container);
  }

  const api = { mount };
  window.humidityReview = api;
  return api;
})();

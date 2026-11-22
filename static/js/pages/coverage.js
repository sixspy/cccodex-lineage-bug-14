import { api } from '../api.js';

export async function renderCoverage(view, setMsg) {
  const reports = await api('/api/coverage/reports');
  const snap = await api('/api/probe_empty/snap');
  view.innerHTML = `
    <section class="card">
      <h2>覆盖对账</h2>
      <div class="row">
        <button class="primary" id="go" type="button">跑覆盖对账</button>
      </div>
      <p class="muted">探针号 ${snap.probe_no || '—'} · 样本来源 ${snap.sample_source || '—'} · 放行 ${snap.gate_allowed ? '可' : '拦'}${snap.gate_reason ? '（' + snap.gate_reason + '）' : ''}</p>
    </section>
    <section class="wall" style="margin-top:1rem">
      ${(reports.items || []).map((r) => `<article class="card">
        <h3>报告 #${r.id}</h3>
        <span class="tag">${r.probe_no ? '探针 ' + r.probe_no : '库无探针'}</span>
        <p>${r.summary}</p>
        <p class="muted">节点 ${r.node_count} · 已发布边 ${r.edge_count} · 孤儿 ${r.orphan_count} · 草稿边 ${r.uncovered_edges}</p>
        <p class="muted">样本来源 ${r.sample_source || '—'}</p>
      </article>`).join('') || '<p class="muted">暂无对账报告</p>'}
    </section>`;
  document.getElementById('go').onclick = async () => {
    try {
      const r = await api('/api/coverage/reports', { method: 'POST', body: '{}' });
      setMsg(r.report.summary);
      await renderCoverage(view, setMsg);
    } catch (e) { setMsg(e.message, true); }
  };
}

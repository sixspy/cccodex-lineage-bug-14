import { api } from '../api.js';

export async function renderSettings(view, setMsg) {
  const j = await api('/api/settings');
  view.innerHTML = `
    <section class="card">
      <h2>台参数</h2>
      <div class="wall">
        ${(j.items || []).map((s) => `<article class="card">
          <h3>${s.label}</h3>
          <p class="muted">${s.key}</p>
          <div class="row">
            <input data-k="${s.key}" value="${s.value}" />
            <button data-save="${s.key}" class="primary" type="button">保存</button>
          </div>
        </article>`).join('')}
      </div>
    </section>`;
  view.querySelectorAll('[data-save]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        const input = view.querySelector('input[data-k="' + btn.dataset.save + '"]');
        await api('/api/settings', {
          method: 'POST',
          body: JSON.stringify({ key: btn.dataset.save, value: input.value }),
        });
        setMsg('参数已保存');
        await renderSettings(view, setMsg);
      } catch (e) { setMsg(e.message, true); }
    };
  });

  try {
    const dash = await api('/api/probe_empty/dash');
    const edges = await api('/api/edges');
    const edgeOpts = (edges.items || []).map((e) =>
      `<option value="${e.id}">${e.edge_code} ${e.upstream_code}→${e.downstream_code}</option>`).join('');
    const shown = dash.probe_no || '—';
    view.innerHTML += `<section class="card" style="margin-top:1rem">
      <h3>抽样探针</h3>
      <span class="tag">${dash.chip}</span>
      <p>探针号 <strong>${shown}</strong> · 状态 ${dash.status || '未落点'}</p>
      <p class="muted">样本来源 ${dash.sample_source || '—'} · 边版本 ${dash.edge_version || '—'} · 包行 ${dash.pack_count}</p>
      <p class="muted">放行闸门 ${dash.gate_allowed ? '可放行' : '已拦住'}${dash.gate_reason ? '（' + dash.gate_reason + '）' : ''}</p>
      <div class="row">
        <label>钉定边<select id="probe_edge">${edgeOpts || '<option value="">无边</option>'}</select></label>
        <button id="go_probe_empty" class="primary" type="button">钉定探针</button>
        <button id="rec_probe_empty" type="button">核对探针</button>
        <button id="note_probe_empty" type="button">记旁路</button>
      </div>
    </section>`;
    document.getElementById('go_probe_empty').onclick = async () => {
      try {
        const edgeId = Number(document.getElementById('probe_edge').value) || null;
        const r = await api('/api/probe_empty/commit', { method:'POST', body: JSON.stringify({ sample_source: '设置页抽样', edge_id: edgeId }) });
        setMsg(r.message);
        await renderSettings(view, setMsg);
      } catch (e) { setMsg(e.message, true); }
    };
    document.getElementById('rec_probe_empty').onclick = async () => {
      try {
        const r = await api('/api/probe_empty/reconcile', { method:'POST', body: JSON.stringify({ probe_no: dash.probe_no || '' }) });
        setMsg(r.message);
        await renderSettings(view, setMsg);
      } catch (e) { setMsg(e.message, true); }
    };
    document.getElementById('note_probe_empty').onclick = async () => {
      try {
        await api('/api/probe_empty/note', { method:'POST', body: JSON.stringify({ ref_id:1, note:'抽样探针空行' }) });
        setMsg('已记旁路（不改探针进度）');
        await renderSettings(view, setMsg);
      } catch (e) { setMsg(e.message, true); }
    };
  } catch (e) { /* optional surface */ }

}

import {el, api} from './common.js';

function textSection(label, text) {
    const section = el('section');
    section.append(el('strong', label));
    const value = String(text);
    const content = el('pre', value.slice(0, 8192));
    section.append(content);
    if (value.length > 8192) {
        let shown = 8192;
        const more = el('button', `继续显示（${shown} / ${value.length} 字符）`);
        more.type = 'button';
        more.addEventListener('click', () => {
            shown = Math.min(value.length, shown + 8192);
            content.textContent = value.slice(0, shown);
            more.textContent = `继续显示（${shown} / ${value.length} 字符）`;
            more.hidden = shown === value.length;
        });
        section.append(more);
    }
    return section;
}

export function attachToolDetails(line, event) {
    if (event.action.kind !== 'tool') {
        return;
    }
    const details = el('details', '', 'tool-details');
    details.append(el('summary', `${event.action.tool || '工具'} · 参数 / 响应`));
    const body = el('div', '', 'tool-payload');
    details.append(body);
    line.append(details);
    let loading = false;
    let loaded = false;
    details.addEventListener('toggle', async () => {
        if (!details.open || loaded || loading) {
            return;
        }
        loading = true;
        body.textContent = '正在读取已保存的工具记录…';
        try {
            let payload = event.action.payload;
            if (!payload && /^[a-f0-9]{32}$/.test(event.job_id) && Number.isInteger(event.seq)) {
                payload = await api(`/api/jobs/${event.job_id}/events/${event.seq}/tool`);
            }
            if (!payload) {
                throw new Error('此条日志未保存工具详情');
            }
            body.replaceChildren();
            const statuses = {pending:'等待中',running:'执行中',completed:'已完成',error:'失败'};
            body.append(el('p', `工具：${payload.tool} · 调用 ID：${payload.callId || '—'} · 状态：${statuses[payload.status] || payload.status}`));
            body.append(textSection('调用参数', JSON.stringify(payload.parameters, null, 4)));
            body.append(textSection('响应内容', payload.response == null ? '尚未返回响应' : payload.response));
            if (payload.error) {
                body.append(textSection('错误内容', payload.error));
            }
            if (payload.truncated) {
                body.append(el('p', '原始内容超过保存上限，此处仅保留前 500000 字符。'));
            }
            loaded = true;
        } catch (error) {
            body.textContent = error.message;
        } finally {
            loading = false;
        }
    });
}

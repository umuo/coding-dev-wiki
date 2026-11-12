import {$, el} from './common.js';

const labels = {queued: '等待中', running: '执行中', completed: '已完成', failed: '失败', waiting: '等待重试', connected: '已连接', reconnecting: '重连中', interrupted: '已中断'};
const rows = new Map();
let sequence = 0;
let terminal = false;

export function resetActions() {
    rows.clear();
    sequence = 0;
    terminal = false;
    $('#runtimeActions').hidden = true;
    $('#actionList').replaceChildren();
    $('#currentAction').textContent = '正在连接实时动作流…';
}

export function renderAction(event) {
    const action = event.action;
    if (!action || !action.id) {
        return;
    }
    $('#runtimeActions').hidden = false;
    let row = rows.get(action.id);
    if (!row) {
        const node = el('li', '', 'runtime-action');
        const heading = el('div', '', 'runtime-action-heading');
        const title = el('strong');
        const status = el('span', '', 'action-status');
        const detail = el('small');
        heading.append(title, status);
        node.append(heading, detail);
        row = {node, title, status, detail};
        rows.set(action.id, row);
        $('#actionList').prepend(node);
    }
    row.value = {...action};
    row.sequence = ++sequence;
    row.title.textContent = action.title;
    row.detail.textContent = action.detail || '';
    row.status.textContent = labels[action.status] || action.status;
    row.node.dataset.status = action.status;
    row.node.dataset.kind = action.kind;
    if (action.durationMs != null && action.status === 'completed') {
        row.status.textContent += ` · ${(action.durationMs / 1000).toFixed(1)}s`;
    }
    while (rows.size > 100) {
        const oldest = rows.entries().next().value;
        oldest[1].node.remove();
        rows.delete(oldest[0]);
    }
    if (terminal) {
        settleActions();
    } else {
        const active = [...rows.values()].filter(item => ['queued', 'running', 'waiting'].includes(item.value.status)).sort((a, b) => b.sequence - a.sequence)[0];
        const current = action.status === 'reconnecting' ? action : (active ? active.value : action);
        $('#currentAction').textContent = `${current.title} · ${labels[current.status] || current.status}${current.detail ? ' · ' + current.detail : ''}`;
    }
}

export function settleActions() {
    terminal = true;
    for (const row of rows.values()) {
        if (['queued', 'running', 'waiting'].includes(row.value.status)) {
            row.value.status = 'interrupted';
            row.node.dataset.status = 'interrupted';
            row.status.textContent = labels.interrupted;
        }
    }
    $('#currentAction').textContent = '本轮执行已结束，动作记录已保留';
}

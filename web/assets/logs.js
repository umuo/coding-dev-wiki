import {$, el} from './common.js';

const labels = {queued: '等待中', running: '执行中', completed: '已完成', failed: '失败', waiting: '等待重试', connected: '已连接', reconnecting: '重连中'};
const seen = new Set();

export function resetLogs() {
    seen.clear();
    $('#logs').replaceChildren();
}

function append(event, text, action = null) {
    if (event.seq != null) {
        if (seen.has(event.seq)) {
            return;
        }
        seen.add(event.seq);
    }
    const logs = $('#logs');
    const follow = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 32;
    const time = event.at ? new Date(event.at).toLocaleTimeString() : new Date().toLocaleTimeString();
    const line = el('span', `${time}  ${text}\n`, 'log-line');
    if (action) {
        line.dataset.kind = action.kind;
        line.dataset.status = action.status;
    }
    logs.append(line);
    if (follow) {
        logs.scrollTop = logs.scrollHeight;
    }
}

export function renderProgress(event) {
    append(event, `[${event.stage}] ${event.message}`);
}

export function renderAction(event) {
    const action = event.action;
    if (!action || !action.id) {
        return;
    }
    const status = labels[action.status] || action.status;
    const detail = action.detail ? ` · ${action.detail}` : '';
    const duration = action.durationMs != null && action.status === 'completed' ? ` · ${(action.durationMs / 1000).toFixed(1)}s` : '';
    append(event, `[OpenCode · ${status}] ${action.title}${detail}${duration}`, action);
}

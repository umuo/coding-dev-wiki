import {$, el, api, jsonPost} from './common.js';
import {resetActions, renderAction, settleActions} from './actions.js';
let kind = 'git';
let activeJob = null;
let stream = null;
const statusLabels = {queued: '排队中', running: '正在运行', stopping: '停止中', stopped: '已停止', completed: '已完成', failed: '失败'};
let currentStatus = 'queued';
let refreshTimer = null;
let loadingJobs = false;

async function loadJobs() {
    if (loadingJobs) {
        return;
    }
    loadingJobs = true;
    try {
        const jobs = await api('/api/jobs');
        if (!refreshTimer) {
            refreshTimer = setInterval(() => {
                if (!document.hidden) {
                    loadJobs();
                }
            }, 3000);
        }
        const list = $('#jobList');
        list.replaceChildren();
        if (!jobs.length) {
            list.append(el('p', '还没有项目，从上方导入你的第一个仓库。', 'muted'));
        }
        for (const job of jobs) {
            const row = el('a', '', 'job-row');
            row.href = job.status === 'completed' ? `/projects/${job.id}` : `/jobs/${job.id}`;
            const info = el('div');
            info.append(el('strong', job.name), el('small', `${job.mode === 'agent' ? 'Agent 分析' : '结构预览'} · ${new Date(job.created).toLocaleString()} · ${job.stage} · ${job.progress}%`));
            row.append(el('span', '◈', 'job-icon'), info, el('span', statusLabels[job.status], `status ${job.status}`), el('span', '↗'));
            list.append(row);
        }
    } catch (error) {
        $('#jobList').replaceChildren(el('p', error.message, 'error'));
    } finally {
        loadingJobs = false;
    }
}
function showStatus(job) {
    currentStatus = job.status;
    $('#jobStatus').textContent = statusLabels[job.status] || job.status;
    $('#jobStatus').className = `status ${job.status}`;
    const active = ['queued', 'running', 'stopping'].includes(job.status);
    $('#stopAnalysis').hidden = !active;
    $('#stopAnalysis').disabled = job.status === 'stopping';
    $('#stopAnalysis').textContent = job.status === 'stopping' ? '正在停止…' : '停止分析';
    $('#retry').hidden = !['completed', 'failed', 'stopped'].includes(job.status);
    $('#continueAnalysis').hidden = !['failed', 'stopped'].includes(job.status) || job.mode !== 'agent';
    $('#continueHint').hidden = $('#continueAnalysis').hidden;
    $('#resultLink').hidden = job.status !== 'completed';
    $('#resultLink').href = `/projects/${job.id}`;
    $('#stage').textContent = job.stage;
}
function finish(job) {
    if (stream) {
        stream.close();
    }
    showStatus(job);
    settleActions();
    $('#jobError').textContent = job.error || '';
    loadJobs();
}
export async function watch(id) {
    if (stream) {
        stream.close();
    }
    activeJob = id;
    currentStatus = 'queued';
    $('#jobStatus').textContent = '正在连接';
    $('#jobStatus').className = 'status queued';
    $('#stopAnalysis').hidden = true;
    $('#stage').textContent = '正在获取任务状态…';
    $('#percentage').textContent = '—';
    $('#progressBar').style.width = '0%';
    $('#progressPanel').hidden = false;
    $('.import-panel').hidden = true;
    $('#logs').textContent = '';
    resetActions();
    $('#jobError').textContent = '';
    $('#retry').hidden = true;
    $('#continueAnalysis').hidden = true;
    $('#continueHint').hidden = true;
    $('#resultLink').hidden = true;
    history.replaceState(null, '', `/jobs/${id}`);
    try {
        const job = await api(`/api/jobs/${id}`);
        $('#jobName').textContent = job.name;
        showStatus(job);
        $('#progressBar').style.width = `${job.progress}%`;
        $('#percentage').textContent = `${job.progress}%`;
        $('#stage').textContent = job.stage;
        const source = new EventSource(`/api/jobs/${id}/events`);
        stream = source;
        source.addEventListener('state', (event) => {
            if (stream === source) {
                showStatus(JSON.parse(event.data));
            }
        });
        source.addEventListener('action', (event) => {
            if (stream === source) {
                renderAction(JSON.parse(event.data));
            }
        });
        source.addEventListener('progress', (event) => {
            if (stream !== source) {
                return;
            }
            const data = JSON.parse(event.data);
            if (currentStatus !== 'stopping' && currentStatus !== 'stopped') {
                $('#stage').textContent = data.stage;
            }
            $('#percentage').textContent = `${data.progress}%`;
            $('#progressBar').style.width = `${data.progress}%`;
            const logs = $('#logs');
            logs.textContent += `${new Date(data.at).toLocaleTimeString()}  [${data.stage}] ${data.message}\n`;
            logs.scrollTop = logs.scrollHeight;
        });
        source.addEventListener('done', (event) => {
            if (stream === source) {
                finish(JSON.parse(event.data));
            }
        });
        source.onerror = () => {
            if (stream === source) {
                $('#stage').textContent = '连接中断，正在自动重连…';
            }
        };
    } catch (error) {
        $('#jobError').textContent = error.message;
    }
}
for (const button of document.querySelectorAll('[data-kind]')) {
    button.addEventListener('click', () => {
        kind = button.dataset.kind;
        for (const tab of document.querySelectorAll('[data-kind]')) {
            tab.classList.toggle('active', tab === button);
        }
        $('#gitField').hidden = kind !== 'git';
        $('#zipField').hidden = kind !== 'zip';
    });
}
$('#importForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('#formError').textContent = '';
    $('#submit').disabled = true;
    $('#submit').textContent = kind === 'zip' ? '正在上传项目…' : '正在创建分析任务…';
    try {
        let job;
        if (kind === 'git') {
            if (!$('#gitUrl').value.trim()) {
                throw new Error('请输入 Git 仓库地址');
            }
            job = await api('/api/jobs/git', jsonPost({url: $('#gitUrl').value.trim(), mode: $('#mode').value}));
        } else {
            const file = $('#zipFile').files[0];
            if (!file) {
                throw new Error('请选择项目 ZIP');
            }
            if (file.size > 50 * 1024 * 1024) {
                throw new Error('文件不能超过 50 MiB');
            }
            const body = new FormData();
            body.append('file', file);
            body.append('mode', $('#mode').value);
            job = await api('/api/jobs/zip', {method: 'POST', body});
        }
        await watch(job.id);
    } catch (error) {
        $('#formError').textContent = error.message;
    } finally {
        $('#submit').disabled = false;
        $('#submit').textContent = '开始分析 ↗';
    }
});
$('#stopAnalysis').addEventListener('click', async () => {
    $('#stopAnalysis').disabled = true;
    try {
        const job = await api(`/api/jobs/${activeJob}/stop`, {method: 'POST'});
        showStatus(job);
        loadJobs();
    } catch (error) {
        $('#jobError').textContent = error.message;
        $('#stopAnalysis').disabled = false;
    }
});
$('#retry').addEventListener('click', async () => {
    $('#retry').disabled = true;
    try {
        const job = await api(`/api/jobs/${activeJob}/retry`, {method: 'POST'});
        await watch(job.id);
    } catch (error) {
        $('#jobError').textContent = error.message;
    } finally {
        $('#retry').disabled = false;
    }
});
$('#continueAnalysis').addEventListener('click', async () => {
    $('#continueAnalysis').disabled = true;
    try {
        const job = await api(`/api/jobs/${activeJob}/continue`, {method: 'POST'});
        await watch(job.id);
    } catch (error) {
        $('#jobError').textContent = error.message;
    } finally {
        $('#continueAnalysis').disabled = false;
    }
});
$('#refresh').addEventListener('click', loadJobs);
$('#login').addEventListener('click', async () => {
    try {
        await api('/api/login', jsonPost({token: $('#token').value}));
        location.reload();
    } catch (error) {
        $('#formError').textContent = error.message;
    }
});
async function init() {
    try {
        const health = await api('/api/health');
        $('#modelStatus').textContent = health.modelReady ? `模型 · ${health.model}` : '模型待配置';
        $('#modelStatus').title = `上下文窗口 ${health.contextTokens || 128000} token；单次输出自动分配 ${health.maxOutputTokens || 8192} token`;
        if (!health.modelReady) {
            $('#mode').value = 'preview';
        }
        $('#authBox').hidden = !health.authRequired;
        await loadJobs();
        const id = location.pathname.split('/')[2];
        if (id) {
            await watch(id);
        }
    } catch (error) {
        $('#formError').textContent = error.message;
    }
}
init();

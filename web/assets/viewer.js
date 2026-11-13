import {$, el, api, evidenceButton} from './common.js';
import {codeBox, fileTree, setupSearch} from './source.js';
import {architecture} from './architecture.js';
import {setupChat} from './chat.js';
const identifier = location.pathname.split('/')[2];
let data;
let features;
let symbolNames;
let symbolSearch;
let leftTab = 'features';
let rightTab = 'outline';
let state = {mode: 'overview', featureId: '', path: '', start: 1, end: 1};
const navigationHistory = [];
let historyIndex = -1;
let toastTimer;
function toast(message) {
    $('#toast').textContent = message;
    $('#toast').classList.add('visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $('#toast').classList.remove('visible'), 2400);
}
function remember(next) {
    state = {...state, ...next};
    navigationHistory.splice(historyIndex + 1);
    navigationHistory.push({...state});
    historyIndex = navigationHistory.length - 1;
    render();
}
function openFile(path, start = 1, end = start, mode = 'full') {
    if (!data.files[path]) {
        toast('文件不在当前快照中');
        return;
    }
    remember({path, start, end: Math.min(end, data.files[path].lines), mode});
    $('.leftbar').classList.remove('open');
}
function openFeature(feature) {
    const step = feature.steps[0];
    remember({featureId: feature.id, path: step.path, start: step.line, end: step.end_line, mode: 'chain'});
    $('.leftbar').classList.remove('open');
}
function updateHash() {
    const params = new URLSearchParams({path: state.path, start: state.start, end: state.end, mode: state.mode, feature: state.featureId});
    history.replaceState(null, '', '#' + params.toString());
}
function source(file, start, end, selected = null) {
    return codeBox(file, start, end, selected, symbolNames, name => symbolSearch(name), (line) => {
        state.start = line;
        state.end = line;
        updateHash();
        navigator.clipboard.writeText(location.href).then(() => toast('已复制行链接')).catch(() => toast('请复制地址栏链接'));
    });
}
function renderNavigation() {
    const box = $('#navigation');
    box.replaceChildren();
    const q = $('#navFilter').value.trim().toLowerCase();
    if (leftTab === 'files') {
        box.append(fileTree(data.files, openFile, q));
        return;
    }
    if (leftTab === 'arch') {
        data.architecture.forEach(node => {
            if (!(node.title + node.description).toLowerCase().includes(q)) {
                return;
            }
            const button = el('button', '', 'nav-item');
            button.append(el('strong', node.title), el('div', node.description, 'nav-desc'));
            button.addEventListener('click', () => {
                remember({mode: 'overview'});
                $('.leftbar').classList.remove('open');
                $('#architectureSection').scrollIntoView({behavior: 'smooth'});
            });
            box.append(button);
        });
        return;
    }
    for (const group of data.featureGroups) {
        const matching = group.features.filter(feature => (group.title + feature.title + feature.desc).toLowerCase().includes(q));
        if (!matching.length) {
            continue;
        }
        const details = el('details', '', 'nav-group');
        details.open = true;
        details.append(el('summary', group.title));
        for (const feature of matching) {
            const button = el('button', `${feature.code}  ${feature.title}`, 'nav-item');
            button.classList.toggle('selected', feature.id === state.featureId && state.mode !== 'overview');
            button.addEventListener('click', () => openFeature(feature));
            details.append(button);
        }
        box.append(details);
    }
}
function deeperButton(question) {
    const button = el('button', '按需深入了解 ↗');
    button.addEventListener('click', () => {
        document.dispatchEvent(new CustomEvent('project-question', {detail: question}));
    });
    return button;
}
function renderOverview(container) {
    container.append(el('div', 'PROJECT OVERVIEW', 'overview-label'), el('h1', data.repoName), el('p', data.overview, 'lead'));
    if (data.mode === 'preview') {
        container.append(el('div', '结构预览 · 本次未调用 LLM，没有生成业务功能和调用链分析。', 'notice'));
    }
    if (data.analysisDepth === 'overview') {
        container.append(el('p', '快速概览 · 聚焦主要思路；源码可直接查看，更多细节通过问答按需分析。', 'notice'), deeperButton('请进一步解释这个项目最主要的业务链路，按需检索源码并附上引用。'));
    }
    container.append(el('h2', '推荐阅读路线'));
    const guide = el('ol');
    data.readingGuide.forEach(text => guide.append(el('li', text)));
    container.append(guide);
    const diagram = architecture(data, openFile);
    diagram.id = 'architectureSection';
    container.append(diagram, el('h2', data.mode === 'preview' ? '源码分组' : '功能地图'));
    for (const group of data.featureGroups) {
        const section = el('section', '', 'feature-card');
        section.append(el('h3', group.title), el('p', group.summary, 'step-note'));
        for (const feature of group.features) {
            const button = el('button', `${feature.code} · ${feature.title}`);
            button.addEventListener('click', () => openFeature(feature));
            section.append(button, document.createTextNode(' '));
        }
        container.append(section);
    }
    if (data.limitations.length) {
        container.append(el('h2', '分析边界'), el('div', data.limitations.join('\n'), 'notice'));
    }
}
function renderChain(container) {
    const feature = features.find(item => item.id === state.featureId) || features[0];
    container.append(el('div', `FEATURE ${feature.code}`, 'overview-label'), el('h1', feature.title), el('p', feature.desc, 'lead'));
    container.append(deeperButton(`请深入解释“${feature.title}”的实现、调用链和边界情况，按需检索源码并附上引用。`));
    feature.steps.forEach((step, index) => {
        const file = data.files[step.path];
        const card = el('article', '', 'step-card');
        const meta = el('div', '', 'step-meta');
        meta.append(el('span', `环节 ${index + 1} · ${step.role}`, 'step-role'), el('strong', step.symbol));
        const focus = el('button', '聚焦此函数');
        focus.addEventListener('click', () => openFile(step.path, step.line, step.end_line, 'focus'));
        meta.append(focus, evidenceButton(step, openFile));
        card.append(meta, el('p', step.note, 'step-note'));
        card.append(source(file, step.line, Math.min(step.end_line, step.line + 139)));
        if (step.end_line - step.line >= 140) {
            card.append(el('p', '此处展示前 140 行，点击文件引用查看完整上下文。', 'muted'));
        }
        container.append(card);
    });
}
function renderOutline() {
    const box = $('#outline');
    box.replaceChildren();
    const file = data.files[state.path];
    if (!file) {
        box.append(el('p', '打开功能或文件后，在这里查看符号与引用。', 'muted'));
        return;
    }
    const q = $('#outlineFilter').value.toLowerCase();
    if (rightTab === 'outline') {
        for (const symbol of file.symbols) {
            if (!(symbol.name + (symbol.doc || '')).toLowerCase().includes(q)) {
                continue;
            }
            const row = el('div', '', 'outline-row');
            const button = el('button', symbol.name);
            button.addEventListener('click', () => openFile(file.path, symbol.line, symbol.end_line, 'focus'));
            row.append(button, el('small', `${symbol.kind} · L${symbol.line}–${symbol.end_line}`), el('small', symbol.doc || ''));
            box.append(row);
        }
    } else {
        box.append(el('p', '字面同名引用，仅作导航线索，不代表已验证的调用关系。', 'notice'));
        const names = [...new Set(file.symbols.map(symbol => symbol.short))].filter(name => name.length > 3);
        let count = 0;
        for (const other of Object.values(data.files)) {
            if (other.path === file.path) {
                continue;
            }
            const name = names.find(item => other.content.includes(item) && (other.path + item).toLowerCase().includes(q));
            if (name && count < 60) {
                const line = other.content.split('\n').findIndex(text => text.includes(name)) + 1;
                const button = el('button', `${other.path}:${line} · ${name}`, 'nav-item');
                button.addEventListener('click', () => openFile(other.path, line, line));
                box.append(button);
                count++;
            }
        }
    }
}
function findInPage() {
    const query = $('#findInput').value.toLowerCase();
    let count = 0;
    for (const row of document.querySelectorAll('.code-row')) {
        const match = query && row.querySelector('.code-text').textContent.toLowerCase().includes(query);
        row.classList.toggle('match', !!match);
        if (match) {
            count++;
        }
    }
    $('#findCount').textContent = query ? `${count} 行匹配` : '';
}
function render() {
    const container = $('#content');
    container.replaceChildren();
    const file = data.files[state.path];
    $('#breadcrumb').textContent = state.mode === 'overview' ? `项目概览 · 快照 ${data.snapshot.slice(0, 12)}` : state.mode === 'chain' ? '功能 → 实现环节 → 核心片段 → 完整文件' : `${state.path} : L${state.start}–${state.end}`;
    document.querySelectorAll('[data-mode]').forEach(button => button.classList.toggle('active', button.dataset.mode === state.mode));
    if (state.mode === 'overview') {
        renderOverview(container);
    } else if (state.mode === 'chain') {
        renderChain(container);
    } else if (file) {
        container.append(el('h3', file.path));
        const start = state.mode === 'focus' ? Math.max(1, state.start - 3) : 1;
        const end = state.mode === 'focus' ? Math.min(file.lines, state.end + 3) : file.lines;
        container.append(source(file, start, end, [state.start, state.end]));
        requestAnimationFrame(() => container.querySelector(`.code-row[data-line="${state.start}"]`)?.scrollIntoView({block: 'center'}));
    }
    renderNavigation();
    renderOutline();
    updateHash();
    findInPage();
}
async function init() {
    try {
        data = await api(`/api/jobs/${identifier}/result`);
        features = data.featureGroups.flatMap(group => group.features);
        symbolNames = new Set(data.symbols.map(symbol => symbol.short));
        $('#repoName').textContent = data.repoName;
        document.title = `${data.repoName} · Coding Dev Wiki`;
        $('#snapshot').textContent = `${data.mode === 'agent' ? 'Agent' : '结构预览'} · ${data.snapshot.slice(0, 8)}`;
        $('#stats').textContent = `${features.length} 个${data.mode === 'agent' ? '功能' : '文件分组'} · ${data.fileCount} 文件 · ${data.totalLines.toLocaleString()} 行`;
        $('#jobLink').href = `/jobs/${identifier}`;
        symbolSearch = setupSearch(data, openFeature, openFile);
        setupChat(identifier, openFile);
        const params = new URLSearchParams(location.hash.slice(1));
        const path = params.get('path');
        const featureId = params.get('feature');
        if (path && data.files[path]) {
            const length = data.files[path].lines;
            state = {path, start: Math.max(1, Math.min(Number(params.get('start')) || 1, length)), end: Math.max(1, Math.min(Number(params.get('end')) || 1, length)), featureId: features.some(item => item.id === featureId) ? featureId : features[0].id, mode: ['full', 'focus', 'chain', 'overview'].includes(params.get('mode')) ? params.get('mode') : 'full'};
        }
        remember(state);
    } catch (error) {
        $('#content').replaceChildren(el('p', error.message, 'error'), el('a', '返回首页重新登录或查看任务', 'button'));
        $('#content a').href = '/';
    }
}
$('#overviewBtn').addEventListener('click', () => remember({mode: 'overview'}));
$('#menuToggle').addEventListener('click', () => $('.leftbar').classList.toggle('open'));
$('#theme').addEventListener('click', () => {
    document.body.classList.toggle('dark');
    localStorage.setItem('wikiTheme', document.body.classList.contains('dark') ? 'dark' : 'light');
});
if (localStorage.getItem('wikiTheme') === 'dark') {
    document.body.classList.add('dark');
}
for (const button of document.querySelectorAll('[data-tab]')) {
    button.addEventListener('click', () => {
        leftTab = button.dataset.tab;
        document.querySelectorAll('[data-tab]').forEach(tab => tab.classList.toggle('active', tab === button));
        renderNavigation();
    });
}
for (const button of document.querySelectorAll('[data-right]')) {
    button.addEventListener('click', () => {
        rightTab = button.dataset.right;
        document.querySelectorAll('[data-right]').forEach(tab => tab.classList.toggle('active', tab === button));
        renderOutline();
    });
}
for (const button of document.querySelectorAll('[data-mode]')) {
    button.addEventListener('click', () => {
        if (!state.path) {
            openFeature(features[0]);
        }
        remember({mode: button.dataset.mode});
    });
}
for (const [selector, direction] of [['#back', -1], ['#forward', 1]]) {
    $(selector).addEventListener('click', () => {
        const next = historyIndex + direction;
        if (next >= 0 && next < navigationHistory.length) {
            historyIndex = next;
            state = {...navigationHistory[next]};
            render();
        }
    });
}
$('#navFilter').addEventListener('input', renderNavigation);
$('#outlineFilter').addEventListener('input', renderOutline);
$('#findInput').addEventListener('input', findInPage);
let findIndex = -1;
$('#findInput').addEventListener('input', () => { findIndex = -1; });
$('#findInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
        const matches = [...document.querySelectorAll('.code-row.match')];
        if (matches.length) {
            findIndex = (findIndex + (event.shiftKey ? -1 : 1) + matches.length) % matches.length;
            matches[findIndex].scrollIntoView({block: 'center', behavior: 'smooth'});
        }
    }
});
$('#jumpBtn').addEventListener('click', () => {
    const line = Number($('#jumpInput').value);
    if (data.files[state.path] && line >= 1 && line <= data.files[state.path].lines) {
        openFile(state.path, line, line);
    } else {
        toast('请先打开文件，并输入有效行号');
    }
});
$('#jumpInput').addEventListener('keydown', event => {
    if (event.key === 'Enter') {
        $('#jumpBtn').click();
    }
});
$('#copyLink').addEventListener('click', () => navigator.clipboard.writeText(location.href).then(() => toast('链接已复制')).catch(() => toast('请复制地址栏链接')));
$('#copyCode').addEventListener('click', () => {
    const text = [...document.querySelectorAll('.code-text')].map(node => node.textContent).join('\n');
    if (!text) {
        toast('请先打开功能链路或源码');
        return;
    }
    navigator.clipboard.writeText(text).then(() => toast('代码已复制')).catch(() => toast('浏览器未允许剪贴板访问'));
});
init();

import {$, el} from './common.js';
const keywords = new Set('def async await class return import from if elif else for while try except finally with as pass break continue raise yield lambda True False None and or not in is function const let var export default new this public private protected static final void int boolean String interface enum record package'.split(' '));
export function codeBox(file, start, end, selected, symbolNames, onSymbol, onLine) {
    const box = el('div', '', 'code-box');
    const lines = file.content.split('\n');
    const fragment = document.createDocumentFragment();
    for (let line = Math.max(1, start); line <= Math.min(end, file.lines); line++) {
        const row = el('div', '', 'code-row');
        row.dataset.line = line;
        if (selected && line >= selected[0] && line <= selected[1]) {
            row.classList.add('highlight');
        }
        const number = el('span', line, 'line-no');
        number.title = '点击复制此行链接';
        number.addEventListener('click', () => onLine(line));
        const code = el('span', '', 'code-text');
        const tokens = lines[line - 1].split(/([A-Za-z_$][\w$]*)/g);
        for (const token of tokens) {
            if (symbolNames.has(token)) {
                const span = el('span', token, 'symbol-token');
                span.addEventListener('click', () => onSymbol(token));
                code.append(span);
            } else if (keywords.has(token)) {
                code.append(el('span', token, 'keyword'));
            } else {
                code.append(document.createTextNode(token));
            }
        }
        row.append(number, code);
        fragment.append(row);
    }
    box.append(fragment);
    return box;
}
export function fileTree(files, open, filter = '') {
    const root = {};
    for (const path of Object.keys(files)) {
        if (!path.toLowerCase().includes(filter)) {
            continue;
        }
        let node = root;
        const parts = path.split('/');
        parts.forEach((part, index) => {
            if (index === parts.length - 1) {
                node[part] = path;
            } else {
                node[part] ||= {};
                node = node[part];
            }
        });
    }
    function render(tree) {
        const box = el('div');
        for (const [name, child] of Object.entries(tree)) {
            if (typeof child === 'string') {
                const button = el('button', name, 'nav-item');
                button.addEventListener('click', () => open(child, 1, 1));
                box.append(button);
            } else {
                const dir = el('details', '', 'tree-dir');
                dir.open = true;
                dir.append(el('summary', '▱ ' + name), render(child));
                box.append(dir);
            }
        }
        return box;
    }
    return render(root);
}
export function setupSearch(data, featureOpen, fileOpen) {
    const input = $('#searchInput');
    const results = $('#searchResults');
    const close = () => $('#searchDialog').close();
    function row(title, detail, callback) {
        const button = el('button', '', 'search-row');
        button.append(el('strong', title), el('small', detail));
        button.addEventListener('click', () => { close(); callback(); });
        results.append(button);
    }
    function search() {
        const q = input.value.trim().toLowerCase();
        results.replaceChildren();
        if (!q) {
            results.append(el('p', '输入关键词，搜索功能、源码与符号。', 'muted'));
            return;
        }
        let count = 0;
        const mode = $('#searchMode').value;
        if ($('#searchMode').value === 'grep') {
            for (const file of Object.values(data.files)) {
                for (const [index, text] of file.content.split('\n').entries()) {
                    if (text.toLowerCase().includes(q) && count < 100) {
                        row(`${file.path}:${index + 1}`, text.slice(0, 200), () => fileOpen(file.path, index + 1, index + 1));
                        count++;
                    }
                    if (count >= 100) {
                        break;
                    }
                }
                if (count >= 100) {
                    break;
                }
            }
        } else {
            for (const feature of data.featureGroups.flatMap(group => group.features)) {
                if (['all', 'features'].includes(mode) && (feature.title + feature.desc).toLowerCase().includes(q)) {
                    row(feature.title, feature.desc, () => featureOpen(feature));
                    count++;
                }
                if (count >= 100) {
                    break;
                }
            }
            for (const file of Object.values(data.files)) {
                if (count >= 100) {
                    break;
                }
                if (['all', 'files'].includes(mode) && file.path.toLowerCase().includes(q)) {
                    row(file.path, `${file.lines} 行 · ${file.lang}`, () => fileOpen(file.path, 1, 1));
                    count++;
                }
            }
            for (const symbol of data.symbols) {
                if (count >= 100) {
                    break;
                }
                if (['all', 'symbols'].includes(mode) && symbol.name.toLowerCase().includes(q)) {
                    row(symbol.name, `${symbol.path}:${symbol.line}`, () => fileOpen(symbol.path, symbol.line, symbol.end_line));
                    count++;
                }
            }
        }
        results.append(el('p', count ? `显示 ${count} 条结果（最多 100 条）` : '没有匹配结果', 'muted'));
    }
    let timer;
    input.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(search, 150);
    });
    $('#searchMode').addEventListener('change', search);
    $('#searchOpen').addEventListener('click', () => {
        $('#searchDialog').showModal();
        input.focus();
        search();
    });
    $('#searchClose').addEventListener('click', close);
    window.addEventListener('keydown', (event) => {
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
            event.preventDefault();
            if (!$('#searchDialog').open) {
                $('#searchOpen').click();
            }
        }
    });
    return (query) => {
        input.value = query;
        $('#searchMode').value = 'all';
        $('#searchOpen').click();
    };
}

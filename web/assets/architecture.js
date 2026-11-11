import {el, evidenceButton} from './common.js';
export function architecture(data, open) {
    const container = el('section');
    container.append(el('h2', data.mode === 'preview' ? '项目目录图' : '整体架构'));
    const ns = 'http://www.w3.org/2000/svg';
    const nodes = data.architecture;
    const columns = Math.min(nodes.length, 3);
    const rows = Math.ceil(nodes.length / 3);
    const width = Math.max(1, columns) * 220;
    const height = rows * 110 + 30;
    const svg = document.createElementNS(ns, 'svg');
    svg.classList.add('arch-map');
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.style.height = `${Math.min(height, 500)}px`;
    svg.setAttribute('preserveAspectRatio', 'xMinYMid meet');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', '架构节点与有证据支持的关系图，节点可点击查看源码');
    function position(index) {
        return [20 + (index % 3) * 220, 25 + Math.floor(index / 3) * 110];
    }
    for (const edge of data.edges || []) {
        const [sx, sy] = position(edge.source);
        const [tx, ty] = position(edge.target);
        const path = document.createElementNS(ns, 'path');
        path.setAttribute('d', `M${sx + 90},${sy + 25} L${tx + 90},${ty + 25}`);
        const title = document.createElementNS(ns, 'title');
        title.textContent = `${nodes[edge.source].title} → ${nodes[edge.target].title}: ${edge.label}`;
        path.append(title);
        svg.append(path);
    }
    nodes.forEach((node, index) => {
        const [x, y] = position(index);
        const group = document.createElementNS(ns, 'g');
        group.style.cursor = 'pointer';
        group.setAttribute('tabindex', '0');
        const rect = document.createElementNS(ns, 'rect');
        for (const [key, value] of Object.entries({x, y, width: 180, height: 55, rx: 8})) {
            rect.setAttribute(key, value);
        }
        const text = document.createElementNS(ns, 'text');
        text.setAttribute('x', x + 10);
        text.setAttribute('y', y + 32);
        text.textContent = `${index + 1}. ${node.title.slice(0, 16)}`;
        const title = document.createElementNS(ns, 'title');
        title.textContent = node.title + ': ' + node.description;
        group.append(rect, text, title);
        const activate = () => {
            const ref = node.evidence[0];
            open(ref.path, ref.line, ref.end_line);
        };
        group.addEventListener('click', activate);
        group.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                activate();
            }
        });
        svg.append(group);
    });
    container.append(svg);
    const grid = el('div', '', 'arch-grid');
    nodes.forEach((node, index) => {
        const card = el('article', '', 'arch-card');
        card.append(el('div', `MODULE ${String(index + 1).padStart(2, '0')}`, 'number'), el('h3', node.title), el('p', node.description, 'step-note'));
        for (const ref of node.evidence) {
            card.append(evidenceButton(ref, open));
        }
        grid.append(card);
    });
    container.append(grid);
    for (const edge of data.edges || []) {
        const row = el('div', `${nodes[edge.source].title} → ${nodes[edge.target].title} · ${edge.label}`, 'arch-edge');
        edge.evidence.forEach(ref => row.append(evidenceButton(ref, open)));
        container.append(row);
    }
    return container;
}

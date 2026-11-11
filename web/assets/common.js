export const $ = (selector) => document.querySelector(selector);
export function el(tag, text = '', className = '') {
    const node = document.createElement(tag);
    node.textContent = text;
    node.className = className;
    return node;
}
export async function api(path, options = {}) {
    const response = await fetch(path, options);
    if (!response.ok) {
        let detail = `请求失败 (${response.status})`;
        try {
            const error = await response.json();
            detail = typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail);
        } catch { /* Preserve HTTP diagnostic. */ }
        throw new Error(detail);
    }
    return response.json();
}
export function jsonPost(body) {
    return {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
}
export function evidenceButton(ref, open) {
    const button = el('button', `${ref.path}:L${ref.line}–${ref.end_line}`, 'evidence');
    button.addEventListener('click', () => open(ref.path, ref.line, ref.end_line));
    return button;
}

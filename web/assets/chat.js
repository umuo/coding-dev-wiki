import {$, el, api, jsonPost, evidenceButton} from './common.js';
export function setupChat(identifier, open) {
    const history = [];
    document.addEventListener('project-question', (event) => {
        $('#question').value = event.detail;
        $('#chatDialog').showModal();
        $('#question').focus();
    });
    $('#chatOpen').addEventListener('click', () => $('#chatDialog').showModal());
    $('#chatClose').addEventListener('click', () => $('#chatDialog').close());
    $('#chatForm').addEventListener('submit', async (event) => {
        event.preventDefault();
        const question = $('#question').value.trim();
        if (!question) {
            return;
        }
        $('#ask').disabled = true;
        $('#messages').append(el('div', question, 'message user'));
        const answer = el('div', '正在检索当前快照的源码…', 'message');
        $('#messages').append(answer);
        $('#messages').scrollTop = $('#messages').scrollHeight;
        try {
            const result = await api(`/api/jobs/${identifier}/chat`, jsonPost({question, history: history.slice(-8)}));
            answer.textContent = result.answer;
            for (const citation of result.citations) {
                answer.append(evidenceButton(citation, (path, start, end) => {
                    $('#chatDialog').close();
                    open(path, start, end);
                }));
            }
            history.push({role: 'user', content: question}, {role: 'assistant', content: result.answer});
            $('#question').value = '';
        } catch (error) {
            answer.textContent = error.message;
            answer.classList.add('error');
        } finally {
            $('#ask').disabled = false;
            $('#messages').scrollTop = $('#messages').scrollHeight;
        }
    });
}

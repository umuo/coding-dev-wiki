import {$, api, jsonPost} from '/assets/common.js';
import {watch} from '/assets/home.js';

$('#localImportForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('#localError').textContent = '';
    $('#localSubmit').disabled = true;
    try {
        const job = await api('/api/developer/jobs/local', jsonPost({path: $('#localPath').value.trim(), mode: $('#mode').value}));
        await watch(job.id);
    } catch (error) {
        $('#localError').textContent = error.message;
    } finally {
        $('#localSubmit').disabled = false;
    }
});

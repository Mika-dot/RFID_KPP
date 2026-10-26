"""Manager-facing report dialog, appended to the existing dashboard."""

STYLE = r"""
<style>
  #efficiencyModal .modal-card { width:min(1180px,100%); }
  #efficiencyModal .modal-body { display:block; }
  #efficiencyModal .modal-head { position:sticky; top:0; z-index:4; background:white; }
  #efficiencyModal .eff-controls { display:flex; flex-wrap:wrap; gap:14px; align-items:end; margin-bottom:18px; }
  #efficiencyModal .eff-controls label { display:grid; gap:6px; font-size:13px; }
  #efficiencyModal input[type=date] { padding:10px; border:1px solid #cbd5e1; border-radius:8px; font:inherit; }
  #efficiencyModal input[type=checkbox] { width:auto; padding:0; margin-right:6px; }
  #efficiencyDownload { display:inline-flex; padding:10px 14px; border-radius:6px; text-decoration:none; font-size:14px; font-weight:700; }
  #efficiencyModal .eff-table { width:100%; min-width:680px; table-layout:fixed; border-collapse:collapse; margin:16px 0; font-size:14px; }
  #efficiencyModal .eff-table th { position:static; background:#dfe8ef; color:#16364d; text-align:left; text-transform:none; letter-spacing:normal; }
  #efficiencyModal .eff-table th, #efficiencyModal .eff-table td { border:1px solid #dfe8ef; padding:10px; }
  #efficiencyModal .eff-table td { white-space:normal; overflow-wrap:anywhere; }
  #efficiencyModal .eff-table tbody tr:last-child { font-weight:600; }
  #efficiencyModal .eff-scroll { overflow-x:auto; }
  #efficiencyModal textarea { box-sizing:border-box; width:100%; min-height:280px; font-family:inherit; font-size:14px; line-height:1.6; padding:14px; border:1px solid #cbd5e1; border-radius:8px; }
  #efficiencyModal [hidden] { display:none!important; }
  #efficiencyModal button:disabled { opacity:.55; cursor:wait; }
  #efficiencyStatus { margin:12px 0; color:#475569; }
  #efficiencyResult h3 { color:#16364d; }
</style>
"""

DIALOG = r"""
<div class="modal" id="efficiencyModal" role="dialog" aria-modal="true" aria-labelledby="efficiencyTitle">
  <div class="modal-card">
    <div class="modal-head">
      <div><h3 class="modal-title" id="efficiencyTitle">Отчёт эффективности «Периметра»</h3>
      <div class="modal-sub">Периметр целиком и RFID-блок: метки 1С против сырых чтений антенны за весь выбранный период.</div></div>
      <button class="btn-light" onclick="closeEfficiencyModal()">Закрыть</button>
    </div>
    <div class="modal-body">
      <form id="efficiencyForm" class="eff-controls">
        <label>Дата от<input type="date" id="efficiencyFrom" required></label>
        <label>Дата до (включительно)<input type="date" id="efficiencyTo" required></label>
        <label><span>Подробности Excel</span><span><input type="checkbox" id="efficiencyDetails" checked> Построчная сверка и история чтений</span></label>
        <button type="submit" class="btn-primary" id="efficiencyBuild">Сформировать отчёт</button>
      </form>
      <div id="efficiencyStatus" role="status" aria-live="polite">Выберите даты и нажмите «Сформировать отчёт».</div>
      <div id="efficiencyOutput" hidden>
        <div class="eff-controls">
          <a class="btn-primary" id="efficiencyDownload" download>Скачать Excel</a>
          <button class="btn-light" id="efficiencyCopy" onclick="copyEfficiencyEmail()">Скопировать письмо</button>
        </div>
        <div id="efficiencyResult" class="eff-scroll"></div>
        <h3>Текст письма</h3>
        <p id="efficiencySubject"></p>
        <div id="efficiencyEmailPreview" class="eff-scroll"></div>
        <details><summary>Письмо обычным текстом</summary><textarea id="efficiencyEmail" readonly aria-label="Текст письма"></textarea></details>
        <details><summary>Как рассчитан отчёт</summary><p id="efficiencyMethod"></p></details>
      </div>
    </div>
  </div>
</div>
"""

SCRIPT = r"""
<script>
let efficiencyJob = null;
let efficiencyEmailHtml = '';
let efficiencyBusy = false;
let efficiencyRun = 0;
function openEfficiencyModal() {
  const from = document.getElementById('efficiencyFrom');
  const to = document.getElementById('efficiencyTo');
  if (!to.value) {
    to.value = new Intl.DateTimeFormat('sv-SE', {timeZone:'Europe/Moscow'}).format(new Date());
    const start = new Date(to.value+'T12:00:00Z');
    start.setUTCDate(start.getUTCDate()-6);
    from.value = start.toISOString().slice(0,10);
  }
  document.getElementById('efficiencyModal').classList.add('open');
  from.focus();
}
function closeEfficiencyModal() {
  document.getElementById('efficiencyModal').classList.remove('open');
}
async function efficiencyRequest(url, options) {
  const response = await fetch(url, options);
  const type = response.headers.get('content-type') || '';
  if (!type.includes('application/json')) throw new Error('Нет ответа сервера отчётов. Повторите формирование после восстановления связи.');
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Ошибка сервера ${response.status}`);
  return data;
}
document.getElementById('efficiencyForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (efficiencyBusy) return;
  const from = document.getElementById('efficiencyFrom').value;
  const to = document.getElementById('efficiencyTo').value;
  const status = document.getElementById('efficiencyStatus');
  if (!from || !to || from > to) { status.textContent='Проверьте даты: дата от должна быть не позже даты до.'; return; }
  efficiencyBusy = true;
  const run = ++efficiencyRun;
  const button = document.getElementById('efficiencyBuild');
  button.disabled = true;
  document.querySelectorAll('#efficiencyForm input').forEach(input=>input.disabled=true);
  document.getElementById('efficiencyOutput').hidden = true;
  status.textContent='Формируется отчёт: сверка склада, меток 1С, исходных чтений RFID Reader и событий КПП…';
  try {
    const started = await efficiencyRequest('/api/efficiency-report', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({date_from:from,date_to:to,include_details:document.getElementById('efficiencyDetails').checked})});
    efficiencyJob = started.job_id;
    const deadline = Date.now()+10*60*1000;
    while (run === efficiencyRun) {
      if (Date.now() > deadline) throw new Error('Формирование занимает слишком много времени. Сократите период и повторите.');
      const data = await efficiencyRequest('/api/efficiency-report/'+encodeURIComponent(efficiencyJob));
      if (data.status === 'error') throw new Error(data.error);
      if (data.status === 'ready') {
        document.getElementById('efficiencyResult').innerHTML=data.html;
        document.getElementById('efficiencySubject').textContent='Тема: '+data.email.subject;
        document.getElementById('efficiencyEmail').value=data.email.text;
        document.getElementById('efficiencyEmailPreview').innerHTML=data.email.html;
        efficiencyEmailHtml=data.email.html;
        document.getElementById('efficiencyDownload').href=data.download_url;
        document.getElementById('efficiencyMethod').textContent=data.methodology;
        document.getElementById('efficiencyOutput').hidden=false;
        status.textContent='Готово. Сформировано: '+new Date(data.generated_at).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})+' МСК. Результат доступен 15 минут.';
        break;
      }
      await new Promise(resolve => setTimeout(resolve,1000));
    }
  } catch (error) {
    status.textContent=error.message;
  } finally {
    efficiencyBusy=false;
    button.disabled=false;
    document.querySelectorAll('#efficiencyForm input').forEach(input=>input.disabled=false);
  }
});
document.getElementById('efficiencyForm').addEventListener('change',()=>{
  if (efficiencyBusy) return;
  efficiencyJob=null;
  document.getElementById('efficiencyOutput').hidden=true;
  document.getElementById('efficiencyStatus').textContent='Параметры изменены. Нажмите «Сформировать отчёт», чтобы получить новый результат.';
});
async function copyEfficiencyEmail() {
  const text=document.getElementById('efficiencyEmail').value;
  const status=document.getElementById('efficiencyStatus');
  try {
    let copied=false;
    if (window.isSecureContext && navigator.clipboard && window.ClipboardItem) {
      const rich='<html><body>'+efficiencyEmailHtml.replaceAll('class="eff-table"','style="border-collapse:collapse;width:100%"').replaceAll('<th>','<th style="border:1px solid #cbd5e1;padding:8px;background:#dfe8ef;text-align:left">').replaceAll('<td>','<td style="border:1px solid #cbd5e1;padding:8px">')+'</body></html>';
      try {
        await navigator.clipboard.write([new ClipboardItem({'text/plain':new Blob([text],{type:'text/plain'}),'text/html':new Blob([rich],{type:'text/html'})})]);
        copied=true;
      } catch (_) { /* Fall back to a selected rich letter below. */ }
    }
    if (!copied) {
      // The factory Gateway uses HTTP. The selection API supports rich-table
      // copy there without requiring a secure clipboard context.
      const target=document.getElementById('efficiencyEmailPreview');
      const selection=window.getSelection();
      const range=document.createRange(); range.selectNodeContents(target);
      selection.removeAllRanges(); selection.addRange(range);
      copied=document.execCommand('copy'); selection.removeAllRanges();
      if (!copied) throw new Error('Выделите и скопируйте письмо вручную из поля «Письмо обычным текстом».');
    }
    status.textContent='Письмо скопировано. Вставьте его в почтовую программу.';
  } catch(error) {
    status.textContent=error.message;
    const field=document.getElementById('efficiencyEmail');
    field.parentElement.open=true; field.focus(); field.select();
  }
}
document.getElementById('efficiencyModal').addEventListener('click',event=>{if(event.target.id==='efficiencyModal')closeEfficiencyModal();});
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeEfficiencyModal();});
</script>
"""


def add_efficiency_dialog(page):
    button = '<button class="btn-primary" onclick="openEfficiencyModal()">Отчёт эффективности</button>'
    page = page.replace('<div class="report-actions">', '<div class="report-actions">'+button, 1)
    page = page.replace('>Скачать отчет</button>', '>Отчёт проходов</button>', 1)
    page = page.replace('</head>', STYLE+'</head>', 1)
    return page.replace('</body>', DIALOG+SCRIPT+'</body>', 1)

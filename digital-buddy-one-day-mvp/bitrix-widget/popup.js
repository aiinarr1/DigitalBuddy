// Digital Buddy Bitrix widget sketch.
// Replace DIGITAL_BUDDY_BACKEND_URL with your public backend URL, for example ngrok.
const DIGITAL_BUDDY_BACKEND_URL = 'https://your-ngrok-url.ngrok-free.app';

async function digitalBuddyCall(path, options = {}) {
  const response = await fetch(`${DIGITAL_BUDDY_BACKEND_URL}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) throw new Error(await response.text());
  return await response.json();
}

async function digitalBuddyOnPortalReady() {
  const bitrixUserId = Number(window.BX?.message?.('USER_ID') || 1001);

  // In production OnUserLogin should call backend server-side.
  // For hackathon demo this JS call can simulate the login event.
  await digitalBuddyCall('/webhooks/bitrix/on-login', {
    method: 'POST',
    body: JSON.stringify({ bitrix_user_id: bitrixUserId }),
  });

  const popup = await digitalBuddyCall(`/api/popup/next?bitrix_user_id=${bitrixUserId}`);
  if (!popup.show) return;

  const content = `
    <div style="font-family:Arial;max-width:680px;padding:16px">
      <div style="display:flex;gap:12px;align-items:center;margin-bottom:12px">
        <img src="${popup.avatar_url}" style="width:60px;height:80px;object-fit:contain" />
        <div><b style="font-size:20px;color:#005b8f">Digital Buddy</b><br/>ИИ-ассистент онбординга КМГ</div>
      </div>
      <p><b>${popup.greeting || ''}</b></p>
      ${popup.video_url ? `<p><a href="${popup.video_url}" target="_blank">Видеообращение Председателя Правления КМГ</a></p>` : ''}
      ${popup.next_task ? `<p><b>Задача дня:</b> ${popup.next_task}</p>` : ''}
      ${popup.progress_text ? `<p><b>Прогресс:</b> ${popup.progress_text}</p>` : ''}
      ${popup.nudge ? `
        <div style="background:#fff9e8;border:1px solid #f0d186;border-radius:12px;padding:14px;margin:12px 0">
          <h3 style="margin-top:0">${popup.nudge.topic}</h3>
          <p>${popup.nudge.text}</p>
          <small>Источник ВНД: ${popup.nudge.source_document}</small>
        </div>` : ''}
      <button onclick="BX.PopupWindowManager.getPopupById('digital-buddy-popup')?.close()">Понятно</button>
      <button onclick="BXIM?.openMessenger?.(); BX.PopupWindowManager.getPopupById('digital-buddy-popup')?.close();">Задать вопрос</button>
    </div>
  `;

  // TЗ mentions BX.showCustomPopup. Bitrix installations differ, so fallback to PopupWindowManager.
  if (window.BX?.showCustomPopup) {
    window.BX.showCustomPopup('digital-buddy-popup', { content });
  } else {
    const popupWindow = window.BX.PopupWindowManager.create('digital-buddy-popup', null, {
      content,
      closeIcon: true,
      overlay: true,
      autoHide: false,
      width: 760,
    });
    popupWindow.show();
  }
}

if (window.BX?.ready) {
  BX.ready(digitalBuddyOnPortalReady);
} else {
  document.addEventListener('DOMContentLoaded', digitalBuddyOnPortalReady);
}

let selectedViewMode = 'browser';
let retainedResearchOutput = '';
let conversationHistoryRecords = [];
let activeLiveStreamConnection = null;

async function fetchTaskHistory() {
    try {
        const response = await fetch('/api/history');
        if (!response.ok) return;
        const data = await response.json();
        renderTaskHistory(data.history || []);
    } catch (err) {
        console.warn('Failed to load task history:', err);
    }
}

function renderTaskHistory(historyItems) {
    const container = document.getElementById('taskHistoryContainer');
    const list = document.getElementById('taskHistoryList');
    if (!container || !list) return;

    if (!historyItems || historyItems.length === 0) {
        container.style.display = 'none';
        list.innerHTML = '';
        return;
    }

    container.style.display = 'flex';
    list.innerHTML = '';

    historyItems.slice(0, 10).forEach((item) => {
        const row = document.createElement('div');
        row.className = 'history-item-row';
        row.title = `Click to load: "${item.task}"`;

        const badge = document.createElement('span');
        badge.className = `history-status-badge ${item.succeeded ? 'status-succeeded' : 'status-failed'}`;
        badge.textContent = item.succeeded ? '✔ Succeeded' : '✕ Failed';

        const taskSpan = document.createElement('span');
        taskSpan.className = 'history-task-text';
        // Show first few words in one line: replace any newlines with space
        taskSpan.textContent = item.task.replace(/[\r\n]+/g, ' ').trim();

        row.appendChild(badge);
        row.appendChild(taskSpan);

        row.addEventListener('click', () => {
            loadHistoryTask(item.task);
        });

        list.appendChild(row);
    });
}

function loadHistoryTask(taskText) {
    if (!taskText) return;
    const directiveInput = document.getElementById('taskDirectiveInputBox');
    const chatInput = document.getElementById('chatQueryInputBox');

    if (directiveInput) {
        directiveInput.value = taskText;
        directiveInput.focus();
        directiveInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    if (chatInput) {
        chatInput.value = taskText;
    }
}

let cachedEnvKeys = {
    openrouter: '',
    gemini: '',
    groq: '',
    openai: '',
    anthropic: '',
};

const providerPortalMap = {
    openrouter: 'https://openrouter.ai/keys',
    gemini: 'https://aistudio.google.com/app/apikey',
    groq: 'https://console.groq.com/keys',
    openai: 'https://platform.openai.com/api-keys',
    anthropic: 'https://console.anthropic.com/settings/keys',
};

async function fetchEnvKeys() {
    try {
        const response = await fetch('/api/keys');
        if (response.ok) {
            const keysData = await response.json();
            cachedEnvKeys = { ...cachedEnvKeys, ...keysData };
            syncCurrentProviderDisplay();
        }
    } catch (err) {
        console.warn('Failed to load keys from env:', err);
    }
}

function syncCurrentProviderDisplay() {
    const select = document.getElementById('apiProviderSelect');
    const input = document.getElementById('apiKeyInputField');
    const statusTag = document.getElementById('providerEnvStatusTag');
    const portalLink = document.getElementById('providerPortalLink');

    if (!select || !input) return;

    const currentProvider = select.value || 'openrouter';
    const savedKey = cachedEnvKeys[currentProvider] || '';

    // "if that api already there in .env it should set from there only in the very next movment of selecting api provider name"
    input.value = savedKey;

    if (portalLink && providerPortalMap[currentProvider]) {
        portalLink.href = providerPortalMap[currentProvider];
    }

    if (statusTag) {
        if (savedKey) {
            statusTag.className = 'env-tag-badge badge-set';
            statusTag.textContent = '✔ Set in .env';
        } else {
            statusTag.className = 'env-tag-badge badge-empty';
            statusTag.textContent = 'Not in .env';
        }
    }
}

function toggleApiModal() {
    const modal = document.getElementById('apiConfigModal');
    if (!modal) return;

    if (modal.style.display === 'none' || !modal.style.display) {
        modal.style.display = 'block';
        const feedbackBox = document.getElementById('apiFeedbackBox');
        if (feedbackBox) feedbackBox.style.display = 'none';
        fetchEnvKeys();
    } else {
        closeApiModal();
    }
}

function closeApiModal() {
    const modal = document.getElementById('apiConfigModal');
    if (modal) modal.style.display = 'none';
}

function onProviderSelectChanged() {
    // "if that api already there in .env it should set from there only in the very next movment of selecting api provider name"
    syncCurrentProviderDisplay();

    // Clear any previous feedback box when switching provider
    const feedbackBox = document.getElementById('apiFeedbackBox');
    if (feedbackBox) {
        feedbackBox.style.display = 'none';
        feedbackBox.textContent = '';
    }
}

function toggleKeyVisibility() {
    const input = document.getElementById('apiKeyInputField');
    const toggleBtn = document.getElementById('btnKeyVisibility');
    if (!input) return;

    if (input.type === 'password') {
        input.type = 'text';
        if (toggleBtn) toggleBtn.textContent = '🔒';
    } else {
        input.type = 'password';
        if (toggleBtn) toggleBtn.textContent = '👁️';
    }
}

async function testCurrentApi() {
    const select = document.getElementById('apiProviderSelect');
    const input = document.getElementById('apiKeyInputField');
    const feedbackBox = document.getElementById('apiFeedbackBox');
    const testBtn = document.getElementById('btnTestApi');

    if (!select || !input || !feedbackBox) return;

    const provider = select.value;
    const key = input.value.trim();

    if (!key) {
        feedbackBox.className = 'api-feedback-msg api-feedback-error';
        feedbackBox.textContent = 'Please enter or select an API key to test.';
        feedbackBox.style.display = 'block';
        input.focus();
        return;
    }

    testBtn.disabled = true;
    const originalText = testBtn.innerHTML;
    testBtn.innerHTML = '<span>⏳</span> Testing...';
    feedbackBox.style.display = 'none';

    try {
        const response = await fetch('/api/test-key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider, key }),
        });

        const result = await response.json();

        // "if is get correct response show in small gren text api tex success else show the real error in the frontend above the test api button below the api key enter box."
        if (result.success) {
            feedbackBox.className = 'api-feedback-msg api-feedback-success';
            feedbackBox.textContent = '✔ api test success';
            feedbackBox.style.display = 'block';
        } else {
            feedbackBox.className = 'api-feedback-msg api-feedback-error';
            feedbackBox.textContent = result.error || 'API validation failed';
            feedbackBox.style.display = 'block';
        }
    } catch (networkFault) {
        feedbackBox.className = 'api-feedback-msg api-feedback-error';
        feedbackBox.textContent = `Network error: ${networkFault.message}`;
        feedbackBox.style.display = 'block';
    } finally {
        testBtn.disabled = false;
        testBtn.innerHTML = originalText;
    }
}

async function setCurrentApi() {
    const select = document.getElementById('apiProviderSelect');
    const input = document.getElementById('apiKeyInputField');
    const feedbackBox = document.getElementById('apiFeedbackBox');
    const setBtn = document.getElementById('btnSetApi');

    if (!select || !input || !feedbackBox) return;

    const provider = select.value;
    const key = input.value.trim();

    if (!key) {
        feedbackBox.className = 'api-feedback-msg api-feedback-error';
        feedbackBox.textContent = 'API key cannot be blank.';
        feedbackBox.style.display = 'block';
        input.focus();
        return;
    }

    setBtn.disabled = true;
    const originalText = setBtn.innerHTML;
    setBtn.innerHTML = '<span>⏳</span> Saving...';

    try {
        const response = await fetch('/api/save-key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider, key }),
        });

        const result = await response.json();

        if (response.ok && result.success) {
            cachedEnvKeys[provider] = key;
            syncCurrentProviderDisplay();

            feedbackBox.className = 'api-feedback-msg api-feedback-success';
            feedbackBox.textContent = `✔ ${result.message || 'API key saved to .env and activated'}`;
            feedbackBox.style.display = 'block';
        } else {
            feedbackBox.className = 'api-feedback-msg api-feedback-error';
            feedbackBox.textContent = result.error || 'Failed to save API key to .env';
            feedbackBox.style.display = 'block';
        }
    } catch (networkFault) {
        feedbackBox.className = 'api-feedback-msg api-feedback-error';
        feedbackBox.textContent = `Network error: ${networkFault.message}`;
        feedbackBox.style.display = 'block';
    } finally {
        setBtn.disabled = false;
        setBtn.innerHTML = originalText;
    }
}

function switchApplicationView(targetView) {
    selectedViewMode = targetView;
    const tabBrowser = document.getElementById('tabBrowserController');
    const tabChat = document.getElementById('tabChatController');
    const panelBrowser = document.getElementById('viewBrowserSection');
    const panelChat = document.getElementById('viewChatSection');

    if (targetView === 'browser') {
        tabBrowser.classList.add('active');
        tabChat.classList.remove('active');
        panelBrowser.style.display = 'flex';
        panelChat.style.display = 'none';
    } else {
        tabChat.classList.add('active');
        tabBrowser.classList.remove('active');
        panelChat.style.display = 'flex';
        panelBrowser.style.display = 'none';
        document.getElementById('chatQueryInputBox').focus();
    }
}

async function storeSecretToken() {
    const inputElement = document.getElementById('secretInputField');
    const feedbackBox = document.getElementById('statusFeedbackBox');
    if (!inputElement || !feedbackBox) return;
    const secretValue = inputElement.value.trim();

    if (!secretValue) {
        feedbackBox.style.display = 'block';
        feedbackBox.style.color = '#f87171';
        feedbackBox.textContent = 'Please enter an API token first.';
        return;
    }

    try {
        const networkCallResult = await fetch('/api/save-key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: secretValue }),
        });
        const serverPacket = await networkCallResult.json();
        feedbackBox.style.display = 'block';
        if (networkCallResult.ok && serverPacket.success) {
            feedbackBox.style.color = '#34d399';
            feedbackBox.textContent = `✔ ${serverPacket.message}`;
            inputElement.value = '';
            setTimeout(() => { feedbackBox.style.display = 'none'; }, 5000);
        } else {
            feedbackBox.style.color = '#f87171';
            feedbackBox.textContent = `Error: ${serverPacket.error || 'Failed to save token'}`;
        }
    } catch (networkFault) {
        feedbackBox.style.display = 'block';
        feedbackBox.style.color = '#f87171';
        feedbackBox.textContent = `Network error: ${networkFault.message}`;
    }
}

function insertPresetTask(sampleText) {
    const textarea = document.getElementById('taskDirectiveInputBox');
    textarea.value = sampleText;
    textarea.focus();
}

function flushTerminalScreen() {
    document.getElementById('logsDisplaySection').innerHTML = '';
}

function appendLogItem(logText, styleFlag = '') {
    const consoleArea = document.getElementById('logsDisplaySection');
    const rowElement = document.createElement('div');
    rowElement.className = `terminal-entry ${styleFlag}`;
    rowElement.textContent = logText;
    consoleArea.appendChild(rowElement);
    consoleArea.scrollTop = consoleArea.scrollHeight;
}

function triggerBrowserWorkflow() {
    const directiveField = document.getElementById('taskDirectiveInputBox');
    const directiveQuery = directiveField.value.trim();
    if (!directiveQuery) {
        alert('Please specify a browsing task before starting.');
        directiveField.focus();
        return;
    }

    const isBackground = document.getElementById('backgroundModeCheckbox').checked;
    const triggerTaskButton = document.getElementById('startExecutionButton');
    const logsDisplaySection = document.getElementById('liveTerminalCard');
    const summaryDisplayCard = document.getElementById('summaryDisplayCard');
    const summaryContentContainer = document.getElementById('summaryContentContainer');
    const verificationCard = document.getElementById('humanVerificationCard');

    if (verificationCard) {
        verificationCard.style.display = 'none';
    }

    logsDisplaySection.style.display = 'flex';
    summaryDisplayCard.style.display = 'none';
    flushTerminalScreen();

    triggerTaskButton.disabled = true;
    triggerTaskButton.innerHTML = '<span>⏳ Running...</span>';

    appendLogItem(`Starting task: "${directiveQuery}"`, 'entry-status');

    if (activeLiveStreamConnection) {
        activeLiveStreamConnection.close();
    }

    const connectionUrl = `/api/run?task=${encodeURIComponent(directiveQuery)}&background_mode=${isBackground}&headless=${isBackground}`;
    activeLiveStreamConnection = new EventSource(connectionUrl);

    activeLiveStreamConnection.onmessage = (eventPacket) => {
        try {
            const receivedPacket = JSON.parse(eventPacket.data);
            if (receivedPacket.type === 'status') {
                appendLogItem(receivedPacket.text, 'entry-status');
            } else if (receivedPacket.type === 'log') {
                appendLogItem(receivedPacket.text);
            } else if (receivedPacket.type === 'verification_required') {
                const card = document.getElementById('humanVerificationCard');
                const promptDesc = document.getElementById('verificationPromptMessage');
                const confirmBtn = document.getElementById('btnConfirmVerification');

                if (card) {
                    card.style.display = 'block';
                    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                }
                if (promptDesc) {
                    const challengeReason = receivedPacket.reason || 'CAPTCHA / bot-detection challenge detected on page.';
                    promptDesc.innerHTML = `<strong>${escapeHtml(challengeReason)}</strong><br>Please switch to your open <strong>Microsoft Edge</strong> browser window, complete the verification, and click <strong>Done</strong> below to continue.`;
                }
                if (confirmBtn) {
                    confirmBtn.disabled = false;
                    confirmBtn.innerHTML = '<span>✔ Done (Continue Task)</span>';
                }
                appendLogItem(`🛡️ [Verification Required] ${receivedPacket.reason || 'Human verification required in Edge browser.'}`, 'entry-warn');
            } else if (receivedPacket.type === 'verification_completed') {
                const card = document.getElementById('humanVerificationCard');
                if (card) {
                    card.style.display = 'none';
                }
                appendLogItem(receivedPacket.text || '✔ Verification confirmed. Resuming browser agent...', 'entry-status');
            } else if (receivedPacket.type === 'result') {
                const card = document.getElementById('humanVerificationCard');
                if (card) card.style.display = 'none';
                retainedResearchOutput = receivedPacket.text;
                summaryDisplayCard.style.display = 'flex';
                summaryContentContainer.innerHTML = marked.parse(receivedPacket.text);
                const statusHeadingElement = document.getElementById('summaryStatusHeading');
                if (receivedPacket.success) {
                    statusHeadingElement.innerHTML = '<span>✔</span> Task Completed Successfully';
                    statusHeadingElement.style.color = '#38bdf8';
                } else {
                    statusHeadingElement.innerHTML = '<span>⚠️</span> Task Finished with Notice';
                    statusHeadingElement.style.color = '#fbbf24';
                }
            } else if (receivedPacket.type === 'error' || receivedPacket.type === 'api_limit') {
                const card = document.getElementById('humanVerificationCard');
                if (card) card.style.display = 'none';
                appendLogItem(receivedPacket.text, 'entry-err');
            } else if (receivedPacket.type === 'done') {
                const card = document.getElementById('humanVerificationCard');
                if (card) card.style.display = 'none';
                activeLiveStreamConnection.close();
                activeLiveStreamConnection = null;
                triggerTaskButton.disabled = false;
                triggerTaskButton.innerHTML = '<span>▶ Run Task</span>';
                fetchTaskHistory();
            }
        } catch (parseFault) {
            appendLogItem(`Stream parsing notice: ${eventPacket.data}`);
        }
    };

    activeLiveStreamConnection.onerror = () => {
        const card = document.getElementById('humanVerificationCard');
        if (card) card.style.display = 'none';
        appendLogItem('Stream connection concluded.', 'entry-warn');
        if (activeLiveStreamConnection) {
            activeLiveStreamConnection.close();
            activeLiveStreamConnection = null;
        }
        triggerTaskButton.disabled = false;
        triggerTaskButton.innerHTML = '<span>▶ Run Task</span>';
        fetchTaskHistory();
    };
}

function escapeHtml(rawString) {
    if (!rawString) return '';
    const tempDiv = document.createElement('div');
    tempDiv.textContent = rawString;
    return tempDiv.innerHTML;
}

async function confirmVerificationDone() {
    const confirmBtn = document.getElementById('btnConfirmVerification');
    if (confirmBtn) {
        confirmBtn.disabled = true;
        confirmBtn.innerHTML = '<span>⏳ Resuming Agent...</span>';
    }

    try {
        const res = await fetch('/api/resume-verification', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        const packet = await res.json();
        if (res.ok && packet.success) {
            appendLogItem(`✔ ${packet.message || 'Verification confirmation sent to browser agent.'}`, 'entry-status');
            const card = document.getElementById('humanVerificationCard');
            if (card) card.style.display = 'none';
        } else {
            appendLogItem(`Notice: ${packet.message || 'Agent not currently waiting on verification.'}`, 'entry-warn');
            const card = document.getElementById('humanVerificationCard');
            if (card) card.style.display = 'none';
        }
    } catch (networkFault) {
        appendLogItem(`Failed to notify agent: ${networkFault.message}`, 'entry-err');
        if (confirmBtn) {
            confirmBtn.disabled = false;
            confirmBtn.innerHTML = '<span>✔ Done (Continue Task)</span>';
        }
    }
}
}

function duplicateSummaryContent() {
    if (retainedResearchOutput) {
        navigator.clipboard.writeText(retainedResearchOutput).then(() => {
            alert('Summary copied to clipboard!');
        });
    }
}

function routeFindingsToDialogue() {
    if (!retainedResearchOutput) return;
    const bannerBox = document.getElementById('researchMemoryBanner');
    const previewArea = document.getElementById('findingsSnippetBody');

    previewArea.innerHTML = marked.parse(retainedResearchOutput);
    bannerBox.style.display = 'flex';

    switchApplicationView('chat');
    document.getElementById('chatQueryInputBox').placeholder = 'Ask questions about the web research findings...';
}

function toggleMemoryPanel() {
    const findingsContainer = document.getElementById('findingsSnippetBody');
    const actionElement = document.getElementById('btnToggleMemory');
    if (findingsContainer.style.display === 'none') {
        findingsContainer.style.display = 'block';
        actionElement.textContent = '▲ Collapse';
    } else {
        findingsContainer.style.display = 'none';
        actionElement.textContent = '▼ Expand';
    }
}

function duplicateContextToClipboard() {
    if (retainedResearchOutput) {
        navigator.clipboard.writeText(retainedResearchOutput).then(() => {
            alert('Context findings copied!');
        });
    }
}

function clearContextMemory() {
    retainedResearchOutput = '';
    document.getElementById('researchMemoryBanner').style.display = 'none';
    document.getElementById('chatQueryInputBox').placeholder = 'Ask AI anything...';
}

function resetDialogueHistory() {
    conversationHistoryRecords = [];
    const threadDisplay = document.getElementById('chatThreadDisplay');
    threadDisplay.innerHTML = `
        <div class="message-row row-ai">
            <div class="avatar avatar-ai">✨</div>
            <div class="bubble bubble-ai">
                <strong>Chat cleared.</strong> How can I assist you next?
            </div>
        </div>
    `;
}

function dispatchPresetChatQuestion(questionText) {
    const promptField = document.getElementById('chatQueryInputBox');
    promptField.value = questionText;
    sendUserQuery();
}

function appendMessageBubble(speakerRole, bubbleHtml) {
    const threadDisplay = document.getElementById('chatThreadDisplay');
    const bubbleRow = document.createElement('div');
    bubbleRow.className = `message-row ${speakerRole === 'user' ? 'row-user' : 'row-ai'}`;

    const speakerIcon = document.createElement('div');
    speakerIcon.className = `avatar ${speakerRole === 'user' ? 'avatar-user' : 'avatar-ai'}`;
    speakerIcon.textContent = speakerRole === 'user' ? '👤' : '✨';

    const textBubble = document.createElement('div');
    textBubble.className = `bubble ${speakerRole === 'user' ? 'bubble-user' : 'bubble-ai'}`;
    if (speakerRole === 'user') {
        textBubble.textContent = bubbleHtml;
    } else {
        textBubble.innerHTML = marked.parse(bubbleHtml);
    }

    bubbleRow.appendChild(speakerIcon);
    bubbleRow.appendChild(textBubble);
    threadDisplay.appendChild(bubbleRow);
    threadDisplay.scrollTop = threadDisplay.scrollHeight;
}

async function sendUserQuery() {
    const promptField = document.getElementById('chatQueryInputBox');
    const userQueryText = promptField.value.trim();
    if (!userQueryText) return;

    appendMessageBubble('user', userQueryText);
    promptField.value = '';

    const thinkingBox = document.getElementById('thinkingIndicatorBox');
    const dispatchQueryButton = document.getElementById('dispatchQueryButton');
    thinkingBox.style.display = 'flex';
    dispatchQueryButton.disabled = true;

    try {
        const networkCallResult = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message: userQueryText,
                history: conversationHistoryRecords,
                context: retainedResearchOutput,
            }),
        });
        const serverPacket = await networkCallResult.json();
        const replyText = serverPacket.reply || serverPacket.error || 'No response returned.';

        appendMessageBubble('ai', replyText);
        conversationHistoryRecords.push({ role: 'user', text: userQueryText });
        conversationHistoryRecords.push({ role: 'assistant', text: replyText });
    } catch (networkFault) {
        appendMessageBubble('ai', `❌ Connection failure: ${networkFault.message}`);
    } finally {
        thinkingBox.style.display = 'none';
        dispatchQueryButton.disabled = false;
        document.getElementById('chatThreadDisplay').scrollTop = document.getElementById('chatThreadDisplay').scrollHeight;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    fetchEnvKeys();
    fetchTaskHistory();

    document.addEventListener('keydown', (keyboardEvent) => {
        if (keyboardEvent.key === 'Escape') {
            closeApiModal();
        }
    });

    const promptTextareaField = document.getElementById('chatQueryInputBox');
    if (promptTextareaField) {
        promptTextareaField.addEventListener('keydown', (keyboardEvent) => {
            if (keyboardEvent.key === 'Enter' && !keyboardEvent.shiftKey) {
                keyboardEvent.preventDefault();
                sendUserQuery();
            }
        });
    }
});

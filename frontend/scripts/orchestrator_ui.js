let selectedViewMode = 'browser';
let retainedResearchOutput = '';
let conversationHistoryRecords = [];
let activeLiveStreamConnection = null;

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

    logsDisplaySection.style.display = 'flex';
    summaryDisplayCard.style.display = 'none';
    flushTerminalScreen();

    triggerTaskButton.disabled = true;
    triggerTaskButton.innerHTML = '<span>⏳ Running...</span>';

    appendLogItem(`Starting task: "${directiveQuery}"`, 'entry-status');

    if (activeLiveStreamConnection) {
        activeLiveStreamConnection.close();
    }

    const connectionUrl = `/api/run?task=${encodeURIComponent(directiveQuery)}&headless=${isBackground}`;
    activeLiveStreamConnection = new EventSource(connectionUrl);

    activeLiveStreamConnection.onmessage = (eventPacket) => {
        try {
            const receivedPacket = JSON.parse(eventPacket.data);
            if (receivedPacket.type === 'status') {
                appendLogItem(receivedPacket.text, 'entry-status');
            } else if (receivedPacket.type === 'log') {
                appendLogItem(receivedPacket.text);
            } else if (receivedPacket.type === 'result') {
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
                appendLogItem(receivedPacket.text, 'entry-err');
            } else if (receivedPacket.type === 'done') {
                activeLiveStreamConnection.close();
                activeLiveStreamConnection = null;
                triggerTaskButton.disabled = false;
                triggerTaskButton.innerHTML = '<span>▶ Run Task</span>';
            }
        } catch (parseFault) {
            appendLogItem(`Stream parsing notice: ${eventPacket.data}`);
        }
    };

    activeLiveStreamConnection.onerror = () => {
        appendLogItem('Stream connection concluded.', 'entry-warn');
        if (activeLiveStreamConnection) {
            activeLiveStreamConnection.close();
            activeLiveStreamConnection = null;
        }
        triggerTaskButton.disabled = false;
        triggerTaskButton.innerHTML = '<span>▶ Run Task</span>';
    };
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

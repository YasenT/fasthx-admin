/**
 * AI Chat Widget for fasthx-admin
 */
(function () {
    const STORAGE_KEY_EXPANDED = 'ai_chat_expanded';
    const STORAGE_KEY_SIZE = 'ai_chat_size';
    const STORAGE_KEY_THINKING = 'ai_chat_thinking';

    const widget = document.getElementById('ai-chat-widget');
    const toggle = document.getElementById('ai-chat-toggle');
    const panel = document.getElementById('ai-chat-panel');
    const minimize = document.getElementById('ai-chat-minimize');
    const clearBtn = document.getElementById('ai-chat-clear');
    const form = document.getElementById('ai-chat-form');
    const input = document.getElementById('ai-chat-input');
    const messagesEl = document.getElementById('ai-chat-messages');
    const typingEl = document.getElementById('ai-chat-typing');
    const thinkingBtn = document.getElementById('ai-chat-thinking-toggle');
    const attachBtn = document.getElementById('ai-chat-attach');
    const fileInput = document.getElementById('ai-chat-file');
    const attachStrip = document.getElementById('ai-chat-attachments');

    if (!widget) return;

    // --- Image attachments (current turn only — never persisted) ---
    const MAX_ATTACHMENTS = 4;
    const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
    let attachedImages = [];

    function flashStatus(msg) {
        if (!attachStrip) { console.warn(msg); return; }
        const note = document.createElement('div');
        note.className = 'ai-chat-attachment-error';
        note.textContent = msg;
        attachStrip.appendChild(note);
        attachStrip.style.display = 'flex';
        setTimeout(function () { note.remove(); renderAttachments(); }, 3000);
    }

    function addAttachment(file) {
        if (!file || !file.type || !file.type.startsWith('image/')) return;
        if (attachedImages.length >= MAX_ATTACHMENTS) {
            flashStatus('Max ' + MAX_ATTACHMENTS + ' images per message.');
            return;
        }
        if (file.size > MAX_IMAGE_BYTES) {
            flashStatus('Image too large (max 5 MB).');
            return;
        }
        const reader = new FileReader();
        reader.onload = function () {
            attachedImages.push({
                id: 'att_' + Math.random().toString(36).slice(2, 10),
                dataUrl: reader.result,
                name: file.name || 'pasted-image',
            });
            renderAttachments();
        };
        reader.onerror = function () { flashStatus('Could not read image.'); };
        reader.readAsDataURL(file);
    }

    function renderAttachments() {
        if (!attachStrip) return;
        attachStrip.innerHTML = '';
        if (attachedImages.length === 0) {
            attachStrip.style.display = 'none';
            return;
        }
        attachStrip.style.display = 'flex';
        attachedImages.forEach(function (item) {
            const wrap = document.createElement('div');
            wrap.className = 'ai-chat-attachment';
            const thumb = document.createElement('img');
            thumb.className = 'ai-chat-attachment-thumb';
            thumb.src = item.dataUrl;
            thumb.alt = item.name;
            const rm = document.createElement('button');
            rm.type = 'button';
            rm.className = 'ai-chat-attachment-remove';
            rm.title = 'Remove';
            rm.innerHTML = '<i class="bi bi-x-circle-fill"></i>';
            rm.addEventListener('click', function () {
                attachedImages = attachedImages.filter(function (x) { return x.id !== item.id; });
                renderAttachments();
            });
            wrap.appendChild(thumb);
            wrap.appendChild(rm);
            attachStrip.appendChild(wrap);
        });
    }

    if (attachBtn && fileInput) {
        attachBtn.addEventListener('click', function () { fileInput.click(); });
        fileInput.addEventListener('change', function (e) {
            const files = e.target.files || [];
            for (let i = 0; i < files.length; i++) addAttachment(files[i]);
            fileInput.value = '';
        });
    }

    if (input) {
        input.addEventListener('paste', function (e) {
            const items = (e.clipboardData && e.clipboardData.items) || [];
            let usedClipboard = false;
            for (let i = 0; i < items.length; i++) {
                const it = items[i];
                if (it.kind === 'file' && it.type && it.type.startsWith('image/')) {
                    const file = it.getAsFile();
                    if (file) { addAttachment(file); usedClipboard = true; }
                }
            }
            if (usedClipboard) e.preventDefault();
        });
    }

    // --- State ---
    let expanded = localStorage.getItem(STORAGE_KEY_EXPANDED) === 'true';
    let thinking = localStorage.getItem(STORAGE_KEY_THINKING) === 'true';

    function setThinking(state) {
        thinking = state;
        localStorage.setItem(STORAGE_KEY_THINKING, state);
        if (thinkingBtn) {
            thinkingBtn.setAttribute('aria-pressed', state ? 'true' : 'false');
            thinkingBtn.classList.toggle('btn-warning', state);
            thinkingBtn.classList.toggle('btn-outline-secondary', !state);
        }
    }

    // --- Markdown rendering ---
    // Common LaTeX commands the model emits in prose (arrows, math operators,
    // greek letters). No KaTeX/MathJax is loaded, so substitute Unicode and
    // strip the surrounding $...$ / \(...\) delimiters.
    const LATEX_TO_UNICODE = {
        rightarrow: '→', to: '→', longrightarrow: '⟶',
        leftarrow: '←', gets: '←', longleftarrow: '⟵',
        Rightarrow: '⇒', implies: '⇒',
        Leftarrow: '⇐',
        leftrightarrow: '↔', Leftrightarrow: '⇔', iff: '⇔',
        uparrow: '↑', downarrow: '↓', updownarrow: '↕',
        mapsto: '↦', hookrightarrow: '↪', hookleftarrow: '↩',
        times: '×', div: '÷', pm: '±', mp: '∓', cdot: '·', bullet: '•',
        le: '≤', leq: '≤', ge: '≥', geq: '≥',
        ne: '≠', neq: '≠', approx: '≈', equiv: '≡', sim: '∼', cong: '≅',
        ll: '≪', gg: '≫',
        infty: '∞', partial: '∂', nabla: '∇',
        ldots: '…', dots: '…', cdots: '⋯', vdots: '⋮',
        forall: '∀', exists: '∃', nexists: '∄',
        in: '∈', notin: '∉', ni: '∋',
        subset: '⊂', supset: '⊃', subseteq: '⊆', supseteq: '⊇',
        cup: '∪', cap: '∩', emptyset: '∅', varnothing: '∅',
        sum: '∑', prod: '∏', int: '∫', oint: '∮',
        sqrt: '√', propto: '∝', therefore: '∴', because: '∵',
        ang: '∠', perp: '⊥', parallel: '∥',
        alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ',
        epsilon: 'ε', varepsilon: 'ε', zeta: 'ζ', eta: 'η',
        theta: 'θ', vartheta: 'ϑ', iota: 'ι', kappa: 'κ',
        lambda: 'λ', mu: 'μ', nu: 'ν', xi: 'ξ',
        pi: 'π', varpi: 'ϖ', rho: 'ρ', varrho: 'ϱ',
        sigma: 'σ', varsigma: 'ς', tau: 'τ', upsilon: 'υ',
        phi: 'φ', varphi: 'ϕ', chi: 'χ', psi: 'ψ', omega: 'ω',
        Gamma: 'Γ', Delta: 'Δ', Theta: 'Θ', Lambda: 'Λ',
        Xi: 'Ξ', Pi: 'Π', Sigma: 'Σ', Upsilon: 'Υ',
        Phi: 'Φ', Psi: 'Ψ', Omega: 'Ω',
    };

    function preprocessLatex(text) {
        // Split out fenced and inline code so we don't rewrite their contents.
        const parts = text.split(/(```[\s\S]*?```|`[^`\n]*`)/);
        return parts.map(function (part, idx) {
            if (idx % 2 === 1) return part;
            let out = part.replace(/\\([a-zA-Z]+)/g, function (match, cmd) {
                return Object.prototype.hasOwnProperty.call(LATEX_TO_UNICODE, cmd)
                    ? LATEX_TO_UNICODE[cmd]
                    : match;
            });
            // Strip $...$ wrappers when nothing LaTeX-y remains inside.
            out = out.replace(/\$([^$\n]+?)\$/g, function (match, inner) {
                return /\\[a-zA-Z]/.test(inner) ? match : inner;
            });
            // Same for \( ... \) inline math.
            out = out.replace(/\\\(([\s\S]+?)\\\)/g, function (match, inner) {
                return /\\[a-zA-Z]/.test(inner) ? match : inner;
            });
            return out;
        }).join('');
    }

    // --- Auto-embed images, videos, and YouTube ---
    // Bare URLs that GFM auto-linked into <a> get rewritten into media tags.
    // We only ever construct <iframe> from a vetted YouTube ID, never from raw
    // model output, so DOMPurify can safely allow iframe in the final pass.
    const IMG_EXT_RE = /\.(png|jpe?g|gif|webp|svg)(?:\?[^\s]*)?$/i;
    const VIDEO_EXT_RE = /\.(mp4|webm|ogg|mov)(?:\?[^\s]*)?$/i;
    const YOUTUBE_RE = /^https?:\/\/(?:www\.)?(?:youtube\.com\/(?:watch\?v=|shorts\/|embed\/)|youtu\.be\/)([\w-]{11})/i;

    function attrEscape(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    function youtubeEmbedHtml(url) {
        const m = url.match(YOUTUBE_RE);
        if (!m) return null;
        const src = 'https://www.youtube-nocookie.com/embed/' + m[1];
        return '<iframe class="ai-chat-embed" src="' + attrEscape(src) +
            '" allow="accelerometer; clipboard-write; encrypted-media; gyroscope; picture-in-picture"' +
            ' allowfullscreen loading="lazy"></iframe>';
    }

    function autoEmbedHtml(html) {
        const tmp = document.createElement('div');
        tmp.innerHTML = html;

        // Auto-linked bare URLs become <a href="X">X</a>. If the text matches
        // the href and the href looks like media, swap it for an embed.
        tmp.querySelectorAll('a[href]').forEach(function (a) {
            const href = a.getAttribute('href') || '';
            const label = (a.textContent || '').trim();
            if (label && label !== href) return;

            const yt = youtubeEmbedHtml(href);
            if (yt) { a.outerHTML = yt; return; }
            if (VIDEO_EXT_RE.test(href)) {
                a.outerHTML = '<video class="ai-chat-embed" controls preload="metadata" src="' +
                    attrEscape(href) + '"></video>';
                return;
            }
            if (IMG_EXT_RE.test(href)) {
                a.outerHTML = '<img class="ai-chat-embed" alt="" src="' +
                    attrEscape(href) + '">';
            }
        });

        // ![alt](video.mp4) → <video> instead of a broken <img>.
        tmp.querySelectorAll('img').forEach(function (img) {
            const src = img.getAttribute('src') || '';
            if (VIDEO_EXT_RE.test(src)) {
                const v = document.createElement('video');
                v.controls = true;
                v.preload = 'metadata';
                v.className = 'ai-chat-embed';
                v.src = src;
                img.replaceWith(v);
            } else if (!img.classList.contains('ai-chat-embed')) {
                img.classList.add('ai-chat-embed');
            }
        });

        return tmp.innerHTML;
    }

    function renderMarkdown(text) {
        const prepared = preprocessLatex(text);
        if (typeof marked !== 'undefined') {
            let html = marked.parse(prepared);
            html = autoEmbedHtml(html);
            if (typeof DOMPurify !== 'undefined') {
                html = DOMPurify.sanitize(html, {
                    ADD_TAGS: ['video', 'source', 'iframe'],
                    ADD_ATTR: ['controls', 'preload', 'allow', 'allowfullscreen', 'loading', 'frameborder'],
                });
            }
            return html;
        }
        // Fallback: basic escaping + newlines
        return prepared
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/\n/g, '<br>');
    }

    // --- UI helpers ---
    function setExpanded(state) {
        expanded = state;
        localStorage.setItem(STORAGE_KEY_EXPANDED, state);
        toggle.style.display = state ? 'none' : 'flex';
        panel.style.display = state ? 'flex' : 'none';
        if (state) {
            restoreSize();
            input.focus();
            scrollToBottom();
        }
    }

    function scrollToBottom() {
        requestAnimationFrame(function () {
            messagesEl.scrollTop = messagesEl.scrollHeight;
        });
    }

    function showTyping() {
        typingEl.style.display = 'block';
        setTypingLabel('');
        scrollToBottom();
    }

    function hideTyping() {
        typingEl.style.display = 'none';
        setTypingLabel('');
    }

    function setTypingLabel(text) {
        let label = typingEl.querySelector('.ai-chat-typing-label');
        if (!text) {
            if (label) label.remove();
            return;
        }
        if (!label) {
            label = document.createElement('div');
            label.className = 'ai-chat-typing-label';
            typingEl.appendChild(label);
        }
        label.textContent = text;
    }

    function addMessage(role, content, thinking, images) {
        // Remove welcome message if present
        const welcome = messagesEl.querySelector('.ai-chat-welcome');
        if (welcome) welcome.remove();

        if (role !== 'user' && thinking) {
            const det = document.createElement('details');
            det.className = 'ai-chat-thought';
            const sum = document.createElement('summary');
            sum.innerHTML = '<i class="bi bi-lightbulb"></i> Thought process';
            det.appendChild(sum);
            const body = document.createElement('div');
            body.className = 'ai-chat-thought-body';
            body.innerHTML = renderMarkdown(thinking);
            det.appendChild(body);
            messagesEl.appendChild(det);
        }

        const bubble = document.createElement('div');
        bubble.className = 'ai-chat-bubble ' +
            (role === 'user' ? 'ai-chat-bubble-user' : 'ai-chat-bubble-ai');

        if (role === 'user') {
            if (content) {
                const txt = document.createElement('div');
                txt.textContent = content;
                bubble.appendChild(txt);
            }
            if (images && images.length) {
                images.forEach(function (url) {
                    const img = document.createElement('img');
                    img.src = url;
                    img.className = 'ai-chat-embed';
                    img.alt = '';
                    bubble.appendChild(img);
                });
            }
        } else {
            bubble.innerHTML = renderMarkdown(content);
        }

        messagesEl.appendChild(bubble);
        scrollToBottom();
    }

    function addToolCall(name, result) {
        const el = document.createElement('div');
        el.className = 'ai-chat-tool-call';
        el.innerHTML = '<i class="bi bi-tools"></i> <strong>' +
            escapeHtml(name) + '</strong>: ' + escapeHtml(truncate(result, 120));
        messagesEl.appendChild(el);
        scrollToBottom();
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    function truncate(text, max) {
        return text.length > max ? text.substring(0, max) + '...' : text;
    }

    // --- Size persistence ---
    function restoreSize() {
        const saved = localStorage.getItem(STORAGE_KEY_SIZE);
        if (saved) {
            try {
                const size = JSON.parse(saved);
                panel.style.width = size.width + 'px';
                panel.style.height = size.height + 'px';
            } catch (e) { /* ignore */ }
        }
    }

    // Save size on resize
    const resizeObserver = new ResizeObserver(function (entries) {
        for (const entry of entries) {
            if (entry.target === panel && panel.style.display !== 'none') {
                const rect = entry.contentRect;
                localStorage.setItem(STORAGE_KEY_SIZE, JSON.stringify({
                    width: Math.round(rect.width),
                    height: Math.round(rect.height)
                }));
            }
        }
    });
    resizeObserver.observe(panel);

    // --- Top-left resize handle ---
    const resizeHandle = document.getElementById('ai-chat-resize-handle');
    if (resizeHandle) {
        let isResizing = false;
        let startX, startY, startWidth, startHeight;

        resizeHandle.addEventListener('mousedown', function (e) {
            e.preventDefault();
            isResizing = true;
            startX = e.clientX;
            startY = e.clientY;
            startWidth = panel.offsetWidth;
            startHeight = panel.offsetHeight;
            document.body.style.userSelect = 'none';
        });

        document.addEventListener('mousemove', function (e) {
            if (!isResizing) return;
            // Dragging left increases width, dragging up increases height
            const newWidth = startWidth - (e.clientX - startX);
            const newHeight = startHeight - (e.clientY - startY);
            panel.style.width = Math.max(320, Math.min(newWidth, window.innerWidth * 0.9)) + 'px';
            panel.style.height = Math.max(400, Math.min(newHeight, window.innerHeight * 0.85)) + 'px';
        });

        document.addEventListener('mouseup', function () {
            if (isResizing) {
                isResizing = false;
                document.body.style.userSelect = '';
            }
        });
    }

    // --- Events ---
    toggle.addEventListener('click', function () {
        setExpanded(true);
    });

    minimize.addEventListener('click', function () {
        setExpanded(false);
    });

    if (thinkingBtn) {
        thinkingBtn.addEventListener('click', function () {
            setThinking(!thinking);
        });
    }

    clearBtn.addEventListener('click', async function () {
        try {
            await fetch('/ai/clear', { method: 'POST' });
        } catch (e) { /* ignore */ }
        messagesEl.innerHTML =
            '<div class="ai-chat-welcome text-center text-muted p-3">' +
            '<i class="bi bi-robot" style="font-size: 2rem;"></i>' +
            '<p class="mt-2 mb-0 small">Ask me anything about the admin data.</p>' +
            '</div>';
    });

    form.addEventListener('submit', async function (e) {
        e.preventDefault();
        const message = input.value.trim();
        if (!message && attachedImages.length === 0) return;

        const turnImages = attachedImages.map(function (a) { return a.dataUrl; });
        attachedImages = [];
        renderAttachments();

        input.value = '';
        addMessage('user', message, undefined, turnImages);
        showTyping();
        input.disabled = true;

        try {
            const resp = await fetch('/ai/chat', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'text/event-stream'
                },
                body: JSON.stringify({
                    message: message,
                    thinking: thinking,
                    images: turnImages,
                })
            });

            if (!resp.ok) {
                hideTyping();
                const err = await resp.json().catch(function () { return {}; });
                addMessage('ai', 'Error: ' + (err.error || resp.statusText));
                return;
            }

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let finalEvent = null;

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });

                let sepIdx;
                while ((sepIdx = buffer.indexOf('\n\n')) !== -1) {
                    const chunk = buffer.slice(0, sepIdx);
                    buffer = buffer.slice(sepIdx + 2);

                    const dataLine = chunk.split('\n').find(function (l) {
                        return l.startsWith('data: ');
                    });
                    if (!dataLine) continue;

                    let event;
                    try { event = JSON.parse(dataLine.slice(6)); }
                    catch (e) { continue; }

                    if (event.type === 'tool_iteration') {
                        const toolList = (event.tools || []).join(', ') || 'tool';
                        setTypingLabel(
                            'Calling ' + toolList + ' (round ' +
                            event.iteration + ' of ' + event.max_iterations + ')'
                        );
                    } else if (event.type === 'error') {
                        finalEvent = { error: event.error };
                    } else if (event.type === 'done') {
                        finalEvent = event;
                    }
                }
            }

            hideTyping();

            if (!finalEvent) {
                addMessage('ai', 'Error: stream ended unexpectedly.');
                return;
            }
            if (finalEvent.error) {
                addMessage('ai', 'Error: ' + finalEvent.error);
                return;
            }

            if (finalEvent.tool_calls && finalEvent.tool_calls.length > 0) {
                finalEvent.tool_calls.forEach(function (tc) {
                    addToolCall(tc.name, tc.result || '');
                });
            }
            addMessage('ai', finalEvent.response, finalEvent.thinking);
        } catch (err) {
            hideTyping();
            addMessage('ai', 'Error: Could not reach the AI service.');
        } finally {
            input.disabled = false;
            input.focus();
        }
    });

    // --- Load history on init ---
    async function loadHistory() {
        try {
            const resp = await fetch('/ai/history');
            if (!resp.ok) return;
            const data = await resp.json();
            if (data.messages && data.messages.length > 0) {
                const welcome = messagesEl.querySelector('.ai-chat-welcome');
                if (welcome) welcome.remove();
                data.messages.forEach(function (msg) {
                    addMessage(msg.role === 'user' ? 'user' : 'ai', msg.content);
                });
            }
        } catch (e) { /* ignore */ }
    }

    // --- Init ---
    setExpanded(expanded);
    setThinking(thinking);
    loadHistory();
})();

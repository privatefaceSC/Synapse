(function() {
    'use strict';

    let opened = null;
    let serial = 0;
    const icon = name => window.synapseIcon(name);

    function close(restoreFocus) {
        const state = opened;
        if (!state) return;
        opened = null;
        clearTimeout(state.hideTimer);
        clearTimeout(state.refreshTimer);
        state.controller.abort();
        state.panel.remove();
        state.anchor.setAttribute('aria-expanded', 'false');
        state.anchor.removeAttribute('aria-controls');
        if (restoreFocus && state.anchor.isConnected) state.anchor.focus();
    }

    function position(state) {
        if (opened !== state) return;
        if (!state.anchor.isConnected) return close();
        const bounds = state.anchor.getBoundingClientRect();
        const panel = state.panel;
        const gap = 3;
        const margin = 8;
        panel.style.maxHeight = Math.max(120, window.innerHeight - margin * 2) + 'px';
        const width = panel.offsetWidth;
        let left = bounds.right + gap;
        let top = bounds.top;
        if (left + width > window.innerWidth - margin) {
            left = bounds.left - width - gap;
        }
        if (left < margin) {
            left = Math.max(margin, window.innerWidth - width - margin);
            top = bounds.bottom + gap;
            if (top + panel.offsetHeight > window.innerHeight - margin) {
                top = bounds.top - panel.offsetHeight - gap;
            }
        }
        panel.style.left = Math.max(margin, left) + 'px';
        panel.style.top = Math.max(margin, Math.min(
            top, window.innerHeight - panel.offsetHeight - margin)) + 'px';
    }

    function note(state, text, error) {
        const line = document.createElement('div');
        line.className = 'folder-menu-note' + (error ? ' error' : '');
        line.setAttribute('role', error ? 'alert' : 'status');
        line.textContent = text;
        state.panel.appendChild(line);
    }

    function button(label, glyph) {
        const node = document.createElement('button');
        node.type = 'button';
        node.innerHTML = icon(glyph);
        const text = document.createElement('span');
        text.className = 'folder-menu-label';
        text.textContent = label;
        node.appendChild(text);
        node.setAttribute('role', 'menuitem');
        return node;
    }

    function render(state) {
        if (opened !== state) return;
        const panel = state.panel;
        const focusedFolder = panel.contains(document.activeElement)
            ? document.activeElement.dataset.folderId : null;
        panel.replaceChildren();
        state.snapshots.forEach(function(data, messenger) {
            if (state.messengers.length > 1) {
                const heading = document.createElement('div');
                heading.className = 'folder-menu-heading';
                heading.textContent = messenger;
                panel.appendChild(heading);
            }
            const folders = Array.isArray(data.folders) ? data.folders : [];
            if (!folders.length) note(state, data.sync_pending
                ? 'Загружаю папки…' : 'Папок пока нет');
            folders.forEach(function(folder) {
                const included = (folder.member_contact_ids || [])
                    .some(id => String(id) === String(state.context.contactId));
                const row = button(folder.title || 'Папка', 'folder');
                row.dataset.folderId = String(folder.id);
                row.setAttribute('role', 'menuitemcheckbox');
                row.setAttribute('aria-checked', included ? 'true' : 'false');
                row.classList.toggle('is-included', included);
                row.disabled = !!folder.read_only || state.busy;
                if (folder.read_only) row.title = 'Эта общая папка редактируется в Telegram';
                const check = document.createElement('span');
                check.className = 'folder-menu-check';
                check.innerHTML = icon(included ? 'check' : 'plus');
                row.appendChild(check);
                row.addEventListener('click', async function(event) {
                    event.stopPropagation();
                    if (state.busy) return;
                    state.busy = true;
                    ++state.loadGeneration;
                    clearTimeout(state.refreshTimer);
                    render(state);
                    note(state, messenger === 'Telegram'
                        ? 'Сохраняю в Telegram…' : 'Сохраняю…');
                    try {
                        const response = await fetch('/contacts/folders/'
                            + encodeURIComponent(folder.id) + '/membership', {
                            method: 'POST', credentials: 'same-origin',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                contact_id: Number(state.context.contactId),
                                included: !included,
                            }),
                        });
                        const result = await response.json().catch(() => ({}));
                        if (!response.ok || !result.folder) throw new Error(
                            result.detail || 'Не удалось изменить папку. Попробуйте ещё раз.');
                        data.folders = folders.map(item => String(item.id) === String(folder.id)
                            ? result.folder : item);
                        window.dispatchEvent(new CustomEvent('synapse:folders-changed', {
                            detail: {messenger, folder: result.folder},
                        }));
                        state.busy = false;
                        render(state);
                    } catch (error) {
                        state.busy = false;
                        render(state);
                        if (opened === state) note(state, error.message, true);
                    }
                    position(state);
                });
                panel.appendChild(row);
            });
            if (data.sync_error) note(state, data.sync_error.detail || 'Папки не обновились', true);
            const create = button('Создать новую папку', 'folder-plus');
            create.classList.add('folder-menu-create');
            create.disabled = state.busy;
            create.addEventListener('click', function(event) {
                event.stopPropagation();
                close();
                window.dispatchEvent(new CustomEvent('synapse:folder-create', {
                    detail: {contactId: state.context.contactId, messenger},
                }));
            });
            panel.appendChild(create);
        });
        if (focusedFolder) {
            const target = Array.from(panel.querySelectorAll('[data-folder-id]'))
                .find(item => item.dataset.folderId === focusedFolder);
            if (target && !target.disabled) target.focus();
        }
        position(state);
    }

    async function load(state, cached, attempt) {
        if (opened !== state) return;
        const generation = ++state.loadGeneration;
        try {
            const results = await Promise.all(state.messengers.map(async messenger => {
                const response = await fetch('/contacts/folders.json?messenger='
                    + encodeURIComponent(messenger) + (cached ? '&cached=1' : ''), {
                    credentials: 'same-origin', signal: state.controller.signal,
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error('Не удалось загрузить папки');
                return [messenger, data];
            }));
            if (opened !== state || state.busy || generation !== state.loadGeneration) return;
            results.forEach(([messenger, data]) => state.snapshots.set(messenger, data));
            render(state);
            if (state.focusFirst) {
                state.focusFirst = false;
                const first = state.panel.querySelector('button:not(:disabled)');
                if (first) first.focus();
            }
            const delays = [1400, 2500, 4000, 6500, 10000];
            if (attempt < delays.length && results.some(([,data]) => data.sync_pending)) {
                state.refreshTimer = setTimeout(() => load(state, true, attempt + 1), delays[attempt]);
            }
        } catch (error) {
            if (opened !== state || generation !== state.loadGeneration
                    || error.name === 'AbortError') return;
            state.panel.replaceChildren();
            note(state, 'Не удалось загрузить папки.', true);
            const retry = button('Повторить', 'refresh');
            retry.addEventListener('click', () => load(state, false, 0));
            state.panel.appendChild(retry);
            position(state);
        }
    }

    function open(anchor, context, focusFirst) {
        if (opened && opened.anchor === anchor) {
            clearTimeout(opened.hideTimer);
            if (focusFirst) {
                const first = opened.panel.querySelector('button:not(:disabled)');
                if (first) first.focus();
                else opened.focusFirst = true;
            }
            return;
        }
        close();
        context = context || {};
        let messengers = Array.from(new Set((context.messengers || [])
            .map(value => String(value).trim()).filter(Boolean)));
        if (context.messenger && messengers.includes(context.messenger)) {
            messengers = [context.messenger];
        }
        if (!context.contactId || !messengers.length) return;
        const panel = document.createElement('div');
        panel.id = 'contact-folder-submenu-' + (++serial);
        panel.className = 'popover-menu contact-folder-submenu';
        panel.setAttribute('role', 'menu');
        panel.setAttribute('aria-label', 'Добавить в папку');
        const state = {
            anchor, context, panel, messengers, snapshots: new Map(),
            controller: new AbortController(), busy: false, focusFirst: !!focusFirst,
            loadGeneration: 0,
        };
        opened = state;
        anchor.setAttribute('aria-expanded', 'true');
        anchor.setAttribute('aria-controls', panel.id);
        document.body.appendChild(panel);
        note(state, 'Загружаю папки…');
        position(state);
        panel.addEventListener('pointerenter', () => clearTimeout(state.hideTimer));
        panel.addEventListener('pointerleave', event => {
            if (event.pointerType !== 'mouse' || state.busy) return;
            state.hideTimer = setTimeout(() => { if (opened === state) close(); }, 220);
        });
        panel.addEventListener('click', event => event.stopPropagation());
        load(state, false, 0);
    }

    function attach(anchor, context) {
        anchor.setAttribute('aria-haspopup', 'menu');
        anchor.setAttribute('aria-expanded', 'false');
        anchor.addEventListener('pointerenter', event => {
            if (event.pointerType === 'mouse') {
                if (opened && opened.anchor === anchor) clearTimeout(opened.hideTimer);
                open(anchor, context);
            }
        });
        anchor.addEventListener('pointerleave', event => {
            if (event.pointerType !== 'mouse' || !opened || opened.anchor !== anchor) return;
            const state = opened;
            state.hideTimer = setTimeout(() => { if (opened === state) close(); }, 220);
        });
        anchor.addEventListener('click', event => {
            event.stopPropagation();
            // A desktop click is preceded by pointerenter. Keep the menu
            // opened by hover instead of immediately toggling it closed.
            open(anchor, context, !event.detail);
        });
        anchor.addEventListener('keydown', event => {
            if (event.key === 'ArrowRight') {
                event.preventDefault();
                open(anchor, context, true);
            }
        });
    }

    document.addEventListener('click', event => {
        if (opened && !opened.panel.contains(event.target)
                && !opened.anchor.contains(event.target)) close();
    });
    document.addEventListener('keydown', event => {
        if (!opened) return;
        if (event.key === 'Escape' || (event.key === 'ArrowLeft'
                && (opened.panel.contains(event.target) || opened.anchor.contains(event.target)))) {
            event.preventDefault();
            close(true);
            return;
        }
        if (!opened.panel.contains(event.target)) return;
        const rows = Array.from(opened.panel.querySelectorAll('button:not(:disabled)'));
        let index = rows.indexOf(document.activeElement);
        if (event.key === 'ArrowDown') index += 1;
        else if (event.key === 'ArrowUp') index -= 1;
        else return;
        event.preventDefault();
        if (rows.length) rows[(index + rows.length) % rows.length].focus();
    });
    window.addEventListener('resize', () => { if (opened) position(opened); });
    window.synapseFolderMenu = {open, attach, close};
})();

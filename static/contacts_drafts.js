(function(root, factory) {
    'use strict';

    const createDraftStore = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = {createDraftStore};
    }
    if (!root || !root.document) return;

    root.__skillwoodCreateDraftStore = createDraftStore;

    function installDraftStore() {
        const list = root.document.getElementById('contact-list');
        if (!list || root.__skillwoodDrafts) return;
        let browserStorage = null;
        try { browserStorage = root.localStorage; } catch (_) {}
        const store = createDraftStore({
            userId: list.dataset.userId || '',
            storage: browserStorage,
            document: root.document,
        });
        root.__skillwoodDrafts = store;
        store.applyToContactList(list);
        root.addEventListener('storage', function(event) {
            if (!event || !event.key || !store.ownsStorageKey(event.key)) return;
            store.handleStorageEvent(event);
            store.applyToContactList(list);
        });
    }

    if (root.document.getElementById('contact-list')) {
        installDraftStore();
    } else {
        root.document.addEventListener('DOMContentLoaded', installDraftStore);
    }
})(typeof window !== 'undefined' ? window : null, function() {
    'use strict';

    const STORAGE_ROOT = 'skillwood:draft:v1:';

    function text(value) {
        return String(value == null ? '' : value);
    }

    function encoded(value) {
        return encodeURIComponent(text(value));
    }

    function createDraftStore(options) {
        options = options || {};
        const userId = text(options.userId);
        const storage = options.storage || null;
        const documentRef = options.document || null;
        const memory = new Map();
        const prefix = STORAGE_ROOT + encoded(userId) + ':';
        let revisionSequence = 0;

        function context(input) {
            input = input || {};
            return {
                contactId: text(input.contactId),
                messenger: text(input.messenger),
                topicId: text(input.topicId),
            };
        }

        function storageKey(input) {
            const value = context(input);
            return prefix + encoded(value.contactId) + '|'
                + encoded(value.messenger) + '|' + encoded(value.topicId);
        }

        function parseRecord(raw, key) {
            if (!raw) return null;
            try {
                const value = JSON.parse(raw);
                if (!value || value.version !== 1
                        || text(value.userId) !== userId
                        || !text(value.contactId)
                        || !text(value.text).trim()) {
                    return null;
                }
                value.contactId = text(value.contactId);
                value.messenger = text(value.messenger);
                value.topicId = text(value.topicId);
                value.text = text(value.text);
                value.updatedAt = Number(value.updatedAt) || 0;
                value.storageKey = key || storageKey(value);
                return value;
            } catch (_) {
                return null;
            }
        }

        function storageGet(key) {
            if (!storage) return null;
            try {
                return storage.getItem(key);
            } catch (_) {
                return null;
            }
        }

        function read(input) {
            const key = storageKey(input);
            const stored = parseRecord(storageGet(key), key);
            if (stored) {
                memory.set(key, stored);
                return stored;
            }
            return memory.get(key) || null;
        }

        function makeRevision() {
            revisionSequence += 1;
            const random = (typeof crypto !== 'undefined' && crypto.randomUUID)
                ? crypto.randomUUID()
                : Math.random().toString(36).slice(2);
            return Date.now().toString(36) + '-' + revisionSequence + '-' + random;
        }

        function writeRecord(record) {
            const key = storageKey(record);
            record.storageKey = key;
            memory.set(key, record);
            if (storage) {
                try {
                    storage.setItem(key, JSON.stringify(record));
                } catch (_) {
                    // Даже при запрете/quota localStorage черновик продолжит
                    // жить при переключениях в пределах текущей вкладки.
                }
            }
            return record;
        }

        function removeRecord(input) {
            const key = storageKey(input);
            memory.delete(key);
            if (storage) {
                try { storage.removeItem(key); } catch (_) {}
            }
        }

        function applyToContactList(list) {
            list = list || (documentRef
                && documentRef.getElementById('contact-list'));
            if (!list || !list.querySelectorAll) return;
            let preferredMessenger = '';
            if (storage) {
                try {
                    preferredMessenger = text(
                        storage.getItem('skillwood:active-folder'));
                } catch (_) {}
            }
            const records = all();
            list.querySelectorAll('.contact-row-wrap[data-contact-id]')
                .forEach(function(wrap) {
                    const contactId = text(wrap.dataset.contactId);
                    const messengers = text(wrap.dataset.messengers)
                        .split(',').map(value => value.trim()).filter(Boolean);
                    const preferred = messengers.some(value =>
                        value.toLocaleLowerCase()
                            === preferredMessenger.toLocaleLowerCase())
                        ? preferredMessenger : '';
                    const candidates = records.filter(record =>
                        record.contactId === contactId
                        && (!preferred || record.messenger.toLocaleLowerCase()
                            === preferred.toLocaleLowerCase()));
                    candidates.sort((a, b) => b.updatedAt - a.updatedAt);
                    const draft = candidates[0] || null;
                    const content = wrap.querySelector('.contact-preview-content');
                    const preview = content && content.querySelector('.preview');
                    if (!content || !preview) return;
                    if (!Object.prototype.hasOwnProperty.call(
                            preview.dataset, 'serverPreview')) {
                        preview.dataset.serverPreview = preview.textContent || '';
                    }
                    content.classList.toggle('has-local-draft', !!draft);
                    preview.classList.toggle('draft-preview', !!draft);
                    if (!draft) {
                        preview.textContent = preview.dataset.serverPreview || '';
                        return;
                    }
                    const compact = draft.text.trim().replace(/\s+/g, ' ');
                    preview.textContent = 'Черновик: '
                        + (compact.length > 160
                            ? compact.slice(0, 159) + '…' : compact);
                });
        }

        function notifyChanged() {
            applyToContactList();
            if (typeof options.onChange === 'function') options.onChange();
        }

        function save(input) {
            const value = context(input);
            const draftText = text(input && input.text);
            const previous = read(value);
            if (!draftText.trim()) {
                if (previous) {
                    removeRecord(value);
                    notifyChanged();
                }
                return null;
            }
            // Повторное сохранение перед навигацией не должно менять revision:
            // ответ старого HTTP-запроса тогда сможет удалить именно отправленную
            // версию, но не новый текст, набранный после возврата в чат.
            if (previous && previous.text === draftText) return previous;
            const record = writeRecord({
                version: 1,
                userId,
                contactId: value.contactId,
                messenger: value.messenger,
                topicId: value.topicId,
                text: draftText,
                updatedAt: Date.now(),
                revision: makeRevision(),
            });
            notifyChanged();
            return record;
        }

        function removeIfUnchanged(snapshot) {
            if (!snapshot || !snapshot.revision) return false;
            const current = read(snapshot);
            if (!current || current.revision !== snapshot.revision) return false;
            removeRecord(snapshot);
            notifyChanged();
            return true;
        }

        function all() {
            const found = new Map(memory);
            if (storage) {
                try {
                    for (let index = 0; index < storage.length; index += 1) {
                        const key = storage.key(index);
                        if (!key || key.indexOf(prefix) !== 0) continue;
                        const record = parseRecord(storage.getItem(key), key);
                        if (record) found.set(key, record);
                    }
                } catch (_) {}
            }
            return Array.from(found.values());
        }

        function handleStorageEvent(event) {
            if (!event || !ownsStorageKey(event.key)) return;
            const record = parseRecord(event.newValue, event.key);
            if (record) memory.set(event.key, record);
            else memory.delete(event.key);
        }

        function ownsStorageKey(key) {
            return text(key).indexOf(prefix) === 0;
        }

        return {
            all,
            applyToContactList,
            get: read,
            handleStorageEvent,
            ownsStorageKey,
            removeIfUnchanged,
            save,
            storageKey,
        };
    }

    return createDraftStore;
});

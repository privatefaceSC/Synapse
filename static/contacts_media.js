(function(root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) root.SynapseContactsMedia = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {
    'use strict';

    const DEFAULT_MAX_SIDE = 1900;
    const DEFAULT_REENCODE_BYTES = 8 * 1024 * 1024;

    function galleryPosition(current, delta, length) {
        const count = Math.max(0, Number(length) || 0);
        if (!count) {
            return {index: -1, hasPrevious: false, hasNext: false};
        }
        const start = Math.max(0, Math.min(Number(current) || 0, count - 1));
        const index = Math.max(0, Math.min(start + (Number(delta) || 0),
                                           count - 1));
        return {
            index,
            hasPrevious: index > 0,
            hasNext: index < count - 1,
        };
    }

    function imageResizePlan(width, height, size, mime, options) {
        options = options || {};
        const sourceWidth = Math.max(1, Math.round(Number(width) || 1));
        const sourceHeight = Math.max(1, Math.round(Number(height) || 1));
        const sourceSize = Math.max(0, Number(size) || 0);
        const sourceMime = String(mime || '').toLowerCase();
        const maxSide = Math.max(1, Number(options.maxSide)
            || DEFAULT_MAX_SIDE);
        const reencodeBytes = Math.max(1, Number(options.reencodeBytes)
            || DEFAULT_REENCODE_BYTES);

        // Анимацию и вектор нельзя безопасно прогонять через обычный canvas.
        if (sourceMime === 'image/gif' || sourceMime === 'image/svg+xml') {
            return {resize: false, width: sourceWidth, height: sourceHeight};
        }

        const longest = Math.max(sourceWidth, sourceHeight);
        const scale = Math.min(1, maxSide / longest);
        const resize = scale < 1 || sourceSize > reencodeBytes;
        return {
            resize,
            width: Math.max(1, Math.round(sourceWidth * scale)),
            height: Math.max(1, Math.round(sourceHeight * scale)),
        };
    }

    function wait(delayMs) {
        const delay = Math.max(0, Number(delayMs) || 0);
        return delay ? new Promise(resolve => setTimeout(resolve, delay))
            : Promise.resolve();
    }

    async function requestWithRetry(request, options) {
        options = options || {};
        const maxAttempts = Math.max(1, Number(options.maxAttempts) || 2);
        let lastError;
        for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
            try {
                // HTTP-ответ уже дошёл до браузера: его возвращаем вызывающему
                // коду и не рискуем повторять осмысленную серверную ошибку.
                return await request(attempt);
            } catch (error) {
                lastError = error;
                if (attempt + 1 >= maxAttempts) throw error;
                await wait(options.delayMs);
            }
        }
        throw lastError;
    }

    return {
        galleryPosition,
        imageResizePlan,
        requestWithRetry,
    };
});

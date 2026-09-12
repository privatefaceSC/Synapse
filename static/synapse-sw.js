self.addEventListener('install', event => {
    event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
    event.waitUntil(self.clients.claim());
});

function readPayload(event) {
    if (!event.data) return {};
    try {
        return event.data.json();
    } catch (e) {
        return { body: event.data.text() };
    }
}

function notificationFromPayload(payload) {
    const declarative = payload && payload.web_push === 8030
        && payload.notification ? payload.notification : null;
    const source = declarative || payload || {};
    const title = source.title || payload.title || 'Synapse';
    const targetUrl = source.navigate || payload.navigate
        || source.url || payload.url || '/contacts';
    const data = Object.assign({}, payload.data || {}, {
        url: payload.url || source.url || targetUrl || '/contacts',
        contact_id: payload.contact_id || source.contact_id || null,
        message_id: payload.message_id || source.message_id || null,
    });
    const options = {
        body: source.body || payload.body || 'Новое сообщение',
        tag: source.tag || payload.tag || 'synapse-message',
        data,
        renotify: false,
        silent: source.silent === true,
    };
    if (source.icon || payload.icon) options.icon = source.icon || payload.icon;
    if (source.badge || payload.badge) options.badge = source.badge || payload.badge;
    if (targetUrl) options.navigate = targetUrl;
    return { title, options };
}

self.addEventListener('push', event => {
    const payload = readPayload(event);
    const { title, options } = notificationFromPayload(payload);
    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {
    event.notification.close();
    const targetUrl = new URL(
        (event.notification.data && event.notification.data.url) || '/contacts',
        self.location.origin);
    event.waitUntil((async () => {
        const windows = await clients.matchAll({
            type: 'window',
            includeUncontrolled: true,
        });
        for (const client of windows) {
            const url = new URL(client.url);
            if (url.origin === targetUrl.origin) {
                await client.focus();
                return client.navigate(targetUrl.href);
            }
        }
        return clients.openWindow(targetUrl.href);
    })());
});

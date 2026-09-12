self.addEventListener('push', event => {
    let payload = {};
    if (event.data) {
        try {
            payload = event.data.json();
        } catch (e) {
            payload = { body: event.data.text() };
        }
    }
    const title = payload.title || 'Synapse';
    const options = {
        body: payload.body || 'Новое сообщение',
        tag: payload.tag || 'synapse-message',
        data: {
            url: payload.url || '/contacts',
            contact_id: payload.contact_id || null,
            message_id: payload.message_id || null,
        },
        renotify: false,
    };
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

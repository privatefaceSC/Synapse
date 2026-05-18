import os
import random
import string
import uuid
from datetime import datetime

from flask import (Flask, Response, abort, g, jsonify, redirect, render_template,
                   request, send_from_directory, session)
from werkzeug.security import check_password_hash, generate_password_hash

from data import db_sessions
from data.users import Messages, User


_AVATAR_PALETTE = [
    "#ef4444", "#f59e0b", "#10b981", "#3b82f6",
    "#8b5cf6", "#ec4899", "#14b8a6", "#f97316",
]


def _avatar_for(contact):
    name = (contact.display_name or "?").strip()
    contact.initial = name[:1].upper() if name else "?"
    contact.avatar_color = _AVATAR_PALETTE[contact.id % len(_AVATAR_PALETTE)]
    return contact


def _enrich_with_last_message(db, contacts):
    from data.contacts import MessengerHandle
    from sqlalchemy import func

    for c in contacts:
        _avatar_for(c)
        handle_ids = [h.id for h in
                      db.query(MessengerHandle).filter(MessengerHandle.contact_id == c.id).all()]
        if not handle_ids:
            c.last_preview = None
            c.last_time = None
            c.last_at = None
            c.unread_count = 0
            continue

        last = (
            db.query(Messages)
            .filter(Messages.handle_id.in_(handle_ids))
            .order_by(Messages.created_at.desc().nullslast(), Messages.id.desc())
            .first()
        )
        if last:
            c.last_preview = last.text
            c.last_time = last.time
            c.last_at = last.created_at
        else:
            c.last_preview = None
            c.last_time = None
            c.last_at = None

        unread_q = db.query(func.count(Messages.id)).filter(Messages.handle_id.in_(handle_ids))
        if c.last_read_at is not None:
            unread_q = unread_q.filter(Messages.created_at > c.last_read_at)
        c.unread_count = unread_q.scalar() or 0
    contacts.sort(
        key=lambda c: (c.last_at or datetime.min),
        reverse=True,
    )
    return contacts


def _attach_media(db, msgs):
    from data.attachments import Attachment
    ids = [m.id for m in msgs]
    by_msg = {}
    if ids:
        for a in (db.query(Attachment)
                  .filter(Attachment.message_id.in_(ids))
                  .order_by(Attachment.id.asc()).all()):
            by_msg.setdefault(a.message_id, []).append(a)
    for m in msgs:
        m.media = by_msg.get(m.id, [])
    return msgs


def create_app(db_path: str = "db/blogs.db") -> Flask:
    db_sessions.global_init(db_path)

    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'yandexlyceum_secret_key'
    app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024

    @app.teardown_appcontext
    def _close_db(_exc):
        sess = g.pop('db', None)
        if sess is not None:
            sess.close()

    register_routes(app)
    return app


def get_db():
    if 'db' not in g:
        g.db = db_sessions.create_session()
    return g.db


def _media_root() -> str:
    return os.environ.get('SKILLWOOD_MEDIA_ROOT') or os.path.join(os.getcwd(), 'media')


def _avatar_file(user_id) -> str:
    return os.path.join(_media_root(), str(user_id), 'avatar.enc')


def _avatar_mime_file(user_id) -> str:
    return os.path.join(_media_root(), str(user_id), 'avatar.mime')


def register_routes(app: Flask) -> None:

    @app.route('/')
    def main_menu():
        if session.get('user_id'):
            return redirect('/home')
        return render_template('main_menu.html')

    @app.route('/logout')
    def logout():
        session.pop('user_id', None)
        return redirect('/')

    @app.route('/home')
    def index():
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import Contact, MergeSuggestion
        from data.devices import Device
        db = get_db()
        user = db.query(User).filter(User.id == session['user_id']).first()
        contacts_count = db.query(Contact).filter(Contact.user_id == user.id).count()
        messages_count = db.query(Messages).filter(Messages.user_id == user.id).count()
        pending_suggestions = (db.query(MergeSuggestion)
                               .filter(MergeSuggestion.user_id == user.id,
                                       MergeSuggestion.status == "pending").count())
        device_connected = db.query(Device.id).filter(Device.user_id == user.id).first() is not None
        return render_template(
            'index.html',
            user=user,
            device_connected=device_connected,
            connect_code=user.connect_code,
            contacts_count=contacts_count,
            messages_count=messages_count,
            pending_suggestions=pending_suggestions,
            has_avatar=os.path.exists(_avatar_file(user.id)),
        )

    @app.route('/home/avatar', methods=['GET', 'POST'])
    def avatar():
        if not session.get('user_id'):
            return redirect('/login') if request.method == 'POST' \
                else ('Unauthorized', 401)
        user_id = session['user_id']

        if request.method == 'POST':
            from data.crypto import encrypt_bytes
            upload = request.files.get('avatar')
            if upload is None or not upload.filename:
                return redirect('/home')
            data = upload.read()
            mime = (upload.mimetype or '').lower()
            if not data or len(data) > 5 * 1024 * 1024 \
                    or not mime.startswith('image/'):
                return redirect('/home')
            os.makedirs(os.path.join(_media_root(), str(user_id)), exist_ok=True)
            with open(_avatar_file(user_id), 'wb') as f:
                f.write(encrypt_bytes(data))
            with open(_avatar_mime_file(user_id), 'w', encoding='utf-8') as f:
                f.write(mime)
            return redirect('/home')

        from data.crypto import decrypt_bytes
        path = _avatar_file(user_id)
        if not os.path.exists(path):
            return 'Not Found', 404
        with open(path, 'rb') as f:
            raw = decrypt_bytes(f.read())
        mime = 'image/jpeg'
        if os.path.exists(_avatar_mime_file(user_id)):
            with open(_avatar_mime_file(user_id), 'r', encoding='utf-8') as f:
                mime = f.read().strip() or mime
        return Response(raw, mimetype=mime)

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            db = get_db()
            name = request.form.get('name')
            surname = request.form.get('surname')
            email = request.form.get('email')
            password = request.form.get('password')
            confirm_password = request.form.get('confirm_password')
            sex = request.form.get('sex')

            if password != confirm_password:
                return render_template('register.html', message="Пароли не совпадают")

            if db.query(User).filter(User.email == email).first():
                return render_template('register.html', message="Такой пользователь уже есть")

            user = User(
                name=name,
                surname=surname,
                email=email,
                sex=sex,
                hashed_password=generate_password_hash(password),
            )
            user.connect_code = _generate_code()
            db.add(user)
            db.commit()
            session['user_id'] = user.id
            return redirect('/code')

        return render_template('register.html')

    @app.route('/code')
    def code():
        if not session.get('user_id'):
            return redirect('/login')
        from data.devices import Device
        db = get_db()
        user = db.query(User).filter(User.id == session['user_id']).first()
        has_device = db.query(Device.id).filter(Device.user_id == user.id).first() is not None
        if has_device:
            return redirect('/home')
        return render_template('code.html', code=user.connect_code)

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            db = get_db()
            email = request.form.get('email')
            password = request.form.get('password')
            user = db.query(User).filter(User.email == email).first()
            if user and check_password_hash(user.hashed_password, password):
                session['user_id'] = user.id
                return redirect('/home')
            return render_template('login.html', message="Неверный email или пароль")
        return render_template('login.html')

    @app.route('/messages')
    def messages():
        return redirect('/contacts')

    @app.route('/contacts')
    def contacts_index():
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import Contact
        db = get_db()
        user_id = session['user_id']
        contacts = (
            db.query(Contact)
            .filter(Contact.user_id == user_id)
            .all()
        )
        _enrich_with_last_message(db, contacts)
        return render_template('contacts.html', contacts=contacts,
                               selected=None, selected_handles=[], messages=None)

    @app.route('/contacts.json')
    def contacts_index_json():
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact
        db = get_db()
        user_id = session['user_id']
        contacts = (
            db.query(Contact)
            .filter(Contact.user_id == user_id)
            .all()
        )
        _enrich_with_last_message(db, contacts)
        return jsonify({'contacts': [
            {
                'id': c.id,
                'display_name': c.display_name,
                'initial': c.initial,
                'avatar_color': c.avatar_color,
                'last_preview': c.last_preview,
                'last_time': c.last_time,
                'unread_count': c.unread_count or 0,
            }
            for c in contacts
        ]})

    @app.route('/contacts/<int:contact_id>')
    def contact_detail(contact_id):
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import Contact, MessengerHandle
        from data.matching import display_author
        db = get_db()
        user_id = session['user_id']
        contact = (
            db.query(Contact)
            .filter(Contact.id == contact_id, Contact.user_id == user_id)
            .first()
        )
        if not contact:
            return 'Not Found', 404

        contact.last_read_at = datetime.now()
        db.commit()

        contacts = (
            db.query(Contact)
            .filter(Contact.user_id == user_id)
            .all()
        )
        _enrich_with_last_message(db, contacts)
        _avatar_for(contact)

        handles = db.query(MessengerHandle).filter(MessengerHandle.contact_id == contact.id).all()
        handle_ids = [h.id for h in handles]
        selected_handles = [f"{h.messenger_name}: {h.sender_raw}" for h in handles]
        msgs = (
            db.query(Messages)
            .filter(Messages.handle_id.in_(handle_ids))
            .order_by(Messages.created_at.asc().nullsfirst(), Messages.id.asc())
            .all()
        )
        for m in msgs:
            m.display_author = display_author(m.sender, contact.display_name)
        _attach_media(db, msgs)
        return render_template('contacts.html', contacts=contacts, selected=contact,
                               selected_handles=selected_handles, messages=msgs)

    @app.route('/contacts/<int:contact_id>/messages.json')
    def contact_messages_json(contact_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        from data.matching import display_author
        db = get_db()
        user_id = session['user_id']
        contact = (db.query(Contact)
                   .filter(Contact.id == contact_id, Contact.user_id == user_id).first())
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        handle_ids = [h.id for h in
                      db.query(MessengerHandle).filter(MessengerHandle.contact_id == contact.id).all()]
        msgs = (db.query(Messages)
                .filter(Messages.handle_id.in_(handle_ids))
                .order_by(Messages.created_at.asc().nullsfirst(), Messages.id.asc())
                .all())
        # Для того чтобы не обновлять страницу каждый раз как пришло уведомление
        contact.last_read_at = datetime.now()
        db.commit()
        _attach_media(db, msgs)
        return jsonify({'messages': [
            {'id': m.id, 'sender': m.sender, 'text': m.text,
             'messenger_name': m.messenger_name, 'time': m.time,
             'display_author': display_author(m.sender, contact.display_name),
             'attachments': [{'id': a.id, 'kind': a.kind} for a in m.media]}
            for m in msgs
        ]})

    @app.route('/contacts/manage')
    def contacts_manage():
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import Contact, MergeSuggestion, MessengerHandle
        db = get_db()
        user_id = session['user_id']
        all_contacts = (db.query(Contact).filter(Contact.user_id == user_id)
                        .order_by(Contact.display_name.asc()).all())
        for c in all_contacts:
            _avatar_for(c)
        handles = (db.query(MessengerHandle).filter(MessengerHandle.user_id == user_id)
                   .order_by(MessengerHandle.messenger_name.asc()).all())
        contact_handles = {c.id: [] for c in all_contacts}
        for h in handles:
            contact_handles.setdefault(h.contact_id, []).append(h)
        suggestions = (db.query(MergeSuggestion)
                       .filter(MergeSuggestion.user_id == user_id,
                               MergeSuggestion.status == "pending")
                       .order_by(MergeSuggestion.score.desc()).all())
        return render_template('contacts_manage.html',
                               all_contacts=all_contacts,
                               contact_handles=contact_handles,
                               suggestions=suggestions)

    @app.route('/contacts/<int:contact_id>/rename', methods=['POST'])
    def contact_rename(contact_id):
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import Contact
        db = get_db()
        user_id = session['user_id']
        contact = db.query(Contact).filter(
            Contact.id == contact_id, Contact.user_id == user_id).first()
        if not contact:
            return 'Not Found', 404
        new_name = request.form.get('display_name', '').strip()
        if new_name:
            contact.display_name = new_name
            db.commit()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'ok': True, 'display_name': contact.display_name})
        return redirect('/contacts/manage')

    @app.route('/contacts/<int:contact_id>/delete', methods=['POST'])
    def contact_delete(contact_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MergeSuggestion, MessengerHandle
        from sqlalchemy import or_
        db = get_db()
        user_id = session['user_id']
        contact = db.query(Contact).filter(
            Contact.id == contact_id, Contact.user_id == user_id).first()
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        handle_ids = [h.id for h in db.query(MessengerHandle)
                      .filter(MessengerHandle.contact_id == contact.id).all()]
        if handle_ids:
            db.query(Messages).filter(Messages.handle_id.in_(handle_ids)).delete(
                synchronize_session=False)
        conditions = [MergeSuggestion.target_contact_id == contact.id]
        if handle_ids:
            conditions.append(MergeSuggestion.source_handle_id.in_(handle_ids))
        db.query(MergeSuggestion).filter(or_(*conditions)).delete(
            synchronize_session=False)
        db.query(MessengerHandle).filter(
            MessengerHandle.contact_id == contact.id).delete(
            synchronize_session=False)
        db.delete(contact)
        db.commit()
        return jsonify({'ok': True})

    @app.route('/messages/<int:message_id>/delete', methods=['POST'])
    def message_delete(message_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        db = get_db()
        user_id = session['user_id']
        msg = db.query(Messages).filter(
            Messages.id == message_id, Messages.user_id == user_id).first()
        if not msg:
            return jsonify({'error': 'not_found'}), 404
        db.delete(msg)
        db.commit()
        return jsonify({'ok': True})

    @app.route('/contacts/merge', methods=['POST'])
    def contacts_merge():
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import merge_contacts
        db = get_db()
        try:
            source_id = int(request.form['source_id'])
            target_id = int(request.form['target_id'])
        except (KeyError, ValueError):
            return 'Bad Request', 400
        try:
            merge_contacts(db, session['user_id'], source_id, target_id)
        except ValueError:
            return 'Bad Request', 400
        except LookupError:
            return 'Not Found', 404
        return redirect('/contacts/manage')

    @app.route('/contacts/handles/<int:handle_id>/move', methods=['POST'])
    def handle_move(handle_id):
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import Contact, MergeSuggestion, MessengerHandle
        db = get_db()
        user_id = session['user_id']
        handle = db.query(MessengerHandle).filter(
            MessengerHandle.id == handle_id, MessengerHandle.user_id == user_id).first()
        if not handle:
            return 'Not Found', 404
        try:
            target_id = int(request.form['target_contact_id'])
        except (KeyError, ValueError):
            return 'Bad Request', 400
        target = db.query(Contact).filter(
            Contact.id == target_id, Contact.user_id == user_id).first()
        if not target:
            return 'Not Found', 404
        old_contact_id = handle.contact_id
        if old_contact_id == target_id:
            return redirect('/contacts/manage')
        handle.contact_id = target_id
        db.flush()
        remaining = db.query(MessengerHandle).filter(
            MessengerHandle.contact_id == old_contact_id).count()
        if remaining == 0:
            db.query(MergeSuggestion).filter(
                MergeSuggestion.status == "pending",
                MergeSuggestion.target_contact_id == old_contact_id,
            ).update({MergeSuggestion.status: "dismissed"}, synchronize_session=False)
            db.query(Contact).filter(Contact.id == old_contact_id).delete(
                synchronize_session=False)
        db.commit()
        return redirect('/contacts/manage')

    @app.route('/contacts/suggestions/<int:sug_id>/dismiss', methods=['POST'])
    def suggestion_dismiss(sug_id):
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import MergeSuggestion
        db = get_db()
        sug = db.query(MergeSuggestion).filter(
            MergeSuggestion.id == sug_id,
            MergeSuggestion.user_id == session['user_id']).first()
        if not sug:
            return 'Not Found', 404
        sug.status = "dismissed"
        db.commit()
        return redirect('/contacts/manage')

    @app.route('/contacts/suggestions/<int:sug_id>/accept', methods=['POST'])
    def suggestion_accept(sug_id):
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import MergeSuggestion, MessengerHandle, merge_contacts
        db = get_db()
        user_id = session['user_id']
        sug = db.query(MergeSuggestion).filter(
            MergeSuggestion.id == sug_id, MergeSuggestion.user_id == user_id).first()
        if not sug:
            return 'Not Found', 404
        source_handle = db.get(MessengerHandle, sug.source_handle_id)
        try:
            merge_contacts(db, user_id, source_handle.contact_id, sug.target_contact_id)
        except (ValueError, LookupError):
            return 'Conflict', 409
        sug.status = "accepted"
        db.commit()
        return redirect('/contacts/manage')

    def _device_from_bearer(db):
        from data.devices import Device, hash_token
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return None
        token = auth[len('Bearer '):].strip()
        if not token:
            return None
        return db.query(Device).filter(Device.token_hash == hash_token(token)).first()

    @app.route('/api/me', methods=['GET'])
    def api_me():
        db = get_db()
        device = _device_from_bearer(db)
        if device is None:
            return jsonify({'error': 'unauthorized'}), 401
        user = db.query(User).filter(User.id == device.user_id).first()
        return jsonify({
            'user': {'id': user.id, 'name': user.name},
            'device': {'id': device.id, 'name': device.name},
        })

    @app.route('/download')
    def download_index():
        return render_template('download.html')

    @app.route('/download/skillwood.apk')
    def download_apk():
        candidates = [
            os.path.join(os.getcwd(), 'dist'),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dist'),
        ]
        for d in candidates:
            if os.path.exists(os.path.join(d, 'skillwood.apk')):
                return send_from_directory(
                    d, 'skillwood.apk',
                    as_attachment=True,
                    mimetype='application/vnd.android.package-archive',
                )
        abort(404)

    @app.route('/add', methods=['POST'])
    def add_message():
        from data.contacts import record_message

        sender = request.form.get('sender')
        text_value = request.form.get('text')
        messenger_name = request.form.get('messenger_name')

        if not sender or not text_value or not messenger_name:
            return 'Bad Request', 400

        db = get_db()
        device = _device_from_bearer(db)
        if device is None:
            return 'Unauthorized', 401
        device.last_seen_ip = request.remote_addr
        device.last_seen_at = datetime.now()
        db.commit()

        record_message(db, device.user_id, messenger_name, sender, text_value)
        return 'OK', 200

    @app.route('/add_media', methods=['POST'])
    def add_media():
        # Приём медиа от Android-клиента
        from sqlalchemy.exc import IntegrityError

        from data.attachments import Attachment
        from data.contacts import find_or_create_handle
        from data.crypto import encrypt_bytes

        sender = request.form.get('sender')
        messenger_name = request.form.get('messenger_name')
        kind = request.form.get('kind') or 'image'
        dedup_key = (request.form.get('dedup_key') or '').strip() or None
        caption = request.form.get('text') or ''
        upload = request.files.get('file')

        if not sender or not messenger_name or upload is None:
            return 'Bad Request', 400

        db = get_db()
        device = _device_from_bearer(db)
        if device is None:
            return 'Unauthorized', 401
        user_id = device.user_id

        device.last_seen_ip = request.remote_addr
        device.last_seen_at = datetime.now()
        db.commit()

        # Если это фото уже было (Max/VK шлёт повторно) - не создаём дубль
        if dedup_key is not None:
            exists = (db.query(Attachment.id)
                      .filter(Attachment.user_id == user_id,
                              Attachment.dedup_key == dedup_key).first())
            if exists is not None:
                return 'OK Duplicate', 200

        data = upload.read()
        if not data:
            return 'Bad Request', 400

        now = datetime.now()
        handle = find_or_create_handle(db, user_id, messenger_name, sender)
        placeholder = {'image': '📷 Фото',
                       'sticker': '🩷 Стикер',
                       'video': '🎬 Видео'}.get(kind, '📎 Вложение')
        msg = Messages(
            sender=sender,
            text=caption or placeholder,
            messenger_name=messenger_name,
            time=now.strftime("%H:%M"),
            user_id=user_id,
            handle_id=handle.id,
            created_at=now,
        )
        db.add(msg)
        db.flush()

        rel_dir = str(user_id)
        os.makedirs(os.path.join(_media_root(), rel_dir), exist_ok=True)
        stored_name = uuid.uuid4().hex + '.enc'
        stored_path = f"{rel_dir}/{stored_name}"
        with open(os.path.join(_media_root(), stored_path), 'wb') as f:
            f.write(encrypt_bytes(data))

        att = Attachment(
            user_id=user_id,
            message_id=msg.id,
            kind=kind,
            mime=upload.mimetype,
            original_name=upload.filename or None,
            stored_path=stored_path,
            size=len(data),
            dedup_key=dedup_key,
        )
        db.add(att)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return 'OK Duplicate', 200
        return 'OK', 200

    @app.route('/attachments/<int:attachment_id>')
    def attachment_get(attachment_id):
        if not session.get('user_id'):
            return 'Unauthorized', 401
        from data.attachments import Attachment
        from data.crypto import decrypt_bytes
        db = get_db()
        att = (db.query(Attachment)
               .filter(Attachment.id == attachment_id,
                       Attachment.user_id == session['user_id']).first())
        if att is None:
            return 'Not Found', 404
        full = os.path.join(_media_root(), att.stored_path)
        if not os.path.exists(full):
            return 'Not Found', 404
        with open(full, 'rb') as f:
            raw = decrypt_bytes(f.read())
        return Response(raw, mimetype=att.mime or 'application/octet-stream')

    @app.route('/api/ping')
    def api_ping():
        return jsonify({'ok': True, 'service': 'skillwood'})

    @app.route('/api/connect', methods=['POST'])
    def api_connect():
        from data.devices import Device, generate_token, hash_token

        body = request.get_json(silent=True) or {}
        code = (body.get('code') or '').strip()
        device_name = (body.get('device_name') or '').strip()
        if not code or not device_name:
            return jsonify({'error': 'code and device_name required'}), 400

        db = get_db()
        user = db.query(User).filter(User.connect_code == code).first()
        if not user:
            return jsonify({'error': 'unknown code'}), 404

        token = generate_token()
        device = Device(user_id=user.id, name=device_name,
                        token_hash=hash_token(token))
        db.add(device)
        db.commit()
        return jsonify({
            'token': token,
            'user': {'id': user.id, 'name': user.name},
            'device': {'id': device.id, 'name': device.name},
        })

def _generate_code() -> str:
    return ''.join(random.choices(string.digits, k=8))


if __name__ == '__main__':
    app = create_app()
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

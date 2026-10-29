"""Statement upload and import history."""
import hashlib
import os
import sqlite3
import uuid
from pathlib import Path

import streamlit as st

from core.db import backup_db, read_sql_query
from core.ingest import (
    import_preview,
    preview_statement,
)
from ui import translate_import_message

MAX_FILE_SIZE = 20 * 1024 * 1024
APP_DIR = Path(__file__).resolve().parent.parent
STATEMENT_ARCHIVE = APP_DIR / 'imported_statements'
LOCAL_DB_FILE = Path(os.environ.get('FINANCE_DB_PATH', APP_DIR / 'finance.db')).expanduser()
BACKUP_DIR = APP_DIR / 'backups'


def _queue_preview(
    conn: sqlite3.Connection,
    data: bytes,
    filename: str,
    mode: dict[str, int | str | None],
) -> None:
    preview = preview_statement(conn, data, filename)
    st.session_state['pending_import_preview'] = preview
    st.session_state['pending_import_mode'] = mode
    st.session_state['pending_import_token'] = uuid.uuid4().hex


def _format_money(amount_minor: int, currency: str) -> str:
    whole, fraction = divmod(abs(amount_minor), 100)
    sign = '-' if amount_minor < 0 else ''
    return f'{sign}{whole:,}.{fraction:02d} {currency}'


def _render_import_preview(conn: sqlite3.Connection) -> None:
    preview = st.session_state.get('pending_import_preview')
    if preview is None:
        return
    mode = st.session_state.get('pending_import_mode', {'kind': 'new'})
    token = st.session_state.get('pending_import_token', uuid.uuid4().hex)
    statement = preview.statement
    currency = statement.currency
    st.subheader('Импортын урьдчилсан шалгалт')
    st.caption(f'Файл: {preview.filename}')
    st.write(
        f"Данс: {preview.masked_account_number} · Валют: {currency or 'тодорхойгүй'} · "
        f"Хугацаа: {statement.period_start or '—'} – {statement.period_end or '—'}"
    )
    metric_cols = st.columns(5)
    for col, (label, value) in zip(metric_cols, (
        ('Шинэ мөр', preview.rows_new),
        ('Давхардсан', preview.rows_duplicate),
        ('Алгасах мөр', preview.rows_skipped),
        ('Орлого', _format_money(preview.income_total_minor, currency)),
        ('Зарлага', _format_money(preview.expense_total_minor, currency)),
    )):
        col.metric(label, value)
    if preview.warnings:
        st.warning('Анхааруулгатай мөрүүд байна. Хадгалахын өмнө шалгаад зөвшөөрнө үү.')
        for warning in preview.warnings:
            st.warning(translate_import_message(warning))
    if preview.sample_rows:
        sample = [
            {
                'Огноо': row['date'],
                'Дүн': _format_money(int(row['amount_minor']), currency),
                'Гүйлгээний утга': row['description'],
            }
            for row in preview.sample_rows
        ]
        st.dataframe(sample, hide_index=True, use_container_width=True)
    warning_acknowledged = True
    if preview.requires_confirmation:
        warning_acknowledged = st.checkbox(
            'Анхааруулгуудыг шалгаж, импортлохыг зөвшөөрч байна',
            key=f'accept_import_warnings_{token}',
        )
    save_label = (
        'Анхааруулгатай хуулгыг зөвшөөрч хадгалах'
        if preview.requires_confirmation
        else 'Баталгаажуулж импортлох'
    )
    save_col, cancel_col = st.columns(2)
    if save_col.button(
        save_label,
        disabled=not warning_acknowledged,
        key=f'save_import_preview_{token}',
    ):
        try:
            if not os.environ.get('DATABASE_URL', '').strip() and LOCAL_DB_FILE.is_file():
                if backup_db(str(LOCAL_DB_FILE), str(BACKUP_DIR)) is None:
                    raise ValueError('Импортын өмнөх нөөцийг үүсгэж чадсангүй. Өгөгдөл хадгалагдсангүй.')
            if not conn.in_transaction:
                conn.execute('BEGIN')
            with conn:
                result = import_preview(
                    conn,
                    preview,
                    accept_warnings=warning_acknowledged,
                )
                if mode.get('kind') == 'extension':
                    conn.execute(
                        '''INSERT INTO import_extensions(
                               parent_import_id, child_import_id
                           ) VALUES (?, ?) RETURNING child_import_id''',
                        (mode['parent_id'], result.import_id),
                    )
            _archive_statement(preview.file_data, preview.file_hash)
            st.session_state['import_result'] = result
            st.session_state.pop('pending_import_preview', None)
            st.session_state.pop('pending_import_mode', None)
            st.session_state.pop('pending_import_token', None)
            st.rerun()
        except ValueError as exc:
            st.session_state.pop('pending_import_preview', None)
            st.session_state.pop('pending_import_mode', None)
            st.session_state.pop('pending_import_token', None)
            st.error(translate_import_message(str(exc)))
    if cancel_col.button(
        'Цуцлах', key=f'cancel_import_preview_{token}'
    ):
        st.session_state.pop('pending_import_preview', None)
        st.session_state.pop('pending_import_mode', None)
        st.session_state.pop('pending_import_token', None)
        st.rerun()


def render(conn: sqlite3.Connection) -> None:
    """Upload an Excel statement and show import results."""
    st.title('Банкны хуулга оруулах')
    st.caption(
        'Дараагийн хугацааны хуулгыг оруулахад өмнөх гүйлгээнүүд дээр нэмнэ. '
        'Давхардсан мөрийг дахин нэмэхгүй. Өмнөх хуулгаа дахин сонгох '
        'боломжтой бөгөөд хуулга '
        'бүрийн тайланг Тайлан хэсгээс тусад нь харна. Банк давтагдашгүй '
        'гүйлгээний ID өгөхгүй тул ижил огноо, дүн, үлдэгдэл, утгатай хоёр '
        'жинхэнэ гүйлгээг давхардал гэж андууран тооцож болзошгүй; preview-г шалгана уу.'
    )
    file = st.file_uploader(
        'ХААН Банкны хуулга (.xlsx, 20 МБ хүртэл)',
        type=['xlsx'],
        help='20 МБ-аас ихгүй хэмжээтэй .xlsx файл сонгоно уу.',
    )
    if st.button(
        'Хуулгыг шалгах',
        disabled=file is None,
        key='upload_statement',
    ):
        if file is None:
            pass
        elif not file.name.lower().endswith('.xlsx'):
            st.error('.xlsx өргөтгөлтэй банкны хуулга сонгоно уу.')
        elif file.size > MAX_FILE_SIZE:
            st.error('Файлын хэмжээ 20 МБ-ын хязгаараас хэтэрсэн байна.')
        else:
            try:
                data = file.getvalue()
                if len(data) > MAX_FILE_SIZE:
                    st.error('Файлын хэмжээ 20 МБ-ын хязгаараас хэтэрсэн байна.')
                else:
                    _queue_preview(conn, data, file.name, {'kind': 'new'})
            except ValueError as exc:
                st.error(translate_import_message(str(exc)))
    _render_import_preview(conn)
    result = st.session_state.get('import_result')
    if result:
        metrics = [
            ('Шинэ', result.rows_new),
            ('Давхардсан', result.rows_duplicate),
            ('Алгассан', result.rows_skipped),
            ('Автоматаар ангилсан', result.auto_classified),
            ('Ангилах шаардлагатай', result.needs_review),
        ]
        for col, (label, value) in zip(st.columns(5), metrics):
            col.metric(label, value)
        for warning in result.warnings:
            st.warning(translate_import_message(warning))
        if st.button('Ангилах хэсэг рүү очих'):
            st.session_state['next_page'] = 'Review'
            st.rerun()
    st.subheader('Оруулсан хуулгын түүх')
    history = read_sql_query(
        '''SELECT filename,
                  CASE
                    WHEN period_start IS NULL THEN COALESCE(period_end, '')
                    WHEN period_end IS NULL THEN period_start
                    ELSE period_start || ' - ' || period_end
                  END AS period,
                  rows_new AS new, rows_duplicate AS dup, imported_at
           FROM imports ORDER BY id DESC''',
        conn,
    )
    st.dataframe(
        history,
        hide_index=True,
        column_config={
            'filename': 'Файлын нэр',
            'period': 'Хуулгын хугацаа',
            'new': 'Шинэ',
            'dup': 'Давхардсан',
            'imported_at': 'Оруулсан огноо',
        },
    )

    history_rows = conn.execute(
        '''SELECT i.id, i.filename, i.file_hash, i.period_start, i.period_end,
                  i.imported_at,
                  CASE WHEN f.import_id IS NULL THEN 0 ELSE 1 END AS stored_in_db
           FROM imports AS i
           LEFT JOIN import_files AS f ON f.import_id=i.id
           ORDER BY i.id DESC'''
    ).fetchall()
    if not history_rows:
        return

    history_options: dict[str, sqlite3.Row] = {}
    archive_available: dict[str, bool] = {}
    for row in history_rows:
        file_hash = row['file_hash']
        safe_hash = (
            isinstance(file_hash, str)
            and len(file_hash) == 64
            and all(character in '0123456789abcdef' for character in file_hash)
        )
        archive_exists = (
            safe_hash and (STATEMENT_ARCHIVE / f'{file_hash}.xlsx').is_file()
        )
        exists = bool(row['stored_in_db']) or archive_exists
        period = ' – '.join(
            value
            for value in (row['period_start'], row['period_end'])
            if value
        ) or 'хугацаа тодорхойгүй'
        status = 'файл хадгалсан' if exists else 'эх файл хадгалаагүй'
        label = (
            f"#{row['id']} · {row['imported_at']} · "
            f"{row['filename']} · {period} · {status}"
        )
        history_options[label] = row
        archive_available[label] = bool(exists)

    st.subheader('Өмнө оруулсан хуулгыг сонгох')
    selected_label = st.selectbox(
        'Суурь хуулгаа сонгоно уу',
        list(history_options),
        key='reimport_statement_choice',
    )
    selected_import = history_options[selected_label]
    selected_hash = selected_import['file_hash']
    selected_id = selected_import['id']
    st.subheader('Сонгосон хуулгад нэмэлт хуулга залгах')
    st.caption(
        'Шинэ хугацааны хуулгаа сонгоно уу. Нэг дансны хуулгуудыг '
        'сонгосон хуулгын бүлэгт нэмнэ; давхардсан гүйлгээг дахин нэмэхгүй.'
    )
    extension_file = st.file_uploader(
        'Нэмэлт банкны хуулга (.xlsx, 20 МБ хүртэл)',
        type=['xlsx'],
        key=f'extension_file_{selected_id}',
    )
    if st.button(
        'Нэмэлт хуулгыг шалгах',
        disabled=extension_file is None,
        key=f'append_statement_{selected_id}',
    ):
        if extension_file is not None:
            extension_data = extension_file.getvalue()
            if len(extension_data) > MAX_FILE_SIZE:
                st.error('Файлын хэмжээ 20 МБ-ын хязгаараас хэтэрсэн байна.')
            else:
                try:
                    preview = preview_statement(
                        conn, extension_data, extension_file.name
                    )
                    target = conn.execute(
                        '''SELECT a.account_number
                           FROM imports AS i
                           JOIN accounts AS a ON a.id=i.account_id
                           WHERE i.id=?''',
                        (selected_id,),
                    ).fetchone()
                    if target is None:
                        st.error(
                            'Сонгосон хуулгын дансны мэдээлэл олдсонгүй. '
                            'Өөр хуулга сонгоно уу.'
                        )
                    elif preview.statement.account_number != target['account_number']:
                        st.error(
                            'Нэмэлт файл сонгосон хуулгын данстай таарахгүй байна. '
                            'Ижил дансны хуулга сонгоно уу.'
                        )
                    else:
                        st.session_state['pending_import_preview'] = preview
                        st.session_state['pending_import_mode'] = {
                            'kind': 'extension', 'parent_id': int(selected_id),
                        }
                        st.session_state['pending_import_token'] = uuid.uuid4().hex
                        st.rerun()
                except ValueError as exc:
                    st.error(translate_import_message(str(exc)))
    stored = conn.execute(
        'SELECT file_data, file_hash FROM import_files WHERE import_id=?',
        (selected_id,),
    ).fetchone()
    archive_path = STATEMENT_ARCHIVE / f'{selected_hash}.xlsx'
    if not archive_available[selected_label]:
        st.info(
            'Энэ өмнөх хуулгын Excel файл Render дээр хадгалагдаагүй байсан. '
            'Эх файлыг нэг удаа сонгож энэ хуулгатай холбоно уу. Шинэ хуулга '
            'нэмэх бол дээрх "Хуулгыг шалгах" хэсгийг ашиглана уу.'
        )
        recovery_file = st.file_uploader(
            'Өмнө оруулсан хуулгын эх Excel файлыг сонгох',
            type=['xlsx'],
            key=f'recover_statement_{selected_id}',
        )
        if st.button(
            'Эх файлыг сонгосон хуулгатай холбох',
            disabled=recovery_file is None,
            key=f'attach_statement_{selected_id}',
        ):
            if recovery_file is not None:
                data = recovery_file.getvalue()
                if len(data) > MAX_FILE_SIZE:
                    st.error('Файлын хэмжээ 20 МБ-ын хязгаараас хэтэрсэн байна.')
                elif hashlib.sha256(data).hexdigest() != selected_hash:
                    st.error(
                        'Сонгосон файл энэ хуулгатай таарахгүй байна. '
                        'Яг өмнө оруулсан Excel файлаа сонгоно уу.'
                    )
                else:
                    with conn:
                        conn.execute(
                            '''INSERT INTO import_files(import_id, file_data, file_hash)
                               VALUES (?, ?, ?)
                               ON CONFLICT(import_id) DO UPDATE SET
                                   file_data=excluded.file_data,
                                   file_hash=excluded.file_hash
                               RETURNING import_id''',
                            (selected_id, data, selected_hash),
                        )
                    _archive_statement(data, selected_hash)
                    st.success(
                        'Эх файлыг хуулгатай холболоо. Одоо дахин оруулж болно.'
                    )
                    st.rerun()
    if st.button(
        'Сонгосон хуулгыг дахин шалгах',
        disabled=not archive_available[selected_label],
        key='reimport_statement',
    ):
        try:
            if stored is not None:
                data = stored['file_data']
                stored_hash = stored['file_hash']
            else:
                data = archive_path.read_bytes()
                stored_hash = hashlib.sha256(data).hexdigest()
            if stored_hash != selected_hash or hashlib.sha256(data).hexdigest() != selected_hash:
                st.error(
                    'Хадгалсан хуулгын бүрэн бүтэн байдал алдагдсан байна. '
                    'Эх файлыг компьютероосоо дахин сонгоно уу.'
                )
            elif len(data) > MAX_FILE_SIZE:
                st.error('Хадгалсан файлын хэмжээ 20 МБ-ын хязгаараас хэтэрсэн байна.')
            else:
                _queue_preview(
                    conn,
                    data,
                    selected_import['filename'],
                    {'kind': 'reimport'},
                )
                st.rerun()
        except ValueError as exc:
            st.error(translate_import_message(str(exc)))
        except OSError:
            st.error(
                'Хадгалсан хуулгыг уншиж чадсангүй. '
                'Файл устсан эсвэл унших эрхгүй байж болзошгүй.'
            )


def _archive_statement(data: bytes, file_hash: str) -> None:
    """Keep a local copy when the filesystem allows it."""
    archive_path = STATEMENT_ARCHIVE / f'{file_hash}.xlsx'
    try:
        STATEMENT_ARCHIVE.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(data)
    except OSError:
        st.warning(
            'Файл өгөгдлийн санд хадгалагдсан боловч локал архив үүсгэж '
            'чадсангүй.'
        )

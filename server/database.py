from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine, func, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from shared.config import ServerConfig
from shared.paths import ensure_runtime_dirs
from shared.protocol import (
    ClientMode,
    ClientStatusPayload,
    MessagePriority,
    MessageRecord,
    MessageStatus,
    ReceiptRecord,
)


def local_now() -> datetime:
    return datetime.now()


class Base(DeclarativeBase):
    pass


class MessageModel(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    sender_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sender_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    msg_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=MessageStatus.UNREAD.value)
    resend_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=local_now)
    resend_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    source_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class ClientStateModel(Base):
    __tablename__ = "client_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    client_name: Mapped[str] = mapped_column(String(128), nullable=False, default="classroom-desktop")
    is_online: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default=ClientMode.NORMAL.value)
    is_in_break: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=local_now)


class PendingReceiptModel(Base):
    __tablename__ = "pending_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_db_id: Mapped[int] = mapped_column(Integer, nullable=False)
    group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=local_now)


@dataclass
class StoredMessageResult:
    message: MessageRecord
    client_status: ClientStatusPayload


class Database:
    def __init__(self, config: ServerConfig):
        ensure_runtime_dirs()
        self._config = config
        self._db_path = self._resolve_database_path(config.database_path)
        self._engine = create_engine(
            f"sqlite:///{self._db_path.as_posix()}",
            future=True,
            connect_args={"check_same_thread": False},
        )
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False, future=True)

    @property
    def database_path(self) -> Path:
        return self._db_path

    def initialize(self) -> None:
        Base.metadata.create_all(self._engine)
        self._migrate_schema()
        with self.session() as session:
            state = session.get(ClientStateModel, 1)
            if state is None:
                session.add(
                    ClientStateModel(
                        id=1,
                        client_name=self._config.client_name,
                        is_online=False,
                        mode=ClientMode.NORMAL.value,
                        updated_at=local_now(),
                    )
                )
                session.commit()

    def session(self) -> Session:
        return self._session_factory()

    def store_message(
        self,
        *,
        sender_id: str,
        sender_name: str,
        content: str,
        msg_type: MessagePriority,
        timestamp: Optional[str],
        group_id: Optional[str],
        source_message_id: Optional[int],
        status: MessageStatus = MessageStatus.UNREAD,
    ) -> StoredMessageResult:
        """写入一条消息。

        ``status`` 默认未读（即转发给客户端）；智能反转需要把被 AI 过滤掉的
        消息也留在库里，此时传 ``MessageStatus.IGNORED``——它不会出现在客户端的
        未读或历史里，但家长可以用 @机器人 让服务端补发。
        """
        with self.session() as session:
            model = MessageModel(
                group_id=group_id,
                sender_id=sender_id,
                sender_name=sender_name,
                content=content,
                msg_type=msg_type.value,
                status=status.value,
                resend_count=0,
                timestamp=_parse_timestamp(timestamp),
                source_message_id=source_message_id,
            )
            session.add(model)
            session.commit()
            session.refresh(model)
            client_status = self._get_or_create_client_state(session)
            return StoredMessageResult(
                message=_to_message_record(model),
                client_status=_to_client_status_payload(client_status),
            )

    def list_unread_messages_for_sender(self, sender_id: str) -> list[MessageRecord]:
        with self.session() as session:
            stmt = (
                select(MessageModel)
                .where(
                    MessageModel.sender_id == str(sender_id),
                    MessageModel.status == MessageStatus.UNREAD.value,
                )
                .order_by(
                    func.coalesce(MessageModel.resend_time, MessageModel.timestamp).desc(),
                    MessageModel.id.desc(),
                )
            )
            return [_to_message_record(item) for item in session.execute(stmt).scalars().all()]

    def list_unread_messages(self) -> list[MessageRecord]:
        with self.session() as session:
            stmt = (
                select(MessageModel)
                .where(MessageModel.status == MessageStatus.UNREAD.value)
                .order_by(
                    func.coalesce(MessageModel.resend_time, MessageModel.timestamp).desc(),
                    MessageModel.id.desc(),
                )
            )
            return [_to_message_record(item) for item in session.execute(stmt).scalars().all()]

    def list_recent_messages(
        self,
        limit: int = 200,
        since: Optional[datetime] = None,
    ) -> list[MessageRecord]:
        """Recent messages, newest first.

        ``since`` restricts the result to messages at least that new; the
        client asks for its configured window (30 days by default) so a fresh
        client never pulls months of history over the wire.
        """
        with self.session() as session:
            stmt = select(MessageModel)
            # ignored 是“AI 判定不是转告”的消息，从未发给客户端，历史里也不该出现
            stmt = stmt.where(MessageModel.status != MessageStatus.IGNORED.value)
            if since is not None:
                stmt = stmt.where(
                    func.coalesce(MessageModel.resend_time, MessageModel.timestamp) >= since
                )
            stmt = stmt.order_by(
                func.coalesce(MessageModel.resend_time, MessageModel.timestamp).desc(),
                MessageModel.id.desc(),
            ).limit(limit)
            return [_to_message_record(item) for item in session.execute(stmt).scalars().all()]

    def get_message(self, db_id: int) -> Optional[MessageRecord]:
        with self.session() as session:
            model = session.get(MessageModel, db_id)
            return None if model is None else _to_message_record(model)

    def latest_message_for_sender(
        self,
        sender_id: str,
        *,
        since: Optional[datetime] = None,
        include_ignored: bool = True,
    ) -> Optional[MessageRecord]:
        """某位家长最近的一条消息（智能反转用它决定撤回还是补发）。

        ``since`` 用于限定时间窗口（默认由调用方给 120 秒）；``include_ignored``
        必须为真才能看到被 AI 过滤的消息——否则家长无法把误判的消息补发出去。
        """
        with self.session() as session:
            stmt = select(MessageModel).where(MessageModel.sender_id == str(sender_id))
            if since is not None:
                stmt = stmt.where(
                    func.coalesce(MessageModel.resend_time, MessageModel.timestamp) >= since
                )
            if not include_ignored:
                stmt = stmt.where(MessageModel.status != MessageStatus.IGNORED.value)
            stmt = stmt.order_by(
                func.coalesce(MessageModel.resend_time, MessageModel.timestamp).desc(),
                MessageModel.id.desc(),
            ).limit(1)
            model = session.execute(stmt).scalars().first()
            return None if model is None else _to_message_record(model)

    def set_message_status(self, db_id: int, status: MessageStatus) -> Optional[MessageRecord]:
        """直接改消息状态（智能反转把 ignored 补发成 unread 时会用到）。"""
        with self.session() as session:
            model = session.get(MessageModel, db_id)
            if model is None:
                return None
            model.status = status.value
            session.commit()
            session.refresh(model)
            return _to_message_record(model)

    def recall_message(self, db_id: int) -> Optional[MessageRecord]:
        with self.session() as session:
            model = session.get(MessageModel, db_id)
            if model is None:
                return None
            model.status = MessageStatus.RECALLED.value
            session.commit()
            session.refresh(model)
            return _to_message_record(model)

    def resend_message(self, db_id: int) -> Optional[MessageRecord]:
        with self.session() as session:
            model = session.get(MessageModel, db_id)
            if model is None:
                return None
            model.resend_count += 1
            model.resend_time = local_now()
            session.commit()
            session.refresh(model)
            return _to_message_record(model)

    def mark_message_read(self, db_id: int) -> Optional[MessageRecord]:
        with self.session() as session:
            model = session.get(MessageModel, db_id)
            if model is None:
                return None
            model.status = MessageStatus.READ.value
            session.commit()
            session.refresh(model)
            return _to_message_record(model)

    def enqueue_read_receipt(self, message: MessageRecord, text: str) -> None:
        if message.source_message_id is None:
            return
        with self.session() as session:
            session.add(
                PendingReceiptModel(
                    message_db_id=message.db_id or 0,
                    group_id=message.group_id or "",
                    target_user_id=message.sender_id,
                    source_message_id=message.source_message_id,
                    text=text,
                )
            )
            session.commit()

    def fetch_pending_receipts(self, limit: int = 20) -> list[ReceiptRecord]:
        with self.session() as session:
            stmt = select(PendingReceiptModel).order_by(PendingReceiptModel.id.asc()).limit(limit)
            rows = session.execute(stmt).scalars().all()
            results = [
                ReceiptRecord(
                    receipt_id=row.id,
                    message_db_id=row.message_db_id,
                    group_id=row.group_id,
                    target_user_id=row.target_user_id,
                    source_message_id=row.source_message_id,
                    text=row.text,
                )
                for row in rows
            ]
            for row in rows:
                session.delete(row)
            session.commit()
            return results

    def get_client_status(self) -> ClientStatusPayload:
        with self.session() as session:
            return _to_client_status_payload(self._get_or_create_client_state(session))

    def update_client_status(
        self,
        *,
        client_name: Optional[str],
        is_online: bool,
        mode: ClientMode,
        is_in_break: bool = False,
    ) -> ClientStatusPayload:
        with self.session() as session:
            state = self._get_or_create_client_state(session)
            if client_name:
                state.client_name = client_name
            state.is_online = is_online
            state.mode = mode.value
            state.is_in_break = is_in_break
            state.updated_at = local_now()
            session.commit()
            session.refresh(state)
            return _to_client_status_payload(state)

    def _get_or_create_client_state(self, session: Session) -> ClientStateModel:
        state = session.get(ClientStateModel, 1)
        if state is None:
            state = ClientStateModel(
                id=1,
                client_name=self._config.client_name,
                is_online=False,
                mode=ClientMode.NORMAL.value,
                updated_at=local_now(),
            )
            session.add(state)
            session.commit()
            session.refresh(state)
        return state

    def _migrate_schema(self) -> None:
        with self._engine.begin() as connection:
            message_columns = {
                row[1]
                for row in connection.execute(text("PRAGMA table_info(messages)")).fetchall()
            }
            if "source_message_id" not in message_columns:
                connection.execute(text("ALTER TABLE messages ADD COLUMN source_message_id INTEGER"))

            state_columns = {
                row[1]
                for row in connection.execute(text("PRAGMA table_info(client_state)")).fetchall()
            }
            if "is_in_break" not in state_columns:
                connection.execute(text("ALTER TABLE client_state ADD COLUMN is_in_break BOOLEAN NOT NULL DEFAULT 0"))

    def _resolve_database_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


def _parse_timestamp(raw_value: Optional[str]) -> datetime:
    if not raw_value:
        return local_now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw_value.strip(), fmt)
        except ValueError:
            continue
    return local_now()


def _format_datetime(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _to_message_record(model: MessageModel) -> MessageRecord:
    return MessageRecord(
        db_id=model.id,
        group_id=model.group_id,
        sender_id=model.sender_id,
        sender_name=model.sender_name,
        content=model.content,
        msg_type=MessagePriority(model.msg_type),
        status=MessageStatus(model.status),
        resend_count=model.resend_count,
        timestamp=_format_datetime(model.timestamp) or "",
        resend_time=_format_datetime(model.resend_time),
        source_message_id=model.source_message_id,
    )


def _to_client_status_payload(model: ClientStateModel) -> ClientStatusPayload:
    return ClientStatusPayload(
        client_name=model.client_name,
        is_online=bool(model.is_online),
        mode=ClientMode(model.mode),
        is_in_break=bool(model.is_in_break),
        updated_at=_format_datetime(model.updated_at) or "",
    )

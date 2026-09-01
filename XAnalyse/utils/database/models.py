"""XAnalyse 的订阅去重表。"""

from __future__ import annotations

from typing import ClassVar

from sqlmodel import Field, SQLModel
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.utils.database.base_models import with_session, with_read_session


class XAnalyseTweet(SQLModel, table=True):
    """按博主保存最近一条推文。"""

    __tablename__: ClassVar[str] = "xanalyse"
    __table_args__ = {"extend_existing": True}

    id: str = Field(primary_key=True, title="Twitter 博主用户名")
    link: str = Field(default="", title="最近推文链接")
    content: str = Field(default="", title="最近推文正文")

    @classmethod
    @with_read_session
    async def get_by_blogger(
        cls,
        session: AsyncSession,
        blogger_id: str,
    ) -> XAnalyseTweet | None:
        return await session.get(cls, blogger_id)

    @classmethod
    @with_session
    async def save_latest(
        cls,
        session: AsyncSession,
        blogger_id: str,
        link: str,
        content: str,
    ) -> None:
        row = await session.get(cls, blogger_id)
        if row is None:
            session.add(cls(id=blogger_id, link=link, content=content))
            return
        row.link = link
        row.content = content
        session.add(row)
